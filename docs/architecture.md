# Architecture

code-with-slack is one Python process. It holds a Slack Socket Mode connection and one Claude
Agent SDK client per live thread session, across every bound channel, all on one asyncio event
loop.

## Startup

`code-with-slack` (`code_with_slack.__main__.main`) starts in this order: it loads the
configuration, so a bad `.env` fails before anything else; takes the single-instance lock, so a
second daemon fails before it opens a Socket Mode connection; reads `state.json`; builds a plain
`AsyncWebClient` and calls `auth.test` for the workspace id and the bot user id; then, with that
client (Socket Mode not opened yet), repairs what a crashed daemon left open
(`code_with_slack.repair.repair_crash`, issue #19: see below) and prunes `state.json`
(`StateStore.prune`, run only after repair, so a pruned thread's leftovers are still repaired
first): a thread whose session id no longer resumes in its folder, and a no-session thread whose
root message is more than a day old, are dropped; asks for the first session index
(`Home.request`, see "The session index" below); only then opens the Socket Mode connection and,
once connected, posts the v1-to-v2 upgrade notice to each channel that still owes one
(`__main__._post_upgrade_notices`), as a message of its own, not a reply. On `SIGTERM`, which
`launchctl kill TERM` and `launchctl bootout` send, `SessionManager.drain` lets the turns already
sent finish, each up to its reply's end (the stream's stop, footer included), and the background tasks with the turns that report them (a task whose end came without
its notification is waited for `sessions.INJECTED_TURN_WAIT`, since the CLI can suppress it), and
sends no other: a new prompt gets `texts.RESTARTING`, a queued turn is dropped without a reply of its own (`ThreadSession.drop_queued`): one note
(`sessions.not_sent`, `N messages were not sent because code-with-slack restarted: send them
again.`, with the start of each) is added to the end of the thread's running reply, or posted as a
message of its own when nothing runs. Approvals and questions stay open: the Socket Mode connection closes only
after the drain. A thread left with only background tasks gets `texts.RESTART_WAITS` once, naming
them by the footer's counts, since the daemon cannot tell whether a task (a dev server, a watcher)
ever ends; `!stop` ends them with `ClaudeSDKClient.stop_task`. Claude Code starts no turn to report a
task stopped this way (measured on 2.1.283), so neither the drain nor the thread's next prompt waits
`sessions.INJECTED_TURN_WAIT` for one. The signal names no sender, and the session that sent it
has a turn running when it arrives: every session with a turn running then gets
`ThreadSession.may_have_ordered_restart`, and a background task it starts after the signal (most
likely its own wait for the new process, which could only end once this one has exited, issue
#87) is left out of `ThreadSession.restart_ready`, the idle test the drain uses, and of
`texts.RESTART_WAITS`. The drain still waits for that session's turn and for every other task.
When no session is working, after `__main__.DRAIN_LIMIT_SECONDS`, or on a second
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

`code_with_slack.state.StateStore` keeps, for each bound channel, its directory and, for each of
its threads, the folder it was opened in, its Claude Code session id, its bypass choice (on, off, or
never chosen), the effort level set with `/effort` and `ended`, the reaction name of its root once
✅ or ❌ is requested (cleared when the root turns ⏳ or ✋ again; read only by the session index),
in `~/.config/code-with-slack/state.json` (version 2). `bypass` keeps its old meaning (`true` on, `false` not on); an explicit off adds
`bypass_off: true`, and neither set means never chosen, so older files read as before and old code
ignores the extra key (an off reads as not on there). Every
change is written to a temporary file beside it, synced, and renamed over it, so a crash leaves
either the old file or the new one. A file that cannot be read stops the daemon instead of being
replaced. A version 1 file (one session id and bypass switch per channel, no threads) is migrated
on load: each channel keeps its directory, gets an empty thread map and a pending upgrade notice;
the old session id and bypass switch, which belonged to the channel itself, are dropped.

Each thread also carries three fields for crash repair (issue #19), ids only, never message
content: `open_replies`, the ts of every open reply's last message, a stream or a stopped message (more
than one can be open at once, since a background task's own reply can outlive the turn that
started it; each `ReplySink` owns exactly one entry, added when its stream starts, replaced on a
continuation, removed once the reply's end is known to have landed or it has given up retrying
for good); `requests`, the ts of
every approval, question and D8 hold message still carrying buttons (added on post, removed on
delete or answer); `status`, the root's reaction name while it is ⏳ or ✋ (cleared once ✅ or ❌ is
requested). All three default to absent ("nothing open") for a v2 file written before they
existed, and are ignored by code that reads a v2 file without knowing them: the version stays 2.
A graceful close (`ThreadSession.close`) clears all three for its thread once it is done, whatever
the fields looked like partway through (`StateStore.clear_repair`), so only a crash ever leaves
them set.

On start, before the Socket Mode connection opens, `code_with_slack.repair.repair_crash` repairs
every thread `state.json` still shows as left open. For each open reply it stops the message's
stream (`chat.stopStream`; `message_not_in_streaming_state` means Slack closed it already, at 5
minutes, and is fine), reads the message back by its own ts (`conversations.replies` with `ts`
and `limit=1`) and edits it with `chat.update`: its blocks as Slack keeps them, every card left
`in_progress` closed as an error (a stopped stream stores it as one anyway), and
`texts.STOPPED_BEFORE_ANSWER` appended as a context block, or, at Slack's 50-block cap, added to
the last context block. The stream's own stop is the one notification the reply owes, and the
edit never notifies; nothing is posted. It deletes each stale request (`message_not_found` counts
as done) and sets ❌ on a root left ⏳ or ✋, through the same `StatusReaction` a live session
uses. Each field is cleared once its own repair has been
attempted, successfully or not (a failed `state.json` write here is logged and swallowed, never
left to break startup), so a second start never retries what an earlier one gave up on; one
thread's failure is logged and does not stop the others.

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

