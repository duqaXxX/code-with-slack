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
`message.groups`), `chat:write`, `files:read` to download the files you attach to a message, and
`reactions:write` for the status reaction on a session's root message. It also switches on the
app's **Home** tab, where code-with-slack keeps the list of your sessions; publishing it needs no
scope and no event.
The app registers no slash command: commands are typed as `!word` messages. Socket Mode is on,
so the app needs no public URL and your machine opens no inbound port.

An app created before `files:read` or `reactions:write` was added needs the scope too: on the
app's **OAuth & Permissions** page add the missing bot scope, then reinstall the app to the
workspace. Without `files:read`, every attached file is refused with `HTTP 302`; without
`reactions:write`, the status reaction is silently skipped (logged, never surfaced).

The line under a thread (`Working…`, `1 shell still running`) is Slack's thread status
(`assistant.threads.setStatus`), which Slack's reference lists under `chat:write`. When Slack
refuses it, the refusal is logged and the line is skipped.

An app created before the Home tab was added needs it switched on: on the app's **App Home**
page, under **Show Tabs**, turn on **Home Tab**, then restart code-with-slack. Without it the
session list is not published, and the log says so once per start.

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
3. Open the channel's notification settings. A reply, an approval request and a question post
   inside their own Slack thread, and Slack notifies you on a new message in a thread you started,
   whatever this setting is. It governs only the bot's top-level messages (the answer to a word
   typed in the channel, with `!resume`'s list, and the upgrade notice): with **Just mentions**
   those stay silent, since the bot writes no `@channel` mention anywhere; with **All new posts**
   they ring too. A word typed inside a thread is answered for you alone and never rings.

The bot sees private channels it was invited to, and nothing else.

## Part 2: configuration

code-with-slack keeps its files in `~/.config/code-with-slack/`:

| File | Written by | Holds |
|---|---|---|
| `.env` | you | the tokens and the settings below |
| `state.json` | code-with-slack | for each channel, its directory; for each of its threads, the folder it was opened in, its Claude Code session id, its bypass choice (on, off or never chosen), the effort level set with `/effort` and the status reaction on its root message |

`state.json` looks like this; you never need to edit it:

```json
{"version": 2, "channels": {"C0123456789": {"directory": "/home/dev/code/project", "notice_pending": false, "threads": {"1700000000.000100": {"directory": "/home/dev/code/project", "session_id": "...", "bypass": false, "effort": null}}}}}
```

code-with-slack keeps `state.json` clean on its own, when it starts and then every 6 hours. It
forgets a channel Slack answers `channel_not_found` about, with its threads, and a thread whose
Claude Code session no longer exists. Anything it cannot tell for certain stays: a rate limit, a
server error or an unreadable folder removes nothing, and neither does a thread or a channel
with a session open at that moment. When Slack finds none of the bound channels it forgets none
and says so in the log, since that is what a bot token of another workspace looks like.

A private channel the bot was removed from answers `channel_not_found` too, so it is forgotten
like a deleted one. After inviting the bot back, send `!bind` there again; `!resume` brings back
the folder's sessions, which stay on disk.

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

The models a new thread offers are the ones Claude Code lists, older versions included. To offer
fewer, set Claude Code's `modelPicker` with `replaceBuiltInOptions: true` in
`~/.claude/settings.json` (Claude Code reads it only from user or managed settings); it changes
the terminal's `/model` picker the same way. Rows naming an alias (`opus`, `sonnet`, `haiku`)
follow the newest version of that model.

A session starts only in a folder you have trusted in Claude Code. Claude Code shows its trust
dialog only in the terminal, and a session started from Slack would otherwise run a
repository's own hooks and apply its settings without asking. Before you bind a channel to a
repository, open `claude` at its root in the terminal once and accept the dialog. A trusted
parent folder does not cover a git repository inside it, such as a clone.

