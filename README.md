# code-with-slack

A local daemon that lets one person drive Claude Code on their own Mac from Slack. Each private
Slack channel is bound to one working directory and holds one Claude Code session there. Nobody
else can talk to it.

Status: first release. How it fits together: [docs/architecture.md](docs/architecture.md).

Powered by Claude, through the
[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview).

## Requirements

- macOS, with the owner logged in: the daemon runs as a user LaunchAgent, and Claude Code keeps
  its login in the Keychain.
- Claude Code logged in with a claude.ai subscription.
- [uv](https://docs.astral.sh/uv/) and Python 3.12 or later.
- A Slack workspace where you are the only member.

## Install

```bash
git clone https://github.com/duqaXxX/code-with-slack.git
cd code-with-slack
uv tool install .
```

Then follow [docs/setup.md](docs/setup.md): the Slack app from
[`slack-app-manifest.json`](slack-app-manifest.json), the configuration file, the LaunchAgent and
the security checklist.

## Usage

In a private channel with only you and the bot, `!guide` explains how the channel works and
`!bind <path>` binds it to a directory.
From then on:

- A message is a prompt. The reply appears below it and grows as Claude works: Claude's text, a
  line per tool call, subagent and background task, and a footer with the branch, the model, the
  effort level, the context used, the usage limits and the channel's folder.
- Whatever Claude Code asks approval for arrives as **Approve** and **Deny** buttons; a question
  from Claude opens a form.
- `!<command>` runs a Claude Code command, such as `!compact` or `!model opus`. `!help` lists the
  commands the session offers.
- `!status` shows the channel's directory, session and mode; `!stop` stops the running turn.
- `!resume` lists the directory's sessions, from the terminal too, with a Resume button each;
  `!resume <id or name>` resumes one directly.
- Images and files attached to a message reach Claude: a JPEG, PNG, GIF or WebP image as an
  image; a text, code, PDF, JSON, XML, YAML or notebook file as a path to a copy saved in a
  private temporary folder. Any other file, and any file past a limit, stops the message, with
  the reason.
- `!bind` lists the folders under `ALLOWED_ROOT` that Claude Code trusts, with a Bind button
  each; `!bind <folder>` binds one directly.
- `!bypass on` switches the channel to `bypassPermissions` until `!bypass off` or a restart.

The full list is in [docs/setup.md](docs/setup.md#using-it).

## Security model

The bot answers one Slack user in one workspace, and only in private channels whose members are
that user and the bot. Every message, button and form submission is checked on its own, and a
session starts only in a folder you have trusted in Claude Code. The tokens live in a mode-600
file, and nothing else is stored except which directory and which session each channel uses. Whoever controls the owner's Slack account controls the machine: see
[SECURITY.md](SECURITY.md) and the checklist in [docs/setup.md](docs/setup.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Changes are listed in
[docs/CHANGELOG.md](docs/CHANGELOG.md).

## License

MIT. See [LICENSE](LICENSE).