Where the daemon's own answers go is decided in `slack_app.handle_word`. A word typed at the top
level, or in a thread that holds no session, acts as a top-level word: its answer is a normal post
in the channel (`in_channel`, or `say` with no thread), which is neither ephemeral nor a thread
reply, so it stays after a reload and never notifies. A word typed inside a session's thread is
answered by `tell_owner` or an ephemeral `say` under the owner's message (`chat.postEphemeral`
with `thread_ts`), which Slack drops on reload, or by `acknowledge`, a ✅ reaction on the word
(`!bypass`); `!stop` there posts nothing when it stops something, since the session reacts on its own root,
and `texts.NOTHING_TO_STOP_THREAD` (ephemeral) when nothing runs. `word_report` chooses the same
place for a word's failure, and `reply_on_failure` logs a report that itself fails instead of
letting it raise, since a raise would reach the message handler's own failure path, which posts
`ERROR_REPLY` threaded under the word. The D5 old-folder notice and `Not sent.` are ephemeral as well. A Resume button
carries `<session id>@<thread ts>`, the thread of the owner's `!resume` message (`resume.parse_resume_value`);
the click is checked like any other inbound path, and a value in another shape (a list posted by
an older version) answers `texts.RESUME_STALE`.

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
that waited on its files is submitted to the thread's session as it is then; if that session
closed meanwhile (D9's idle close, most likely), the submit is retried once against a freshly
looked-up session for the same thread.

## Rendering

`code_with_slack.render.renderer.TurnRenderer` reads SDK message types only, never tool names, so
a tool Claude Code adds later shows in the reply with no code change. The table says "line" for
what the model holds per tool (`renderer.TaskUpdate`); the sink draws the lines on task cards, two
for a run of calls and one for a line with a view of its own (see below).

| SDK input | What the owner sees |
|---|---|
| `StreamEvent` with no parent, a `text_delta` | the text, as it is written |
| a top-level `TextBlock` in an `AssistantMessage` | nothing more: the same text already arrived as deltas |
| `ToolUseBlock` or `ServerToolUseBlock` with no parent | a new tool line, in progress, titled `Name: first string argument` |
| the same inside a subagent or a skill run in a forked context (`parent_tool_use_id` set) | the parent's line counts the subagent's calls (`Agent: review · 12 calls`) and, while it runs, holds its latest ones (`renderer.CHILD_LINES`) as the card's `details`, and becomes a task's line: it keeps a card of its own once it ends, in the foreground or in the background |
| `ToolResultBlock` or `ServerToolResultBlock` for a line | the line completes, or shows an error with the output's first line when `is_error`; a line whose task already ended as stopped keeps `Stopped` |
| `TaskStartedMessage` | for a tool call, nothing yet: Claude Code starts a task for a long command in the foreground too, which ends before the call's result. When the call's result arrives with its task still running, the line becomes a task's line, notes "Running in background" on its nested line and stays in progress. A task with no call in the reply gets a new line; one started by a call the reply never saw, while such a task (a command's) runs, is an agent inside that command and shows on the command's line, counted as a call with its description, as a subagent's calls show on its line |
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

`code_with_slack.render.sinks.ReplySink` writes each reply as a native Slack stream inside the
session's own thread, below the message that asked for it (`chat.startStream` in chunks mode,
addressed to the owner's user and team). The stream starts with Claude's first content, its first
text or the card of the first tool when a turn opens with one, and never with a placeholder. It
grows with `chat.appendStream` at most once a second per reply, and each append draws from one
shared `UpdateLimiter` (`sinks.UpdateLimiter`, injected through `SessionDeps`), as does every
`chat.update`: a token bucket that paces writes evenly at 40 per 60 seconds plus a burst of 5,
worst case 45 in one window, under the documented floor of `chat.update` (Tier 3, 50 or more a
minute, per app), so several busy threads together stay under the app's own budget instead of
racing through it and then freezing until it resets. The retry of a call that failed on the
connection is switched off for the three stream calls (`sinks.ConnectionRetryUnlessStream`): a
start, an append and a stop are not idempotent, and a reset can come after Slack applied the
call.

Within a reply, writes happen in the order things happen. Claude's text goes as `markdown_text`
chunks. Tools go as `task_update` chunks, each a card Slack updates in place by its id.

