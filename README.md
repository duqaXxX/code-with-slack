# code-with-slack

A local daemon that lets one person drive Claude Code on their own Mac from Slack. Each private
Slack channel is one Claude Code session in one working directory. Nobody else can talk to it.

Status: first release. [docs/setup.md](docs/setup.md) takes a Mac from nothing to a working
channel.
How it fits together: [docs/architecture.md](docs/architecture.md).

Powered by Claude, through the
[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview).

## What it does

- A message in a bound channel is a prompt; the reply appears below it and grows as Claude
  works, with a line per tool call, subagent and background task, and a footer with the branch,
  the model, the context used and the usage limits.
- Whatever Claude Code asks approval for arrives as **Approve** and **Deny** buttons.
- `!<command>` runs a Claude Code command; `!help` lists them, read from the session.
- `!bypass on` switches the channel to `bypassPermissions` until `!bypass off` or a restart.

## Security model

The bot answers one Slack user in one workspace, and only in private channels whose members are
that user and the bot. Every message, command and button is checked on its own. The tokens live
in a mode-600 file, and nothing else is stored except which directory and which session each
channel uses. Whoever controls the owner's Slack account controls the machine: see
[SECURITY.md](SECURITY.md) and the checklist in [docs/setup.md](docs/setup.md).

## Security and contributing

See [SECURITY.md](SECURITY.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

## Changes

See [docs/CHANGELOG.md](docs/CHANGELOG.md).

## License

MIT. See [LICENSE](LICENSE).
