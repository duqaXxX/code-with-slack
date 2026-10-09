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
| One session per thread: a top-level message opens a thread and its session; a reply continues it; a reply in a thread that holds no session is refused for the owner alone and starts nothing | `test_sessions`, `test_state`, `test_slack_app`, `test_guards` | none | Send two top-level messages in the same channel: each opens its own thread and its own session. Not run yet: reply in a session's thread with **Also send to #channel** ticked, and the session answers in the thread; the same with a file attached |
| `!bind` and the folder list: only folders Claude Code trusts; refused inside a session's thread; the old-folder notice when an existing thread keeps another folder | `test_folders`, `test_trust`, `test_slack_app`, `test_state` | none | Bind a folder you trusted in the terminal the same day |
| A prompt gets one reply: a native Slack stream that grows as Claude writes, goes on by edit past 280 seconds or when Slack refuses to grow it, and splits past Slack's limits | `test_sessions`, `test_sessions_stream`, `test_sinks`, `test_renderer` | P1, P3, P20 | A reply that runs past 5 minutes keeps growing in the same message and ends with a new message that holds what Claude wrote after its last call and the footer, its push reading the start of that text; an older reply keeps its footer when a newer one ends; how a long reply reads on desktop and on mobile; not run yet: send `!goal <condition>` and check, on desktop and on iOS, that the reply opens with `Goal set:` and the condition and that no text shows twice; not run yet: open, on desktop and on iOS, a reply that holds several large collapsed diffs in one message (past 280 seconds) and check that each diff opens; the card of a subagent that made 15 calls in a streamed reply, opened once the reply had ended, shows each call once, a line each (checked 2026-10-08 on desktop and on iOS); not run yet: open that card while the reply is still streaming; an answer of 40 headings with a sentence each continues in a second message that opens on a heading, with no text missing (checked 2026-10-08 on desktop and on iOS, and in the messages read back from Slack: 44 and 38 blocks, the cut between the 22nd section and the 23rd heading) |
| A run of tool calls is two task cards: counts of what ended in the terminal's words, and the call running now; when the reply ends, each run folds into one line | `test_fold`, `test_sinks`, `test_previews`, `test_sessions_stream` | P10 | Ask for three shell commands in a row: the first card turns into the counts when the second command starts, the second card names the command that runs; when the reply ends the cards give way to one line, with no second notification |
| An Edit or a Write shows once it has ended: one collapsed container titled with the call's line (`Update(notes.txt)`), the sentence under it, the diff or the new file's first lines inside, and no card. One that failed is a card with the reason | `test_previews`, `test_sinks`, `test_renderer`, `test_fold` | P13 | Ask for two edits and a new file: each is one row, its title in code style, with no `Update(...)` card above it; a diff opens and closes on desktop and mobile; it colours on desktop, the squares on mobile; the subtitle shows while the container is closed, on desktop and mobile; an edit that fails shows as a card with the reason |
| Notifications: one when a reply ends (its stream stops), a second for a reply that runs past 280 seconds or whose stream Slack refused to grow, one per approval or question, none while Claude writes | `test_sinks`, `test_sessions`, `test_sessions_stream` | none | In a thread you started, away from Slack: a reply rings once, a reply past 5 minutes rings twice, `!stop` typed in the thread rings once, with `Stopped.`, and `!stop` typed in the channel does not ring; kill the Claude Code process more than 5 minutes into a turn whose last part is a tool call: the reply rings a second time, with `Claude Code reported an error`, and that line is the new message |
| Status reaction: one on each session's root message, ⏳ working, ✋ waiting for you, ✅ ended or stopped with `!stop`, ❌ error or a restart that cut it short | `test_status`, `test_sessions` | none | The reaction moves ⏳ to ✅ in the channel list; after `!stop` it shows ✅, not ❌ |
| Thread status: Slack's status line under the thread's last message reads `Working…` while a turn runs, `Compacting conversation…` while Claude Code compacts, and `1 shell still running` while a task outlives the turn | `test_status`, `test_sessions_stream`, `test_sessions_compaction`, `test_sinks` | P22 | Send a message in a session thread, on desktop and on iOS (on iOS with desktop closed, or with the thread opened there first: iOS can show no line for a thread desktop already has open, see `docs/setup.md`): `Working…` shows under it within a second, stays through a silent stretch of the turn (a long command, a subagent) and goes when the reply ends. Ask for `sleep 200` and `sleep 330` in the background and an answer at once: the status reads `2 shells still running`, then `1 shell still running` with no line left in the reply, and goes when the footer shows. Repeat on an app installed from the manifest, whose token holds `chat:write` and no `assistant:write` |
| Approvals: Approve and Deny buttons for a tool call, each resolved only in its own thread | `test_approvals`, `test_sessions`, `test_slack_app` | P11 | none |
| Questions: the Answer form shows every option whole when one says more than Slack's 75 characters, and the answers stay in the reply under the call's card | `test_approvals`, `test_slack_app`, `test_previews`, `test_sinks`, `test_sessions` | none | Answer a real question in a turn that goes on working: the request message goes, the answers show in the reply under the call's card, and what Claude does next shows below them, on desktop and on iOS. Answer a question whose options have long descriptions and a preview: each description reads whole above the choice, the preview keeps its lines, on desktop and on iOS |
| `!bypass`, kept per thread in `state.json` across restarts; refused at the top level, and in a thread before its setup's Start, which sets it | `test_sessions`, `test_state`, `test_slack_app` | P9 | none |
| A thread with bypass off runs in the permission mode the owner's Claude Code settings give it; `!status` names it on its `Mode:` line and `!bypass off` returns to it | `test_sessions`, `test_slack_app` | none | With `permissions.defaultMode` set to `auto` in `~/.claude/settings.json`: `!status` in a thread says `Mode: auto`; after `!bypass on` then `!bypass off` it says so again |
| `!stop`: at the top level every session of the channel, inside a thread that session alone; its reply ends like any other and the answer stays in the thread | `test_sessions`, `test_sessions_stream`, `test_slack_app` | P8, P12 | Send `!stop` in a busy thread: the reply ends with its footer, the root shows ✅ and `Stopped.` follows in the thread; send `!stop` again: `Nothing is running in this session.` follows, and both lines are still there after a reload |
| `!status`: at the top level the channel's folder and each live session; inside a thread that session's directory, id, `Terminal:` command, mode and the footer's values | `test_footer`, `test_footer_git`, `test_sessions`, `test_slack_app` | P2, P14 | Once a turn has ended, send `!status` in the thread, run its `Terminal:` command, leave the terminal, then run `claude --resume` in that folder: the picker lists the fork. With two idle threads in a channel, send `!status` there: each row is named by its session's title and says how long ago it was last active |
| `!resume`: the session list posted in the channel, and resuming one in the thread of the `!resume` message by click or name; a session already open in a thread has no row and is refused with a link to it | `test_resume`, `test_sessions`, `test_slack_app` | P6, P7 | Click Resume on a list: the session continues in the thread of your `!resume` message and the list is gone from the channel, with no notification for its removal; send `!resume` again: the session just resumed has no row and is counted in the line under the list |
| `!open`: a `Choose a file` button opens a modal with a search field and up to 10 rows; `Open` shares the chosen file into the thread for Slack's file viewer | `test_openfile`, `test_slack_app`, `test_commands` | none | After reinstalling the app with `files:write`: `!open` in a thread, click `Choose a file`, then type fast (the rows must follow the last character, never an older one, and the field must keep what was typed through every update); pick a row and click `Open`: a `.md` file opens in the viewer with the thread beside it and the modal closes; click `Open` with no row chosen: the error shows and the modal stays; a name of 80 characters or more shows shortened in its middle, a folder of 100 characters or more from the left; in a folder that is no repository the search lists its files, and in one that holds a repository a level down the changed rows name `repo/path` and the search leaves out an ignored `.venv` of that repository; `!open setup` with several matches posts the count and a button whose modal holds `setup`; without the scope the line names `files:write`; restart the daemon after Claude changed files in a thread, then `!open` there: the same files are listed; in a thread whose folder Claude made a repository in, the new repository's files are all listed; type a name that matches nothing, create such a file in the terminal and type the name again: it is found; choose a row, type until the rows change, and click `Open` without choosing again: `Choose a file first.` shows (not measured on Slack: that Slack keeps the state of a radio group across an update, and how `plain_text` with `emoji` false shows `:tada:`); files named `__init__.py`, `*a*.md`, `~b~.txt` and `x:tada:.md` show their names as written, with no bold, strike or emoji; `!open` of an empty file says it is empty and uploads nothing; click `Choose a file` in a channel with a slow reply and a large repository: the modal opens, empty of rows if need be, and is filled; in a repository run `git status` in the terminal while a search is typing: no `index.lock` is left |
| Where a word answers: in the channel a normal post that stays; inside a session's thread a message only you see; `!bypass` adds a ✅ on your word | `test_slack_app` | none | Send `!status` at the top level and inside a thread: the first stays after a reload, the second shows `Only visible to you` and vanishes |
| The old-folder notice and `Not sent.` (a same-folder hold cancelled by `Don't send`, `!stop` or a restart) are shown only to you, under your message | `test_slack_app`, `test_sessions` | none | none |
| Session setup: a top-level message that opens a session waits for a Model select, an Effort select and a Bypass checkbox in its thread; Start applies them and sends the message | `test_setup`, `test_slack_app` | P17, P18 | Send a message at the top level: the thread offers the CLI's models; change the model and watch the efforts change; pick Opus, high, Bypass and press Start: the summary line shows them and the reply's footer agrees. Live checks on 2026-09-30 (Claude Code 2.1.285, slack-bolt 1.30.0) covered: setup shown, Haiku and Sonnet+high+bypass Starts, bypass approvals on and off, a restart keeping model, effort and bypass, `!bypass off` surviving a restart |
| Same-folder hold: a message to an idle session asks `Send anyway?` while another live session in the same folder is working; `!stop` and a restart cancel the wait | `test_sessions`, `test_slack_app` | none | Bind two channels to the same folder, keep one busy, send a message in the other: it holds and asks |
| Idle close and resume: a thread's Claude Code process closes after an hour with nothing to do and resumes on the next message; `!resume` moves a folder's session into the thread of the `!resume` message | `test_sessions`, `test_resume`, `test_slack_app`, `test_state` | none | none |
| Chrome: a session has Claude Code's browser tools when `/chrome` is set to Enabled by default in the terminal; each browser action asks in the thread as any tool does | `test_chrome`, `test_sessions` | none | not run yet: with `/chrome` on Enabled by default and Chrome open, ask in a new thread to open a page and read its title: an approval names the navigation, and after Approve the tab opens in your Chrome and the reply gives the title. Turn it off in `/chrome` and ask the same in another new thread: Claude has no browser tool |
| Audio clip: a clip recorded in Slack is sent to Claude as the text of the transcript Slack writes when you choose Generate transcript on it | `test_voice`, `test_slack_app` | none | Send a clip in a bound channel: a line for you alone says it waits for the transcript; choose Generate transcript: a thread opens under the clip and Claude answers what you said. Not run yet: send a clip and wait 5 minutes without the transcript: a line says nothing was sent |
| A command of Claude Code typed as `!name` is passed through as typed; the ones Claude Code keeps for its terminal answer that they are not available, and `docs/limits.md` lists them | `test_commands`, `test_slack_app`, `test_docs` | P23 | none |
| Effort kept across a resume: `/effort` set in a thread survives an idle close or a restart | `test_sessions` | P15, P16 | none |
| `!help` lists the daemon's words at the top level and both sets inside a thread, where `!clear` is refused; a `!word` is read whatever its formatting | `test_commands`, `test_slack_app` | none | not run yet: paste `!status` in inline code and in a code block, on desktop and on iOS: each answers as the word; send `\!status`: it goes to Claude as text |
| A compaction shows as one line of the reply, `Compacted the conversation: 20.7k → 5.0k tokens.`, on `!compact` and when Claude Code compacts on its own, before a turn's first words or among its tool calls | `test_renderer`, `test_sessions_compaction` | P21 | not run yet: in a thread with a few turns send `!compact`, on desktop and on iOS: the line under the thread reads `Compacting conversation…` while it runs (about 20 seconds in the recordings) and the reply is the line alone, with the footer |
| Attached images and files | `test_attachments`, `test_slack_app` | P4, P5 | none |
| Background commands and subagents: their cards, the running count and Claude Code's report; the reply's stream stays open until every task it started has ended and been reported | `test_renderer`, `test_sinks`, `test_sessions`, `test_sessions_stream`, `test_sessions_report_turn`, `test_sessions_prompt_replay` | P19 | Run a subagent and a background command; each card completes when its task ends and the reply ends after the last one. Ask a background subagent to run `sleep 12` twice and send a message while it works: no card of the commands appears, the message is answered at once, and the report opens with the agent's line alone. Ask it to start `sleep 20` in the background and end: the command shows as `1 shell` and the reply stays open until it ends. Ask it to start `sleep 20` in the background and end, then while the report runs a foreground command send a message: if Claude Code takes it into the report, the reply ends with the note and the session goes idle (`!status`, a restart); if it answers the message in a turn of its own, there is no note (not measured on Slack: which of the two a given timing gives) |
| A restart lets running turns finish (up to 29 minutes after `launchctl kill TERM`), keeps approvals open, and refuses new messages with a list of the threads it still waits for | `test_sessions`, `test_sessions_stream`, `test_sessions_report_turn`, `test_slack_app`, `test_state`, `test_main`, `test_approvals` | none | `launchctl kill TERM` with a turn running in one thread, then a message in another: the refusal names the first thread with a link that opens it, and `!status` in the channel lists it too; `launchctl kill TERM` with an approval pending: the daemon keeps running and the approval stays open; once it is answered, or `!stop` is typed in its thread, the turn ends, the approval's buttons are gone, and the daemon reconnects; a session that sends `launchctl kill TERM` and then waits in the background for the new process no longer holds the restart |
| Repair after a crash: the open replies, requests and root reactions a crashed daemon left behind are repaired on the next start | `test_repair`, `test_sinks`, `test_sessions`, `test_slack_app`, `test_state`, `test_main` | none | `kill -9` the daemon mid-turn with an approval pending; on the next start the reply's stream is stopped, the reply ends with "code-with-slack stopped before this answer.", a card left running shows as an error, the approval's buttons are gone, and the root shows ❌ |
| A logged-out Claude Code gets the login instructions | `test_renderer` | none | Log out in the terminal, send a message |
| API errors: a reply Claude Code wrote itself about a failure shows its text; a subagent's failed request shows on its card and stays out of the reply's text | `test_renderer`, `test_sessions` | none | Not reproducible on demand against the real API. When a turn ends on an API error, the thread shows the same sentence as the terminal, once Claude Code has given up retrying |
| `!login` and `!logout` are never sent to Claude Code: each answers where it is done, on the host, as a post in the channel or as a message only you see inside a session's thread | `test_slack_app` | none | Send `!login` in the channel and inside a thread |
| Configuration: `.env` private, every missing variable named | `test_config` | none | none |
| Session index: the app's Home tab lists sessions by channel, newest first, under four filters; each shows its status, replies, time of the last reply and an Open link; never with a notification | `test_home`, `test_state`, `test_main`, `test_slack_app` | none | Open the app's Home tab on desktop and on iOS: the thread used last is the first of the first channel, Open opens it on its last reply, in a thread long enough to scroll, and New thread opens the channel; each filter narrows the page and All brings it back; starred, the app opens on the Home tab from the sidebar; the age reads right after the page sat for an hour |
| Deleting a thread from the Home tab, with the optional `SLACK_USER_TOKEN`: Edit mode shows a Delete button on sessions that are not busy; a confirmed delete removes every message of the thread and forgets it | `test_delete`, `test_home`, `test_slack_app`, `test_sessions`, `test_config`, `test_main` | none | With the token set, open the Home tab on desktop and on iOS: press Edit, then Delete on an ended session; the dialog names it; confirm: the session's line reads `deleting…` at once, and within a few minutes the thread, your messages included, is gone from the channel and from the page, and `!resume` lists the session again. While a long thread is being deleted, send a message in it: you are told the thread holds no session, and that message is gone with the thread. Try it on a session that is working: it has no Delete. Press Edit, then Show all on a channel, then Done: every channel is listed again. Remove the token and restart: no Edit button, and the header line is small again |
| Cleaning up a channel from the Home tab, with the optional `SLACK_USER_TOKEN`: in Edit mode a Clean up button deletes the messages outside threads that have no reply, yours and the bot's | `test_delete`, `test_home`, `test_slack_app` | none | Type `!stop` twice in a channel outside a thread, and leave there a message with no reply. Open the Home tab, press Edit, then Clean up beside that channel: the dialog says what goes; confirm: the channel's name reads `cleaning up…`, and within a few minutes the two `!stop`, the bot's answers and the message with no reply are gone while every thread with a reply is still there. Check on desktop and on iOS |
| Cleanup of `state.json` on start and every 6 hours: a channel Slack no longer has is forgotten with its threads, and a thread whose session is gone is dropped | `test_cleanup`, `test_state`, `test_main` | none | Delete a test channel that is bound, restart: the log says `forgot channel`, and the channel is gone from the Home tab's menu |
| One instance at a time | `test_lock`, `test_main` | none | none |
| The LaunchAgent and the Slack app installed from `docs/setup.md` | `test_main` (the manifest) | none | Follow `docs/setup.md` on a new machine |

