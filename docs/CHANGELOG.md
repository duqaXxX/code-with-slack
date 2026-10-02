# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added

- Thread status (issues #83 and #95): Slack's own status line under a thread's last message
  (`render.status.ThreadStatus`, `assistant.threads.setStatus` with a loading message, which is
  what makes it show on iOS). It reads `Working…` from the moment a prompt is received until
  its turn ends, and while a turn Claude Code starts to report a task runs. Once the turn has
  ended it reads what the turn left running, `1 shell still running`, the words the terminal
  ends such a turn with, and follows the count until nothing runs
  (`ThreadSession._thread_line`). The count is a state of the thread and is kept out of the
  reply, whose stream cannot change what it was sent. The status is set again within 2 seconds
  of a write of a reply or of a notice of the session's and every 60 seconds, since Slack
  clears a status when the app replies and removes it after two minutes. An answer to a word
  typed in the thread (`!bypass`, say) is posted outside the session, so the status it clears
  comes back with the 60 second refresh. It goes while an approval or a question waits for the
  owner, on `!stop`, on an error and when the session closes. A clearing call that fails is
  tried once more, and again when the session closes. A refusal from Slack is logged
  and the line is skipped; `missing_scope` or `not_allowed_token_type` ends the attempts for
  the rest of the run. It never notifies and stores nothing.
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
  visible to you`, and it disappears on reload), or by a ✅ on the word for `!bypass`; the D5
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
