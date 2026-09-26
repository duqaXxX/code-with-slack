# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Changed

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
- `claude-agent-sdk` 0.2.159, which bundles Claude Code 2.1.281 (was 0.2.158 with 2.1.280). The
  SDK streams in `tests/fixtures/sdk/` are recorded again on 2.1.281.

### Added

First release.

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
