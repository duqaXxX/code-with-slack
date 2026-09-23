# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added

- Package scaffold: `pyproject.toml` with pinned dependencies, MIT license, docs test.
- CI (sensitive-data scan; tests, types, lint), the published text scan, Dependabot for uv and
  GitHub Actions.
- Configuration loading from `~/.config/code-with-slack/.env` with the mode 600 check.
- `state.json` with atomic writes, and a single-instance lock on the configuration directory.
- Parsing of `/cc` subcommands and the `!` prefix.
- Identity and channel guards on every inbound path.
- Generic rendering of SDK messages into streamed text and task cards.
- Streaming replies with task cards, and the `chat.update` fallback.
- Approvals and clarifying questions answered with Slack buttons.
- The reply footer: branch, model, context, tokens, usage limits, bypass.
- One Claude Code session per channel with a turn queue, resume, stale-session recovery, bypass
  and stop.
- Slack handlers for messages, `/cc`, approvals and the command picker.
- The `code-with-slack` entry point, started by a user LaunchAgent.
- A Slack network failure drops a reply without stopping the session; a Claude Code process that
  exits releases the channel; a missing directory asks to bind again.
- A directory macOS privacy protection denies gets a message that says how to grant access.
- A decided approval request is removed from the thread; the tool's card records it.
- Replies live in the channel's main window: one message per reply, rewritten about once a second,
  with a line per tool call where it happens and a `Claude is writing…` line until the footer.
  Native streaming in threads is gone, and so are the `assistant:write` scope and the agent view.
- A reply appears as soon as the owner's message arrives, with a writing or waiting status; a
  divider separates it from the footer; the footer shows the effort level.
- A blank line separates a reply's tool lines from its text.
- A background task keeps its line in progress in the reply that started it, with a count of
  running tasks above the footer, until it ends or its Claude Code process goes away. A stopped
  line says `Stopped`.