## Details

Facts about a feature that neither `setup.md` nor `architecture.md` states, kept here so the table stays short.

### A prompt gets one reply

- Two text blocks with no tool call between them (the inner turns of a `/goal`) are written a paragraph apart.

### Thread status

- The status line also goes on an error, besides while an approval or a question waits for you, on `!stop` and when the session closes. It never notifies.

### `!status`

- At the top level each live session is a link to its thread labelled with the session's title (`Session` while it has none), followed by `waiting for you`, `busy` or `idle`.
- After `idle` comes how long ago the session's last message was written (`idle · 16m ago`). A live session is closed after an hour idle, so the time stays under the hour.

### `!resume`

- A thread that already holds a session, live or only stored in `state.json`, refuses a resume.

### `!open`

- The modal is titled `Open a file`, with the buttons `Close` and `Open`. Its search field is labelled `Search any file`, with the placeholder `Type part of a name`.
- With the field empty, the line above the rows reads `Changed in this session (N), newest first`; with text in it, `N files match`. More than 10 rows say so under them.
- A listing that was cut short adds `The folder could not be listed in full, so some files may be missing here.` An `!open <words>` that finds no match in such a listing answers `No file matching ... was found, but the folder could not be listed in full.`
- An empty file answers `... is empty: there is nothing to show.`
- The modal waits for the listing at most 1 second after the click. Git runs in at most 4 repositories at once, and the repositories found in a folder are kept for 5 seconds.

