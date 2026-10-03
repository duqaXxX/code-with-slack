# Features and how each is checked

Every feature the owner can see, and what verifies it: the test suite on every change, the
release probe (`uv run python -m probe`, described in `CONTRIBUTING.md`) on every new
`claude-agent-sdk`, and what nothing checks automatically. A new feature adds its row in the same
pull request. `tests/test_features.py` fails when a probe claim in `probe/claims.py` is missing
from the Probe column, or the column names a claim that does not exist. The probe prints the By
hand column at the end of every run, as the checklist of what it cannot see.

The suite replays recorded SDK streams and Slack payloads (`tests/fixtures/`), so it cannot see a
change in a new Claude Code CLI: the probe runs the real one. Neither drives the Slack clients, so
how a reply looks on desktop and on mobile is checked by hand.

| Feature | Test suite | Probe | By hand |
|---|---|---|---|
| Owner only: a message, button or form from anyone else does nothing | `test_guards`, `test_slack_app` | none | none |
| Channel guard: a private channel holding the owner and the bot only | `test_guards`, `test_slack_app` | none | none |
| One session per thread: a top-level message opens a new thread with its own session; a reply inside a thread continues that session | `test_sessions`, `test_state`, `test_slack_app` | none | Send two top-level messages in the same channel: each opens its own thread and its own session |
| `!bind` and the folder list, only folders Claude Code trusts; refused inside a session's thread; the answer names each existing thread's own folder when it differs from the new one (D5), and every prompt sent in such a thread after that gets the same notice, shown to you alone | `test_folders`, `test_trust`, `test_slack_app`, `test_state` | none | Bind a folder you trusted in the terminal the same day |
| A prompt gets one reply, a native Slack stream that starts with Claude's first content and grows as it is written; past 280 seconds, or once Slack refuses to grow the stream (`msg_too_long`), the same message goes on by edit; split past Slack's limits (12,000 characters, 50 cards) until the reply has ended, and after that never into a new message; two text blocks with no tool call between them (the inner turns of a `/goal`) are a paragraph apart | `test_sessions`, `test_sessions_stream`, `test_sinks`, `test_renderer` | P1, P3 | A reply that runs past 5 minutes keeps growing in the same message and ends with a new message that holds what Claude wrote after its last call and the footer, its push reading the start of that text; an older reply keeps its footer when a newer one ends; how a long reply reads on desktop and on mobile |
| A run of tool calls (the calls between two pieces of text) is two task cards: the first counts what ended in the terminal's words (`Ran 2 shell commands · Read 1 file · ✗ Ran 1 shell command`), the second shows the call running now (of calls running together, the last one started), or the last one that ended; a single call is one card. When the reply ends, a silent edit turns each run into one line (`✓ Ran 2 shell commands · Read 1 file`). An Edit or a Write that ended well, a subagent, a background task and a stopped call keep a card of their own | `test_fold`, `test_sinks`, `test_previews`, `test_sessions_stream` | P10 | Ask for three shell commands in a row: the first card turns into the counts when the second command starts, the second card names the command that runs; when the reply ends the cards give way to one line, with no second notification |
| Edit and Write previews under the tool's card: sentence, diff or new file's lines | `test_previews`, `test_sinks` | P13 | A diff opens and closes on desktop and mobile; it colours on desktop, the squares on mobile; the container's title is the sentence and does not repeat the card's path |
| Notifications: one when a reply ends (its stream stops), a second for a reply that runs past 280 seconds or whose stream Slack refused to grow, one per approval or question, none while Claude writes | `test_sinks`, `test_sessions`, `test_sessions_stream` | none | In a thread you started, away from Slack: a reply rings once, a reply past 5 minutes rings twice, `!stop` rings once |
| Status reaction: one on each session's root message, ⏳ working, ✋ waiting for you, ✅ ended or stopped with `!stop`, ❌ error or a restart that cut it short | `test_status`, `test_sessions` | none | The reaction moves ⏳ to ✅ in the channel list; after `!stop` it shows ✅, not ❌ |
| Thread status: Slack's status line under the thread's last message reads `Working…` from the moment a prompt is received until its turn ends, and while a turn Claude Code starts to report a task runs; once the turn has ended and its reply is kept open for a task, it reads what still runs, `1 shell still running`, and follows the count until nothing runs (a reply that has ended says it in its footer instead, `⏳ 1 shell`); it goes while an approval or a question waits for you, on `!stop`, on an error and when the session closes; it never notifies | `test_status`, `test_sessions_stream`, `test_sinks` | none | Send a message in a session thread, on desktop and on iOS (on iOS with desktop closed, or with the thread opened there first: iOS can show no line for a thread desktop already has open, see `docs/setup.md`): `Working…` shows under it within a second, stays through a silent stretch of the turn (a long command, a subagent) and goes when the reply ends. Ask for `sleep 200` and `sleep 330` in the background and an answer at once: the status reads `2 shells still running`, then `1 shell still running` with no line left in the reply, and goes when the footer shows. Repeat on an app installed from the manifest, whose token holds `chat:write` and no `assistant:write` |
| Approvals: Approve and Deny buttons for a tool call, each resolved only in its own thread | `test_approvals`, `test_sessions`, `test_slack_app` | P11 | none |
| Questions: the Answer form, which shows every option whole (full description and preview above the choice) when one says more than the 75 characters a Slack option holds, and the answers kept in the reply where the question was asked (`User answered Claude's questions:` on the call's card, a line per answer under it) | `test_approvals`, `test_slack_app`, `test_previews`, `test_sinks`, `test_sessions` | none | Answer a real question in a turn that goes on working: the request message goes, the answers show in the reply under the call's card, and what Claude does next shows below them, on desktop and on iOS. Answer a question whose options have long descriptions and a preview: each description reads whole above the choice, the preview keeps its lines, on desktop and on iOS |
| `!bypass`, kept per thread in `state.json` across restarts; refused at the top level, and in a thread before its setup's Start, which sets it | `test_sessions`, `test_state`, `test_slack_app` | P9 | none |
| `!stop`: at the top level, every session of the channel and its background tasks, answered by one post in the channel; inside a thread, that session alone: its reply ends like any other (the stream stops with the footer and the stopped command's card) and ✅ shows on its root; the answer is a line that stays in the thread, `Stopped.`, or `Nothing is running in this session.` when nothing runs | `test_sessions`, `test_sessions_stream`, `test_slack_app` | P8, P12 | Send `!stop` in a busy thread: the reply ends with its footer, the root shows ✅ and `Stopped.` follows in the thread; send `!stop` again: `Nothing is running in this session.` follows, and both lines are still there after a reload |
| `!status`: at the top level, the channel's folder and every live session, each linked to its thread; inside a thread, that session's directory, mode and the footer's values (branch and changes of the folder the session works in, model, context, tokens, usage limits and their resets) | `test_footer`, `test_sessions`, `test_slack_app` | P2, P14 | none |
| `!resume`: the session list, posted in the channel, and resuming one in the thread of the `!resume` message (a click or a name); each button carries the session id and that thread, so a list posted by an older version is refused as out of date; the list is deleted once the confirmation is in the thread, and rewritten to say what was resumed and where when the confirmation or the delete fails; a session already open in a thread of any channel takes no row, so the twenty rows are sessions that can be resumed, and one line under the list counts the open ones (`3 more are open in their own threads.`); such a session is refused with a link to its thread if named, or clicked on an older list (D6); a thread that already holds a session, live or only stored in `state.json`, refuses a resume | `test_resume`, `test_sessions`, `test_slack_app` | P6, P7 | Click Resume on a list: the session continues in the thread of your `!resume` message and the list is gone from the channel, with no notification for its removal; send `!resume` again: the session just resumed has no row and is counted in the line under the list |
| Where a word answers: typed in the channel (or in a thread that is not a session), a normal post in the channel, which stays after a reload; typed inside a session's thread, a message only you see under your word (`!help`, `!guide`, `!status`, `!bypass`, a refusal), with a ✅ on your word too for `!bypass`; a failure that cannot be reported in the channel is logged, never pushed as a thread reply | `test_slack_app` | none | Send `!status` at the top level and inside a thread: the first stays after a reload, the second shows `Only visible to you` and vanishes |
| The old-folder notice (D5) and `Not sent.` (a D8 hold cancelled by Cancel, `!stop` or a restart) are shown only to you, under your message | `test_slack_app`, `test_sessions` | none | none |
| Session setup: a top-level message that opens a session waits for a Model select, an Effort select and a Bypass checkbox in its thread; Start applies them and sends the message; `!stop` and a restart cancel the wait with `Not sent.`, also while Start is applied; a reply in a thread where nothing was sent asks it again from the defaults | `test_setup`, `test_slack_app` | P17, P18 | Send a message at the top level: the thread offers the CLI's models; change the model and watch the efforts change; pick Opus, high, Bypass and press Start: the summary line shows them and the reply's footer agrees. Live checks on 2026-09-30 (Claude Code 2.1.285, slack-bolt 1.30.0) covered: setup shown, Haiku and Sonnet+high+bypass Starts, bypass approvals on and off, a restart keeping model, effort and bypass, `!bypass off` surviving a restart |
| D8: two busy sessions in one folder; a message to an idle session holds and asks `Send anyway?` with Continue and Cancel while another live session (any channel) is not idle in the same resolved folder; `!stop` and a restart cancel the wait | `test_sessions`, `test_slack_app` | none | Bind two channels to the same folder, keep one busy, send a message in the other: it holds and asks |
| Idle close and resume: a thread's Claude Code process closes after an hour with nothing to do and resumes on the next message; `!resume` moves a folder's session into the thread of the `!resume` message | `test_sessions`, `test_resume`, `test_slack_app`, `test_state` | none | none |
| Effort kept across a resume: `/effort` set in a thread survives an idle close or a restart | `test_sessions` | P15, P16 | none |
| `!help` at the top level lists the daemon's words and points to a thread for Claude Code's own; inside a thread it lists both, without `!clear`, which a thread refuses under each of its names (`!reset`, `!new`); `!guide` and `!name` sent to Claude Code as `/name` work the same either way | `test_commands`, `test_slack_app` | none | none |
| Attached images and files | `test_attachments`, `test_slack_app` | P4, P5 | none |
| Background commands and subagents: their cards, the running count, Claude Code's report; the reply's stream stays open until every task it started has ended and been reported; a long command a subagent runs and that ends with it shows only on the subagent's card (its call count), and neither holds a message you send meanwhile nor adds a line to the report; one the subagent leaves running in the background keeps a line, a `1 shell` count and a `!stop` of its own | `test_renderer`, `test_sinks`, `test_sessions`, `test_sessions_stream` | none | Run a subagent and a background command; each card completes when its task ends and the reply ends after the last one. Ask a background subagent to run `sleep 12` twice and send a message while it works: no card of the commands appears, the message is answered at once, and the report opens with the agent's line alone. Ask it to start `sleep 20` in the background and end: the command shows as `1 shell` and the reply stays open until it ends |
| A restart lets running turns finish, for up to 29 minutes after `launchctl kill TERM` (`DRAIN_LIMIT_SECONDS`); a turn waiting on an approval or a question counts as running, so the request stays open and holds the restart until the owner answers it, `!stop` in its thread ends it, or the limit denies it; the restart also prunes threads whose session is gone, and says which background tasks hold it in the thread's status line (`Restart waits for 1 shell · !stop ends it now`), with no message and no push; a message refused meanwhile (`code-with-slack is restarting; send this again in a moment.`) lists under it each thread the restart still waits for, of any channel, as its channel, a link labelled with the session's title and what holds it there (`a turn is running`, `an approval or a question is waiting for you`, `2 shells running`, `a background task is about to report, within 30 seconds`), eight rows at most and a count of the rest, then `` `!stop` in a thread ends the wait there. `` unless every listed thread only waits for a report, which ends by itself; `!status` typed in a channel adds, in a message of its own, the rows of that channel's threads and a count of those in other channels, which it does not name (issue #119); a background task started after the signal by a session whose turn was running then (the one that sent it is among them) is not waited for (issue #87), and when the shutdown ends only such a task its root shows ✅; a reply the restart cuts short ends like any other (stream stopped, ❌ on the root), and the messages queued in that thread get no reply of their own: the running reply's end says `N messages were not sent because code-with-slack restarted: send them again.` with the start of each, or one message in the thread says it when nothing runs; closing a session also deletes any approval, question or D8 hold message it denies (issue #19), not just `!stop`'s own | `test_sessions`, `test_sessions_stream`, `test_slack_app`, `test_state`, `test_main`, `test_approvals` | none | `launchctl kill TERM` with a turn running in one thread, then a message in another: the refusal names the first thread with a link that opens it, and `!status` in the channel lists it too; `launchctl kill TERM` with an approval pending: the daemon keeps running and the approval stays open; once it is answered, or `!stop` is typed in its thread, the turn ends, the approval's buttons are gone, and the daemon reconnects; a session that sends `launchctl kill TERM` and then waits in the background for the new process no longer holds the restart |
| A crashed daemon's open replies, requests and root reactions are repaired on the next start (issue #19): each open reply's stream is stopped (Slack having closed it already is fine), then the message is read back by its own ts and edited (its blocks kept, every card left running closed as an error, `code-with-slack stopped before this answer.` appended, nothing posted), each stale request is deleted, and a root left ⏳ or ✋ gets ❌ | `test_repair`, `test_sinks`, `test_sessions`, `test_slack_app`, `test_state`, `test_main` | none | `kill -9` the daemon mid-turn with an approval pending; on the next start the reply's stream is stopped, the reply ends with "code-with-slack stopped before this answer.", a card left running shows as an error, the approval's buttons are gone, and the root shows ❌ |
| A logged-out Claude Code gets the login instructions | `test_renderer` | none | Log out in the terminal, send a message |
| `!login` and `!logout` are never sent to Claude Code: each answers where it is done, on the host, as a post in the channel or as a message only you see inside a session's thread | `test_slack_app` | none | Send `!login` in the channel and inside a thread |
| Configuration: `.env` private, every missing variable named | `test_config` | none | none |
| Session index: the app's Home tab lists the sessions the threads hold, grouped by channel, newest first, five per channel with Show all; each session shows the root's status reaction and its title, then the status in a word, the number of replies, the time of the last reply and an Open link; New thread beside a channel opens it; four filters (channel, status, period, a search on titles) that add up, the period starting on the last 48 hours; a deleted thread or channel is left out; rewritten when a session starts, ends or changes status, never with a notification | `test_home`, `test_state`, `test_main`, `test_slack_app` | none | Open the app's Home tab on desktop and on iOS: the thread used last is the first of the first channel, Open opens it and New thread opens the channel; each filter narrows the page and All brings it back; starred, the app opens on the Home tab from the sidebar; the age reads right after the page sat for an hour |
| Cleanup of `state.json`, on start and every 6 hours: a channel Slack answers `channel_not_found` about is forgotten with its threads, a thread whose session is gone is dropped; nothing is removed on an error, under a live session, or when Slack finds none of the bound channels | `test_cleanup`, `test_state`, `test_main` | none | Delete a test channel that is bound, restart: the log says `forgot channel`, and the channel is gone from the Home tab's menu |
| One instance at a time | `test_lock`, `test_main` | none | none |
| The LaunchAgent and the Slack app installed from `docs/setup.md` | `test_main` (the manifest) | none | Follow `docs/setup.md` on a new machine |

## Notifications

Every reply, approval request and question is a message inside the session's own thread. The
answer to a word typed in the channel (`!help`, `!guide`, `!status`, `!stop`, `!bind` and its list,
`!resume` and its list, and each refusal), and the upgrade notice, are top-level messages of their
own. A word typed inside a session's thread is answered by an ephemeral message under it (Slack
labels it `Only visible to you`), which disappears when Slack reloads, or by a reaction; `!stop`
is the exception, answered by a message that stays in the thread and can notify. The
old-folder notice and `Not sent.` are ephemeral too. Ephemeral messages did not ring an iPhone
in the two probes of 2026-09-29 that watched for it; one earlier reading did ring and stays
unexplained.

**Inside a thread**, Slack notifies the owner on a new message in a thread it started, mention or
none. Measured 2026-09-28 (Slack free plan, Slack iOS app, slack-sdk 3.44.1):

- a new bot message in a thread the owner started rang, with or without a mention;
- the same message in a thread the owner did not start stayed silent;
- a `chat.update` rewrite never rang;
- a message posted about one second after the owner's own was sometimes not pushed; at 15
  seconds it always was;
- `@channel` does not notify inside a thread (Slack help center, read 2026-09-28).

A reply is a native Slack stream, and a stream notifies in its own way. Measured 2026-09-29 (iPhone
locked, Slack open in a browser, channel on Just mentions, slack-sdk 3.44.1):

- a stream in a thread the owner started pushed once, when it was stopped, with the stream's first
  text as the banner; nothing pushed when it started or while it grew (`mixed-slow` probe: one
  push, reading the reply's first words);
- Slack closes a stream 5 minutes after `chat.startStream`, and an unstopped one pushed at that
  moment;
- after `chat.stopStream`, a `chat.update` of the same message never pushed (`long-one` probe: stop
  at 4 minutes 51 seconds, ten updates, silent), and a new message posted in the thread then did.

So, inside a thread:

- A reply rings once, when its stream stops at the reply's end. The end comes once the turn has
  ended and none of its tasks still runs or still waits on a turn Claude Code starts to report it
  (D1): a reply that starts a background command or an agent stays open, showing that task's
  card, until the task and its report are done. The banner is the start of Claude's answer.
- A reply still open 280 seconds after it started stops its stream then (Slack would close it at
  300 seconds), which rings, and goes on in the same message by `chat.update`, silently. Its end
  posts the text Claude wrote after its last call and the footer as a new message, which rings
  a second time with the start of that text, and takes the text out of the first message. An
  answer that is text alone keeps it, and the new message is the footer alone.
- A reply whose stream Slack refuses to grow (`msg_too_long`, which the text of many cards can
  reach below the limits the daemon counts) stops its stream at that moment, which rings, and
  goes on the same way: by `chat.update`, with its ending in a new message. If Slack refuses
  that edit too and no later one passes, the reply is short of what Claude wrote and the root
  shows ❌.
- A reply longer than one message (12,000 characters or 50 cards) continues in a new message, and
  each extra message rings. Once the reply's end has landed (its stream stopped with the footer, its
  ending or its closing message posted), its messages are a fixed set: a late update (a preview,
  a card or words that arrive after the footer) only edits them, never opens a new one. Late
  content is all or nothing: if the last message holds it with everything it showed, it is shown;
  if not, the message keeps what it showed when the end landed and none of the late content
  appears, not even the part that would fit. A late preview of a card that was shown is replaced
  by the note `Preview left out: it did not fit this message.` when a block is free for it; a
  late card or late words that do not fit are left out without one.
- An approval request and a question (`AskUserQuestion`) ring.
- A reply that fails outright (a directory gone missing, untrusted or unreadable, a session
  Claude Code no longer has, or any other exception) ends like any other, ringing once as its
  stream stops; when the whole process exits instead, each open reply's stream is stopped on the
  way out.
- `!stop` and a restart end the reply the same way: the stream stops with the footer and rings
  once. The messages a restart dropped from the thread's queue get no reply of their own: they are
  named in the end of the running reply, or in a single message when nothing was running.
- Nothing rings while Claude writes, and a card updating inside the stream does not ring.

**At the top level**, a message rings only under the channel's own notification setting; with
**Just mentions** ([setup](setup.md#create-a-channel-per-project)) it needs a `@channel` mention,
which the daemon writes nowhere. Slack lets a user choose notifications per channel, never per
message, and its reference says nothing about which bot messages notify. Eleven probe messages
sent on 2026-09-27 to an iPhone (Slack iOS app, channel on Just mentions) showed: only a new
message rings, a `chat.update` never does; a mention in the text alone mostly does not; `@here`
reaches only the members Slack counts as active, so it misses an owner who is away.

What is not covered: a second ringing message from the same channel within about a minute of a
first one was silent in most 2026-09-27 probes, which Slack does not document; Android and the
desktop client were not tested.
