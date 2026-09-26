# Architecture

code-with-slack is one Python process. It holds a Slack Socket Mode connection and one Claude
Agent SDK client per bound channel, all on one asyncio event loop.

## Startup

`code-with-slack` (`code_with_slack.__main__.main`) starts in this order: it loads the
configuration, so a bad `.env` fails before anything else; takes the single-instance lock, so a
second daemon fails before it opens a Socket Mode connection; reads `state.json`; calls `auth.test`
for the workspace id and the bot user id; then opens the Socket Mode connection. On `SIGTERM`, which
`launchctl kill TERM` and `launchctl bootout` send, `SessionManager.drain` lets the turns already
sent finish, each up to its reply's final write (footer included), and the background tasks with the turns that report them (a task whose end came without
its notification is waited for `sessions.INJECTED_TURN_WAIT`, since the CLI can suppress it), and
sends no other: a new prompt gets `texts.RESTARTING`, a queued or taken turn ends with
`texts.ENDED_RESTARTING`. Approvals and questions stay open: the Socket Mode connection closes only
after the drain. When no channel is working, after `__main__.DRAIN_LIMIT_SECONDS`, or on a second
signal, the daemon closes the connection, then every session. After `bootout` launchd kills the
daemon once the LaunchAgent's `ExitTimeOut` passes (60 seconds at most), whatever the drain is
doing. `SIGINT` skips the drain: from a terminal it also reaches the Claude Code processes, which
claude-agent-sdk starts in the daemon's process group. Logs go to standard error, which the
LaunchAgent writes to `~/Library/Logs/code-with-slack/code-with-slack.log`.

## Configuration

`code_with_slack.config.load_config` reads `~/.config/code-with-slack/.env` with
`python-dotenv`, without copying anything into the process environment. It refuses a file that
is not a regular file, belongs to another user, or is readable by group or others, and it
refuses tokens of the wrong kind. [setup.md](setup.md) lists the variables.

## State and the single-instance lock

`code_with_slack.state.StateStore` keeps, for each bound channel, its directory, its Claude
Code session id and its bypass switch, in `~/.config/code-with-slack/state.json`. Every change is written to a
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

Messages with a subtype (edits, deletions, joins) and messages from bots are ignored, except
`file_share`, a message carrying files. A `file_share` event has no `team` field (measured
2026-09-25): `guards.message_actor` takes the workspace from the files' `user_team`, which must be
the same for every file, or the message is refused. A refusal reaches the owner as an ephemeral
message; everyone else gets nothing.

## Attached files

`code_with_slack.attachments` handles the files of a `file_share` message before anything reaches
Claude Code. Every file is checked first (`attachments.refusal`): its download URL must be
`https://files.slack.com/...`, the only host that receives the bot token, and an image must be
JPEG, PNG, GIF or WebP, at most 7.5 MB (10 MB once base64-encoded) and 8000x8000 px, the limits of
Claude's vision API; any other image type is refused. A file that is not an image must be a
`text/*` type (which covers source code) or one of `attachments.FILE_TYPES` (PDF, JSON, XML,
YAML, JavaScript, shell, SQL, TOML, Jupyter notebook), at most 100 MB; any other type is refused. One message takes at most 5 images and 15 MB
of images in all: images stay in the conversation and are sent again at every turn, and a request
is capped at 32 MB. Then the files are downloaded together, with the bot token and no redirect
followed; `attachments.download` checks the host again beside the header. Slack answers 302 when
the app lacks `files:read` (measured 2026-09-25). An image becomes an image block, and the turn's
prompt one user message of content blocks, sent through the SDK's streaming input; any other file
is saved to `$TMPDIR/code-with-slack/` and its path is appended to the prompt, but only once every
file arrived, so a failed message leaves no copy. The folder must be a directory of this user with
mode 700, or nothing is written there; at each start the files older than 3 days are removed, so a
conversation resumed after a restart still finds its files. A refused file or a failed download
sends nothing and tells the owner which file and why. Prompts, messages with files and Claude Code
commands enter the queue in the order they were sent, although downloads take a while. A message
that waited for its turn is sent to the channel's session as it is then; if a `!bind` moved the
channel to another folder meanwhile, nothing is sent and the owner is told.

## Rendering

`code_with_slack.render.renderer.TurnRenderer` reads SDK message types only, never tool names, so
a tool Claude Code adds later gets its line in the reply with no code change.