When Claude Code is logged out, a message in Slack replies with a note asking you to run `claude`
and `/login` on the machine; the login is never done from Slack. `!login` and `!logout` typed in
Slack get the same pointer to the machine and are never sent to Claude Code.

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
`!stop` ends a long turn so the restart goes on. A thread left with only background tasks says so
in the line under its last message (`Restart waits for 1 shell · !stop ends it now`), since
code-with-slack cannot tell whether a task such as a dev server ever ends; `!stop` there stops
them and the restart goes on. That line is no message: it does not ring, and it goes with the
restart. A restart posts nothing in a thread that has nothing running, bypass on or not, since
bypass outlives it. If Slack refuses the app that line, one message says what the restart waits
for instead. The signal does not say who sent it, and a session that sends it
has a turn running at that moment: for every session with a turn running when the signal arrives,
code-with-slack waits for the turn but not for a background task the session starts after the
signal, such as a loop waiting for the new process, which could only end once this one has exited.
The shutdown ends those tasks, and the session's root shows ✅. After 29 minutes code-with-slack stops waiting and ends what still
runs, whose replies say that it stopped. Sending the signal a second time stops without waiting.
`SIGINT` (Ctrl-C in a terminal) stops without waiting too, because the terminal sends it to the
Claude Code processes as well.

How long a turn can take to finish depends on who sends the signal. `launchctl kill TERM` only
sends it, so a restart waits up to those 29 minutes. `launchctl bootout` and `launchctl
kickstart -k` stop the job themselves and kill it `ExitTimeOut` seconds later; on macOS 27.0
launchd caps `ExitTimeOut` at 60 (`launchctl print` shows 60 for any larger value, and a job
stopped with `bootout` was killed within 60 seconds, measured 2026-09-26). A turn still running
then leaves its reply's stream open until Slack closes it, 5 minutes after it started.

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
- The Home tab is published to you alone: code-with-slack writes that page for no other member.
- `~/.config/code-with-slack/.env` is mode `600`.
- Files you attach are copied to `$TMPDIR/code-with-slack/` (mode `700`) and stay there for 3
  days, so a conversation resumed after a restart still finds them.
- Trust a folder in Claude Code only after reading its `.claude/` settings and hooks: trusting it
  is what lets a session from Slack start there.
- With a static public IP, you can also restrict the tokens to it under **OAuth & Permissions**,
  **Restrict API Token Usage**. With a dynamic IP, the first address change stops the bot with
  `invalid_auth`.

## Using it

A top-level message in a bound channel opens a new Slack thread and starts a session there; a
reply inside that thread continues the same session, even days later. Before the first message
is sent, the thread shows one row of controls under `Choose how this session starts`: a model
from the list Claude Code offers, an effort (`Effort: default` passes none), **Bypass**, and
**Start**, which starts the session with them and sends the message. `!stop` in the thread
cancels it with `Not sent.`, and a reply in a thread where nothing was sent asks again. Each
thread keeps the folder it was opened in: `!bind` only changes where the *next* thread starts. A
thread's Claude Code process closes on its own after an hour with nothing to do; the next message
sent to it resumes the session, as `claude --resume <id>` would.

### Finding a session again: the Home tab

Open code-with-slack from Slack's sidebar and choose its **Home** tab. It lists the sessions your
threads hold, one group per bound channel, the channel used last first. A channel shows its five
newest sessions and, when it has more, a **Show all** button. Each session takes two lines, with
a blank row before the next:

- the status reaction of the thread's root message and the title Claude Code gives the session;
- in small text, the status in a word (`working`, `waiting for you`, `ended`, `error`), the
  thread's number of replies and the time since its last reply, as the channel shows them under
  the root message, and an **Open** link to the thread.

**New thread**, beside a channel's name, opens that channel: the message you send there starts a
session.

