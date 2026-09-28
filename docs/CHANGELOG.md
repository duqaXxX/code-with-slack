# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added

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

### Changed

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
  of its own. `!stop`, an error, a restart and an idle close still close a reply at once, with no
  further wait, as before.

### Fixed

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
