# Architecture

code-with-slack is one Python process. It holds a Slack Socket Mode connection and one Claude
Agent SDK client per bound channel, all on one asyncio event loop.

## Startup

`code-with-slack` (`code_with_slack.__main__.main`) starts in this order: it loads the
configuration, so a bad `.env` fails before anything else; takes the single-instance lock, so a
second daemon fails before it opens a Socket Mode connection; reads `state.json`; calls
`auth.test` for the workspace id and the bot user id; then opens the Socket Mode connection.
`SIGTERM`, which `launchctl bootout` sends, and `SIGINT` close the connection, then every
session. Logs go to standard error, which the LaunchAgent writes to
`~/Library/Logs/code-with-slack/code-with-slack.log`.

## Configuration

`code_with_slack.config.load_config` reads `~/.config/code-with-slack/.env` with
`python-dotenv`, without copying anything into the process environment. It refuses a file that
is not a regular file, belongs to another user, or is readable by group or others, and it
refuses tokens of the wrong kind. [setup.md](setup.md) lists the variables.

## State and the single-instance lock

`code_with_slack.state.StateStore` keeps, for each bound channel, its directory and its Claude
Code session id, in `~/.config/code-with-slack/state.json`. Every change is written to a
temporary file beside it, synced, and renamed over it, so a crash leaves either the old file or
the new one. A file that cannot be read stops the daemon instead of being replaced.

`code_with_slack.lock.single_instance` holds an exclusive `flock` on the configuration directory
itself. A second process fails to start. The kernel releases the lock when the holder exits, so
a crash leaves no stale lock and no lock file.

## Who may talk to it

Every inbound path that acts (a message, including a `!word`, and a button) runs two checks of its own before anything reaches Claude Code:

1. `code_with_slack.guards.is_owner`: the Slack user is the configured owner AND the workspace is
   the one `auth.test` reported at startup. A click from a user whose home workspace differs is
   refused.
2. `code_with_slack.guards.ChannelGuard.refusal`: the channel is private, not shared with another
   workspace, and its members are exactly the owner and the bot. It is read from Slack every
   time, so inviting a third person stops the bot in that channel at once.

Messages with a subtype (edits, deletions, joins) and messages from bots are ignored. A refusal
reaches the owner as an ephemeral message; everyone else gets nothing.

## Rendering

`code_with_slack.render.renderer.TurnRenderer` reads SDK message types only, never tool names, so
a tool Claude Code adds later gets its line in the reply with no code change.

| SDK input | What the owner sees |
|---|---|
| `StreamEvent` with no parent, a `text_delta` | the text, as it is written |
| a top-level `TextBlock` in an `AssistantMessage` | nothing more: the same text already arrived as deltas |
| `ToolUseBlock` or `ServerToolUseBlock` with no parent | a new tool line, in progress, titled `Name: first string argument` |
| the same inside a subagent (`parent_tool_use_id` set) | the parent's line shows the subagent's latest call |
| `ToolResultBlock` or `ServerToolResultBlock` for a line | the line completes, or shows an error with the output's first line when `is_error` |
| `TaskStartedMessage` | its tool's line notes "Running in background" and stays in progress, even after the call's own result, or a new line |
| `TaskProgressMessage` | the line shows the task's description |
| `TaskNotificationMessage`, a terminal `TaskUpdatedMessage` | the line completes, shows an error when the task failed, or completes with `Stopped` |
| `AssistantMessage.error` `authentication_failed` | a note asking to run `claude` and `/login` on the host |
| any other `AssistantMessage.error` | `Claude Code reported an error` with the error code |
| `SystemMessage` `compact_boundary` | `Compacted the conversation: 15.0k → 2.0k tokens.`, from its `compact_metadata`, whether the owner asked (`!compact`) or Claude Code compacted on its own |
| `ResultMessage` | its text, when nothing else was written (local commands such as `/usage` send no deltas) |

A turn that ends with no text and no tool line says `Done. Claude Code returned no text.`, or
`Stopped the current turn.` when it was interrupted, so no reply is left empty. When the turn
ends, every line still in progress is closed first (with `Stopped` when the turn
was interrupted), then the reply ends. A task that started and has not ended is the exception:
its line stays open with "Running in background". `TurnRenderer.running_tasks` lists them. Only
the task lifecycle messages decide this, because a subagent can move to the background with no
second `TaskStartedMessage`.

## Writing to Slack

`code_with_slack.render.sinks.ReplySink` writes each reply as one message in the channel's main
window, below the message that asked for it. The message is rewritten with `chat.update` at most
once a second (Slack allows `chat.update` 50 or more times a minute), in the order things happen:
text as Claude writes it, and a line per tool call where the call happens, updated in place
(`…` while it runs, `✓` when it succeeds, `✗` and the first line of its output when it fails).
Consecutive tool lines form one paragraph; a blank line separates them from the text around them.
The reply is posted as soon as the owner's message is queued, showing only a status line:
`Claude is writing…`, or `Waiting for the previous reply…` behind another turn. While the turn
runs the status stays last; when it ends, a divider and the footer replace it. A reply longer than about 11,000 characters continues in a new message.

Slack's native streaming API (`chat.startStream`) is not used: in an ordinary channel it works
only inside a thread, and replies belong in the main window. A write Slack refuses, or cannot
receive because the network is down, is retried with the whole reply at the next rewrite; it never
stops the Claude Code session.

