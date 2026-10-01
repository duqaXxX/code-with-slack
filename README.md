# code-with-slack

A local daemon that lets one person drive Claude Code on their own Mac from Slack. Each private
Slack channel is bound to one working directory; each Slack thread in it is its own Claude Code
session. Nobody else can talk to it.

Status: first release. How it fits together: [docs/architecture.md](docs/architecture.md). What
each feature is checked by: [docs/features.md](docs/features.md).

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

- A top-level message opens a new thread, which first asks for the model, the effort and bypass;
  **Start** starts the session with them and sends the message. The reply appears in that thread
  and grows as Claude works: Claude's text, the tool calls as a count of what ended above the call
  that runs now (one line of counts once the reply has ended), a card per subagent and background
  task, and a footer with the branch and the lines changed since the last commit, the model, the
  effort level, the context used, the usage limits with their resets, and the folder. A reply
  inside a thread continues that thread's session, even days later.
- Whatever Claude Code asks approval for arrives as **Approve** and **Deny** buttons; a question
  from Claude opens a form.
- `!<command>` runs a Claude Code command, such as `!compact` or `!model opus`, inside a
  session's thread. `!help` lists the commands that session offers.
- `!status`, typed in the channel, lists every live session with a link to its thread; inside a
  thread it shows that session's directory and mode, then the footer's values one per line.
  `!stop`, typed in the channel, stops every session; inside a thread it stops that one.
- `!resume`, typed in the channel, lists the directory's sessions, from the terminal too, with a
  Resume button each; `!resume <id or name>` or a click moves that session into a new thread.
- Images and files attached to a message reach Claude: a JPEG, PNG, GIF or WebP image as an
  image; a text, code, PDF, JSON, XML, YAML or notebook file as a path to a copy saved in a
  private temporary folder. Any other file, and any file past a limit, stops the message, with
  the reason.
- `!bind`, typed in the channel, lists the folders under `ALLOWED_ROOT` that Claude Code trusts,
  with a Bind button each; `!bind <folder>` binds one directly.
- `!bypass on`, sent inside a session's thread, switches that session to `bypassPermissions`
  until `!bypass off`, as ticking Bypass at Start does; a restart of code-with-slack keeps it.

The full list is in [docs/setup.md](docs/setup.md#using-it).

## Security model

The bot answers one Slack user in one workspace, and only in private channels whose members are
that user and the bot. Every message, button and form submission is checked on its own, and a
session starts only in a folder you have trusted in Claude Code. The tokens live in a mode-600
file, and nothing else is stored except each channel's directory and, per thread, its session id,
bypass switch and effort level. Whoever controls the owner's Slack account controls the machine: see
[SECURITY.md](SECURITY.md) and the checklist in [docs/setup.md](docs/setup.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Changes are listed in
[docs/CHANGELOG.md](docs/CHANGELOG.md).

## License

MIT. See [LICENSE](LICENSE).
