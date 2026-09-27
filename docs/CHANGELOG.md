# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added

- The footer and `!status` show the lines changed since the last commit next to the branch,
  `(+42,-10)`, counted as the terminal's ccstatusline counts them (#39), and the time to the
  weekly limit's reset, `7d 45% ↻ 3d 4h`, as they already did for the 5-hour one (#42).
- Probe claim P14: a hook's `cwd` follows a `cd`, which the footer's branch depends on.
- With the channel on Slack's "Just mentions", a reply rings once, when it is complete, and its
  closing message ends its footer with `@channel`, its notification reading `Reply to: ` and the
  start of the owner's message; an approval request, a question and a reply that ends in an error
  ring too. Nothing rings while Claude writes, nor for
  `!stop`, a restart or a turn started by a background task (#26). `docs/setup.md` gains the
  channel setting.

### Changed

- The footer is a closing message of its own, posted when the reply ends. A reply that ends
  below a newer one says which message it answers.
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
