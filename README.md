# awaydesk

Code from anywhere with Claude Code: your Mac does the work, Slack is the remote.

<!-- Demo recording goes here: a reply growing in a thread, an Approve click, the footer. -->

Claude Code runs in a terminal on your Mac. awaydesk is a small daemon on that Mac that
lets you keep working with it from Slack, on your phone or on another computer. Each private
channel is one project and each thread in it is one session: you write a message, Claude Code
runs in the project's folder with your own login, settings and permissions, and the reply shows
up in the thread while Claude works. Nobody else can talk to it.

Powered by Claude, through the
[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview).

## Before you install

- It runs on macOS only, as a LaunchAgent in your login session.
- It needs a Slack workspace where you are the only member. The bot checks who writes, but a
  workspace admin could still read or export what Claude prints, and whoever controls your Slack
  account controls the machine.
- It needs Claude Code logged in with a claude.ai subscription,
  [uv](https://docs.astral.sh/uv/) and Python 3.12 or later.
- If your projects live in a folder macOS protects, such as `~/Documents` or `~/Desktop`, the
  daemon needs Full Disk Access.
- Status: version 0.1.0, no tagged release yet. [docs/features.md](docs/features.md) lists what
  is tested and what is still checked by hand.

## Install

```bash
git clone https://github.com/duqaXxX/awaydesk.git
cd awaydesk
uv tool install .
```

[docs/setup.md](docs/setup.md) takes you through the rest:

1. Create the Slack app from [`slack-app-manifest.json`](slack-app-manifest.json) and copy its
   tokens.
2. Write the tokens and your settings in `~/.config/awaydesk/.env`.
3. Trust your project folder in Claude Code.
4. Start the LaunchAgent.
5. In a private channel with you and the bot, send `!bind <folder>`, then your first message.

## What you get

- A session per thread. A message in the channel opens a thread and asks for the model, the
  effort and bypass; a reply in that thread continues the session, even days later.
- The reply as it is written: Claude's text, the tool calls, a card for each subagent and
  background task.
- Approve and Deny buttons for whatever Claude Code asks permission for, and a form for Claude's
  questions.
- A footer under each reply: branch and lines changed, model, effort, context used, usage limits.
- Claude Code's own commands with `!` in place of `/`, such as `!compact` or `!model opus`.
- Images and files attached to a message reach Claude.
- A Home tab that lists your sessions by channel, the last used first.

## Commands

| Command | What it does |
|---|---|
| `!guide` | Explains how the channel works |
| `!bind <folder>` | Binds the channel to a project folder |
| `!status` | Lists the live sessions; in a thread, shows that session's details |
| `!stop` | Stops every session of the channel; in a thread, that one |
| `!resume` | Lists the folder's sessions, the terminal's included, with a Resume button each |
| `!bypass on` / `off` | Switches a session to `bypassPermissions` and back |
| `!open` | Shares a file of the session's folder into the thread |
| `!help` | Lists the commands a session offers |

Every command with its answers and limits: [docs/setup.md](docs/setup.md#using-it).

## Security model

The bot answers one Slack user in one workspace, and only in private channels whose members are
that user and the bot. Every message, button and form submission is checked on its own, and a
session starts only in a folder you have trusted in Claude Code. The tokens live in a mode-600
file, and nothing else is stored except each channel's directory and, per thread, its session id,
bypass switch, effort level and the status reaction on its root message. See
[SECURITY.md](SECURITY.md) and the checklist in
[docs/setup.md](docs/setup.md#part-5-security-checklist).

## Documentation

- [docs/setup.md](docs/setup.md): the whole setup and every command.
- [docs/architecture.md](docs/architecture.md): how the daemon is built.
- [docs/features.md](docs/features.md): each feature and what verifies it.
- [docs/limits.md](docs/limits.md): what the terminal does and Slack does not, and why.
- [docs/CHANGELOG.md](docs/CHANGELOG.md): what changed.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE).