Four controls at the top of the page narrow it: a channel, a status, a period (`Last 48 hours`,
`Today`, `Yesterday`, `Last 7 days`, `Last 30 days`, `Any time`, by the thread's last reply,
the days being those of the machine code-with-slack runs on)
and a search on the titles (type a word and press Enter). The page starts on `Last 48 hours`.
The controls add up, and with a channel, a status or a search chosen a channel shows every
session that matches, not five. **Show all** chooses that channel. The choices last until
code-with-slack restarts.

The page leaves out a thread whose root message was deleted and a channel Slack no longer has.
It holds about 30 sessions at once and says so when it stops short. code-with-slack rewrites it
whenever a session starts, ends or changes status, and nothing notifies you when it does. The
number of replies is read when a session starts or ends a turn, so a word typed in a thread
(`!status`) is counted at the thread's next turn.

To keep the page one click away, star the app: open code-with-slack in Slack on desktop and
click the star beside its name at the top of its page, or drag it from the apps list into
**Starred**. It then sits in the **Starred** section at the top of the sidebar, and a click on
it opens the Home tab. Slack's own steps for starring, mobile included, are in its Help Center
under [Star channels and direct messages](https://slack.com/help/articles/201331016-Star-channels-and-direct-messages).

From outside Slack, this link opens the Home tab in the desktop and mobile apps, for the Dock or
a shortcut: `slack://app?team=<workspace id>&id=<app id>&tab=home`. The workspace id starts with
`T` and the app id with `A`; the app id is on the app's **Basic Information** page.

Upgrading from an earlier version that held one session per channel: the channel keeps its
directory, loses its old session pointer and bypass switch, and gets this message once, posted
top-level, not as a reply:

> code-with-slack now runs one Claude Code session per thread. Send a new message in the channel
> to start a session; reply in its thread to continue it. The session this channel had is still
> in the folder: !resume brings it into a thread. Bypass is now set per session: send !bypass on
> inside a thread.

### Commands

| Word | At the top level (answered by a post in the channel) | Inside a session's thread (answered for you alone) |
|---|---|---|
| `!guide` | Explains in a few lines how code-with-slack works | Same, as an ephemeral message you alone see |
| `!bind` | Lists `ALLOWED_ROOT` itself (shown as `.`) and the folders up to two levels below it, never inside a git repository, that Claude Code trusts, with a **Bind** button each; the channel's own folder is marked when it is listed. At most 20 are shown, in path order; when there are more, the higher levels fill the list first. A click never ends a running turn: it is refused until every session of the channel is idle | Refused, in an ephemeral message: `!bind works in the channel, not inside a thread.` |
| `!bind <folder>` | Binds the channel to a folder under `ALLOWED_ROOT`, given relative to it (`!bind my-project`); an absolute path inside it works too. A new channel does nothing else until bound. The answer names the folder each existing thread keeps working in, when it differs from the new one; every prompt sent in that thread after the bind gets the same notice, as an ephemeral message | Same refusal as above |
| `!bypass on` / `off` | Refused, in a post in the channel: `Bypass belongs to one session: send !bypass on inside its thread.` | Switches that session to `bypassPermissions` and back, kept in `state.json` per thread: an idle close and a restart of code-with-slack keep it, `!resume` starts a session with it off. The answer is a line only you see, `Bypass is on in this session: every tool runs without asking, until !bypass off. It survives a restart.` or `Bypass is off in this session: Claude Code asks again before tools that need approval.`, and a ✅ on your word, which stays after a reload. Before the setup's Start (the setup waits, or was cancelled) the word changes nothing and answers `This session has not started yet: tick Bypass in its setup and press Start. If no setup is shown, send a message here first.` |
| `!status` | The channel's directory, then every live session of it, each linked to its thread, busy, waiting or idle, its bypass and running tasks, and its folder when it moved elsewhere | That session's directory, session id and mode, the folder it works in when it moved elsewhere, then the footer's values one per line, in an ephemeral message |
| `!stop` | Stops every running session of the channel and its background tasks, and denies its pending approvals; the answer is `Stopped what was running in this channel.`, or `Nothing is running in this channel.`; each stopped session's root shows ✅ | Stops that session the same way; no message, ✅ on the session's root; when nothing runs, `Nothing is running in this session.` in an ephemeral message. A stop you gave is not an error, so it never shows ❌ |
| `!help [text]` | Lists code-with-slack's own words; Claude Code's own commands are listed inside a session's thread | Lists code-with-slack's own words and every command that session offers now, in an ephemeral message; with a text, only the lines whose name or description contains it, for example `!help model` |
| `!resume` | Lists the twenty newest sessions of the channel's directory (not of other worktrees), terminal and Slack alike, as a post in the channel, each with the first 8 characters of its session id and a **Resume** button, except a session already open in another thread of any channel, whose row shows a link to that thread instead. A click resumes the session in the thread of your `!resume` message and edits the list to say what was resumed and where; a list posted before that change answers `This list is out of date: send !resume again for a current one.` | Refused, in an ephemeral message: `!resume works in the channel, not inside a thread.` |
| `!resume <id or name>` | A Resume click, or the id (its first 8 characters, as the list shows them, or any longer start) or the name (set with `/rename` or generated by Claude Code, and must match one session), resumes that session in the thread of your `!resume` message, with bypass never chosen (the folder's own mode) and no `/effort` level set, whatever it had before; refused when the session is already open in another thread; it never touches or waits on any other thread | Same refusal as above |
| `!clear` (or `!reset`, `!new`) | Opens a new session (harmless: nothing to clear yet) | Refused: `One thread is one session: send a new message in the channel to start a new one.` |
| `!login`, `!logout` | Never sent to Claude Code, and no session starts. Answered in the channel: ``Log in on the host: on that machine, run `claude`, then `/login`. The login is never done from Slack.`` (for `!logout`: ``Log out on the host: on that machine, run `claude`, then `/logout`. It is never done from Slack.``) | Never sent to Claude Code. The same answer, as a message only you see |
| any other `!name [args]` | Opens a new session, asks its model, effort and bypass, and after **Start** runs `/name args` there if it offers that command, else sends the text as written | Runs `/name args` in that session if it offers the command, else sends the text as written |
| a message, or one with files | Opens a new session, asks its model, effort and bypass, and after **Start** sends the message; the reply appears in the thread and grows as Claude works | Continues that session |
| a question from Claude | Appears as one line with **Answer** and **Skip**; Answer opens a form with one question at a time, the options (one or several) and an **Other** field; **Next** moves on once the question has an answer, **Submit** on the last | Same |

A word typed in the channel is answered by a normal post in the channel, which stays there. A word
typed inside a session's thread is answered by an ephemeral message under it: Slack shows it with
`Only visible to you`, and it disappears when Slack reloads, so read it before you switch away.
The same holds for the notice that a thread keeps the folder it was opened in, and for `Not
sent.`. None of these rings a phone.

Slack does not pass a Claude Code command typed with its own slash: `/compact` alone makes Slack
answer that it is not a valid command. Type `!compact`. A `!word` that is not a command the
session offers is sent as a normal prompt, so `!important: …` reaches Claude as written.
`!resume` stands in for Claude Code's own `/resume`, which an SDK session does not offer. A reply
in a thread whose session no longer resumes (its transcript was deleted) gets `This thread's
session no longer exists: Claude Code deleted it or cannot find it. Send a new message in the
channel to start one.`; a reply in a thread that holds no session at all (a word's own thread, or
one from before this change) gets `This thread is not a session: send a new message in the
channel to start one.`, except a word, which acts as if typed at the top level. A Resume click or
`!resume <id or name>` sent to a thread that already holds a session gets
``This thread already holds a session: send `!resume` again in the channel to pick another.`` A Resume
click or
`!resume <id or name>` that names a session already open in another thread of any channel gets
`This session is already open in another thread: <link>.` instead, with a permalink to that thread: one
session never runs in two threads at once.

A message with files: Claude sees a JPEG, PNG, GIF or WebP image (up to 7.5 MB and 8000x8000 px;
at most 5 images and 15 MB of images per message) as an image. Any other image type (SVG, HEIC,
TIFF...) is refused. A text, source code, PDF, JSON, XML, YAML or Jupyter notebook file, up to 100
MB, reaches Claude as the path of a copy in `$TMPDIR/code-with-slack/`, kept 3 days; any other
file (archives, Office documents, binaries) is refused. A file past a limit, or one that fails to
download, stops the whole message, and the reply says which file and why.

A reply is one message in the session's thread, and a native Slack stream. It appears when Claude
has something to show: its first words, or the card of the first tool it uses. Text then grows in
the order it is written. The tool calls between two pieces of text show as two cards: the first
counts what ended, in the terminal's words (`Ran 2 shell commands · Read 1 file`), and the second
names the call that runs now. A subagent's card counts the calls it made and shows what it is
doing now, and a background task keeps its card open until it ends. An Edit or a Write shows its
preview under its card. When the reply ends, a divider and the footer close the same message, and
each pair of cards gives way to one line (`✓ Ran 2 shell commands · Read 1 file`). A reply still
running 280 seconds after it started stops being a stream (Slack closes streams at 5 minutes) and
goes on in the same message, updated instead of streamed; what Claude wrote after its last call
then arrives in a new message, with the footer under it. A reply longer than one Slack message (12,000 characters or 50 cards) continues in the next one.

An approval request is a message of its own in the thread, below the reply, and rings, as a
question does; once you decide, it disappears and the tool's line in the reply records the call.
If Slack does not accept the request, Claude Code is told it was denied because it could not be
shown.

Messages sent to a thread while its turn is running wait their turn; each gets its own reply. When
a background task finishes while nothing runs, Claude Code starts a turn of its own to report it,
as it does in the terminal: that reply opens with Claude Code's own line for the task's end, such
as `✓ Agent "review" finished · 3m 59s`. While tasks run, the footer counts them, such as
`⏳ 1 shell · 1 agent`.

From the moment you send a message until its turn ends, Slack shows `Working…` under the
thread's last message. When the turn has ended and a command or an agent it started still
runs, the same line reads `1 shell still running` and follows the count; the reply gets its
footer once that task and its report have ended. If you sent another message meanwhile and its
reply has ended, that reply's footer says it instead (`⏳ 1 shell`), until the command ends.
The line goes while an approval or a question waits for you.

What code-with-slack says on its own (the answer to `!bind`, `!bypass` or `!stop`, a notice that
it is restarting, a refused attachment, an error) shows small and grey, as the footer does, so it
reads apart from Claude's replies. `!help`, `!guide`, `!status` and the answer to a resume show at
full size.

Every reply ends with a footer, which says how the session stood when its turn ended, and
keeps it:

```
⚡ bypass · claude-opus-5-5 · effort medium · my-project · main · (+42,-10) · 10.2M tok · ctx 15% · 5h 16% ↻ 43m · 7d 46% ↻ 3d 4h
```

It holds `⚡ bypass` when bypass is on, the model, the effort level, the name of the folder this
thread was opened in (its whole path is on `!status`), the git branch, the lines changed since
the last commit (untracked files not counted), the session's tokens, the context used, and the
5-hour and weekly limits with the time to each reset, which exist only with a claude.ai
subscription. The labels (`effort`, `tok`, `ctx`, `5h`, `7d`) are bold. A field that is not known
is left out.
The branch and the changes are those of the folder the session works in: the thread's own folder
at first, then the one Claude moved to with `cd` or a worktree, as Claude Code reported it after
its last tool; `!status` names that folder when it is not the thread's own. The effort level is
the one Claude Code reported at the end of the last turn, or the one set since with `!effort` (or
`!model`); it reads `default` on a model that takes no effort level. A level set with `!effort` is
kept for this thread and reapplied on every reconnect, so it survives an idle close and a restart
of code-with-slack: the footer shows that level at once after the reconnect, until Claude Code's
own report at the end of the next turn corrects it. With no stored level, the field is left out
after a restart or an idle close until a turn ends normally (an interrupted turn or an API error
reports no level either way). A model set with `!model` needs no such help, since Claude Code's
own resume keeps it by itself.

### Notifications

A reply, an approval request and a question post inside the session's own thread, and Slack
notifies you on a new message in a thread you started, whatever the channel's own notification
setting is:

- A reply rings once, when it ends: Slack pushes a stream when it stops, with the start of
  Claude's answer as the text. The reply ends once the turn has ended and every task it started,
  and the report for it, are done too.
- A reply that is still open after about 4 minutes 40 seconds rings when its stream stops, and a
  second time when it ends: what Claude wrote after its last call and the footer arrive as a
  new message, and the notification reads the start of it.
- An approval request and a question ring.
- A reply that fails outright rings once, as any reply ending does.
- `!stop` and a restart end the reply the same way, so each rings once. A reply longer than one
  message rings for every extra message.
- Nothing rings while Claude writes.

The channel's own setting (Part 1) governs only the bot's top-level messages (the answer to `!bind`, `!status`, `!resume`'s
own list, the upgrade notice): with **Just mentions** those stay silent, since the bot writes no
`@channel` mention anywhere; with **All new posts** they ring too.

The measurements behind this (what was tried, on what Slack plan and app, and on what date) are
in [features.md](features.md#notifications), not restated here.

## Troubleshooting

| Symptom | Cause |
|---|---|
| The bot does not see a channel | The channel is public, or the bot was not invited |
| `Claude Code is not logged in on the host` | Claude Code on the machine is logged out: run `claude`, then `/login` |
| Some messages get no reply | A second instance is running and receiving part of the events |
| `This thread's session no longer exists: Claude Code deleted it or cannot find it. Send a new message in the channel to start one.` | The stored session id can no longer be resumed (its transcript was deleted); send a new message in the channel to start one |
| `This thread is not a session: send a new message in the channel to start one.` | The thread holds no session (a word's own thread, or one from before this change); send a new message in the channel to start one, or type a word, which works as if typed there |
| ``This thread already holds a session: send `!resume` again in the channel to pick another.`` | A Resume click or `!resume <id or name>` was sent to a thread that already has a session; a new `!resume` in the channel gives a thread of its own |
| `This session is already open in another thread: <link>.` | A Resume click or `!resume <id or name>` named a session already open in some other thread; follow the link and send the message there instead |
| `Claude Code has not been trusted in ...`, after a message or a `!bind` | Open `claude` in that folder (the repository root) in the terminal, accept the trust dialog, and send the message again |
| `The directory ... no longer exists` | The thread's directory was moved or deleted: bind the channel again with `!bind <path>` for the next thread |
| `macOS does not let code-with-slack read ...` | The directory is in a folder macOS protects: see Part 4, "Folders macOS protects" |
| `another code-with-slack is running` in the log | A second instance tried to start; only one may run |
| The app has no **Home** tab, or it stays empty | **Home Tab** is off in the app's settings, and the log says `the Home tab is not enabled in the Slack app`: turn it on under **App Home**, **Show Tabs**, then restart code-with-slack |
| `forgot channel ...: Slack no longer has it` in the log | The channel was deleted, or the bot was removed from it: its binding and its threads are gone from `state.json`. If the bot is back in it, send `!bind` there again |
| `Slack finds none of the ... bound channels: nothing is forgotten` in the log | Every bound channel is out of the bot's reach: check that `SLACK_BOT_TOKEN` is the one of this workspace and that the bot is still in its channels |
| A session is missing from the Home tab | The page starts on `Last 48 hours`: choose `Any time`. A thread whose root message was deleted, and a channel the bot is no longer in, are left out |