A run of calls, the calls between two pieces of text, shares two cards (`render.fold.Fold`, fed
by `ReplySink.task`). One call of the run is shown whole, titled with the tool's name and its
first argument: the last call started that still runs, else the last one shown, which joins the
counts when another call takes its place. Until there is a call to count, the first card shows
that call, and a run of one call stays one card. From then on the first card holds the counts of
what ended, in the terminal's words where the terminal has words (`render.previews.folded`: `Ran
2 shell commands · Read 1 file`), the failed calls after `✗`, and the second card shows the call.
Calls that run at the same time show one at a time. A failed call says why in its title (`Bash: pytest -q · Exit
code 1`). Neither card carries `details` or `output`: Slack appends both to what a card already
holds (measured 2026-10-01, slack-sdk 3.44.1), so a card that is reused keeps its text in its
title.

A call with a view of its own has a card of its own, keyed by its `tool_use_id`, and ends the
run: an `Edit` or a `Write` that ended well, a subagent, whose title counts its calls and whose
`details` say what it is doing now, a background task, which keeps its card `in_progress` for as
long as it runs, and a stopped call. A call that turns into one of these while a card of a run
shows it keeps that card.

Once the reply's body has ended, each run reads as one line of counts in a `context` block
(`✓ Ran 2 shell commands · Read 1 file · ✗ Ran 1 shell command`), as the terminal folds a run
that ended (`TaskUpdate.folded`, `ReplySink._card_blocks`). A stream cannot replace what it
showed, so the line is written by the `chat.update` that follows the stream's stop
(`ReplySink._end`), which never notifies. The reply has ended with the stop: an update that fails
is tried once more with the next write, and the cards stay if that fails too.

A finished `Edit` or `Write` shows its preview as a `blocks` chunk under the card
(`render.previews.preview`): a diff is a collapsible,
full-width `container` block (`sinks.diff_containers`), closed until the owner opens it, whose
title is the call's line (`✓ Update(notes.txt)`), its subtitle the sentence (`Added 1 line,
removed 1 line`), and inside is the whole numbered diff in a rich text preformatted element with
the language `diff`, which Slack desktop colours; each changed line also carries a red or green
square after its sign, since Slack mobile colours nothing. A diff longer than
`sinks.MESSAGE_LIMIT` continues in a second container with the same title, in the next message. A
new file shows its sentence and a `markdown` code block with its first 10 lines and `… +N lines`.
The preview reads `UserMessage.tool_use_result`, which the SDK does not document; any shape other
than the one measured falls back to the generic card, and the release probe's claim P13 checks the
shape on each new SDK.

The stream stays open until the reply ends, or until `sinks.STREAM_SECONDS` (280 seconds) after
it started, whichever comes first. Slack closes a stream 5 minutes after `chat.startStream`
(measured 2026-09-28: `chat.appendStream` refused at 300.3 seconds), and the daemon stops it a
little earlier itself.

- **The reply ends first.** `ReplySink.close_out` stops the stream with `chat.stopStream`,
  passing the footer, a divider and a context block, as `blocks` at the message's bottom (Slack
  renders them below the stream, buttons included). The stop is the one notification.
- **280 seconds pass first.** `ReplySink._expire` stops the stream (Slack pushes on the stop, the
  first notification) and the same message keeps growing with `chat.update`, which never
  notifies (measured 2026-09-29: stop at 4 minutes 51 seconds, ten updates, silent). Each update
  writes the whole message from the renderer's model as `markdown` blocks and `task_card` blocks
  (a run of calls keeps its two cards until the body ends), with a short `text`, since a
  `chat.update` whose `text` is long fails `msg_too_long`. The end
  posts a closing message in the thread with the footer (`ReplySink._write_closing`), the second
  notification. Its `text`, the banner, is the start of Claude's answer as plain text
  (`sinks.banner_text`), never a line of the daemon's.

A message holds 12,000 characters and 50 blocks or task cards (measured 2026-09-28); a reply past
`sinks.MESSAGE_LIMIT` or `sinks.BLOCKS_LIMIT` continues in a new message, a new stream while the
first one still streams, else a post. Every message a reply adds notifies once.

The reply ends once its turn has ended and none of its tasks still runs or still waits on a turn
Claude Code starts to report it (D1): a task the turn started keeps its card open and updating
in place, `ReplySink.finish` ends only the body, and `ThreadSession._still_owed` decides when
`ReplySink.close_out` runs (the CLI can suppress the report's notification, so past
`sessions.INJECTED_TURN_WAIT` `ThreadSession._expire_unreported` gives up on it). A report turn
can name only the reply of the first task it covers when several end together;
`ThreadSession._sweep_closed_out`, run after every turn and after
`ThreadSession._expire_injected_turn`, ends every other reply left eligible. A turn Claude Code
starts on its own to report a background task renders into the reply that started the task
(`ThreadSession._opening_target`); when that reply has already ended, the report gets a reply of
its own.

`!stop`, a restart, an error that cuts a turn, an idle close and `SessionGone` end the reply
through the same path, at once: the stream stops with the footer and, for `!stop`, the stopped
command's card, and that stop is the notification (`ThreadSession._stop_task_replies` for the
tasks' replies). A stream whose last append has an unknown outcome (a reset, a timeout) is told
nothing more: it is stopped and the message goes on by `chat.update` from the model. Only the
thread's latest reply shows the footer (`ReplySink.set_latest`), so it stays at the bottom of the
thread as the terminal's status line. A card left `in_progress` in a stopped message is stored
as an error until it is updated (measured 2026-09-28), so every end closes its cards first.

Slack's push behaviour is what makes this shape: a stream in a thread the owner started notifies
once, when it stops, with its first text as the banner, and never when it starts (measured
2026-09-29, iPhone locked, Slack open in a browser, channel on Just mentions; Slack's reference
says nothing on it). Every write while Claude works is silent. An open stream cannot be deleted
(measured 2026-09-28), which is why a reply never starts as a placeholder that a later write
replaces.

`chat.startStream` works only inside a thread and answers `invalid_thread_ts` without
`thread_ts` (measured 2026-09-23), which the thread model satisfies. A write Slack refuses, or
cannot receive because the network is down, is sent again with the whole reply at the next write;
it never stops the Claude Code session. Every message the daemon posts turns link and media
previews off (`unfurl_links`, `unfurl_media`), so a link in Claude's text is never fetched by
Slack on its own. The end has no next write to fix it: when Slack refuses the content of an
edited message (`invalid_blocks`, `msg_too_long` and the like, not a rate limit), that message is
written once more as plain text, and the reply goes on to the next messages. An end that fails
for any other reason (the network, or a rate limit slack-sdk has already retried) is tried once
more after `FINAL_RETRY_SECONDS`; until it lands, `ThreadSession` shows no ✅ (`_track_landing`),
shows ❌ if the retry fails too, and keeps the persisted status so the next start's repair still
finds the reply.

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
answers. The modal carries no channel or thread: each click in it is checked against the owner,
the workspace, and the channel and thread its request was posted in (`approvals.Draft.thread_ts`,
alongside its `private_metadata`). Each request has a random id that only its buttons carry; a
click resolves it once, only from the channel and thread it was posted in, and only after the
identity and channel guards. Once decided, the request message is deleted: the tool's line in the
reply records the call. An answered question is kept instead, rewritten with no buttons as the
terminal keeps it (`approvals.answered_blocks`: `User answered Claude's questions:`, then
`⎿ · question → answer`, cut at Slack's 3,000 characters); if Slack refuses that rewrite, the
request is deleted, so no button is left that no longer works.
`!stop` denies every request still pending in the session's own thread and deletes its message. A
request Slack does not accept is denied at once, with a message telling Claude Code that it could
not be shown, and the tool's line records the denial.