### Background commands and subagents

- A message that Claude Code took into a report turn is released when that turn ends, and the reply says so at its end: `Claude Code took your message into this reply: send it again if it is not answered here.`
- A message sent while a background task is about to report is answered in a reply of its own, and the report stays with the reply that started the task, whichever of the two turns Claude Code runs first. This holds for a message of plain text; for a command or a message with an image the answer can still land in the task's reply.

### A restart

- The refusal lists each thread the restart still waits for: its channel, a link labelled with the session's title and what holds it there (`a turn is running`, `an approval or a question is waiting for you`, `2 shells running`, `a background task is about to report, within 30 seconds`). It shows eight rows at most and a count of the rest.
- Under the rows it adds `` `!stop` in a thread ends the wait there. ``, unless every listed thread only waits for a report, which ends by itself.
- `!status` typed in a channel adds, in a message of its own, the rows of that channel's threads and a count of those in other channels, which it does not name.
- The 29-minute limit denies an approval or a question that is still open.
- Closing a session deletes any approval, question or same-folder hold message it denies, not just the ones `!stop` denies.

### Repair after a crash

- The line appended to a repaired reply reads `code-with-slack stopped before this answer.`

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
  ended and none of its tasks still runs or still waits on a turn Claude Code starts to report it:
  a reply that starts a background command or an agent stays open, showing that task's
  card, until the task and its report are done. The banner is the start of Claude's answer.
