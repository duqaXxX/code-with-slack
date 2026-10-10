# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added

- The release probe looks at commands (`probe/commands.py`). It compares the commands a session
  is offered on the release with the recorded `server-info.json` and prints the new and the gone
  ones, to be tried in Slack before the release is pinned; `uv run python -m probe --commands`
  does that alone, with no token. A new claim, P23, sends every command `docs/limits.md` lists as
  not offered: one that answers anything else is a limit that is gone, and the claim is BROKEN
  until the page says so. On Claude Code 2.1.292 the comparison names one new command,
  `/plugin-authoring`, and P23 holds on 19 commands.
- `docs/limits.md` lists what the terminal does and Slack does not, under whoever sets each
  limit: the Claude Agent SDK (the commands and features Claude Code keeps for its interactive
  terminal, 22 of them measured on 2026-10-09 with Claude Code 2.1.292), Slack (a clip's
  transcript), what is done on the machine, and the project's own choices. A limit that may have
  a way around it names the issue that looks into it (#213 to #221). `tests/test_docs.py` fails
  when a row has no such cell, or when a command the page lists as not offered appears in the
  recorded `server-info.json`. `docs/setup.md` points to the page for computer use in place of
  stating the limit itself.
- An audio clip recorded in Slack, or an audio file uploaded to it, can be the message (#35);
  an audio file was refused at once as a type Claude cannot read. The clip waits until the owner
  chooses Generate transcript on it; Slack then sends `file_change` for the file, and when its
  `transcription` is `complete` the daemon sends that text to Claude as a typed message
  (`voice.py`), never as a `!word` or a command. The daemon transcribes nothing and downloads no audio: Claude Code takes no audio
  file, and its own dictation is the microphone of an interactive session. The manifest gains the
  `file_change` bot event; an app created earlier adds it under Event Subscriptions. Known
  limit: Slack picks the language it hears, and in three clips spoken in Italian it wrote two as
  English (2026-10-09, free plan, workspace set to English). Not recorded: the events' own
  payloads, a transcript Slack cut, a status other than `complete`.
- A session started from Slack has Claude Code's Chrome integration when the owner chose
  "Enabled by default" in `/chrome`. Claude Code applies that choice to interactive sessions
  only, and in the non-interactive mode the Agent SDK uses it connects the `claude-in-chrome`
  server only when started with `--chrome` (measured 2026-10-09, Claude Code 2.1.292): the
  daemon now passes the flag when `claudeInChromeDefaultEnabled` is true in `~/.claude.json`,
  read at every connect (`chrome.py`). The permissions stay Claude Code's own: a browser action
  it asks about reaches Slack as Approve and Deny buttons, and the daemon adds no rule for a
  thread in bypass. Not checked: the same under the LaunchAgent, whether a browser action still
  asks in a thread in bypass, and how a screenshot in a tool result shows in Slack.
- The thread's status line reads `Compacting conversation…`, the terminal's words, while Claude
  Code compacts the conversation, on `!compact` and on a compaction of its own (#169). Claude
  Code opens a compaction with a `status` system message that says `compacting`, repeats it
  after 30 seconds, and sends nothing else until the `status` message that carries
  `compact_result`, 14 to 37 seconds later in eleven recordings (2026-10-08, claude-agent-sdk
  0.2.164, bundled CLI 2.1.292, Haiku); the line said `Working…` meanwhile. That first message
  now also starts the turn it belongs to, when one is due, so a turn Claude Code starts to
  report a task and that compacts first is no longer given up on after `INJECTED_TURN_WAIT`,
  its report then landing in another reply (`tests/fixtures/sdk/auto-compact-report-turn.jsonl`,
  the end of a session recorded just past its threshold, where that compaction took 23.8
  seconds). A background subagent that compacts its own context sends neither message on the
  conversation's stream (one recording). Probe claim P22 reads the line during a `/compact` on
  every new release.

- With bypass off a thread runs in the mode the owner's settings start Claude Code in
  (`permissions.defaultMode`), auto mode included; the daemon sets none. The `Mode:` line of
  `!status` shows the mode Claude Code reports (`ThreadSession.mode`: the connect's
  `current_permission_mode`, then the `permissionMode` of its `status` system messages). The
  footer and the channel list mark bypass alone. `!bypass off` in a thread that started in auto
  mode returns to it, and sets
  `default` when Claude Code refuses it, which it does for a model with no auto mode. The answer
  to `!bypass off` is `Bypass is off in this session: Claude Code follows your permission settings
  again.` whatever that mode is (`texts.BYPASS_OFF_THREAD`).

- `docs/sdk-surface.md`, the map of what the daemon depends on in `claude-agent-sdk`: 200 rows,
  one for each type, function, method, option, field, key and value the source reads, calls or
  decides on, with where it is used and where it is known from. Read on 2026-10-07 against SDK
  0.2.164 and the Agent SDK reference for Python: 116 rows are named by the reference, 20 are
  defined by the package only (`TaskUpdatedMessage`, `ModelUsage`, the two private functions of
  `claude_agent_sdk._internal.sessions` among them) and 64 are in neither (the keys of the
  server info, of `tool_use_result`, of the stream events, the `effort` of the Stop hook input).
  `tests/test_sdk_surface.py` keeps the table in step with the source's imports and with the
  installed package, so the daily SDK release watch runs it on the newest release. The probe
  (`probe/surface.py`) checks it before its scenes: a row the package no longer defines is
  BROKEN and ends the run with status 3 before any token is spent; a `reference` row the
  published reference no longer names, or a reference that cannot be read, is UNPROVEN; the
  types the package exports that the table neither lists nor sets aside are printed as new.
- `!open`, sent inside a session's thread: a message with one `Choose a file` button, and
  `!open <path>` or `!open <words>` for a file directly. The button opens a modal with a search
  field and one radio row for each file (its name as plain text, its folder below, at most 10, with a
  line above them that says what they are): the files changed in the session, newest first, while
  the field is empty, and the files whose path contains what is typed otherwise, updated on every
  character (`dispatch_action_config` with `on_character_entered`, answered with `views.update`).
  Several matches of `!open <words>` post the count with the same button, whose modal holds the
  words. `Open` shares the chosen file into the thread with `files_upload_v2`, so Slack's own file
  viewer opens it (a `.md` file with Markdown rendered) with the thread beside it, and the modal
  closes; with no row chosen the modal shows an error (`response_action: "errors"`) and stays. The
  modal opens at once when the rows are ready within one second and is filled by an update
  otherwise, since a click's `trigger_id` lives 3 seconds: the listing starts at once, beside the
  channel check, and the wait is counted from the click. The thread travels in the view's
  `private_metadata` and is resolved to the folder of its own session by every handler; the
  typed characters are checked for the owner and the workspace alone. An update that a newer
  keystroke has overtaken is never sent (`openfile.ModalUpdates`, ordered by `action_ts`; no
  update carries Slack's `hash`, the daemon being the view's only writer). The rows' block id
  follows the rows and a row counts only when it is among the submitted view's own options, so a
  choice Slack kept from other rows is never opened. The new module `openfile` holds the logic. The search reads the folder from disk:
  inside a repository git may run in, git's own list (`ls-files`, which leaves out what
  `.gitignore` excludes, as the terminal's `@` file picker does under `respectGitignore`),
  everywhere else a walk of regular files that enters no symlinked folder and no `.git`. A
  listing stops after 2 seconds with what it found and is kept for 30 seconds (3 when
  incomplete), and at most 16 folders are kept; the repositories found in a folder are kept for
  5 seconds, the lookups run together and git runs in at most 4 repositories at once. A kept
  listing that finds no match is made again first, and a listing that was cut is said in the
  answer, never answers "no match" alone and never opens its single match on its own. Typing
  checks the disk only until the rows are full (past ten the count is the matches by name). The changed files come from git, through the
  footer's git helper (now `footer.run_git`), with commands measured never to write the index
  (git 2.54.0, 2026-10-05): `status` under `--no-optional-locks` and `diff-tree` from where each
  repository's `HEAD` was when the thread started, read from the reflog at the thread's
  `thread_ts` (`openfile.start_commit`, `rev-parse HEAD@{<seconds> +0000}`; nothing is kept in
  memory, so a restart changes nothing), summed over the repositories of the folder (the one that
  holds it, or those at most two levels below it), with paths from the session's folder; there are
  none when there is no such repository or nothing changed, and they are what is uncommitted or
  untracked where there is no reflog; a repository made or cloned after the thread began counts
  every file (the start is the empty tree). An empty file is refused before any upload. The file
  is opened once (`O_NOFOLLOW`, size from the
  descriptor, at most 1 MB read) and its bytes are passed to `files_upload_v2`, so a path that
  changes after the check is not followed. A file name over 75 characters is shortened in its
  middle, a folder over 75 from the left, and a path over 150 gets no row and stays reachable by
  `!open <path>`. A file over 1 MB, a path that leaves the folder and a click, a typed character
  or a submit from anyone but the owner are refused. The bot needs the new scope `files:write`:
  an installed app is reinstalled from `slack-app-manifest.json` (`docs/setup.md`, Part 1). The
  guide and `!help` list the word, and the guide is now longer than the notification text Slack
  shows for a message (3,000 characters), so that text is its first 3,000.
- Cleaning up a channel from the Home tab (issue #146), with the optional `SLACK_USER_TOKEN`:
  in edit mode a **Clean up** button beside each channel's name, with a confirmation dialog
  that says what it deletes. `ThreadDeleter.clean` reads the channel's history and deletes what
  sits outside a thread: the owner's messages that have no reply (a word such as `!stop`, a
  prompt never answered, the first message of a thread whose replies are gone) and the bot's
  own. Every thread that has a reply stays, as do Slack's own lines; a message that carries
  `thread_ts` is deleted only once Slack answered that its thread is empty. While it runs the channel's name and a line under the header say so;
  it shares one queue with the thread deletes. A message Slack refuses to delete is counted
  and the rest still goes.
- Deleting a session's whole thread from the Home tab (issue #145), with a new optional
  variable, `SLACK_USER_TOKEN`: the owner's own user token with the user scope `chat:write`.
  Slack lets a bot delete only its own messages, and a thread's root and the owner's replies
  are the owner's. With the token set, the line above the sessions carries **Edit**; in edit
  mode each session that is not working or waiting for the owner carries a red **Delete** with
  Slack's confirmation dialog, which names the thread, and the **New thread** buttons are left
  out. **Done** brings back the filters the page had when **Edit** was pressed, so **Show all**
  used in edit mode does not leave the page on one channel. Confirmed, the new module `delete` (`ThreadDeleter`) closes the thread's idle session
  (`SessionManager.release`), deletes every message, the root last, and drops the thread from
  `state.json`; the Claude Code session stays and `!resume` lists it again. Slack's rate limit
  makes a delete take from seconds to minutes: from the click on a line under the header counts
  the threads being deleted, a channel's name is followed by the count of its own, the
  session's line reads `deleting…` and has no Delete, and
  threads are deleted one at a time. While a thread is deleted it is held
  (`SessionManager.release`, `.held`, `.free`): a message sent in it builds no session, and the
  root goes only once a read of the thread shows no reply left. A message Slack refuses to
  delete (`cant_delete_message`) is counted, and the root then stays. Each delete or clean-up
  that did not end keeps its own line under the header, which names its thread. A delete that is
  refused or stops half way leaves a line under the header and continues when asked again. The
  daemon refuses to start with a user token that is not the owner's own in the bot's workspace.
  Without the token nothing changes: no Edit button, and the header stays a small line. The
  manifest does not ask for the user scope; `docs/setup.md` has the steps.
- `!status` typed in a session's thread shows, under `Session:`, the command that continues
  that session in the terminal (issue #12, first step):
  `` Terminal: `cd <folder> && claude --resume <session id> --fork-session` ``, with the folder
  quoted for the shell (`sessions.terminal_line`). Claude Code leaves a session created through
  the Agent SDK out of the terminal's picker, and a fork made in the terminal is listed. The
  line is absent while the thread has no session id and when its folder is missing, unreadable
  or not trusted; a command holding a backtick shows as escaped text. `!guide` names that line in place of
  `claude --resume <id>`, and `docs/setup.md` has a section on it, which also says why the
  command keeps `--fork-session` and that Slack does not look for the fork by itself; the
  README names the command.
- Thread status (issues #83 and #95): Slack's own status line under a thread's last message
  (`render.status.ThreadStatus`, `assistant.threads.setStatus` with a loading message, which is
  what makes it show on iOS). It reads `Working…` from the moment a prompt is received until
  its turn ends, and while a turn Claude Code starts to report a task runs. Once the turn has
  ended it reads what the turn left running, `1 shell still running`, the words the terminal
  ends such a turn with, and follows the count until nothing runs
  (`ThreadSession._thread_line`). The count is a state of the thread and is kept out of the
  reply, whose stream cannot change what it was sent. The status is set again within 2 seconds
  of a write of a reply or of a notice of the session's and every 60 seconds, since Slack
  clears a status when the app replies and removes it after two minutes. The answer to a word
  typed in the thread (`!bypass`, say) is such a write too (`SessionManager.wrote`). It goes
  while an approval or a question waits for the owner, on `!stop`, on an error and when the
  session closes. A clearing call that fails is
  tried once more, and again when the session closes. A refusal from Slack is logged
  and the line is skipped; `missing_scope` or `not_allowed_token_type` ends the attempts for
  the rest of the run. It never notifies and stores nothing. Known limit (issue #130): the iOS
  app can show no status for a thread that Slack on desktop already has open; `docs/setup.md`
  says so.
- Cleanup of `state.json` (new module `code_with_slack.cleanup`): on start and then every 6
  hours, a bound channel Slack answers `channel_not_found` about is forgotten with its threads
  (`StateStore.remove_channel`), and the pruning of threads whose session is gone, which ran on
  start alone, runs on the same schedule. A failure that is not that answer removes nothing, a
  thread or channel with a live session is left for the next pass, and when Slack finds none of
  the bound channels nothing is forgotten. A private channel the bot was removed from is
  forgotten like a deleted one: `!bind` there again once the bot is back.
- Session index: the app's Home tab lists the sessions the threads hold, grouped by channel, the
  channel and the session used last first (new module `code_with_slack.home`). A channel shows
  its five newest sessions and a Show all button; each session is two lines, the root's status
  reaction and the title, then the status in a word, the thread's number of replies, the time of
  its last reply and an Open link to the thread. The replies and the last reply are read from
  Slack (`conversations.replies` on the root), which also orders the sessions. New
  thread, beside a channel, opens it. Four controls narrow the page and add up: channel, status,
  period (the last 48 hours to start with) and a search on titles; the choices live in memory. A thread whose root was deleted and a
  channel Slack no longer has are left out. The page is published with `views.publish` whenever
  `state.json`'s sessions change, with no notification. The manifest switches the Home tab on
  (`features.app_home`); an existing app needs **Home Tab** turned on under **App Home**, and
  without it the daemon logs once and publishes nothing. `state.json` gains an additive
  per-thread key, `ended`: the root's reaction name once ✅ or ❌ is requested (`state.json` stays
  version 2; a thread that ended before this shows the reaction read from its root).
- Session setup before the first prompt (issue #74): a top-level message that opens a session
  first gets a message in its thread with a Model select (the CLI's own list), an Effort select
  (`Default` or the chosen model's levels), a Bypass checkbox and Start. Start applies the choice
  and sends the held message; `!stop`, a drain or a restart cancels it with `Not sent.`, also
  while Start is being applied. The Bypass box starts ticked in a folder whose own Claude Code
  settings start in bypass; unticking it turns bypass off, as `!bypass off` does, and that off
  now survives an idle close or a restart (`state.json` keeps on, off and never chosen apart,
  with an additive `bypass_off` key; an older file's `false` reads as never chosen). A reply in a
  thread where nothing was ever sent (after a cancel) asks the setup again, from the defaults.
  Probe claims P17 (the server info's model fields) and P18 (a model set with `set_model()`
  survives a resume).
- Crash repair (issue #19): `state.json` now tracks, per thread, the ts of every open reply's
  last message (`open_replies`, a list: a background task's own reply can outlive the turn that
  started it, so more than one can be open at once), the ts of every approval, question and D8
  hold request still carrying buttons, and the root's reaction while it is ⏳ or ✋, ids only,
  never message content (`state.json` stays version 2, additive). On start, before the Socket
  Mode connection opens, `code_with_slack.repair.repair_crash` reads each open reply back by its
  own ts and rewrites it to say the daemon stopped before an answer, dropping the daemon's own
  status line (found by its own fixed block_id, since Slack assigns one to a block that was
  posted without one) rather than the whole message; deletes each stale request; and sets ❌ on a
  root left mid-turn, through the same reaction helper a live session uses. A graceful close now
  also deletes any request it denies (as `!stop` already did) and clears these fields itself, so
  a second start repairs nothing.
- A status reaction on each session's root message: ⏳ working, ✋ waiting for the owner, ✅ once
  everything has ended or `!stop` stopped it, ❌ on an error or a restart that cut a busy session
  short (D10).
  `reactions:write` is added to the bot's Slack scopes.
- One `chat.update` limiter shared by every reply: a token bucket paced at 40 writes per 60
  seconds plus a burst of 5, worst case 45 in one window, still under Slack's documented floor,
  so several busy threads stay under the app-wide `chat.update` budget together instead of each
  keeping its own. The per-reply once-a-second rewrite is
  unchanged.
- The footer and `!status` show the lines changed since the last commit next to the branch,
  `(+42,-10)`, counted as the terminal's ccstatusline counts them (#39), and the time to the
  weekly limit's reset, `7d 45% ↻ 3d 4h`, as they already did for the 5-hour one (#42).
- Probe claim P14: a hook's `cwd` follows a `cd`, which the footer's branch depends on.
- One Claude Code session per Slack thread, instead of one per channel: a top-level message opens
  a new thread with its own session id, bypass switch and effort level, kept in `state.json`
  (now version 2); a reply inside a thread continues that session, even after code-with-slack
  restarts or the thread's Claude Code process closes from an hour with nothing to do (the next
  message resumes it). A channel bound under version 1 keeps its directory and gets one top-level
  notice explaining the new model, with its old session still reachable through `!resume`.
- Probe claims P15 and P16: a resumed session keeps the model set with `/model`, and loses the
  effort set with `/effort` until `ClaudeAgentOptions(effort=...)` restores it.
- `!resume`, typed at the top level, lists the channel's folder's sessions in its own thread; a
  Resume click or `!resume <id or title>` makes that thread the resumed session's thread. It is
  refused inside a thread that already holds a session.
- Every reply, approval request and question posts inside the session's own thread, and Slack
  notifies the owner on a new message in a thread it started, mention or none, once when a reply
  is complete, for an approval request and a question, and for a reply that ends in an error;
  nothing rings while Claude writes, nor for `!stop`, a restart or an idle close. `docs/setup.md`
  gains the notification section and `docs/features.md` the measurements behind it.
- D5/D6: a bind's answer names each existing thread's own folder when it differs from the new
  one, and every prompt sent in that thread after the bind repeats the notice, as an ephemeral message; the
  resume picker's row for a session already held by another thread, and the refusal when that
  session is named or clicked, both carry a permalink to the holding thread instead of a plain
  marker.
- D8: two sessions can now work in the same folder at once, but the daemon asks before a message
  wakes an idle one while a live session of another thread (any channel, resolved path) is not
  idle in that folder: `Another session is working in this folder: <link>. Send anyway?`, with
  Continue and Cancel. `!stop` inside the held thread, a top-level `!stop` of its channel, and a
  restart cancel the wait the same way Cancel does, with a `Not sent.` notice; the idle-close
  timer and the ✋ root reaction treat a held message the same way they treat an open approval.

### Changed

- `!stop`, `!resume` and `!bind <folder>` typed in a thread that holds no session change
  nothing and say where to send them, for the owner alone. They acted as if typed in the
  channel: `!stop` under the answer to a `!bind` stopped every session of the channel, and
  `!resume` there turned that thread into a session. `!help`, `!guide`, `!status` and `!bind`
  with no folder still answer there as they do in the channel. `!stop` answers with
  `texts.STOP_OUTSIDE_SESSION`, the other two with `texts.WORD_IN_THREAD` (#73).
- The docs say that computer use is not available to a session started from Slack. Claude Code
  offers its built-in `computer-use` server in an interactive session only, and the Agent SDK
  runs Claude Code in its non-interactive mode: the server is absent there even when it is
  switched on for the folder (measured 2026-10-09, Claude Code 2.1.292). `docs/setup.md` states
  the limit beside what does load, the MCP servers the owner configured, and that a server
  cannot be signed in from Slack; `docs/architecture.md` records the measurement, which also
  covers the Chrome integration. No change in behaviour.
- The docs are rearranged for a first reader, with nothing removed from what they state.
  `docs/setup.md` is one numbered path from the clone to the first reply, with a checkpoint after
  the install, after `.env` and after the LaunchAgent starts; Full Disk Access comes before the
  LaunchAgent, and what a first install does not need (`state.json`, restarts, scopes, trust and
  git) is under `Reference`. `docs/architecture.md` opens with an overview and a diagram, names
  each behaviour in words and gathers its measurements in one table. The Feature column of
  `docs/features.md` is one line per feature, with the rest under `Details`. `tests/test_docs.py`
  now fails on a dotted name a doc cites that the source does not define, on a variable, a scope
  or a word `docs/setup.md` does not name, and on a Feature cell past `FEATURE_NAME_LIMIT`
  characters.

- The README is a landing page: what the daemon is for, what it needs before an install, the
  steps of the setup, what a session shows and one row per command. The detail of each command
  lives in `docs/setup.md` alone. `tests/test_docs.py` fails when the README's command table
  differs from the `Word` union of `code_with_slack.commands`, when the version or the Python it
  states differ from `pyproject.toml`, and when it passes `README_WORD_LIMIT` words.

- A reply ends with ❌ whenever an edit Slack refused left one of its messages short of what
  Claude wrote, whatever kind of message it is (#92, the owner's decision of 2026-10-08). An
  edit refused for its content is dropped, and the message keeps what it showed. That was
  remembered only for a message whose stream had been refused as too long; a reply past 280
  seconds and a continuation message ended with ✅ on a text that was missing its last change.
  `ReplySink._update_step` now marks any such message as short until an update of it passes,
  and the reply's end counts as not landed while one is: the one retry runs, then the root
  shows ❌. One case is not short: the update that only folds the cards of a stream that was
  sent all of its span, where every word and every card is already on Slack. Nothing is added
  to the thread; the log line of the refused write says which write and why.
- The confirmation of `!resume` says what a session open in two places does (#41): `If it is
  open in a terminal, close it there first: while it is open in both, neither sees the other's
  messages, and a later resume keeps only one side's.` It said the messages of both would mix in
  one conversation. Measured on 2026-10-03 with claude-agent-sdk 0.2.163 beside a host CLI
  2.1.288 process on the same session id, both orders: the turns of both land in one transcript
  file on separate branches, neither process has the other's turns in its context, no error
  shows on either side, and a later resume continues one branch. The interactive terminal was
  not part of that measurement.

- `!help` with no text opens with one line on what makes a message a command: its first
  character is `!`, in code or bold too, and anything before the `!` sends it as text (`\!goal`).
  The rule of #178 was in `docs/setup.md` only. A search (`!help <text>`) lists its matches
  alone, as before.
- The two buttons under `Another session is working in this folder: ... Send anyway?` read
  `Send anyway` and `Don't send` (issue #76); they read `Continue` and `Cancel`. Slack sizes a
  button by its text and offers no width, so the shorter `Cancel` looked the lesser of the two.
  The new labels differ by one character and answer the question in its own words. Their styles
  are unchanged: `Send anyway` is `primary`, `Don't send` has none.
- A channel's `!status` names each session by its title (issue #76). The link to a thread read
  `Session` on every row, so several idle threads showed as identical `Session: idle` lines.
  The link's label is now the title Claude Code gave the session, as the Home tab and a
  restart's notice already name it, cut at 80 characters and shown as written, and an idle row
  says how long ago the session was last written to, in one unit: a row reads
  `Fix the footer: idle · 2h ago`, its title being the link. A session with no title yet, or whose folder's sessions cannot be listed, keeps
  `Session` and shows no time. The answer is a post every member of the channel reads, as it
  was.
- A subagent's failed API request no longer shows at the top level of the reply (issue #161).
  For a background subagent Claude Code forwards the subagent's own error message, with `error`
  and `parent_tool_use_id` both set (recorded: `subagent-api-error.jsonl`, CLI 2.1.286, every
  request of the subagent answered 529 by a local endpoint while the main conversation's
  succeeded), and `TurnRenderer._assistant` wrote its sentence into the reply, although the main
  turn went on and succeeded. The sentence was already on the subagent's card, as the output of
  the failed task, so it showed twice. The message is now left out of the text, as every other
  text of a subagent is, and its words are noted under the card of the call that started the
  subagent, so they show even when no task frame of that subagent reaches the reply. The session
  logs `Claude Code reported a subagent's error in <channel>/<thread>: <category>`; such a
  message logged nothing before. For a foreground subagent no such message is forwarded and
  nothing changes (recorded: `subagent-api-error-foreground.jsonl`): the card shows the error
  with the reason from the task's notification.
- The text Claude Code writes for nine API statuses is recorded and replayed (issue #162):
  `api-error-400.jsonl` to `api-error-504.jsonl`, CLI 2.1.286, one status and its documented
  error type per recording. Nothing changes in what the reply shows: `authentication_failed`
  keeps the project's note, and every other category shows Claude Code's sentence, whether the
  SDK's `AssistantMessageError` names it or not (`model_not_found` for a 404 is not in it). A 403
  arrives as `authentication_failed`, as a 401 does. The recordings for 401, 402 and 403 were
  made with a made-up API key, so their sentence is that of API-key auth.
- `claude-agent-sdk` 0.2.164, which bundles Claude Code 2.1.292 (was 0.2.163 with 2.1.286). The
  probe certified it on 2026-10-07: all 19 claims hold, every row of `docs/sdk-surface.md` is
  still in the package and every `reference` row is still named by the reference. The two
  private helpers `resume.py` imports from `claude_agent_sdk._internal.sessions` are unchanged.
  The recorded fixtures stay those of CLI 2.1.286.
- A refused click on a session setup or a held message is logged at INFO (issue #142), as
  `refused a click (<path>): action <action id> in <channel>/<thread> on message <ts>; <flags>`,
  where `<path>` is `setup start, not held`, `setup start, not resolved`,
  `setup model, no open setup` or `hold decision`, and the flags (`held`, `decided`,
  `same_thread`, `open_at_message`) tell which check failed. An accepted Start logs
  `accepted a setup start in <channel>/<thread> on message <ts>`. Nothing the owner sees changes,
  and the log holds ids and flags only.
- A posted approval or question request, and its removal, are logged at INFO (issue #71):
  `posted an approval request in <channel>/<thread>: message <ts>` (or `a question request`) and
  `removed a request in <channel>: message <ts>, <n>s after it was posted`, the age taken from the
  message's own `ts`. The log shows the timing of a push that did not arrive; it holds ids and a
  duration only.
- `docs/features.md` and `docs/setup.md` say what a `!stop` rings as measured (issue #67): typed
  in the thread, one push that shows `Stopped.`; typed in the channel, none. Both said that a
  `!stop` rings once wherever it is typed, through the stop of the reply's stream.
- The case of issue #149 is also replayed from a recording in the order the CLI sent it:
  `tests/fixtures/sdk/subagent-nested-background-mid-turn.jsonl` (CLI 2.1.286, recorded
  2026-10-06). A subagent leaves `sleep 15` running and the owner's turn runs a 40-second
  foreground command of its own. The command's `task_updated` and `task_notification` both
  arrive before the turn's result, the agent then starts and ends a second time, and a report
  turn follows. `test_a_recorded_turn_that_outlives_its_subagent_s_command_ends_once_with_its_footer`
  fails with the #164 change taken out.
- `ThreadSession._react_done_if_idle` logs one line at INFO when a reply's end does not bring
  ✅ (issue #160): `no done reaction in <channel>/<thread>, held by: …`, with the conditions
  that held it as flag names and counts (`running=1 shell`, `sent=1`, `unsettled`), never
  content. The check returned in silence before, so a root left on ⏳ could not be traced.
- A reply Claude Code wrote itself about a failure (an `AssistantMessage` with `error`) shows its
  text, as the terminal does: for a 529, a sentence that starts `API Error: 529 Overloaded.` and
  says to try again, where the thread showed only the category,
  ``Claude Code reported an error: `server_error` ``. The rule covers every category except
  `authentication_failed`, which keeps its login note. A message with no text block leaves the
  word to the turn's result, which repeats the sentence, and the category line shows only when
  the result has no text either (`TurnRenderer._assistant`, `TurnRenderer.feed`). The daemon log
  gets a warning with the channel, the thread and the category, never the text
  (`ThreadSession`, issue #157). Recorded for `server_error` on a 529 against a local endpoint,
  SDK 0.2.163 with CLI 2.1.286, on 2026-10-06 (fixture `server-error.jsonl`). The text for the
  other categories that CLI can send has not been recorded: `billing_error`, `rate_limit`,
  `overloaded`, `invalid_request`, `model_not_found`, `max_output_tokens`, `unknown`,
  `oauth_org_not_allowed`, `account_on_hold`, `verification_required` and
  `cloud_credential_error`.
- A message written by `chat.update` no longer counts the text of its collapsed containers (an
  Edit's diff, with a card or without) toward the 11,000 characters a message holds, only the
  block each takes of the 45. A stopped message, or a message whose span is fixed, holds several
  large diffs where it used to continue in a second one or cut a late preview, and a continuation
  that is posted is brought to what an update takes by a `chat.update` of the same message, so it
  opens no message of its own for them. If Slack refuses an update that held a container (for
  content), that message counts its containers as a post does from then on: the update is tried
  once more at once, what fits is written, and the rest goes on in a new message, or is cut with
  the preview note in a message whose span is fixed. Measured on 2026-10-06 (slack-sdk 3.44.1, free plan, one
  run per row): `chat.update` accepted 50 containers of 10,000 characters (500,000), and 45 of
  11,000 in the shape a call with no card gets (a rich title and a subtitle), which is the most
  the counting lets a message hold, and a message born as a stream (text, three cards, one
  container) grown to 45 blocks and 444,823 characters, and never refused one, while `chat.appendStream` and `chat.postMessage` count a container's text toward
  the same cap as the reply's words (about 13,200 characters in all). The stream path is
  unchanged. How a message with that much collapsed diff opens on desktop and on iOS is not
  checked yet. A new file's first lines and a question's answers still count (#52).
- The daemon's own git (the footer, `!status` and `!open`) also runs in a repository inside the
  folder a session started in, when that folder passes `workspace_trusted`: Claude Code launched
  in a trusted folder works in its subfolders, and a bound folder that is not a repository
  itself, holding one a level or two down, now shows the branch and the changes there.
  `trust.trusted_repository` takes the session's folder as a second argument; inside is decided
  on resolved paths, so a symlink that leads to a repository elsewhere and a worktree whose main
  checkout is elsewhere still need their own trust, as does any repository outside the folder.
  So does a repository inside the folder whose `.git` leads out of it (a `.git` file or symlink
  naming a git dir elsewhere, a worktree moved in by hand, a `commondir` naming another
  repository): the key, the git dir and the common dir must all lie inside. The small files read
  for that are cut at their line ends as git cuts them (a `commondir` ending in a CR and LF named
  another place than git's), and an empty `commondir`, which git refuses, covers nothing.
  `workspace_trusted`, the gate of a session's start and `!bind`, is unchanged. A repository
  that ends up inside the session's folder (a clone made during the session, say) now has git
  run under its own config, filters included, outside the approval prompt.
- Session index (issue #101): a session's **Open** link in the Home tab opens the thread on its
  last reply, where it opened it on its first message. The link is the permalink of the message
  the root names in `latest_reply`, the owner's own messages included, and the root's permalink
  while the thread has no reply (`Home._permalink`). It is asked again only when the thread's
  last reply changed, so each reply costs one `chat.getPermalink` call; `state.json` is
  unchanged. Slack scrolling a thread to the reply its permalink names was seen in the Mac app
  and on iOS on 2026-10-05.
- An `Edit` or a `Write` that ended well is one row in a reply (issue #136): a collapsed
  container titled with the call's line in code style (`Update(notes.txt)`), with the sentence
  (`Added 1 line, removed 1 line`) as its subtitle and the diff inside, and no task card. A new
  file's first lines are in a container of the same shape, where they were an open code block.
  The renderer sends these two tools to the sink once they have ended
  (`previews.PREVIEWED`, `TurnRenderer._block`), so the reply shows nothing for one while it
  runs. One that failed joins its run with the reason in its title, and one that was stopped
  has a card of its own. `sinks.diff_containers` is now `sinks.preview_containers`.
- The SDK release watch (`.github/workflows/sdk-release-watch.yml`) runs as two jobs. `test`
  installs the latest `claude-agent-sdk` and runs the suite with a token that only reads.
  `report`, now the only job with `issues: write`, starts on a runner of its own from a clean
  checkout and never installs the project: `.github/scripts/watch-sdk-release.sh` runs
  `sdk_release_report.py` with the machine's `python3` where it used `uv run`. Until now one job
  did both, so a release nobody had reviewed ran in the workspace and the environment that the
  issue step then used with its token. The report refuses an outcome other than `pass`, `fail`,
  `install failed` and `skipped`, as it already refused a version that is not one.
- Two refusal texts say what the thread model does (issue #78). `texts.DIRECTORY_MISSING` ends
  ``Restore it to keep using this thread, or, in the channel, bind another folder with
  `!bind <path>` and send a new message to start a session there.``: a thread keeps its folder,
  so binding the channel again helps only a new thread, and `!bind` is refused inside a thread.
  `texts.NOT_A_SESSION` reads ``This thread holds no session. In the channel, send a new message
  to start one, or `!resume` to continue an earlier one.``: it names no cause, since the daemon
  keeps no record of a thread whose entry it dropped, and points at `!resume` for a session
  Claude Code still has. `docs/architecture.md` states that a reply in a thread whose session
  is gone gets this text and starts nothing.
- The `!resume` list shows only sessions that can be resumed (issue #69). A session a thread
  already holds (D6) takes no row and is not dated; one line under the list counts them
  (`texts.RESUME_OPEN_ONE`, `texts.RESUME_OPEN_MANY`), and a folder whose sessions are all open
  says `No session to resume in <folder>.` Before, each held session took one of the twenty
  rows with an `open elsewhere` link, so in an active folder the list could offer nothing to
  resume. The list no longer asks Slack for a permalink per held row.
- After a Resume click the list is deleted (issue #70): the owner's `!resume` stays as the
  thread's root and the confirmation in the thread is the record. When the confirmation did
  not post, or `chat.delete` fails, the list is rewritten into `Resumed <title> in this
  thread.` as before, so its buttons never stay live. The `chat.delete` reference (read
  2026-10-03) does not say whether a delete notifies; a bot deleting its own message in a
  thread was silent when measured on 2026-09-29, and the top-level case is a check by hand.
- A message refused while the daemon stops names what the stop waits for (issue #119). Under
  `code-with-slack is restarting; send this again in a moment.` the refusal lists each thread
  that still holds the restart, of any channel: its channel, a link to the thread labelled with
  the session's title as the Home tab names it (`Session` when it has none, or when the
  folder's sessions cannot be listed), and what holds it there
  (`ThreadSession.restart_hold`: a turn running, an approval or a question waiting for the
  owner, the background tasks still running, a task about to report). The last line says
  `!stop` in a thread ends the wait there; it is left out when every thread only waits for a
  task's report, which ends by itself within 30 seconds and which `!stop` does not shorten.
  The list holds eight rows at most (`RESTART_WAIT_ROWS`) and counts the rest, so a notice is
  never cut inside a link. `!status` typed in a channel adds, as a notice of its own while a
  stop waits, the rows of that channel's threads and a count of those in other channels: its
  answer is a post the channel's members read, so another channel's thread is never named
  there. Before, nothing in Slack said which thread held a
  restart, and the owner could only wait out the 29 minutes or send a second signal. The
  refusal stays ephemeral and the list is built from what the daemon holds in memory, one
  `chat.getPermalink` call per listed thread and one listing of each folder's sessions.
- `!stop` typed inside a session's thread is answered by a message that stays in the thread
  (issue #85): `Stopped.` (`texts.STOPPED_THREAD`) when it stopped a turn or a background task,
  `Nothing is running in this session.` when nothing ran. Before, a stop that stopped something
  posted nothing, and the other answer was ephemeral, gone on reload and shown on one client
  only, so the owner could not tell whether the stop was received. The message is the app's in
  a thread the owner started, so it can notify. A `!stop` that only cancels a held message
  still answers `Not sent.` alone. `Stopped.` is posted once the reply the stop cut short has
  ended (`ThreadSession.stop_landed`, at most 15 seconds), so it sits under that reply's
  ending and footer when those are a message of their own. An answer that cannot be posted is
  logged and is not reported as the word's failure.
- A failed `chat.update` or `chat.postMessage` of a reply is logged as `chat.update failed` or
  `chat.postMessage failed` with the error code or the exception type, the characters of text,
  the blocks and the task cards it sent, never content, in the shape of the
  `chat.appendStream refused` line (issue #92). The log can tell the two methods apart; what is
  retried, dropped or adopted is unchanged.
- The answers to a question show in the reply, where the question was asked (#82). The call's
  card reads `User answered Claude's questions:` and a small line per question follows it,
  `⎿ · question → answer` (`render.previews.answered`, `Preview.plain`). The session hands
  the answers the owner gave to the reply (`ThreadSession._keep_answers`,
  `TurnRenderer.answered`), keyed by the call id the permission request carries (measured on
  Claude Code 2.1.286), and deletes the request message, as an approval's is. Before, that
  message was rewritten into the record and stayed below the reply, so everything Claude did
  after the answer showed above it. The rewrite remains as the fallback for a question whose
  call the reply has no line of its own for (`approvals.answered_blocks`). A `context` block
  inside a stream's `blocks` chunk was seen drawn on the daemon on 2026-10-03.
- A diff's container is titled with the preview's sentence alone (#110): `Added 10 lines`, with
  no subtitle. Before, it repeated the call's line (`✓ Update(notes.txt)`) that the task card
  above it already shows, so every `Edit` or `Write` read as two rows with the same name
  (`sinks.diff_containers`).
- `!bypass on` and `!bypass off` inside a session's thread answer with a line of text (#68):
  an ephemeral message under the word (`texts.BYPASS_ON_THREAD`, `texts.BYPASS_OFF_THREAD`)
  that says what changed, and for `on` that it survives a restart. The ✅ on the word stays,
  since the ephemeral line is gone on reload. Before, the ✅ was the whole answer and read as
  no answer. Not measured: the ephemeral line when the word is typed from the iPhone app.
- `!bypass on` and `!bypass off` typed in a thread before its setup's Start was applied (the
  setup waits, or was cancelled) change nothing and answer
  `This session has not started yet: tick Bypass in its setup and press Start. If no setup is
  shown, send a message here first.`
  (`texts.BYPASS_BEFORE_START`, ephemeral, no ✅). Before, the word switched the session and
  Start then wrote the checkbox over it, so the switch did not hold. After Start, while the
  first prompt waits on the question about another session in the folder, the word works
  (`ThreadSession.before_start`); a Cancel there drops the whole setup, bypass included. A
  word typed while Start is being applied waits for it and then switches the session
  (`ThreadSession.switch_bypass`), so its answer is never written over by the box.
- A restart posts no message in a session thread. What it waits for, when only background tasks
  hold it, is said by the thread's status line
  (`Restart waits for 1 shell · !stop ends it now`, `ThreadSession.show_restart_wait`), which
  does not notify and goes with the restart: the message it replaces rang and stayed in the
  thread. The notice that bypass stays on across a restart is gone, since bypass always
  outlives one. Where Slack refuses the app a thread status, one message still says what the
  restart waits for.

- A reply that outlives its stream ends with what Claude wrote after its last call and the
  footer in a new message (issue #66): the notification of that message reads how the work
  ended, where it repeated the reply's first paragraph, and the message holds more than a
  footer. The text is posted first and then taken out of the message it grew in by a silent
  edit (`ReplySink._end`, `ReplySink._ending_cursor`); an answer that is text alone keeps it,
  and the new message is the footer alone, as before.
- Every reply keeps its footer once it has ended. Only the counts of what still runs
  (`⏳ 1 shell`) stay with the thread's latest reply, so a reply that ends after a newer one
  shows its own footer, with the values of its last turn.
- What still runs is said once, on the last line of the thread: in the footer when the latest
  reply has ended, in the thread's status line when that reply is still open. The status line
  no longer repeats a count the footer above it shows.

- Tool calls in a reply (issue #80): a run of calls, the calls between two pieces of text, is two
  task cards in place of one card per call. The first holds the counts of what ended (`Ran 2 shell
  commands · Read 1 file · ✗ Ran 1 shell command`), the second the call running now. A failed call
  says why in its title. When the reply's body ends, one silent `chat.update` after the stream's
  stop turns each run into a line of counts in a `context` block (`✓ Ran 2 shell commands · Read 1
  file`). An Edit or a Write that ended well, a subagent, a background task and a stopped call
  keep a card of their own. New module `render/fold.py` (`Fold`), new field `TaskUpdate.folded`;
  `ReplySink.task` feeds the fold, `ReplySink._card_blocks` and `ReplySink._end` write the line.
  The cards of a run carry no `details` and no `output`: Slack appends both to what a card
  already holds (measured 2026-10-01, slack-sdk 3.44.1).

- `claude-agent-sdk` 0.2.163, which bundles Claude Code 2.1.286 (was 0.2.162 with 2.1.285); its
  Python source differs from 0.2.162 only in the version strings. The SDK streams in
  `tests/fixtures/sdk/` are recorded again on 2.1.286. A `thinking_delta` now carries
  `estimated_tokens`, which the daemon does not read. The rate-limit event can now come before
  the reply's `message_start`, so the test that cuts a turn mid-reply cuts after that event
  instead of at a fixed index.
- `claude-agent-sdk` 0.2.162, which bundles Claude Code 2.1.285 (was 0.2.160 with 2.1.283); its
  Python source differs from 0.2.160 only in the version strings. The SDK streams in
  `tests/fixtures/sdk/` are recorded again on 2.1.285. The background stream's report turn now
  runs to several sentences, so the debounce test that reads it has enough deltas.
- A reply is a native Slack stream (`chat.startStream`, `chat.appendStream`, `chat.stopStream`)
  instead of a message rewritten with `chat.update`. It starts with Claude's first content, with
  no `Claude is writing…` or `Waiting for the previous reply…` placeholder, so `texts.WRITING`,
  `texts.WAITING`, `texts.REPLY_TO` and `texts.REPLY_ABOVE` are gone. Each tool is a task card
  (`task_update`) in place of a folded tool line, titled in the terminal's words and updated as
  the call runs; Edit and Write previews go in a `blocks` chunk under the card. The footer is
  passed to `chat.stopStream` as `blocks`. Measured 2026-09-29 (iPhone locked, Slack open in a
  browser, channel on Just mentions): a stream pushes once, when it stops, with its first text as
  the banner. So a reply rings once when it ends, in place of once in a separate closing message,
  and a reply that runs past `sinks.STREAM_SECONDS` (280 seconds, since Slack closes a stream at
  5 minutes) stops its stream, which rings, goes on in the same message with `chat.update`, which
  does not, and ends with a closing message, a second push. A reply past 12,000 characters or 50
  cards continues in a new message, and each extra message rings. The three stream calls are
  never retried after a connection reset (`sinks.ConnectionRetryUnlessStream`), since they are
  not idempotent.
- `!stop`, a restart and an error end a reply through the same path as a normal end: the stream
  stops with the footer (a push), and the root shows ✅ or ❌ as before. A message queued in a
  thread when a restart drops it gets no reply of its own: the end of the running reply, or one
  message when nothing runs, says `N messages were not sent because code-with-slack restarted:
  send them again.` with the start of each (`sessions.not_sent`).
- Crash repair stops each open reply's stream first (`message_not_in_streaming_state` is fine),
  then edits the message: a card left running becomes an error, and `code-with-slack stopped
  before this answer.` is appended. Nothing is posted. `open_replies` in `state.json` now holds
  the ts of the reply's stream or message.
- `docs/features.md` and `docs/setup.md` describe the notification behaviour above: one push when
  a reply ends, a second for a reply past about 4 minutes 40 seconds.

- Where the daemon's own answers go. A word typed in the channel, or in a thread that holds no
  session, is answered by a normal post in the channel (never a thread reply, never ephemeral,
  so it stays after a reload): `!help`, `!guide`, `!status`, `!stop`, `!bind` with its list and
  each answer, the `!bypass` refusal, `!resume` with its list and each refusal. A word typed
  inside a session's thread is answered by an ephemeral message under it (Slack shows `Only
  visible to you`, and it disappears on reload), with a ✅ on the word too for `!bypass`; the D5
  old-folder notice and `Not sent.` are ephemeral too. None of these rings a phone.
- `!resume`: the Resume button carries the session id and the thread of the owner's `!resume`
  message. A click, or a name, resumes the session in that thread and edits the list to say what
  was resumed and where, instead of opening a new thread. A list posted by an earlier version
  (a button holding a bare session id) answers that it is out of date.
- `!stop` shows ✅ on the stopped session's root, for a thread stop and for each session a channel
  `!stop` stops, instead of ❌: a stop the owner gave is not an error. ❌ stays for errors and for
  a restart that cuts work short. `!stop` inside a thread with nothing running answers `Nothing
  is running in this session.` as an ephemeral message.
- `!bypass`, `!stop`, `!status`, `!bind` and `!help` answer differently at the top level than
  inside a session's thread: `!bypass on`/`off` is per thread and refused at the top level;
  `!stop` and `!status` act on every session of the channel at the top level and on one session
  inside its thread; `!bind` works only at the top level, refused inside a thread; `!help` lists
  the daemon's words everywhere and a session's own commands only inside its thread.
- Approvals and questions are resolved only in the channel and thread they were posted in,
  instead of the channel alone, and `!stop` denies only the pending requests of the session it
  stops.
- The footer is a closing message of its own, posted in the session's thread when the reply ends.
  A reply that ends below a newer one keeps its closing message, now a bare line with no text of
  its own, since a new message in the thread is what notifies.
- An Edit or Write diff shows whole, as the terminal shows it, in a collapsible full-width
  container closed by default: the call's line is its title and `Added … lines, removed … lines`
  its subtitle. It no longer stops at 20 lines.
- code-with-slack's own notices (the answer to `!bind`, `!bypass` and `!stop`, a restart, a
  refused attachment, the ephemeral errors, and the lines of the `!bind` and `!resume` lists)
  are a context block, small and grey as the footer, so they read apart from Claude's replies.
  `!help`, `!guide`, `!status` and the answer to a resume stay full size.
- The footer's order: model and effort, the folder, branch and changes, then tokens, context
  and limits; `!status` lists its values in the same order. The folder shows by its name alone,
  its whole path staying on `!status`, and the labels (`effort`, `tok`, `ctx`, `5h`, `7d`) are
  bold.
- A reply's closing message, and the notification it carries, now wait for every task it started
  and for the turn Claude Code starts to report one, instead of posting the moment the turn ends;
  that report turn renders into the same reply, appended after its body, rather than opening one
  of its own. `!stop`, a restart, an idle close and a lost session close a reply at once instead
  of waiting further, with no message of its own: its footer, if any, joins the body's own last
  message with an edit instead, since a new message would still ring whatever it said and an
  edit never does. A process that exits still rings once, with a new closing message, on the
  first owner reply it ends.

### Fixed

- A Claude Code process lost while no turn is active is told where it matters. With a
  background task of the session still running, the reply that waited for it closed with its
  footer as if all had ended well, and the ❌ on the root was the only sign: the reply now
  ends with `Claude Code reported an error: …`, the line a turn cut short already gets
  (`ThreadSession._abandon`). With nothing running and nothing waiting, the root of a reply
  that had ended well turned to ❌: it now keeps its reaction and nothing is written, since
  the next message connects a new process. Seen live on 2026-10-10, both cases (#202).
- A thread reply sent with **Also send to #channel** is a prompt of its thread. It was dropped
  with no answer: Slack delivers it as subtype `thread_broadcast`, and
  `guards.is_prompt_message` took `file_share` as the only subtype a person's message can have.
  The event has no `team`, so `guards.message_actor` now takes the envelope as well and reads
  the workspace from its `team_id`, only for this subtype and only when the envelope's
  `is_ext_shared_channel` is `false`. Recorded payloads:
  `tests/fixtures/slack/200-event_callback-thread_broadcast.json` and the hidden
  `message_changed` that follows it (#73).
- The probe's claim on stopping a background command (P12) reads the card's status. It read the
  card's details, and a streamed card keeps the details an earlier update gave it: `Running in
  background` stays on a command that has ended, so the claim was left unproven although the
  command was stopped (three runs on 2026-10-09, Claude Code 2.1.292: the process gone, the card
  complete with `Stopped`).
- An owner's prompt that crosses a background task's report is answered in its own reply
  (#205). The session decided whose turn was starting from what it was waiting for, and learned
  the answer from the result's origin when the turn had already been written: a report turn
  could take the reply of a waiting prompt, and a prompt's turn could land in the reply of the
  task. Claude Code replays a prompt of plain text before its turn's first words and replays
  nothing before a report turn (every turn of the `prompt-replay-*`, `compact` and
  `auto-compact*` fixtures, CLI 2.1.286 and 2.1.292), so `ThreadSession._whose_turn` now reads
  the replay: it names the prompt of the turn that starts, and with a notification waiting a
  turn that opens with no replay is the report. The correction at the turn's end stays, for the
  turns no recording covers: one that opens with a compaction, a command, a prompt with an
  image, a turn that fails before its first word.
- The setup guide and the docstring of `code_with_slack.trust` say what a trusted parent folder
  leaves out (#9). Outside a repository a parent's trust lets a session start, and Claude Code
  still holds the `permissions.allow` rules and `additionalDirectories` of the folder's own
  `.claude/settings.json` until the dialog is accepted in that folder, which is why the terminal
  shows the dialog again in a folder used from Slack. The docstring said those rules applied at
  once in an SDK session. No change in behaviour.
- The line on how a cut-short reply ended reaches the message that notifies in three more
  cases (#165). A report turn cut short in a reply that already had the footer of its first
  turn ended with that footer alone, the line left in a silent edit: the line now moves with
  the footer under it. A reply that held only a line of the daemon's above the line ended
  with a message that showed empty: the line now moves and the other stays. A reply whose
  full message had pushed the line, or the note under it, into a continuation got an
  empty-looking closing message after it, a third ring: the continuation is now the ending
  (`ReplySink._opens_on_ending`). Not covered: a line that does not fit the room left in a
  message is cut there, as any text is, and the continuation rings with the rest of it. The
  empty-looking message remains in one known case, a turn that ended well with an answer of
  text alone whose footer could not be built, where it is the only notification of the end.
- A turn cut short after its reply's stream stopped ends with the error line as a new message,
  where it ended with a message that showed empty (#165). Past 280 seconds, or after an append
  Slack refused, a reply's end is a new message, the one that notifies. A turn that lost its
  Claude Code process has no footer, and when its last part was a tool call, or its answer was
  text alone, there was no ending to move: the new message held one zero-width space, its
  notification named a tool call or the answer's first words, and the error reached the
  reply by an edit, which never rings. Seen live on 2026-10-08 with a process killed 320
  seconds into a turn. The line `ThreadSession._abandon` writes on how a reply ended is now a
  part of its own in the sink (`TurnRenderer.feed_ending`, `ReplySink.text` with `ending`), and `ReplySink._ending_cursor`
  takes it as the ending when there is no footer to post. It is the notification's text ahead
  of a tool's title. The same holds for `This reply ended before an answer: …`, after a
  restart of the daemon or a closed session. A notice written just before the line stays in
  the reply, a card that closes after it moves under it, and no other line of the daemon's
  moves. The running list is no footer: a turn cut while a subagent works closes with
  `⏳ 1 agent` still showing, and ends with the line too. A reply that ends with its footer is
  unchanged: a line of the daemon's stays where it was written. Not covered: a process lost
  while no turn is active writes no line at all; a reply cut short after text Claude wrote
  past its last call still rings with the start of that text.
- A message counts its text as the blocks Slack makes of it, so a reply with many headings or
  tables continues in a new message where an edit of it was refused (#92). Slack translates a
  `markdown` block, and a stream's text, into several stored blocks: a `header` per heading, a
  `table` per table, a `divider` per rule, and `rich_text` for each run of anything else between
  them. `chat.update` and `chat.postMessage` answer `invalid_blocks` with
  `no more than 50 items allowed` when a message passes 50 of them; a stream stored 90 with no
  refusal, and the update that follows its stop is held to the 50 (measured 2026-10-08 in a
  private test channel, slack-sdk 3.45.0; the markdown block reference says only that one block
  "may result in multiple blocks after translation"). The daemon counted a text as one block.
  The owner's log holds six such refusals on 2026-10-07: one message of 17 blocks by the
  daemon's count was 51 by Slack's, and each refused edit was dropped. `sinks.markdown_starts`
  now finds the blocks of a text, and a stream's plan, a post and an update all count them
  toward `BLOCKS_LIMIT` and cut a text at the line that would start one block too many. A
  heading is never the last block before the cut: it opens the next message with the text it
  heads (`sinks.markdown_cut`; in the first run on the owner's daemon, 40 sections were cut
  between the 23rd heading and its sentence). For
  the five texts of that message the count gives what Slack stored (7, 7, 9, 10 and 6), and the
  turn replayed through the sink stays at 45 or under in every write, 67 of which were sent to
  Slack and accepted. When Slack still refuses a write of the reply's last message with that sentence
  while the reply is being written, the message's room is halved and the write is tried again,
  so the rest of the reply goes on in a new message where the change was dropped
  (`ReplySink._tighten`). A message that already has a successor, and the writes of the reply's
  end (the fold of the cards, the ending's post), are not split and drop the change as before:
  the text the message showed stays. A refused write's log line now names where
  in the payload Slack pointed and whether it counted too many blocks, never Slack's sentence.
  Not measured: markdown shapes beyond the sixteen in `test_sinks` (seven more are counted as
  CommonMark reads them), and how a reply cut between two sections reads in a Slack client.
- A streamed card is sent each line of its text once, and its text counts toward the message's
  size (#92). Slack adds the `details` and the `output` of every `task_update` to what the card
  already holds, and an update that carries neither leaves them (measured 2026-10-01 with
  slack-sdk 3.44.1 and 2026-10-08 with slack-sdk 3.45.0, in a private test channel). The daemon
  sent a subagent's last ten lines with every nested call, so the stored card held each line up
  to ten times, joined with no line break between two updates. The owner's log holds 16
  `chat.appendStream` refusals from 2026-10-02 to 2026-10-06, 8 of them in messages of at most
  4 cards. `sinks.card_addition` now
  sends a stream only the lines the card lacks (`sinks.lacking`), a line break first, and
  no text when the card says nothing new. `ReplySink._plan_card` counts each card
  toward `MESSAGE_LIMIT`: its title, the text it was sent, and a fixed cost for the card, for
  each of its two texts and for each line (`CARD_COST`, `CARD_FIELD_COST`, `CARD_LINE_COST`).
  Replayed over the 15 streams measured on 2026-10-01, that count is 13,514 at most in an
  accepted append and 13,801 at least in a refused one, and `MESSAGE_LIMIT` is 11,000. A new
  card or new text that does not fit continues in a new message; a card already in a full
  message keeps its title and status up to date, gains no more lines of details, and still
  gets its output when it ends. Slack's cap stays
  undocumented and follows what Slack stores, so text heavy with formatting can still be
  refused below the count; that case goes on by `chat.update` as before. Not checked in a
  Slack client: how a card that holds every line of a long subagent run reads.
- A compaction that comes before its turn's first message shows its line (#169). On `!compact`,
  and when Claude Code compacts on its own as a turn begins, the `compact_boundary` frame arrives
  before any frame that starts a turn, and `ThreadSession._dispatch` dropped it: a `!compact` was
  answered `_Done. Claude Code returned no text._` and an automatic compaction showed nothing.
  The boundary now starts the turn, so the reply reads
  `Compacted the conversation: 20.7k → 5.0k tokens.`; a compaction among a turn's tool calls
  already showed. Recorded on 2026-10-08 with claude-agent-sdk 0.2.164 (bundled CLI 2.1.292) and
  the daemon's CLI arguments: `tests/fixtures/sdk/compact.jsonl` (a `/compact` session) and
  `auto-compact.jsonl` (the one turn of a longer session in which Claude Code compacted at 67.9k
  tokens, with `CLAUDE_CODE_AUTO_COMPACT_WINDOW=100000`), replayed in
  `tests/test_sessions_compaction.py`. A boundary with no prompt sent and no report awaited
  starts no turn. Probe claim P21 sends `/compact` on every new release and reads the line in
  the reply.

- A `!word` sent in Slack's formatting is read as the word (#178). A command pasted from a place
  that showed it as code kept the formatting, the event's `text` then started with a backtick,
  and `parse_bang` took the message for a prompt with no word about it: a `!goal` set no goal, a
  `!stop` would have started a turn. The rule is now the first character, whatever the
  formatting. `commands.unformatted` reads the run the message opens with in the `rich_text`
  block Slack's composer sends and, when it starts with `!`, takes the marks around it out of the
  text; the arguments stay as sent. Read back from Slack on 2026-10-07: a message in inline code
  holds one `text` element with `"style": {"code": true}` and its text opens and closes with a
  backtick; a code block is a `rich_text_preformatted` part and its text opens and closes with
  three. Bold, italic and strikethrough follow the Block Kit reference and were not read from a
  real message. Anything before the `!` keeps a message a prompt (`\!goal`); so do a quote, a
  list, and a message with no composer block. A message that starts with a command meant as a
  quotation now runs it, `!stop` and `!bypass on` alone in inline code included.
- A command's word ends at any whitespace. `!compact` followed by a line break and notes was
  read as one unknown word and sent as text; it now runs `/compact` with the notes as arguments.
- The reply to `!goal` opens with Claude Code's own `Goal set: <condition>` line (#113). The line
  is an assistant message that no stream event announces, and the renderer wrote a command's
  output only when the reply held no other text, so a goal's first inner turn hid it.
  `TurnRenderer` now keeps the id of every message a `message_start` event announces, and writes
  the text of a top-level assistant message whose `message_id` is not among them, a paragraph
  apart from text before it. In the 37 recorded streams the rule matches three messages with no
  error: this line, and the output of `/usage` and of a forked skill, which read as before. The
  rule stands down where it could write a text twice, in two cases no recording shows: while a
  streamed message has had no `message_stop`, and once a `message_start` named no id. Probe claim
  P20 sets a goal on every new release and reads the line in the reply. The
  evaluator's verdicts and the end of the goal are still not shown: a verdict reaches the stream
  only as the text of a user message (`Stop hook feedback:`), which no reference describes, and
  nothing in the stream says a goal was achieved (measured 2026-10-07, Claude Code 2.1.292).

- An idle row of a channel's `!status` dates its session by the session's last message. It
  read the time of the session's file (`SDKSessionInfo.last_modified`), and Claude Code appends
  entries with no timestamp to a transcript when it connects the session again: on 2026-10-07 a
  session idle for 16 minutes read `idle · just now` after a restart. `channel_status` now dates
  the sessions it shows with `SessionManager.dated`, the reading of the transcript's end that
  already orders the list of `!resume`, and reads no other session's file. A row cannot read
  more than an hour: a live session is closed after `IDLE_CLOSE_SECONDS` idle, and only live
  sessions are listed.
- A reply is no longer ended while the turn that writes it still runs (issue #149). A command a
  subagent starts and that outlives the subagent's Bash call becomes a task of the reply, and
  when it ended the task branch of `ThreadSession._dispatch` ended that reply without checking
  that its turn was over: the stream stopped with no footer, or, when Slack had already stopped
  the stream, a closing message with nothing in it was posted, and the turn's own end then
  wrote no footer. The branch now leaves the active turn's reply to that turn's
  `_close_reply`, as `_sweep_closed_out` does. Replayed in `tests/test_sessions.py` and
  `tests/test_sessions_stream.py` from the recorded `subagent-nested-background.jsonl`, with
  the turn's last text and result moved after the command's end.
- An agent continued with `SendMessage` in a daemon restarted since the agent's first run no
  longer starts a turn of its own (issue #150). Its frames name the `Agent` call that first
  started it, which the restarted daemon never saw, so `ThreadSession._dispatch` opened a turn
  with no prompt behind it. While that turn was active the agent's notification did not hold the
  owner's next prompt, which went to Claude Code during the turn that reports the agent, was
  taken into it and got no result of its own: the session read busy from then on, a restart
  waited on it for `DRAIN_LIMIT_SECONDS`, and `!stop` interrupted an idle client. A subagent's
  frame that no reply holds, with no turn running, now starts no turn. Replayed in
  `test_sessions_report_turn` from `report-turn-agent-resume` (SDK 0.2.163, CLI 2.1.286). A
  prompt that reaches Claude Code during a report turn by another way is released by the next
  entry.
- A prompt Claude Code takes into a turn it runs to report a background task is released when
  that turn ends, and the reply says so (issue #150). Every session now passes
  `--replay-user-messages` and sends each prompt as one user message under a uuid of its own
  (`Turn.uuid`; `prompt.user_message` takes the text of a string prompt too). Claude Code
  re-emits the prompt under that uuid: inside the running turn when it took the prompt in, and
  after the `init` of the turn it starts for the prompt otherwise. A replay inside a running turn
  is recorded (`ThreadSession._acknowledge`, `ActiveTurn.taken`) and, when that turn ends with an
  injected origin, the prompt leaves `_sent` instead of waiting for a result that never comes:
  the session stops reading busy, a restart no longer waits on it and `!stop` has nothing to
  stop. The reply ends with `Claude Code took your message into this reply: send it again if it
  is not answered here.` A prompt that was not taken in keeps its own turn, and a stream with no
  replay behaves as before. A taken prompt is also released, with the note, when `!stop` ends
  the turn with an injected result; when the session is abandoned (`_abandon`) it is listed among
  the messages not sent. Measured on 2026-10-06 with claude-agent-sdk 0.2.163 (CLI 2.1.286,
  Haiku, one run per scene); replayed in `test_sessions_prompt_replay` from
  `prompt-replay-during-tool`, `-at-init`, `-after-tools` and `-stop-queued`. Probe claim P19
  (the replay, with the uuid sent, comes before the first stream event of its turn) holds on
  0.2.163, certified again on 2026-10-06 with all 19 claims. The probe's stand-in for
  `trusted_repository` takes the session folder the daemon has passed since #48, without which
  every footer of the probe failed, and its image scene asks for an English word, since the
  owner's settings load there.
- The footer and `!status` run git only on a repository the owner trusted in Claude Code, and
  leave the branch and the changes out anywhere else (`footer.git_state`, new;
  `footer.git_branch` and `footer.git_changes` are gone). The diff ran in whatever folder the
  session had moved to, under that folder's own config, and `git diff-files` runs the `clean`
  filter a repository's config names: a repository delivered in an archive or laid out as a
  bare repository inside a clone ran its command with no approval asked. git is now given the
  repository with `--git-dir` and never searches for one from the folder, and
  `--ignore-submodules=dirty` keeps it out of a nested repository the index names as a gitlink.
  That flag changes one count: a submodule set to `ignore = all` whose checked-out commit
  differs from the recorded one adds one line each way. Anywhere inside a repository's own
  `.git` directory the branch alone shows, also in a submodule's git dir under `.git/modules`,
  where the changes showed too. The git calls of one footer share `GIT_TIMEOUT`, where each had
  its own.
- The trust check reads which repository a folder belongs to from the filesystem
  (`trust.locate`, `trust.Repository`, `trust.trusted_repository`, new; `trust.repository_root`
  and `trust.GitUnavailable` are gone) and runs no git. It took the answer from git run in the
  folder, so a folder whose `.git` file, `.git` symlink, `commondir` or `core.worktree` named a
  repository the owner had trusted passed for that repository, and a session started there with
  the folder's own hooks and settings. A folder holding a `.git` entry is keyed on itself; a
  linked worktree is keyed on its main checkout only when that checkout registers the folder.
  What else changes at a session's start and in `!bind`: a `.git` file or symlink that names no
  git dir no longer lets a trusted parent cover the folder; a folder holding entries named as a
  git dir's are (`HEAD` with `objects` and `refs`, or with `commondir`) is untrusted with all
  below it, also when git itself rejects it and also inside a trusted repository; a FIFO in a
  folder's git metadata no longer holds the check; a folder outside git reached by a path in
  another case is the trusted folder it is on disk; a worktree whose folder was moved by hand
  is untrusted until `git worktree repair`; and the check answers the same on a machine with
  no git.
- A reply's banner is computed in a time linear in its first paragraph (`strip_markdown`). Three
  of its patterns read ahead and were tried again from every later start, so a paragraph that was
  one long run of `[`, of blank lines or of `_` inside a word held the event loop, and with it
  every other thread's reply and approval: about 15 s for 100,000 `[` characters (Python 3.12.13,
  measured 2026-10-04). The banner's text is unchanged.
- A long command a background subagent runs in the foreground of its own context is the
  subagent's work (`TurnRenderer.nests`). Claude Code starts a task for it on the main
  conversation's stream (recorded: Claude Code 2.1.286, `subagent-nested-command.jsonl`) and
  reports its end to the subagent, so no turn follows it. The session took every such end for a
  notification that a turn would report: it held the owner's next prompt for
  `INJECTED_TURN_WAIT`, logged `no turn followed a task notification` once the wait passed, and
  put an end line for the command at the top of the agent's report. The reply also gave the
  command a card beside the agent's, and counted it as a running task. A task started by a call
  that runs inside another call is now held aside while that call is open: when it ends before
  the call's result it has no card, record, end line, wait or running count of its own, and shows
  on its root's card through the call count. When it is still running at the call's result (a
  command the subagent put in the background, recorded in `subagent-nested-background.jsonl`) it
  outlives the call and is an ordinary background task from then on: its own line, counted
  as a running shell, reached by `!stop`, kept in the reply's footer and awaited by a restart
  (`TurnRenderer.take_promoted`). A task of a call no tracked reply holds (a restart dropped it)
  is still treated as any other task.
- A task notification that arrives while `ThreadSession._expire_injected_turn` is writing (a
  background update for held task frames, or the sweep that ends replies) gets its own wait
  for a report turn once that work ends. `_expect_injected_turn` found the working task still
  running and armed no timer, then the task's end marked the session settled: the owner's next
  prompt was sent to Claude Code while a report turn was still expected, ✅ showed, and (when the
  notification arrived during the sweep) nothing would ever clear the expectation, which held
  every later prompt. The session stays unsettled, the sweep stops ahead of the replies the
  report may render into, and a fresh timer is armed, whatever the sweep does and unless the
  session is closing; a close cancels it like the first.

- An end of a reply that began always resolves (`ReplySink.close_out`, `wait_landed`). A caller
  cancelled while the end's write was out (a report turn starting while
  `ThreadSession._expire_injected_turn` was writing, a close) left the reply neither landed nor
  scheduled for its retry: the session kept it in `_unlanded` for good, so ✅ was never shown
  and the root of an idle session stayed on ⏳ until the session closed. The end now runs as a
  task the caller's cancellation does not stop, which resolves `wait_landed` (with the one
  retry when Slack refused the write, or `False` when the end raised), and `settle` waits for
  it before cancelling the retry, so no write goes out after a shutdown's last pass.

- A reply that has ended never opens a message below its footer or its closing message (issue
  #51). A preview, a card or words that arrived late used to open a new message when the last
  one was full. Once the end has landed on Slack (the last stream stopped with the footer, the
  ending was posted, or the closing message was posted) the reply's messages are a fixed set and
  a late update only edits them. Late content is all or nothing: when the last message holds it
  with everything it showed, it is shown as before; when it does not, the message goes back to
  what it showed when the end landed (`ReplySink._hold`, `_blocks`) and none of the late
  content appears, not even the part that would fit. A card that only changes state or title
  still updates in place, and a late preview of a card that was shown is replaced by the
  existing `Preview left out` note when a block is free for it. A close whose write failed (a
  stream stop, a post, the closing message) has not landed, so its retry, and any update before
  it, still opens the messages the reply needs.
- Two text blocks with no tool call between them are a paragraph apart (issue #113, which stays
  open for the `Goal set` line and the evaluator's verdicts). The three inner turns of a `/goal`
  that each answered `tick` were written as `tickticktick`; a Stop hook that makes a turn go on
  has the same shape. The stream announces each block with a `content_block_start` event, and
  `TurnRenderer.feed` now puts `\n\n` before the first text of a block that follows Claude's
  text directly. A new tool card, a notice, a block that stays empty, a thinking block and a
  subagent's text change nothing. A card that changes where it sits (a background task that
  ends above the text) separates nothing, so the text of the turn that reports the task is a
  paragraph apart too. Recorded fixtures: `tests/fixtures/sdk/goal.jsonl`, `background.jsonl`.
- The Answer form shows every word of an option (issue #47). Slack caps an option object's text
  and description at 75 characters each, so a longer description ended in `…` mid-sentence, and
  an option's `preview` was never shown. When an option of a question says more than a choice
  holds, the page now shows the question in bold and each option whole above the choice: its
  label in bold and its full description in a `rich_text` block, its preview in a preformatted
  element of the same block (`approvals.question_view`, `approvals._option_whole`). The choice keeps the labels alone,
  titled with the question's header. A question whose options all fit keeps the form it had.
  The input shape with a `preview` was recorded on Claude Code 2.1.286
  (`tests/fixtures/sdk/ask-preview-can-use-tool.json`).
- A restart that lands at the end of a turn no longer leaves both ⏳ and ✅ on the thread's root
  message (issue #104). `StatusReaction.show` changes the reaction with two calls, and the
  session read idle between them, so the drain closed it and the close cancelled the reader
  before `reactions.remove`. A `StatusReaction` whose change is cancelled now reads as a fresh
  instance, so its next change strips every other name, and it records the state asked for
  last: `ThreadSession.close` calls `StatusReaction.settle` for a session it closes idle, which
  makes that change and makes no call when the root already shows it. A close that cuts a
  prompt short in the same window ends on ❌ alone.
- `!help` inside a session's thread no longer lists `!clear`, which a thread refuses (issue
  #76). `commands.help_text` leaves out what `commands.refused_in_thread` returns, the names
  `slack_app.is_clear` refuses.
- `!reset` and `!new` are refused inside a session's thread as `!clear` is: Claude Code gives
  them as aliases of `/clear`, which starts a new session, and they reached it from a thread.
  `commands.refused_in_thread` returns `commands.NEW_SESSION_NAMES` (`clear`, `reset`, `new`),
  known before a session rebuilt after a restart has connected, with any other alias the
  session's own command list gives `clear`.
- `!login` and `!logout` are never sent to Claude Code, from the channel or from a thread. They
  act on the host's own login, which the daemon and every session run on, and they went through
  as `/login` and `/logout`. `commands.host_only` gives each its answer, `texts.LOGIN_ON_HOST`
  or `texts.LOGOUT_ON_HOST`, which says to run it in `claude` on the host; no session starts.
- A streamed reply whose append Slack refuses with `msg_too_long` no longer loses its end and no
  longer leaves ❌ on a thread whose turn ended well (issues #92 and #96). The same append was
  sent again on every write and refused each time, the end and its one retry included, so the
  session showed ❌; the reply became whole only when its stream reached `STREAM_SECONDS`, and the
  ❌ stayed. `ReplySink._stream_step` now stops the stream on that refusal alone, without the footer,
  and the message goes on by `chat.update`, with a closing message at the end, as for a reply
  past `STREAM_SECONDS`: the stop notifies, and the closing message notifies a second time. The
  refusal is logged as `chat.appendStream refused` with the text size, the element and card
  counts and the card text sent so far, never content. When the update of that message is
  refused as well and no later one passes, the end counts as not landed and the root shows ❌,
  since the reply is short of what Claude wrote. The card text is still not counted
  toward a message's size: Slack's formula is not established.
- A restart ordered from a Slack session no longer waits for that session's own background wait
  for the new process (issue #87). The signal names no sender, so every session with a turn
  running when it arrives is treated as a possible sender: its turn is waited for, a background
  task it starts after the signal is not, and the shutdown ends it. Every other background task is
  still waited for, and the drain notice no longer counts the ones it skips. A session whose only
  unfinished work at the shutdown is such a task ends with ✅ on its root, not ❌.
- A top-level word whose failure report itself fails no longer pushes `ERROR_REPLY` as a reply
  under the word; the failure is logged. A Resume click edits its list before it posts the
  confirmation, so a failing confirmation no longer leaves buttons for a session already
  resumed, and the list's line no longer italicises a title containing `_`.

- The footer's branch read the channel's folder, not the folder the session works in: a channel
  bound to a folder holding its repo one level down showed no branch, and a session that moved
  to a worktree showed the old one (#37). The branch and the changes now follow the `cwd` Claude
  Code reports after each tool and at the end of each turn, while the folder shown stays the
  channel's, and
  `!status` names the session's folder when it is not the channel's.

- A background task that never ends (a dev server, a watcher) held a restart for the full 29
  minutes, with nothing in Slack saying why. During a restart, a channel left with only background
  tasks now gets one message naming them by the footer's counts, and `!stop` stops them, so the
  restart goes on.
- A `!bind` or a Resume while `!help`, `!bypass` or `!status` was starting the channel's Claude
  Code left that process running, and after a Resume two processes could run one session (#15).
  The old session now waits for the start, then closes the process; the word answers that the
  channel changed meanwhile, and a `!bypass on` caught this way is not stored for the new folder.
- A `!bind` to a folder Claude Code has not trusted, typed or clicked, answered that the next
  message starts a session there, and the message then got the trust refusal (#14). The channel
  is still bound, and the answer now says no session can start there yet and why: the same
  reason, from the same checks, a message would get (untrusted, unreadable or missing).
- A long command in the foreground no longer shows "Running in background" while it runs, and
  folds into the counts when it ends. Claude Code starts a task for such a command too; a call's
  line now becomes a task's line only when its result arrives while its task still runs. The
  new `foreground` stream in `tests/fixtures/sdk/` records one.
- A reply whose turn ended while the daemon was stopping could keep `Claude is writing…` and no
  footer (#25). The turn counted as ended before its reply was closed, while the footer was still
  being read, so the stop saw the channel idle and the process exited before the reply's final
  write. A turn now stays active until its reply is closed.
- A skill that runs in a forked context, typed as a command (`!review`), showed nothing but
  `Claude is writing…` while it worked, then its line once it had ended. Its task starts before
  the turn's first message and was held until then; the owner's turn now starts at a task that
  no call started, so its line (`⏳ /review`) shows while it works. Its agent's calls are not
  streamed by Claude Code in this case, so the line carries no call count. The new
  `skill-fork-command` stream in `tests/fixtures/sdk/` records it. A task started by a call
  inside a background subagent now goes to the reply that holds that subagent. An agent the
  command starts inside it shows on the command's line (`⏳ /review · 1 call`, then `⎿ Run the tests`)
  instead of a second running line.
- D6 minimum: a Resume click or `!resume <id or name>` that names a session already open in
  another thread of any channel is refused, with a new text, instead of resuming it a second
  time; the `!resume` list marks such a session and gives it no button. The session id is
  recorded as soon as Claude Code reports it (its first message), not only at the end of its
  first turn, so a still-running first turn is already covered by this refusal too.
- A resumed session whose transcript turned out gone left its worker task, and any armed
  idle-close timer, running forever after the close: `asyncio.all_tasks()` kept growing. A turn
  that raced the close during its own `sink.open` gets the "session gone" reply when its own
  thread's entry is gone too, or "session closed" otherwise (an idle close or a restart can also
  close mid-`sink.open`, and only a gone thread's entry tells the two apart). A turn a direct
  call (`!status`, `!bypass`) had already taken from the queue, when that same call then finds
  the session gone, is rescued the same way, instead of being left saying "writing" forever.
- The stale wording "the channel was bound to another folder or resumed another session" for a
  session closed under a running command is gone (neither still happens): it now says the
  session closed while the command ran, from an idle close or a restart. When a prompt's retry
  after such a close finds the thread's own entry gone too (not just closed), it now says the
  session is gone, instead of implying a retry would help.
- `close_all` (a restart, or the daemon stopping) stopped closing sessions at the first one
  whose `close()` raised, leaving the channel's other sessions never closed. Each is now closed
  under its own `try`/`except`, logged by id and skipped on failure, before the call waits for
  every close in flight to finish.
- Pruning stale threads on start treated a session the SDK's `list_sessions` filters out
  (sidechain or metadata-only) as gone even when its transcript file exists, and could drop a
  thread that should have survived. Pruning now also checks the transcript file itself.
- `!status` at the top level fetched each live session's permalink one after another; it now
  fetches them at once, in the same order.
- A level set with `!effort` showed as unknown right after a reconnect (an idle close or a
  restart), until the first turn ended and Claude Code reported one. The footer and `!status` now
  show the requested level at once, until Claude Code's own report corrects it.

### Changed

- Tool lines read closer to the terminal (Claude Code 2.1.283, measured 2026-09-27). Finished
  calls fold in the terminal's words for Bash and Read (`Ran 2 shell commands · Read 1 file`), by
  name for any other tool. A finished `Edit` or `Write` no longer folds: it shows as `Update(path)`
  or `Write(path)` with `Added N lines, removed M lines` or `Wrote N lines to path`, and a code
  block with the numbered diff (its first 20 lines), or a new file's first 10 lines. An answered question stays in the
  channel as `User answered Claude's questions:` with each answer, instead of being deleted.
- `!stop` also stops the channel's background commands and agents (`ClaudeSDKClient.stop_task`),
  besides the running turn and its pending approvals, and answers
  `Stopped what was running in this channel.` Claude Code starts no turn to report a task stopped
  this way, so neither a restart nor the channel's next prompt waits 30 seconds for one.
- Bypass is kept in `state.json`, one `bypass` field per channel, so a restart of the daemon no
  longer turns it off (#20). On `SIGTERM` every channel with bypass on gets a line saying that it
  stays on. `!resume` keeps bypass on, as the terminal's `/resume` does; before, it turned it off
  with no word. A `!bind` that ends a session in bypass says in its answer that bypass is off.
  A `state.json` written before this change loads with bypass off.
- Each row of the `!resume` list ends with the first 8 characters of the session's id, and
  `!resume <id>` accepts that start (or any longer one) as well as the full id. The line under a
  long list names `!resume <id>` and no longer says that `claude --resume` in the terminal lists
  every session.
- On `SIGTERM` the daemon lets the turns, background commands and agents already running finish
  before it exits, for up to 29 minutes after `launchctl kill TERM`, and up to the LaunchAgent's
  `ExitTimeOut` (60 seconds, launchd's cap) after `launchctl bootout`. A new prompt meanwhile, or a
  queued one, is answered with a request to send it again; an approval or a question asked meanwhile
  stays open; a second signal, or `SIGINT`, stops without waiting. The documented restart command is
  `launchctl kill TERM`, which returns at once, so a session running from Slack can restart the
  daemon and still finish its turn.
- `claude-agent-sdk` 0.2.160, which bundles Claude Code 2.1.283 (was 0.2.158 with 2.1.280). The
  SDK streams in `tests/fixtures/sdk/` are recorded again on 2.1.283; the subagent stream now runs
  to the agent's end instead of stopping at the first result.
- Tool calls fold while a turn runs too, not only once the reply is final: after each piece of
  Claude's text, one line of tool names and counts, updated as calls end (#11). Failed calls are
  counted in the same line after `✗` (`✓ Bash ×3 · Read · ✗ Bash`), in the final reply as well,
  where each had a line with its command and the first line of its output. A running call, a
  task and a stopped call keep a line of their own below the counts, and so does the latest
  call while Claude is still working after it. The `tool-error` stream in
  `tests/fixtures/sdk/` is recorded again with a Bash command that exits 1.
- A subagent's line counts the calls it made (`· 12 calls`), while it runs and once it ends.
  What a running line is doing now (a subagent's latest call, a command's latest agent,
  `Running in background`) shows on a line below it, indented under `⎿` as in the terminal,
  instead of after a `·` on the same line.
- Every line of something still running (a call, a subagent, a task, a skill) is marked `⏳`,
  as the footer marks running tasks, in place of `…`, which did not read as running.

### Added

First release.

- `docs/features.md`: every feature the owner sees, with the tests, the probe claims and the
  checks by hand that cover it. `tests/test_features.py` keeps its probe column in step with
  `probe/claims.py`, and the probe prints its By hand column after every run.
- A probe for new `claude-agent-sdk` releases, `uv run python -m probe`, described in
  `CONTRIBUTING.md`. It drives the daemon's `SessionManager` against the real bundled CLI, with
  Slack faked, through thirteen claims: a session starts, `!status`, a text, image and file prompt,
  `!resume`, `!stop` on a turn and on a background command, a Bash call's line, approvals, bypass,
  and the Edit and Write previews. A release is certified in `probe/certified-versions.json` when
  every claim the probe causes itself holds or is retired.
  The SDK release watch's issue now asks for the probe instead of the checks by hand.
- Package scaffold: `pyproject.toml` with pinned dependencies, MIT license, docs test.
- CI (sensitive-data scan; tests, types, lint), the published text scan, Dependabot for uv and
  GitHub Actions.
- Configuration loading from `~/.config/code-with-slack/.env` with the mode 600 check.
- `state.json` with atomic writes, and a single-instance lock on the configuration directory.
- Identity and channel guards on every inbound path: messages, buttons and form submissions.
- The `code-with-slack` entry point, started by a user LaunchAgent.
- One Claude Code session per channel with a turn queue, resume after a restart, stale-session
  recovery, bypass held in memory, and stop. Shutting down or rebinding a channel ends every
  waiting reply with the reason.
- Commands typed as `!word` messages: `!help [text]` lists the daemon's words and the session's
  Claude Code commands, filtered by the text when one is given, each description shown as
  written; `!bind`, `!bypass`, `!status`
  and `!stop`. Any other `!name` is sent to Claude Code as `/name`. The app registers no slash
  command.
- Replies in the channel's main window: one message per reply, rewritten about once a second,
  with a `Claude is writing…` or `Waiting for the previous reply…` status until the reply ends,
  and a continuation message past Slack's size limit. Claude's text is markdown; each tool call
  is a line of small grey text where it happens, and calls that succeed fold into one line of
  tool names and counts once the reply is finished, as in the terminal. Rendering is generic
  over SDK message types.
- Background tasks: a task keeps its line in progress in the reply that started it until it
  ends or its Claude Code process goes away, and a stopped line says `Stopped`. A background
  subagent's calls update its line. Claude Code's report of a finished task is a reply of its
  own that opens with Claude Code's line for the task's end (`✓ Agent "review" finished ·
  3m 59s`).
- A footer on the channel's latest reply: bypass, git branch, model, effort level, context,
  session tokens, the 5-hour and weekly limits, and the tasks still running (`⏳ 1 shell ·
  1 agent`). The effort level is the one Claude Code reports to a `Stop` hook. `!status` lists
  the same values one per line, or says why Claude Code cannot start.
- Approvals with Approve and Deny buttons; a decided request is removed and the tool's line
  records the call. A clarifying question is one line with Answer and Skip; Answer opens a form
  with one question at a time, radio buttons or checkboxes with each option's description, and
  an Other field.
- A compaction shows the tokens before and after; a turn with no text says it is done.
- A Slack network failure drops a write without stopping the session; a final write Slack
  refuses for its content is written again as plain text. A Claude Code process that exits
  releases the channel; a missing directory asks to bind again; a directory macOS privacy
  protection denies gets a message that says how to grant access. A final write lost to the
  network is tried again once. An approval request Slack does not accept is denied, with the
  reason. A failure reading the channel's members refuses the event and tells the owner.
- A session starts only in a folder the owner has trusted in Claude Code, by Claude Code's own
  record and rules; an untrusted folder gets a reply saying how to trust it.
- An approval request shows the tool's whole input, or says how much it leaves out, and every
  model-written text in it is escaped for Slack. The footer shows `⚡ bypass` when the folder's
  own settings start the session in bypass, and `!bypass off` then returns to `default`. Nothing
  the daemon posts gets a link or media preview.
- `!guide` explains how to use the channel in a few lines; a test fails when a word of the
  daemon is missing from the guide or from `!help`.
- `!resume` lists the twenty newest sessions of the channel's directory (terminal and Slack)
  with a Resume button each, and says when more exist, and `!resume <id or name>` resumes one, as Claude Code's `/resume` does in the
  terminal.
- A daily workflow runs the tests on the latest `claude-agent-sdk` release and keeps one issue
  with the versions, the outcome and the checks left to do by hand.
- Issue forms for bug reports and feature requests; blank issues are off, and security reports
  are pointed to private vulnerability reporting.
- Files attached to a message: JPEG, PNG, GIF and WebP images (up to 7.5 MB and 8000x8000 px,
  at most 5 and 15 MB per message) reach Claude as image blocks; text, source code, PDF, JSON,
  XML, YAML and notebook files (up to 100 MB) as the path of a copy in a private temporary
  folder, kept 3 days. Other image types and other files (archives, Office documents, binaries)
  are refused. Messages keep the order they were sent in while files download, and one that a
  `!bind` overtook is not sent. A refused file or a failed
  download sends nothing and says why. The app needs the `files:read` scope.
- The footer ends with the channel's folder, by its last two names.
- `!bind` alone lists `ALLOWED_ROOT` and the folders up to two levels below it that Claude Code
  trusts, never inside a git repository, with a Bind button each; a click is refused while a
  turn runs.
- `!bind` reads a relative path from `ALLOWED_ROOT`; a link Slack made in a message reaches
  Claude Code as typed.