## Footer

Every reply ends with one context line (`footer.format_footer`): `⚡ bypass` when bypass is on,
the model from the SDK's `get_context_usage()`, the effort level, the name of the folder this
thread was opened in, the git branch and the uncommitted changes of the folder the session works
in, the session's tokens from the turn's `ResultMessage.model_usage`, the context percentage from
`get_context_usage()`, and the 5-hour and weekly limits with the time to each reset. The folder the session works in is
the `cwd` of the latest hook input, which follows a `cd` and a worktree
(`ThreadSession.working_directory`): the same `Stop` hook, and a `PostToolUse` hook after every
tool, so a turn stopped or failed before its `Stop` still moves it. Until a hook reports it, and
again after the client restarts, it is the thread's own folder. The changes are the lines
inserted and deleted since the last commit, staged and unstaged, untracked files not counted, as
ccstatusline's git-changes counts them. They come from plumbing commands (`git diff-files
--shortstat` and `git diff-index --cached --shortstat HEAD`, the empty tree before a first
commit), which never write the index: `git diff` refreshes it under `index.lock`, and a diff
killed at `GIT_TIMEOUT` would leave the lock behind and stop every commit. git runs there with
`core.fsmonitor` off, so a repo's own configuration runs no command. The effort level is the one Claude Code reports in the input of a
`Stop` hook the daemon registers on each client (`effort.level`); `/effort` and `/model` run no
hook, so after one of them the footer follows its output (`Set effort level to ...`). Until Claude
Code reports a level on the running client the footer leaves it out, and when the model takes no
effort parameter it shows `default`. The limits
come from Claude Code's `/usage`, sent on a separate
long-lived client and cached for five minutes; a rate-limit event from the SDK invalidates the
cache. The limit fields exist only with a claude.ai subscription. A field that cannot be read is
left out.

`!status`, sent inside a session's thread, lists the same values one per line (`Model: ...`,
`Context: ...`), read by the same `ThreadSession._footer_data` and written from the same list,
`footer.footer_fields`, as the footer writes them, then the running tasks; bypass and the folder
are left out, since its Mode and Directory lines show them. When the session works in another
folder than the one it was opened in, a `Working in:` line names it before the values. It starts
the thread's client when none is running, since the model and the
context come from it (`get_context_usage()` answers before a session's first turn and during a
turn: measured on claude-agent-sdk 0.2.158, bundled CLI 2.1.280, 2026-09-25). The session tokens
are those of the client's last result, left out until its first turn and after a result that
reports none (`/usage`, `/clear`). Claude Code's version comes with a turn's `init` message and not
with the connect (measured 2026-09-26, same versions), so a client started by `!status` shows
`started, version shown after the first turn`. When the directory is missing, unreadable or not
trusted, the status ends with the message a prompt would get there; when the client fails to start
for another reason, it ends with the error line a prompt would get.

## Sessions

`code_with_slack.sessions.SessionManager` keeps one `ThreadSession` per open Slack thread, across
every bound channel. A top-level message opens one in the channel's current folder
(`SessionManager.open`); a reply inside a thread hands back its existing one, rebuilding it first
if a restart, an idle close or a gone resume dropped it (`SessionManager.get`); a Resume click or
`!resume <id or title>` opens one already set to a chosen session id, in the thread of the owner's
`!resume` message (`SessionManager.resume`); `slack_app.resume_into_thread` posts the confirmation, then edits the list the click
came from whether or not the confirmation posted, so buttons never outlive a resume and a failed
edit never blocks the confirmation.
Each thread keeps the folder it was opened in for as long as it exists: `!bind` changes only
where the *next* thread starts, and refuses while any of the channel's threads is not idle
(`SessionManager.bind`).

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
- The client has the thread's own directory as its
  working directory, `resume` set to the thread's stored session id and effort level
  (`sessions.client_options`), the owner's own settings
  (`setting_sources` user, project and local), streaming of partial messages, the approval
  callback, and `--allow-dangerously-skip-permissions`, which makes `!bypass on` possible
  without turning it on.
- After connecting, `get_server_info()` gives the commands the session offers (for `!help` and
  `!`) and the permission mode Claude Code started in (`native_mode`, kept as reported):
  `!bypass off` returns to it, or to `default` when the folder's own settings start it in
  `bypassPermissions`. `ThreadSession.bypass` is the one answer to "does this run in bypass":
  the owner's choice when there is one, else `native_mode`. The footer's `⚡ bypass`, `!status`,
  the channel list and the setup's checkbox all read it.
- If the thread's stored session cannot be resumed (its transcript was deleted), its entry is
  dropped, the thread ends (`SessionGone`), and its reply, and every reply still waiting in it,
  says so (`texts.SESSION_GONE`); the next message in that thread starts a session there again,
  as a fresh top-level message would. If the thread's directory no longer exists, nothing starts
  and the reply says so (`texts.DIRECTORY_MISSING`). If macOS privacy protection denies the
  daemon the directory (a launchd service does not inherit Terminal's access to `~/Documents`),
  nothing starts and the reply says how to grant access.
- If the Claude Code process exits or its stream fails, the open reply ends with an error line,
  every waiting message is told, and the next message starts a new process. A Slack failure
  while a reply is written never stops the session or the running turn.
- Messages are queued and run one at a time; each gets its own reply in the thread.
  `!stop` interrupts the running turn, denies its pending approvals and stops the thread's
  background tasks (`ClaudeSDKClient.stop_task`), then shows `✅` on the root through
  `ThreadSession._react`, which clears `_error_standing`: a stop the owner gave is not an error.
- D8: before a message would wake an idle session (`slack_app.submit_to_session`), a live session
  of any other thread, of any channel, whose resolved folder is the same and is not idle
  (`SessionManager.working_in`) makes the daemon ask first: `Another session is working in this
  folder: <link>. Send anyway?`, with Continue and Cancel (`slack_app.hold_before_sending`, kept
  in `hold.Holds`, memory only). The wait runs inside `submit_to_session`'s own `arrival_lock`, so
  a later message of the same thread queues behind it rather than opening a second hold. `!stop`
  inside the held thread or a top-level `!stop` of its channel cancels the wait the same way
  Cancel does (`Holds.cancel`); so does `SessionManager.drain`, which also cancels every hold
  still open when a restart starts (a hold opened after that point checks `sessions.draining`
  itself, since the drain never revisits it). Either way the owner gets `Not sent.`; a hold a
  message could not post is cancelled and told `HOLD_UNPOSTED`, failing closed rather than
  sending into a folder another session is using.
- Session setup (issue #74): a session's first prompt is held before D8 and before anything is
  sent: a top-level message that opens a session (a prompt, files, or a `!name` passthrough), and
  a reply in a thread where nothing was ever sent (`ThreadSession.never_ran`: no turn queued and
  no stored session id), as after a cancelled setup or a D8 Cancel. `slack_app.setup_before_sending`
  connects the client, then posts one message in the thread (`setup.setup_blocks`): a header
  (`texts.SETUP_HEADER`) and one `actions` block (block_id `setup`, one row that Slack wraps on a
  narrow screen) holding the four controls. A Model select lists the CLI's own models
  (`get_server_info()["models"]`, kept as `ThreadSession.models` and stored with the pending
  setup, so a click reads its choice against the list the message was built from), each option
  showing the model's `displayName` and, under it, the CLI's own `description` (cut to Slack's 75
  characters, left out when the entry has none). An Effort select offers `Effort: default`
  (which passes nothing) and `Effort: <level>` for the chosen model's `supportedEffortLevels`.
  A Bypass checkbox is ticked when the folder's own Claude Code settings start the process in
  bypassPermissions, so unticking is an explicit off, with `!bypass off`'s semantics. Start
  carries the setup id. `state.values` is keyed by that block_id, then by each control's
  action_id (`setup.read_choice`). Changing the model rewrites the message
  (`chat.update`, which never notifies, and is skipped once the setup is decided) with the new
  model's levels. Start reads every control from the click's `state.values`
  (`setup.read_choice`), and `ThreadSession.apply_setup` applies it, and Start is authoritative: effort and bypass are written
  from the choice whatever `state.json` held (a restart or a `!bypass on` typed meanwhile can
  have left either), so what runs is what the summary says. An effort the live client was not
  built with is stored and the client reconnected (the SDK has no runtime effort setter and no query has been
  sent, so no session is lost), a non-default model is `set_model()` on the live client (not
  stored: it survives a resume and leaves the owner's default alone, measured 2026-09-30, CLI
  2.1.285), and bypass goes through `set_bypass` when the choice differs from what the live client
  effectively runs (`_client_bypass`: the choice, or the folder's own bypass). The message then
  becomes one summary line, written inside the same wait (the cancel window covers the limiter
  wait; a cancel that already deleted the message skips the edit), and stays; the held message goes on unchanged. The wait shares `slack_app.ask_owner` and
  `hold.Holds` with D8. The entry stays in `Holds` while the answer is applied, so `!stop`, a
  top-level `!stop` and a drain cancel it then too (`Pending.cancelled`): nothing is sent, the
  message is deleted, the owner gets `Not sent.` and `!stop` does not say nothing is running. An
  answer that arrives before `chat.postMessage` has returned is applied the same way. A failed
  apply removes the message and the error reaches the owner. Whatever an unsent Start applied
  (stored effort, bypass, the client with its effort and model) is undone by
  `ThreadSession.forget_setup`, called when a setup is cancelled or fails, when D8 cancels after
  Start, and before each new setup, so the setup asked again shows the defaults. A setup shows
  ✋ and pauses the idle timer; crash repair deletes a setup message left standing.
- Each `ThreadSession` keeps one `render.status.StatusReaction` on its own root message (D10),
  which `thread_ts` always is: a top-level owner message, or the owner's own `!resume` message.
  `ThreadSession._react` shows it as a tracked background task, since a reaction must never delay
  a turn; the one exception is `✅`, awaited right after the closing message it follows, so it
  never shows first. `⏳` working: a turn is submitted or sent, or a report turn starts. `✋`
  waiting: an approval or a question is open, back to `⏳` once it is answered and the turn
  continues. `✅` ended: the closing message of the latest prompt posts with nothing else of the
  session running, queued or owed (`ThreadSession.idle`); a second prompt queued behind the first
  keeps it `⏳` until everything has ended. `❌` error: a turn fails, a
  restart's drain drops a queued or taken turn, `SessionGone`, or a shutdown's drain cuts short a
  busy session. A session whose only unfinished work at the shutdown is a task the drain left out
  (`ThreadSession.may_have_ordered_restart`) gets `✅` instead. A `❌` stands until new work starts (a submit or a report turn, both of which
  clear `ThreadSession._error_standing`), never flipped back to `✅` by some unrelated task's own
  idle sweep in between (`ThreadSession._react_done_if_idle` reads that flag, not
  `StatusReaction.current`, which only updates once its own `reactions.add` call returns and can
  lag a quick turn); D9's own idle close never touches the reaction at all, whatever it reads. A
  session rebuilt on the same root (a restart, a resumed thread) starts a fresh
  `StatusReaction`, whose first successful `show` strips every other reaction name already on
  the root, so an earlier session's leftover `✅` or `❌` never sits next to the new one. A
  `missing_scope` failure (the workspace has not reinstalled the app for `reactions:write`) is
  logged once for the whole process; every `StatusReaction` instance stops calling Slack for
  reactions for the rest of the run. A top-level word (`!status`, `!stop`, `!bind`) is not a
  session and gets no reaction of its own.
- One reader task follows the SDK's message stream for the life of the client. A turn starts
  at its first text or tool message, or earlier at a `TaskStartedMessage` with no
  `tool_use_id` that comes while a message is sent and no report turn is expected: a skill with
  `context: fork` typed as a command runs its agent before the turn's first message, and streams
  none of that agent's calls, so its line shows the command while it works. A task frame that
  names a call (`tool_use_id`) held by a reply whose background subagent still works goes to
  that reply, as the subagent's calls do (`parent_tool_use_id`). On each result the session id is stored
  (so `/clear`, which starts a new session, is recorded), the footer is built and the reply
  closed. The turn stays active until the reply is closed.
- A background task that finishes between turns sends its notification while the session is
  idle, then Claude Code starts a turn of its own to report it. That turn renders into the reply
  that started the task (D1: `ThreadSession._opening_target`, keyed by the task id through
  `ThreadSession._task_replies`), appended after that reply's own body with one line per task it
  reports, as the terminal prints it (`ThreadSession._ended_line`): a command's notification
  `summary`, which already reads `Background command "..." completed (exit code 0)`, or `Agent
  "<description>" finished` built from the task's `TaskStartedMessage`, since an agent's
  `summary` is its result; plus `usage.duration_ms` when the task reports it
  (`sessions.TASK_KINDS`, `SUMMARY_IS_END_LINE`). No new message follows for it; only when the
  reply it would render into is no longer tracked (a restart or an idle close dropped it) does
  the report get a reply of its own, as it always did. The next queued message waits for it to
  finish. `texts.BACKGROUND_NOTICE` opens it only when no task end was seen.
  When a message was already sent and waits for its turn, that turn comes first and Claude Code
  reports the task inside it, with no turn of its own (measured on Claude Code 2.1.280), so
  nothing waits.
  If no turn follows within 30 seconds, the queue moves on; a notification for a task no reply
  tracks is then posted on its own, and one for a task a reply still tracks closes that reply's
  own closing message instead, since nothing more is coming for it either. When a queued
  message and a notification cross, the result's `origin` tells whose turn it was, and the
  queue is put back in order; that one reply can carry the other's label.
- A task that outlives its turn keeps its line in the reply that started it: the session maps
  the task id to that reply, and every later task message for it updates that line only, never
  another reply. A background subagent's own calls (`parent_tool_use_id` pointing at a line of
  an ended turn) go under its line the same way and never open a reply. A notification for such
  a task still makes the next queued message wait for the turn Claude Code starts to report it.
- The thread's latest reply counts what is still running at the end of its footer, or of its
  status line while Claude writes: `⏳ 1 shell · 1 agent`, by each task's `task_type`
  (`sessions.TASK_KINDS`; a type not listed counts as a task). A new reply takes the counts
  over and the previous one drops them; they disappear when nothing runs.
- When the Claude Code process goes away (shutdown, an idle close, a process that exits), its
  tasks go with it: their lines close with `Stopped` and the list empties. The map lives in
  memory only.
- A thread's process closes on its own (D9) after `sessions.IDLE_CLOSE_SECONDS` (an hour) with
  nothing running, sent, taken or queued, and no approval, question or background task pending
  (`ThreadSession.idle`); the next message rebuilds the `ThreadSession` and reconnects it with
  `resume` set to the stored session id, silently, as any other reconnect does. The timer
  (`ThreadSession._idle_timer_check`) is armed or cancelled wherever that state could change, and
  always re-armed synchronously before a lookup is handed to a caller (`SessionManager.get`'s
  `touch()`), so it cannot fire in the gap between a lookup and the caller's own next `await` (a
  download, a slow Slack call).
- A stored effort level (`/effort`) does not survive Claude Code's own `--resume` by itself, unlike
  the model (`/model`), so the daemon stores it per thread and passes it back through
  `ClaudeAgentOptions(effort=...)` on every connect (`sessions.client_options`): it is read from
  `state.json` before each connect and reset to unknown until Claude Code reports a level again.
  `ThreadSession._finish` parses the level from a `!effort` turn's own output
  (`footer.effort_change`) and stores it; `"auto"`, the CLI's word for the default, is stored as
  unset.
- Shutting down, once the drain has ended, closes every session. A thread's own session also
  closes on its own: from D9's idle close, or at once when its stored session id can no longer be
  resumed (`SessionGone`). Either way, every reply still waiting in it (running, sent or queued)
  ends with `This reply ended before an answer:` and the reason. A closing session waits for a
  client that a daemon word (`!help`, `!bypass`, `!status`) is still starting, then closes it, and
  starts no other: the word gets `SessionClosed`, which tells the owner to send it again, and
  `!bypass` stores nothing. A session rebuilt to replace one still closing (D9's idle close, or a
  fresh lookup after a gone resume) waits for the old one to finish tearing down before its own
  first connect: the SDK's transport needs real time, up to about 20 seconds, to flush the old
  process after EOF, and resuming the same session id any sooner would race it.
- Logs carry channel and thread ids and exception type names, never prompt or reply text.

Bypass is a thread's own `ThreadState.bypass` in `state.json` (on, off or never chosen), which
`ThreadSession.bypass_choice` reads: an idle close and a restart of the daemon, whatever its
cause, keep it, and the next Claude Code process in that thread gets it back from
`ensure_connected`, through `set_permission_mode`: on sets `bypassPermissions`; an explicit off in
a folder whose own settings start in bypass sets `default`, so the folder's bypass does not return
silently; never chosen leaves Claude Code's own mode. This is needed since Claude Code's own `--resume` never restores `bypassPermissions` (sessions reference, read
2026-09-26). At the start of `SessionManager.drain`, every thread whose owner turned bypass on gets
`texts.BYPASS_RESTARTING` (a thread with no session id never ran: its next message asks the setup
again, so it is not told). A session `!resume` opens starts in its new thread with bypass never chosen (it follows the folder's own mode) and
no `/effort` level set, whatever the session had before: both belong to the thread, not to the
Claude Code session id, and `!resume` never touches or waits on any other thread.

## The session index

`code_with_slack.home.Home` publishes the owner's Home tab with `views.publish`, which takes no
scope and needs no event from the owner. It always publishes to the configured owner's user id.

`Home.publish` reads the bound channels (`StateStore.channels`) and asks Slack for each one's
name once per run (`conversations.info`); a channel Slack refuses is left out with its threads.
It then builds one row per thread of those channels that holds a session id
(`StateStore.threads`). The title comes from Claude Code: `sessions.directory_sessions` lists
each folder once, and a session it does not list yet shows `Session` and the start of its id.
The rest comes from Slack: `conversations.replies` with the root's `ts` and `limit=1` returns
the root alone, and `home.thread_facts` reads its `reply_count`, its `latest_reply` and, among
its `reactions`, the status one. A row is dated by the last reply, or by the root while it has
none, and the rows are ordered by that date. The status is the root's reaction name:
`ThreadState.ended` when it is set, `ThreadState.status` otherwise, and the reaction read from
the root for a thread that has neither (one that ended before the daemon kept it). `ended` and
`status` are both set only for an answer that never reached Slack, where the root shows ❌ and
`status` stays for crash repair. A root is read again only when its thread's session id, `status`
or `ended` changed since the last read, which every turn does at its start and at its end;
`thread_not_found` (the root was deleted) leaves the thread out for the rest of the run, and a
request that got no answer keeps what was read before. Each row also needs the root message's
permalink (`chat.getPermalink`, asked once per thread per run): a thread whose permalink Slack
refuses is left out for the rest of the run, and one whose request failed without an answer is
asked again at the next publish. `home.THREADS_AT_ONCE` threads are asked about at a time.

`home.home_view` lays the rows out. Under the controls, each channel is a group: a header with
the channel and a **New thread** link button (the `slack://channel` deep link), then two blocks
per session: a section with the title, and a context line with the status word, the number of
replies, the time of the last reply as Slack's own `{ago}` date token, which the client renders,
so the age stays right between two publishes, and an **Open** link to the thread. A context line
holding `home.SPACER` leaves a blank row between two sessions of a channel. With no filter chosen every bound channel is a group, the ones with
sessions first by their newest, each cut to `home.PER_CHANNEL` cards with a **Show all** button;
with a channel, a status or a search chosen (`HomeFilter.narrowed`), only the channels with a
match, uncut. The period applies either way and leaves that shape alone: a channel whose sessions
are all older keeps its header and says `texts.HOME_NO_MATCH`. A Home view holds 100 blocks: the page
stops before that and ends with `texts.HOME_MORE`.