- A reply still open 280 seconds after it started stops its stream then (Slack would close it at
  300 seconds), which rings, and goes on in the same message by `chat.update`, silently. Its end
  posts the text Claude wrote after its last call and the footer as a new message, which rings
  a second time with the start of that text, and takes the text out of the first message. An
  answer that is text alone keeps it, and the new message is the footer alone. A turn cut
  short gets no footer. When its reply has no text after the last call to move, the new
  message is the line that says how it ended (`Claude Code reported an error: …`, or
  `This reply ended before an answer: …` after a restart of the daemon), taken out of the
  first message, with the footer of an earlier turn under it if the reply has one, and the
  second ring reads that line. When Claude wrote text after its last call, the new message is
  that text with the line under it, and the ring reads the start of the text. No other line
  of the daemon's moves.
- A reply whose stream Slack refuses to grow (`msg_too_long`: Slack does not document its cap, and
  the daemon counts text and cards toward it by measured figures) stops its stream at that moment, which rings, and
  goes on the same way: by `chat.update`, with its ending in a new message. An edit Slack refuses for
  its content is dropped, on any message of a reply: if no later edit of that message passes, the
  reply is short of what Claude wrote and the root shows ❌.
- A reply longer than one message (12,000 characters or 50 blocks, counting each heading, table
  and rule of Claude's text as a block) continues in a new message, and
  each extra message rings. The text of a collapsed diff counts toward the 12,000 in a stream and
  in a post, and not in a message that is edited, which holds up to 45 diffs. Once the reply's end has landed (its stream stopped with the footer, its
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
- `!stop` and a restart end the reply the same way: the stream stops with the footer. A restart
  rings once. A `!stop` typed in the thread rings once, and the push shows `Stopped.`, the answer
  posted in the thread: the stream's stop lands within about a second of the owner's own message
  there and did not push on its own (iOS, channel on "Just mentions", one run on 2026-09-29 and
  one on 2026-10-07). A `!stop` typed at the top level is answered in the channel, with
  `Stopped what was running in this channel.`, and sends no push (one run, 2026-10-07). The
  messages a restart dropped from the thread's queue get no reply of their own: they are
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
