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