The filters are a `home.HomeFilter` kept in memory: channel, status, period and a search on the
title. The period starts on the last 48 hours; the others start unset. The two blocks that hold
the controls take an id that follows the chosen filter: Slack keeps what a control shows for as
long as its block keeps its id, so a page built with another choice (after a restart, after
**Show all**) must change it for the controls to show that choice. Every use of a control reaches `slack_app` as a `block_actions`
payload, checked on its own for the owner and the workspace (a Home tab payload names no
channel). A filter's payload carries the state of all four controls in `view.state.values`,
which `home.read_filter` reads by action id and turns into the filter; a status or a date the page never offered keeps
the current one. **Show all** carries its channel, and `Home.publish` drops a chosen channel
that is not one of the page's own. `Home.choose` then publishes at once. The **New thread** link
button is followed by Slack itself and still sends its click, which is acknowledged and not read;
a session's **Open** is a link in text and sends nothing.

`StateStore.on_sessions_change` calls `Home.request` after a write that changed what the page
shows: a channel bound for the first time, a thread added or removed, a session id, a root's
reaction. A write that changes anything else (a reply's bookkeeping, a request, bypass, effort,
a rebind) does not. `Home.request` returns at once and publishes after `home.DEBOUNCE_SECONDS`,
so the burst of reaction changes one turn makes costs one publish; a change that lands during a
publish is followed by another. Publishes run one at a time, each built after the one before it
landed, so a filter just chosen is never replaced by an older page. On a stop, `Home.close` runs
after every session has closed and publishes what was still owed, giving up after
`home.CLOSE_SECONDS`.

