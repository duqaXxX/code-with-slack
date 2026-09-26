# Setup

This guide takes a Mac from nothing to a Slack channel that drives a local Claude Code session.
It covers the Slack app, the configuration code-with-slack reads, how it starts on macOS, and the
security settings the design relies on.

Status: first release. Every part below describes what the code does.

## Requirements

- macOS, with a user account that stays logged in on the machine that runs the sessions.
- Claude Code installed and logged in with a claude.ai subscription: run `claude`, then `/login`.
  The 5-hour and weekly usage figures in the status footer exist only with a subscription.
- [uv](https://docs.astral.sh/uv/) and Python 3.12 or later.
- A Slack workspace where you are the only member. On the free plan Slack allows one workspace;
  use it only if nobody else is in it (see Part 5).

## Part 0: install

```bash
git clone https://github.com/duqaXxX/code-with-slack.git
cd code-with-slack
uv tool install .
```

This puts the `code-with-slack` command in `~/.local/bin`, where the LaunchAgent of Part 4 runs
it. To update, pull and run `uv tool install --reinstall .`.

code-with-slack runs the Claude Code CLI that ships inside the Claude Agent SDK it depends on,
not the `claude` on your `PATH`. Both read the same login, so logging in once with `claude` and
`/login` covers both.

## Part 1: the Slack app

### Create the app from the manifest

1. Open <https://api.slack.com/apps> and choose **Create New App**, then **From a manifest**.
2. Pick your workspace and choose **Next**.
3. On the **JSON** tab, replace the example with the contents of
   [`slack-app-manifest.json`](../slack-app-manifest.json). To give the bot a name of your own,
   change `features.bot_user.display_name`: it is the name Slack shows on every reply and at the
   top of the bot's profile. Leave `display_information.name`, the app's name, as
   `code-with-slack`. Choose **Next**.
4. Check the summary and choose **Create**.

The manifest asks for private channels only (`groups:history`, `groups:read`,
`message.groups`), `chat:write`, and `files:read` to download the files you attach to a message.
The app registers no slash command: commands are typed as `!word` messages. Socket Mode is on,
so the app needs no public URL and your machine opens no inbound port.

An app created before `files:read` was added needs the scope too: on the app's **OAuth &
Permissions** page add the bot scope `files:read`, then reinstall the app to the workspace.
Without it, every attached file is refused with `HTTP 302`.

code-with-slack needs none of the app's agent features: leave **Agent experience** and the
**Slack Model Context Protocol (MCP) Server** off in the app settings. The MCP server lets an app
act on behalf of Slack users, which code-with-slack never needs.

### Install it and collect two tokens

1. Open **Install App** and install the app to your workspace. Copy the **Bot User OAuth Token**
   (`xoxb-…`).
2. Open **Basic Information**, find **App-Level Tokens**, and choose **Generate Token and
   Scopes**. Name it, add the scope `connections:write`, and generate it. Copy the token
   (`xapp-…`).

Treat both as passwords: they go in the `.env` file of Part 2 and nowhere else. If one leaks,
regenerate it on the same page.

### Find your member ID

In Slack, open your profile, choose the **⋮** button, then **Copy member ID** (it starts with
`U`). If the profile does not open in the desktop app, use <https://app.slack.com>.

### Create a channel per project

1. Create a channel and turn on **Make private**. Name it after the project, with a common prefix
   so the channels sort together, for example `cc-myproject`. Custom sidebar sections would group
   them better, but Slack offers those on paid plans only.
2. In the channel, run `/invite @code-with-slack`.

The bot sees private channels it was invited to, and nothing else.

## Part 2: configuration

code-with-slack keeps its files in `~/.config/code-with-slack/`:

| File | Written by | Holds |
|---|---|---|
| `.env` | you | the tokens and the settings below |
| `state.json` | code-with-slack | for each channel, its directory and its Claude Code session id |

`state.json` looks like this; you never need to edit it:

```json
{"version": 1, "channels": {"C0123456789": {"directory": "/home/dev/code/project", "session_id": "..."}}}
```

Create the directory and the file, readable by you only. code-with-slack refuses to start when
`.env` is readable by anyone else, is a symbolic link, or belongs to another user, and when a
token is of the wrong kind (`xoxb-` for the bot token, `xapp-` for the app-level token).

```bash
mkdir -p ~/.config/code-with-slack
touch ~/.config/code-with-slack/.env
chmod 600 ~/.config/code-with-slack/.env
```

| Variable | Value |
|---|---|
| `SLACK_BOT_TOKEN` | the `xoxb-…` token |
| `SLACK_APP_TOKEN` | the `xapp-…` token |
| `SLACK_OWNER_USER_ID` | your member ID, the only person the bot answers |
| `ALLOWED_ROOT` | the directory `!bind` accepts paths under, for example `~/code` |

The workspace ID is not configured: code-with-slack reads it from Slack at startup with the bot
token and rejects events from any other workspace.

`ALLOWED_ROOT` guards against a typo such as `!bind /`. It is not a security boundary:
Claude Code can read and run outside its working directory once you approve it.

## Part 3: Claude Code

code-with-slack drives Claude Code through the Claude Agent SDK, with the login already on the
machine. Your own Claude Code settings apply: `~/.claude/settings.json`, the project's
`.claude/settings.json`, and `.claude/settings.local.json`. Whatever Claude Code asks your
approval for reaches Slack as **Approve** and **Deny** buttons, with the tool's whole input.

A session starts only in a folder you have trusted in Claude Code. Claude Code shows its trust
dialog only in the terminal, and a session started from Slack would otherwise run a
repository's own hooks and apply its settings without asking. Before you bind a channel to a
repository, open `claude` at its root in the terminal once and accept the dialog. A trusted
parent folder does not cover a git repository inside it, such as a clone.

When Claude Code is logged out, a message in Slack replies with a note asking you to run `claude`
and `/login` on the machine; the login is never done from Slack.

If you turn on Claude Code's Bash sandbox, do it in your own settings, where it applies to the
terminal and to Slack alike. Its auto-allow mode runs sandboxed Bash commands without asking,
so they would not reach Slack for approval.

## Part 4: starting code-with-slack on macOS

code-with-slack runs as a user LaunchAgent: it starts when you log in and restarts if it exits.
It has to run inside your login session, because Claude Code keeps its credentials in the macOS
Keychain. After the Mac restarts, it starts again when you log in.

Only one instance may run. Slack spreads a Socket Mode app's events across all its open
connections, so a second instance would receive part of your messages; code-with-slack refuses to
start while another instance holds its lock.

The plist lives at `~/Library/LaunchAgents/local.code-with-slack.plist`. launchd does not expand
`~` or `$HOME`, so write your home directory in full where the example says `<home>`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>local.code-with-slack</string>
  <key>ProgramArguments</key>
  <array>
    <string><home>/.local/bin/code-with-slack</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string><home>/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ExitTimeOut</key>
  <integer>60</integer>
  <key>StandardOutPath</key>
  <string><home>/Library/Logs/code-with-slack/code-with-slack.log</string>
  <key>StandardErrorPath</key>
  <string><home>/Library/Logs/code-with-slack/code-with-slack.log</string>
</dict>
</plist>
```

Load it, restart it, stop it, and read its state:

```bash
mkdir -p ~/Library/Logs/code-with-slack
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.code-with-slack.plist
launchctl kill TERM gui/$(id -u)/local.code-with-slack
launchctl bootout gui/$(id -u)/local.code-with-slack
launchctl print gui/$(id -u)/local.code-with-slack
```

On `SIGTERM` code-with-slack stops starting turns and lets everything already running finish: the
turns, the background commands and agents, which end with the Claude Code process, and the turn in
which Claude reports each one. Then it exits, and `KeepAlive` starts it again. Meanwhile a new
message gets `code-with-slack is restarting; send this again in a moment.`, a queued one ends with
the same request. An approval or a question Claude asks meanwhile stays open and can be answered, so
a session that restarts the daemon can still finish its turn. The daemon's `!words` keep working:
`!stop` ends a long turn so the restart goes on. After 29 minutes code-with-slack stops waiting and
ends what still runs, whose replies say that it stopped: a background command that never ends, such
as a dev server, holds a restart that long. Sending the signal a second time stops without waiting.
`SIGINT` (Ctrl-C in a terminal) stops without waiting too, because the terminal sends it to the
Claude Code processes as well.

How long a turn can take to finish depends on who sends the signal. `launchctl kill TERM` only
sends it, so a restart waits up to those 29 minutes. `launchctl bootout` and `launchctl
kickstart -k` stop the job themselves and kill it `ExitTimeOut` seconds later; on macOS 27.0
launchd caps `ExitTimeOut` at 60 (`launchctl print` shows 60 for any larger value, and a job
stopped with `bootout` was killed within 60 seconds, measured 2026-09-26). A turn still running
then keeps a reply that says Claude is writing.

`launchctl kill TERM` returns at once, so a Claude Code session running from Slack can restart
the daemon that hosts it and still finish its turn. `launchctl kickstart -k` waits until the old
instance has exited, which from such a session means waiting on its own turn, until launchd
kills the daemon and that turn with it after `ExitTimeOut`.

The log holds what the service did, never the content of your messages.

### Folders macOS protects

macOS keeps `~/Documents`, `~/Desktop`, `~/Downloads` and a few other folders private to the
apps you allowed. A program started from Terminal uses Terminal's permission; code-with-slack,
started by launchd, has none, and Claude Code fails to start in a directory there. If your
projects live in one of those folders, give Full Disk Access to the Python interpreter that runs
code-with-slack:

1. Show the interpreter in Finder (it sits in a hidden folder, so this is the simplest way to
   reach it):

   ```bash
   open -R "$(readlink -f "$(head -1 ~/.local/share/uv/tools/code-with-slack/bin/code-with-slack | cut -c3-)")"
   ```

2. Open **System Settings**, **Privacy & Security**, **Full Disk Access**, and drag the selected
   file (`python3.12` or similar) from Finder into the list. Alternatively choose **+** and press
   **⌘⇧.** in the file picker to show hidden folders.
3. Check that the new entry is turned on, then restart the service:
   `launchctl kill TERM gui/$(id -u)/local.code-with-slack`.

The permission belongs to that interpreter, which uv shares between the tools that use the same
Python version: any of them started outside Terminal gets the same access. Projects outside the
protected folders need no permission at all.

## Part 5: security checklist

The bot answers one person, and the rest of this list protects what that person sees.

- Your workspace has no other member and no other admin. The bot checks who writes; a workspace
  admin could still read or export what Claude prints.
- Two-factor authentication is on for your Slack account. Whoever controls that account controls
  the machine, and no check in code-with-slack can tell them apart from you.
- Every channel is private and holds you and the bot only. code-with-slack refuses a channel that
  is public, shared, Slack Connect, or has a third member.
- The app stays undistributed: never turn on public distribution under **Manage Distribution**.
- The Slack MCP server stays off.
- `~/.config/code-with-slack/.env` is mode `600`.
- Files you attach are copied to `$TMPDIR/code-with-slack/` (mode `700`) and stay there for 3
  days, so a conversation resumed after a restart still finds them.
- Trust a folder in Claude Code only after reading its `.claude/` settings and hooks: trusting it
  is what lets a session from Slack start there.
- With a static public IP, you can also restrict the tokens to it under **OAuth & Permissions**,
  **Restrict API Token Usage**. With a dynamic IP, the first address change stops the bot with
  `invalid_auth`.

## Using it

| In Slack | What it does |
|---|---|
| `!guide` | Explains in a few lines how to use the channel: binding, prompts, commands, approvals, sessions, bypass |
| a message | Sends a prompt to the channel's session; the reply appears below it in the channel and grows as Claude works |
| a message with files | Claude sees a JPEG, PNG, GIF or WebP image (up to 7.5 MB and 8000x8000 px; at most 5 images and 15 MB of images per message) as an image. Any other image type (SVG, HEIC, TIFF...) is refused. A text, source code, PDF, JSON, XML, YAML or Jupyter notebook file, up to 100 MB, reaches Claude as the path of a copy in `$TMPDIR/code-with-slack/`, kept 3 days; any other file (archives, Office documents, binaries) is refused. A file past a limit, or one that fails to download, stops the whole message, and the reply says which file and why |
| `!help [text]` | Lists code-with-slack's own words and every command the channel's session offers now; with a text, only the lines whose name or description contains it, for example `!help model` |
| `!bind` | Lists `ALLOWED_ROOT` itself (shown as `.`) and the folders up to two levels below it, never inside a git repository, that Claude Code trusts, with a **Bind** button each; the channel's own folder is marked when it is listed. At most 20 are shown, in path order; when there are more, the higher levels fill the list first. A click never ends a running turn: it is refused until the channel is idle |
| `!bind <folder>` | Binds this channel to a folder under `ALLOWED_ROOT`, given relative to it (`!bind my-project`); an absolute path inside it works too. A new channel does nothing else until bound |
| `!<command> [args]` | Runs a Claude Code command, for example `!compact` or `!model opus` |
| `!bypass on` / `off` | Switches the channel's session to `bypassPermissions` and back; a restart turns it off |
| `!status` | Shows the channel's directory, session and mode, then the footer's values one per line |
| `!stop` | Stops the turn that is running and denies its pending approvals |
| `!resume` | Lists the twenty newest sessions of the channel's directory (not of other worktrees), terminal and Slack alike, with a **Resume** button each; the channel's own is marked `current` |
| `!resume <id or name>` | Resumes that session directly; the name is the session's title, set with `/rename` or generated by Claude Code, and must match one session |
| a question from Claude | Appears as one line with **Answer** and **Skip**; Answer opens a form with one question at a time, the options (one or several) and an **Other** field; **Next** moves on once the question has an answer, **Submit** on the last |

Slack does not pass a Claude Code command typed with its own slash: `/compact` alone makes Slack
answer that it is not a valid command. Type `!compact`. A `!word` that is not a command the
session offers is sent as a normal prompt, so `!important: …` reaches Claude as written.

`!help`, `!guide`, `!bind`, `!bypass`, `!status`, `!stop` and `!resume` are code-with-slack's own
and come first. Claude Code's own `/resume` is interactive and not offered to an SDK session, so
`!resume` does its job: the next message continues the chosen session, in a new Claude Code
process, with bypass off. It refuses while a turn or a background task is running or waiting in
the channel, since resuming ends the channel's Claude Code process. If the session is still open in a
terminal, close it there first: Claude Code interleaves the messages of two processes resuming
the same session into one transcript. Claude Code has no other command with these names today; `!help` lists what the session offers. The
answers to these words are messages in the channel.

A reply is one message in the channel. It appears as soon as you send your message, reading
`Claude is writing…`, or `Waiting for the previous reply…` when another turn is still running.
It is then rewritten about once a second while Claude works: text in the order it is written,
and a line per tool call where it happens (`…` while it runs, `✗` with its output when it
fails), in small grey text. Once the reply is complete, calls that succeeded fold into one line
of tool names and counts, such as `✓ Bash · Read ×2`; a subagent or a background task keeps a
line of its own, and a divider and the footer replace the status line. A reply longer than one Slack message continues in the next one.

An approval request is a message of its own below the reply; once you decide, it disappears and
the tool's line in the reply records the call. If Slack does not accept the request, Claude Code
is told it was denied because it could not be shown.

Messages sent while a turn is running wait their turn; each gets its own reply. When a background
task finishes while nothing runs, Claude Code starts a turn of its own to report it, as it does
in the terminal: that reply opens with Claude Code's own line for the task's end, such as
`✓ Agent "review" finished · 3m 59s`. While tasks run, the footer counts them, such as
`⏳ 1 shell · 1 agent`.

The channel's latest reply ends with a footer, which moves to each new reply: `⚡ bypass` when bypass is on, the git branch, the model, the
effort level, the context used, the session's tokens, and the 5-hour and weekly limits
(`5h N% ↻ 2h · 7d N%`), which exist only with a claude.ai subscription, and last the channel's
folder by its last two names (`code/my-project`). The effort level is the
one Claude Code reported at the end of the last turn, or the one set since with `!effort` (or
`!model`); it reads `default` on a model that takes no effort level, and is left out after a
restart until a turn ends normally (an interrupted turn or an API error reports no level). A level set with `!effort` lasts until the daemon restarts: the resumed session
runs at the level your Claude Code settings give the model.

## Troubleshooting

| Symptom | Cause |
|---|---|
| The bot does not see a channel | The channel is public, or the bot was not invited |
| `Claude Code is not logged in on the host` | Claude Code on the machine is logged out: run `claude`, then `/login` |
| Some messages get no reply | A second instance is running and receiving part of the events |
| `The previous session could not be resumed` | The stored session no longer exists (its transcript was deleted); the reply runs in a new session |
| `Claude Code has not been trusted in ...` | Open `claude` in that folder (the repository root) in the terminal, accept the trust dialog, and send the message again |
| `The directory ... no longer exists` | The channel's directory was moved or deleted: bind the channel again with `!bind <path>` |
| `macOS does not let code-with-slack read ...` | The directory is in a folder macOS protects: see Part 4, "Folders macOS protects" |
| `another code-with-slack is running` in the log | A second instance tried to start; only one may run |
