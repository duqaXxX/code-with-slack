# Architecture

code-with-slack is one Python process. It holds a Slack Socket Mode connection and one Claude
Agent SDK client per bound channel, all on one asyncio event loop.

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

Every inbound path (a message, `/cc`, a button, the command picker) runs two checks of its own
before anything reaches Claude Code:

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