| SDK input | What the owner sees |
|---|---|
| `StreamEvent` with no parent, a `text_delta` | the text, as it is written |
| a top-level `TextBlock` in an `AssistantMessage` | nothing more: the same text already arrived as deltas |
| `ToolUseBlock` or `ServerToolUseBlock` with no parent | a new tool line, in progress, titled `Name: first string argument` |
| the same inside a subagent or a skill run in a forked context (`parent_tool_use_id` set) | the parent's line counts the subagent's calls and shows its latest one (`… Agent: review · 12 calls · Bash: ls`), and becomes a task's line: it keeps a line of its own once it ends, in the foreground or in the background |
| `ToolResultBlock` or `ServerToolResultBlock` for a line | the line completes, or shows an error with the output's first line when `is_error`; a line whose task already ended as stopped keeps `Stopped` |
| `TaskStartedMessage` | for a tool call, nothing yet: Claude Code starts a task for a long command in the foreground too, which ends before the call's result. When the call's result arrives with its task still running, the line becomes a task's line, notes "Running in background" and stays in progress. A task with no call in the reply gets a new line |
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
text as Claude writes it, and the tool calls where they happen. Claude's text is a `markdown`
block; each run of tool calls between two pieces of text is a `context` block (small, grey text,
as the terminal dims them), escaped for mrkdwn and marked with a `tools-` block id.
`sinks.tool_lines` shows such a run the same way while the turn runs and once it ends: the calls
that ended fold into one first line of tool names and counts, by the tool's name whatever the
tool, succeeded ones after `✓` and failed ones after `✗` (`✓ Bash ×3 · Read · ✗ Bash`). Below
it, a line of its own for a call still running (`…` and its title), a task
(`TaskUpdate.task`: a subagent, a background command, with its own `✓` or `✗` and summary once
it ends) and a stopped call (`Stopped`). While the reply is written, the last call of the last
run also keeps its line, running or ended, with no icon unless it failed (`✗` and the output's
first line), until another call or Claude's text follows it: a
call that ends within the one-second rewrite would otherwise never show. Then it moves into
the counts.
The reply is posted as soon as the owner's message is queued, showing only a status line:
`Claude is writing…`, or `Waiting for the previous reply…` behind another turn. While the turn
runs the status stays last; when it ends, a divider and the footer replace it. Only the
channel's latest reply shows the footer: a new reply takes it over (`ReplySink.set_latest`), so
it stays at the bottom of the channel as the terminal's status line. A reply longer than about 11,000 characters continues in a new message.

Slack's native streaming API (`chat.startStream`) is not used: in an ordinary channel it works
only inside a thread, and replies belong in the main window. A write Slack refuses, or cannot
receive because the network is down, is retried with the whole reply at the next rewrite; it never
stops the Claude Code session. Every message the daemon posts turns link and media previews off
(`unfurl_links`, `unfurl_media`), so a link in Claude's text is never fetched by Slack on its
own. The final rewrite has no next one: when Slack refuses its content
(`invalid_blocks`, `msg_too_long` and the like, not a rate limit), that message is written once
more as plain text, its text and the footer with no blocks, and the rewrite goes on to the next
messages, so none keeps saying `Claude is writing…`. A final rewrite that fails for any other
reason (the network, or a rate limit slack-sdk has already retried) is tried once more after
`FINAL_RETRY_SECONDS`.

## Approvals

