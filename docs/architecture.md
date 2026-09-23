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

Every inbound path that acts (a message, `/cc`, a button, a command picked from the picker)
runs two checks of its own before anything reaches Claude Code:

1. `code_with_slack.guards.is_owner`: the Slack user is the configured owner AND the workspace is
   the one `auth.test` reported at startup. A click from a user whose home workspace differs is
   refused.
2. `code_with_slack.guards.ChannelGuard.refusal`: the channel is private, not shared with another
   workspace, and its members are exactly the owner and the bot. It is read from Slack every
   time, so inviting a third person stops the bot in that channel at once.

The picker's list of commands, which Slack requests while the owner types, runs the owner check
only: it returns command names and changes nothing.

Messages with a subtype (edits, deletions, joins) and messages from bots are ignored. A refusal
reaches the owner as an ephemeral message; everyone else gets nothing.

## Rendering

`code_with_slack.render.renderer.TurnRenderer` reads SDK message types only, never tool names, so
a tool Claude Code adds later renders as a card with no code change.

| SDK input | What the owner sees |
|---|---|
| `StreamEvent` with no parent, a `text_delta` | the text, as it is written |
| a top-level `TextBlock` in an `AssistantMessage` | nothing more: the same text already streamed |
| `ToolUseBlock` or `ServerToolUseBlock` with no parent | a new card, in progress, titled `Name: first string argument` |
| the same inside a subagent (`parent_tool_use_id` set) | a line in the parent card's details (the last 10) |
| `ToolResultBlock` or `ServerToolResultBlock` for a card | the card completes, or shows an error when `is_error`; its output is the first line |
| `TaskStartedMessage` | its tool's card notes "Running in background", or a new card |
| `TaskProgressMessage` | the card's details show the task's description |
| `TaskNotificationMessage`, a terminal `TaskUpdatedMessage` | the card completes, shows an error when the task failed, or completes with `Stopped` |
| `AssistantMessage.error` `authentication_failed` | a note asking to run `claude` and `/login` on the host |
| any other `AssistantMessage.error` | `Claude Code reported an error` with the error code |
| `ResultMessage` | its text, when nothing else was written (local commands such as `/usage` stream nothing) |

When the turn ends, every card still open is closed first (with `Stopped` when the turn was
interrupted), then the reply ends.

## Writing to Slack

Each reply streams into the thread of the message that asked for it, through slack-sdk's
`chat_stream` (`chat.startStream`, `chat.appendStream`, `chat.stopStream`) with task cards in
`timeline` mode. Cards finish with the status `complete`; Slack rejects `completed`. Every card
still open when the turn ends is closed first, because Slack draws a card left open as an error.
The footer travels as a context block on `chat.stopStream`.

If Slack refuses to start a stream, `code_with_slack.render.sinks.ReplySink` replays the reply
into one message updated with `chat.update` at most once a second, and every later reply in the
process does the same. If a stream fails after it started, only that reply moves to a new message.

## Approvals

When Claude Code asks for permission, the SDK calls `can_use_tool`. The session posts the
request in the reply's thread with **Approve** and **Deny** buttons, and waits, for as long as it
takes. A clarifying question (Claude Code's `AskUserQuestion` tool) arrives the same way and is
posted as one menu per question with **Submit** and **Skip**; the picked labels go back as the
tool's answers. Each request has a random id that only its buttons carry; a click resolves it
once, only from the channel it was posted in, and only after the identity and channel guards.
`/cc stop` denies every request still pending in the channel.

## Footer

Every reply ends with one context line: `⚡ bypass` when bypass is on, the git branch of the
channel's directory, the model and the context percentage from the SDK's
`get_context_usage()`, the session's tokens from the turn's `ResultMessage.model_usage`, and the
5-hour and weekly limits. The limits come from Claude Code's `/usage`, sent on a separate
long-lived client and cached for five minutes; a rate-limit event from the SDK invalidates the
cache. The limit fields exist only with a claude.ai subscription. A field that cannot be read is
left out.

## Sessions

`code_with_slack.sessions.SessionManager` keeps one `ChannelSession` per bound channel.

- The Claude Agent SDK client is created on first use, with the channel's directory as its
  working directory, `resume` set to the stored session id, the owner's own settings
  (`setting_sources` user, project and local), streaming of partial messages, the approval
  callback, and `--allow-dangerously-skip-permissions`, which makes `/cc bypass on` possible
  without turning it on.
- After connecting, `get_server_info()` gives the commands the session offers (for the picker
  and `!`) and the permission mode that `/cc bypass off` returns to.
- If the stored session cannot be resumed (its transcript was deleted), the session id is
  cleared, a new session starts, and the reply opens with a line saying so. If the channel's
  directory no longer exists, nothing starts and the reply asks to bind the channel again. If
  macOS privacy protection denies the daemon the directory (a launchd service does not inherit
  Terminal's access to `~/Documents`), nothing starts and the reply says how to grant access.
- If the Claude Code process exits or its stream fails, the open reply ends with an error line,
  every waiting message is told, and the next message starts a new process. A reply Slack cannot
  take (a network failure) is dropped; the session and the running turn go on.
- Messages are queued and run one at a time; each reply streams in the thread of its message.
  `/cc stop` interrupts the running turn and denies its pending approvals.
- One reader task follows the SDK's message stream for the life of the client. On each result
  the session id is stored (so `/clear`, which starts a new session, is recorded), the footer is
  built and the reply closed.
- A background task that finishes between turns sends its notification while the session is
  idle, then Claude Code starts a turn of its own to report it. That turn is posted under a new
  root message, `Background task update`, and the next queued message waits for it to finish. If
  no turn follows within 30 seconds, the notification is posted on its own and the queue moves
  on. When a queued message and a notification cross, the result's `origin` tells whose turn it
  was, and the queue is put back in order; that one reply can land in the other thread.
- Logs carry channel ids and exception type names, never prompt or reply text.

Bypass is a field of the in-memory session and nothing else: `state.json` never holds it, and a
restart brings every channel back to Claude Code's own mode. That matches Claude Code, whose
`--resume` does not restore `bypassPermissions` either.

## Slack handlers

`code_with_slack.slack_app.build_app` registers one listener per inbound path: `message`
events, the `/cc` command, the Approve, Deny, Submit and Skip buttons, and the command picker
(an `external_select` whose options come from the session's `get_server_info()["commands"]`).
Each acknowledges Slack first, then checks the owner, the workspace and the channel itself. A
failure after the checks reaches the owner as an ephemeral error line. A
message starting with `!` runs a Claude Code command when the word after it is one the session
offers, and is sent as a normal prompt otherwise. Bolt's per-request authorization returns the
identity `auth.test` gave at startup, so no request costs an extra API call.