## Approvals

When Claude Code asks for permission, the SDK calls `can_use_tool`. The session posts the
request as a message of its own, below the reply, with **Approve** and **Deny** buttons, and waits, for as long as it
takes. A clarifying question (Claude Code's `AskUserQuestion` tool) arrives the same way and is
posted as one menu per question with **Submit** and **Skip**; the picked labels go back as the
tool's answers. Each request has a random id that only its buttons carry; a click resolves it
once, only from the channel it was posted in, and only after the identity and channel guards.
Once decided, the request message is deleted: the tool's line in the reply records the call.
`!stop` denies every request still pending in the channel and deletes its message.

## Footer

Every reply ends with one context line: `⚡ bypass` when bypass is on, the git branch of the
channel's directory, the model and the context percentage from the SDK's
`get_context_usage()`, the effort level, the session's tokens from the turn's `ResultMessage.model_usage`, and the
5-hour and weekly limits. The SDK reports no effort level, so, as ccstatusline does, the
footer follows the output of `/effort` and `/model` in the session (`Set effort level to ...`),
then `effortLevel` in the user, project and local settings, then shows `default`. The limits
come from Claude Code's `/usage`, sent on a separate
long-lived client and cached for five minutes; a rate-limit event from the SDK invalidates the
cache. The limit fields exist only with a claude.ai subscription. A field that cannot be read is
left out.

## Sessions

`code_with_slack.sessions.SessionManager` keeps one `ChannelSession` per bound channel.

- The Claude Agent SDK client is created on first use, with the channel's directory as its
  working directory, `resume` set to the stored session id, the owner's own settings
  (`setting_sources` user, project and local), streaming of partial messages, the approval
  callback, and `--allow-dangerously-skip-permissions`, which makes `!bypass on` possible
  without turning it on.
- After connecting, `get_server_info()` gives the commands the session offers (for `!help` and
  `!`) and the permission mode that `!bypass off` returns to.
- If the stored session cannot be resumed (its transcript was deleted), the session id is
  cleared, a new session starts, and the reply opens with a line saying so. If the channel's
  directory no longer exists, nothing starts and the reply asks to bind the channel again. If
  macOS privacy protection denies the daemon the directory (a launchd service does not inherit
  Terminal's access to `~/Documents`), nothing starts and the reply says how to grant access.
- If the Claude Code process exits or its stream fails, the open reply ends with an error line,
  every waiting message is told, and the next message starts a new process. A Slack failure
  while a reply is written never stops the session or the running turn.
- Messages are queued and run one at a time; each gets its own reply in the channel.
  `!stop` interrupts the running turn and denies its pending approvals.
- One reader task follows the SDK's message stream for the life of the client. On each result
  the session id is stored (so `/clear`, which starts a new session, is recorded), the footer is
  built and the reply closed.
- A background task that finishes between turns sends its notification while the session is
  idle, then Claude Code starts a turn of its own to report it. That turn gets a reply of its own
  that starts with `Background task update`, and the next queued message waits for it to finish.
  When a message was already sent and waits for its turn, that turn comes first and Claude Code
  reports the task inside it, with no turn of its own (measured on Claude Code 2.1.280), so
  nothing waits.
  If no turn follows within 30 seconds, the queue moves on; a notification for a task no reply
  tracks is then posted on its own. When a queued message and a notification cross, the result's `origin` tells whose turn it
  was, and the queue is put back in order; that one reply can carry the other's label.
- A task that outlives its turn keeps its line in the reply that started it: the session maps
  the task id to that reply, and every later task message for it updates that line only, never
  another reply. A background subagent's own calls (`parent_tool_use_id` pointing at a line of
  an ended turn) go under its line the same way and never open a reply. A notification for such
  a task still makes the next queued message wait for the turn Claude Code starts to report it.
- The channel's latest reply ends with the list of what is still running (`⏳ N running`, one
  line per task, at most 10), above its status or footer. A new reply takes the list over and
  the previous one drops it, so the list stays at the bottom of the channel.
- When the Claude Code process goes away (shutdown, rebinding, a process that exits), its tasks
  go with it: their lines close with `Stopped` and the list empties. The map lives in memory only.
- Logs carry channel ids and exception type names, never prompt or reply text.

Bypass is a field of the in-memory session and nothing else: `state.json` never holds it, and a
restart brings every channel back to Claude Code's own mode. That matches Claude Code, whose
`--resume` does not restore `bypassPermissions` either.

## Slack handlers

`code_with_slack.slack_app.build_app` registers one listener per inbound path: `message`
events and the Approve, Deny, Submit and Skip buttons. The app registers no slash command. Each
acknowledges Slack first, then checks the owner, the workspace and the channel itself. A
failure after the checks reaches the owner as an ephemeral error line.
`code_with_slack.commands.parse_bang` reads a message starting with `!`: `help`, `bind`,
`bypass`, `status` and `stop` are the daemon's own words, answered with a message in the
channel (`!help` and `!bind` also work before the channel is bound); any other `!name args` runs
that Claude Code command when the session offers `name`, and is sent as a normal prompt
otherwise. `commands.help_text` builds `!help` from `get_server_info()["commands"]` at the time
of asking, keeping only the lines that contain the text after `!help` when there is one, so a command a new Claude Code release adds needs no change here. Bolt's per-request authorization returns the
identity `auth.test` gave at startup, so no request costs an extra API call.