`Home.publish` never raises. `not_enabled` (the Home tab is off in the Slack app's settings) is
logged once and ends the publishing for that run; any other failure is logged by its error code
and the next change tries again.

## Slack handlers

`code_with_slack.slack_app.build_app` registers one listener per inbound path: `message`
events, the Approve, Deny, Answer and Skip buttons, the setup's Model select and Start, the question form's Next and Submit, and the session index's controls (its filters and Show all, and its New thread link button, which is only acknowledged). The app registers no slash command. Each
acknowledges Slack first, then checks the owner, the workspace and the channel itself. A
failure after the checks reaches the owner as an ephemeral error line.
A link Slack made from a typed address (`<url|label>`, `<url>`) reaches Claude Code as typed; a
link the owner named reaches it as `label (url)`, so the address is not lost; a mention stays in Slack's form (`<@U…>`), since naming the user would need a
scope the app does not have.

A `message` event is routed by whether it is a reply in an existing thread: `slack_app.handle_message`
reads `sessions.get(channel, thread_ts)` for a reply (`None` for a thread that holds no session),
and always `None` for a top-level one (`thread_ts == ts`), even in a channel that is bound.
`code_with_slack.commands.parse_bang` reads a message starting with `!` (none for one
carrying files, which is always a prompt): `help`, `guide`, `bind`, `bypass`, `status`, `stop`
and `resume` are the daemon's own words (`commands.Word`), dispatched in `handle_word` by
whether the lookup above found a session: `!bind` and `!resume` work only at the top level,
refused inside a thread (`texts.WORD_IN_THREAD`); `!bypass` only inside a thread, refused at the
top level (`texts.BYPASS_TOP_LEVEL`); `!guide` answers the same either way; `!help`, `!status`
and `!stop` answer both, but with different content: `!help` lists only the daemon's words at the
top level and a session's own commands too inside its thread; `!status` lists the channel's live
sessions at the top level and one session's own values inside its thread; `!stop` stops every
session of the channel at the top level and one session inside its thread. `!clear` is not a word of its own: it is an
ordinary `Passthrough` that `slack_app.is_clear` catches only inside a thread, refused there
(`texts.CLEAR_IN_THREAD`, one thread is one session); at the top level it opens a new session
like any other message, and reaches Claude Code as `/clear` if the freshly connected session
offers that command. Any other `!name args` is a `Passthrough` too: sent as `/name args` when the
session (freshly opened, at the top level) offers `name`, as the text itself otherwise. A
top-level message with no existing thread opens a new session (`SessionManager.open`); a message
in a thread that holds no session and is not a daemon word gets `texts.NOT_A_SESSION`, with
nowhere to send it.

`!resume` stands in for Claude Code's interactive `/resume`, which an SDK session
does not offer: `code_with_slack.resume` lists the directory's sessions from the SDK's
`list_sessions` with the columns of the terminal's picker (name or title, time since the last
activity, git branch, size), the first 8 characters of the session id and a Resume button each, or matches
`!resume <id or name>`. The terminal's picker shows no id; the list shows its start because
`!resume` takes a full id or any start of one at least 8 characters long (`resume.ID_SHOWN`). The
list is posted in the channel, and each button carries the session id and the ts of the `!resume`
message (`resume.parse_resume_value`). A Resume click or a typed `!resume <id or name>`
(`slack_app.resume_into_thread`) opens the chosen session in the thread of that message
(`sessions.resume`), with a fresh thread entry: bypass never chosen (the folder's own mode) and no `/effort` level, whatever
the session had before. It is refused, with no `await` between the check and the `resume` call it
guards so nothing can change in between, when that thread already holds a session
(`texts.RESUME_HELD`: a resume is never a swap) or the channel was bound to another folder while
the chosen session was read from the old one (`texts.RESUME_GONE`); it never waits on, or
touches, any other thread of the channel.

The daemon's notices (the answer to `!bind`, `!bypass` and `!stop`, a word used in the wrong
place, a resume that did not happen or is refused, a refused attachment, a restart, and the
ephemeral errors) are a context block, small and grey as the footer, so they read apart from
Claude's replies: `slack_app.build_app`'s `notice`, `tell_owner`, and `ThreadSession._post`. The
same holds for the lines of the `!bind` and `!resume` lists; their rows keep a section, since a
context block holds no button. Their text is mrkdwn, and what comes from outside (a folder, a
file name, a typed target) is escaped with `render.escape.mrkdwn_escape`. `!help`, `!guide`,
`!status` and the answer to a resume stay a markdown block at full size: the first three are
read, and a resumed session's title keeps every character inside its bold only there, since
mrkdwn has no escape for `*`.
A shorter target is read only as a title.
The list holds the directory's own sessions, not other worktrees', as the terminal's picker
starts. A Resume click is checked like any other button, and the session must still be one of
the directory's.
`!bind` alone lists, through `code_with_slack.folders`, the folders where a session can start:
`ALLOWED_ROOT`, then its folders, then theirs, skipping hidden folders and symlinks and never
descending into a git repository (a `.git` directory or file). A folder is kept when
`code_with_slack.trust` accepts it. The checks run eight at a time and stop once one more than
the 20 rows shown is found, so the higher levels fill the rows, which are then shown in path
order. The trust record is parsed again only
when its mtime or size changes. A Bind click is checked like any other button, its folder goes
through the same `resolve_directory` check as a typed `!bind <folder>`, a click on the channel's
own folder changes nothing, and a click while any of the channel's threads is not idle is
refused, as a typed `!bind` is.
The `!resume` list reads the last message of a session's transcript only while that session can
still be among the 20 shown: a file's mtime bounds its last message from above.
Top-level `!status` (`slack_app.channel_status`) lists the channel's directory, then one line per
live session of the channel, each with a permalink to its thread (`slack_app.thread_link`), busy,
waiting for the owner or idle, its running tasks, its bypass and its folder when it differs from
the channel's; inside a thread `!status` is that session's own (`ThreadSession.status`, see
Footer above). Top-level `!stop` (`SessionManager.stop_channel`) and `!bind`'s busy check
(`SessionManager.sessions_of`) both read the channel's live sessions the same way.
`commands.help_text` lists the daemon's own words, then, only when it is called with a session's
commands (never at the top level, where a word never has one), those from
`get_server_info()["commands"]` at the time of asking, keeping only the lines that contain the
text after `!help` when there is one, so a command a new Claude Code release adds needs no change
here. Bolt's per-request authorization returns the
identity `auth.test` gave at startup, so no request costs an extra API call.
