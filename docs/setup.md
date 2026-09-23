# Setup

This guide takes a Mac from nothing to a Slack channel that drives a local Claude Code session.
It covers the Slack app, the configuration code-with-slack reads, how it starts on macOS, and the
security settings the design relies on.

Status: code-with-slack is under development. Part 1 (the Slack app) works today. Parts 2 to 5
describe the configuration and the service the first release reads; the implementation keeps
this file true as it lands.

## Requirements

- macOS, with a user account that stays logged in on the machine that runs the sessions.
- Claude Code installed and logged in with a claude.ai subscription: run `claude`, then `/login`.
  The 5-hour and weekly usage figures in the status footer exist only with a subscription.
- [uv](https://docs.astral.sh/uv/) and Python 3.12 or later.
- A Slack workspace where you are the only member. On the free plan Slack allows one workspace;
  use it only if nobody else is in it (see Part 5).

## Part 1: the Slack app

### Create the app from the manifest

1. Open <https://api.slack.com/apps> and choose **Create New App**, then **From a manifest**.
2. Pick your workspace and choose **Next**.
3. On the **JSON** tab, replace the example with the contents of
   [`slack-app-manifest.json`](../slack-app-manifest.json) and choose **Next**.
4. Check the summary and choose **Create**.

The manifest asks for private channels only (`groups:history`, `groups:read`,
`message.groups`), `chat:write`, `commands` for `/cc`, and `assistant:write` for the agent
features. Socket Mode is on, so the app needs no public URL and your machine opens
no inbound port.

### Turn on the agent experience

In the app settings, open **Agents** and turn on **Agent experience**. Without it, Slack accepts
the task updates code-with-slack streams but does not draw them: a reply shows its text and none
of the tool, subagent or background-task cards.

Leave **Slack Model Context Protocol (MCP) Server** off. It lets an app act on behalf of Slack
users, which code-with-slack never needs.

If Slack asks for a description, any short sentence will do. For **Suggested Prompts**, choose
**Fixed** and leave the list empty.

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
| `ALLOWED_ROOT` | the directory `/cc bind` accepts paths under, for example `~/code` |

The workspace ID is not configured: code-with-slack reads it from Slack at startup with the bot
token and rejects events from any other workspace.

`ALLOWED_ROOT` guards against a typo such as `/cc bind /`. It is not a security boundary:
Claude Code can read and run outside its working directory once you approve it.

## Part 3: Claude Code

code-with-slack drives Claude Code through the Claude Agent SDK, with the login already on the
machine. Your own Claude Code settings apply: `~/.claude/settings.json`, the project's
`.claude/settings.json`, and `.claude/settings.local.json`. Whatever Claude Code asks your
approval for reaches Slack as **Approve** and **Deny** buttons.

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
launchctl kickstart -k gui/$(id -u)/local.code-with-slack
launchctl bootout gui/$(id -u)/local.code-with-slack
launchctl print gui/$(id -u)/local.code-with-slack
```

The log holds what the service did, never the content of your messages.

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
- With a static public IP, you can also restrict the tokens to it under **OAuth & Permissions**,
  **Restrict API Token Usage**. With a dynamic IP, the first address change stops the bot with
  `invalid_auth`.

## Using it

| In Slack | What it does |
|---|---|
| `/cc bind <path>` | Binds this channel to a directory under `ALLOWED_ROOT`. A new channel does nothing until bound |
| a message | Sends a prompt to the channel's session; the reply streams in a thread under it |
| `/cc <command> [args]` | Runs a Claude Code command, for example `/cc compact` or `/cc model opus` |
| `/cc` | Lists the commands the session offers |
| `!<command>` | The same as `/cc <command>`, for use inside a thread, where Slack runs no slash command |
| `/cc bypass on` / `off` | Switches the channel's session to `bypassPermissions` and back; a restart turns it off |
| `/cc status` | Shows the channel's directory, session and mode |

Slack does not pass a Claude Code command typed with its own slash: `/compact` alone makes Slack
answer that it is not a valid command. Use `/cc compact` or `!compact`.

## Troubleshooting

| Symptom | Cause |
|---|---|
| The bot does not see a channel | The channel is public, or the bot was not invited |
| Replies show text but no task cards | **Agent experience** is off in the app settings |
| `Not logged in · Please run /login` | Claude Code on the machine is logged out: run `claude`, then `/login` |
| Some messages get no reply | A second instance is running and receiving part of the events |