When Claude Code asks for permission, the SDK calls `can_use_tool`. The session posts the
request as a message of its own, below the reply, with **Approve** and **Deny** buttons, and waits, for as long as it
takes. The request shows the tool's whole input, since Approve hands Claude Code the whole input:
it runs over as many code blocks as it needs, and past one message it keeps the start and the
end with a line saying how many characters are not shown. Everything the model wrote (the
title, the description, the input, a question's header) goes to Slack with `&`, `<` and `>`
escaped and a zero-width space after each backtick, so no `<url|label>` can hide what it links
and no text can close its code block. A clarifying question (Claude Code's `AskUserQuestion` tool, 1 to 4 questions) arrives the
same way and is posted as one line naming the questions, with **Answer** and **Skip**. Answer
opens a modal (`approvals.question_view`) that shows one question at a time, since Slack has
no tabs: radio buttons, or checkboxes when several may be picked, each option with its
description, and an **Other** field, as the terminal offers. The modal's own button reads
`Next (1/3)` and moves on (`response_action: update`) only once the question has an answer,
otherwise the question gets an error (`response_action: errors`); it reads `Submit` on the last.
What was filled travels in the view's `private_metadata` (`approvals.Draft`, under Slack's
3,000 characters) from one question to the next.
The picked labels, with the text typed under Other as the answer itself, go back as the tool's
answers. The modal carries no channel: each click in it is checked against the owner, the
workspace, and the channel its request was posted in. Each request has a random id that only its buttons carry; a click resolves it
once, only from the channel it was posted in, and only after the identity and channel guards.
Once decided, the request message is deleted: the tool's line in the reply records the call.
`!stop` denies every request still pending in the channel and deletes its message. A request
Slack does not accept is denied at once, with a message telling Claude Code that it could not be
shown, and the tool's line records the denial.

## Footer

Every reply ends with one context line: `⚡ bypass` when bypass is on, the git branch of the
channel's directory, the model and the context percentage from the SDK's
`get_context_usage()`, the effort level, the session's tokens from the turn's `ResultMessage.model_usage`, the
5-hour and weekly limits, and last the last two names of the channel's directory, as a terminal
status line such as ccstatusline shows the working directory. The effort level is the one Claude Code reports in the input of a
`Stop` hook the daemon registers on each client (`effort.level`); `/effort` and `/model` run no
hook, so after one of them the footer follows its output (`Set effort level to ...`). Until Claude
Code reports a level on the running client the footer leaves it out, and when the model takes no
effort parameter it shows `default`. The limits
come from Claude Code's `/usage`, sent on a separate
long-lived client and cached for five minutes; a rate-limit event from the SDK invalidates the
cache. The limit fields exist only with a claude.ai subscription. A field that cannot be read is
left out.

`!status` lists the same values one per line (`Model: ...`, `Context: ...`), read by the same
`ChannelSession._footer_data` and written from the same list, `footer.footer_fields`, as the
footer writes them, then the running tasks; bypass and the folder are left out, since its Mode and Directory
lines show them. It starts the channel's client when none is running, since the model and the
context come from it (`get_context_usage()` answers before a session's first turn and during a
turn: measured on claude-agent-sdk 0.2.158, bundled CLI 2.1.280, 2026-09-25). The session tokens
are those of the client's last result, left out until its first turn and after a result that
reports none (`/usage`, `/clear`). Claude Code's version comes with a turn's `init` message and not
with the connect (measured 2026-09-26, same versions), so a client started by `!status` shows
`started, version shown after the first turn`. When the directory is missing, unreadable or not
trusted, the status ends with the message a prompt would get there; when the client fails to start
for another reason, it ends with the error line a prompt would get.

## Sessions

`code_with_slack.sessions.SessionManager` keeps one `ChannelSession` per bound channel.

- The Claude Agent SDK client is created on first use, and only in a folder the owner has
  trusted in Claude Code. An SDK session never shows Claude Code's trust dialog and counts as
  trusted, so a repository's own hooks, `env` block and allow rules would apply at once.
  `code_with_slack.trust.workspace_trusted` reads Claude Code's record
  (`projects["<path>"].hasTrustDialogAccepted` in `~/.claude.json`) by Claude Code's rules: in a
  git repository the repository root decides (the main checkout's root for a worktree) and a
  trusted parent does not cover it; outside git, a trusted folder covers its subdirectories. A
  folder git cannot answer about (git missing or failing) counts as untrusted. An untrusted
  folder starts nothing, and the reply says to open `claude` there in the terminal
  once and accept the dialog. A `!bind` runs the same checks (`sessions.check_directory`: missing,
  unreadable, untrusted): the channel is bound, and the answer gives the reason instead of
  promising a session.
- The client has the channel's directory as its
  working directory, `resume` set to the stored session id, the owner's own settings
  (`setting_sources` user, project and local), streaming of partial messages, the approval
  callback, and `--allow-dangerously-skip-permissions`, which makes `!bypass on` possible
  without turning it on.
- After connecting, `get_server_info()` gives the commands the session offers (for `!help` and
  `!`) and the permission mode that `!bypass off` returns to, or `default` when the folder's own
  settings start it in `bypassPermissions`. The footer shows `⚡ bypass` in either case.
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
  that opens with one line per task it reports, as the terminal prints it
  (`ChannelSession._ended_line`): a command's notification `summary`, which already reads
  `Background command "..." completed (exit code 0)`, or `Agent "<description>" finished` built
  from the task's `TaskStartedMessage`, since an agent's `summary` is its result; plus
  `usage.duration_ms` when the task reports it (`sessions.TASK_KINDS`, `SUMMARY_IS_END_LINE`).
  The next queued message waits for it to finish. `Background task update` opens it only when no task end was seen.
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
- The channel's latest reply counts what is still running at the end of its footer, or of its
  status line while Claude writes: `⏳ 1 shell · 1 agent`, by each task's `task_type`
  (`sessions.TASK_KINDS`; a type not listed counts as a task). A new reply takes the counts
  over and the previous one drops them; they disappear when nothing runs.
- When the Claude Code process goes away (shutdown, rebinding, a process that exits), its tasks
  go with it: their lines close with `Stopped` and the list empties. The map lives in memory only.
- Shutting down, once the drain has ended, or binding the channel to another directory closes the
  session: every reply still waiting (running, sent or queued) ends with `This reply ended before an answer:` and the
  reason. A bind stores the new directory before the old session closes, so a message that
  arrives meanwhile opens the new session. A closing session waits for a client that a daemon
  word (`!help`, `!bypass`, `!status`) is still starting, then closes it, and starts no other:
  the word gets `SessionClosed`, which tells the owner to send it again, and `!bypass` stores
  nothing.
- Logs carry channel ids and exception type names, never prompt or reply text.

Bypass is the channel's `ChannelState.bypass` in `state.json`, which `ChannelSession.bypass`
reads: a restart of the daemon, whatever its cause, keeps it, and the next Claude Code process
gets it from `ensure_connected`. Claude Code's own `--resume` never restores `bypassPermissions`
(sessions reference, read 2026-09-26); here the daemon restarts on its own (an update, launchd
after a crash), so the switch stays with the owner's `!bypass off`. A restarted daemon starts no
Claude Code process until a message arrives. At the start of `SessionManager.drain`, every channel
with bypass on gets `texts.BYPASS_RESTARTING`. `!resume` keeps bypass, as the terminal's `/resume`
keeps the current session's mode; `!bind` starts a new session in another folder without it,
and its answer says so when bypass was on.

## Slack handlers

`code_with_slack.slack_app.build_app` registers one listener per inbound path: `message`
events, the Approve, Deny, Answer and Skip buttons, and the question form's Next and Submit. The app registers no slash command. Each
acknowledges Slack first, then checks the owner, the workspace and the channel itself. A
failure after the checks reaches the owner as an ephemeral error line.
A link Slack made from a typed address (`<url|label>`, `<url>`) reaches Claude Code as typed; a
link the owner named reaches it as `label (url)`, so the address is not lost; a mention stays in Slack's form (`<@U…>`), since naming the user would need a
scope the app does not have.
`code_with_slack.commands.parse_bang` reads a message starting with `!`: `help`, `bind`,
`bypass`, `status`, `stop`, `resume` and `guide` are the daemon's own words, answered with a message in the
channel (`!help`, `!guide` and `!bind` also work before the channel is bound); any other `!name args` runs
that Claude Code command when the session offers `name`, and is sent as a normal prompt
otherwise. `!resume` stands in for Claude Code's interactive `/resume`, which an SDK session
does not offer: `code_with_slack.resume` lists the directory's sessions from the SDK's
`list_sessions` with the columns of the terminal's picker (name or title, time since the last
activity, git branch, size), the first 8 characters of the session id and a Resume button each, or matches
`!resume <id or name>`. The terminal's picker shows no id; the list shows its start because
`!resume` takes a full id or any start of one at least 8 characters long (`resume.ID_SHOWN`).
A shorter target is read only as a title.
The list holds the directory's own sessions, not other worktrees', as the terminal's picker
starts. Resuming stores the session id for the channel and closes the channel's client, only
when no turn or background task is running or waiting; the next message starts Claude Code with `resume` set to it. A Resume click is
checked like any other button, and the session must still be one of the directory's.
`!bind` alone lists, through `code_with_slack.folders`, the folders where a session can start:
`ALLOWED_ROOT`, then its folders, then theirs, skipping hidden folders and symlinks and never
descending into a git repository (a `.git` directory or file). A folder is kept when
`code_with_slack.trust` accepts it. The checks run eight at a time and stop once one more than
the 20 rows shown is found, so the higher levels fill the rows, which are then shown in path
order. The trust record is parsed again only
when its mtime or size changes. A Bind click is checked like any other button, its folder goes
through the same `resolve_directory` check as a typed `!bind <folder>`, a click on the channel's
own folder changes nothing, and a click while work is in flight is refused, as a Resume click is.
The `!resume` list reads the last message of a session's transcript only while that session can
still be among the 20 shown: a file's mtime bounds its last message from above. Resuming the
channel's own session changes nothing, and a resume whose listing overlapped a `!bind` is refused.
`commands.help_text` builds `!help` from `get_server_info()["commands"]` at the time
of asking, keeping only the lines that contain the text after `!help` when there is one, so a command a new Claude Code release adds needs no change here. Bolt's per-request authorization returns the
identity `auth.test` gave at startup, so no request costs an extra API call.
