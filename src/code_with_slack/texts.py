"""Every sentence the owner can read in Slack. English only; one place to review the wording."""

UNBOUND = (
    "This channel is not bound to a folder yet. Send `!bind` to choose one of the folders "
    "Claude Code trusts, or `!bind <folder>` with the folder's path relative to `{root}`, for "
    "example `!bind my-project`. `!guide` explains how code-with-slack works."
)
AUTH_FAILED = (
    "Claude Code is not logged in on the host. On that machine, run `claude`, then `/login`. "
    "The login is never done from Slack."
)
LOGIN_ON_HOST = (
    "Log in on the host: on that machine, run `claude`, then `/login`. "
    "The login is never done from Slack."
)
LOGOUT_ON_HOST = (
    "Log out on the host: on that machine, run `claude`, then `/logout`. "
    "It is never done from Slack."
)
CHANNEL_REFUSED = (
    "code-with-slack does not work in this channel: {reason}. It answers only in a private "
    "channel whose only members are you and the bot."
)
REASON_NOT_PRIVATE = "it is not a private channel"
REASON_SHARED = "it is shared with another workspace"
REASON_MEMBERS = "it has members other than you and the bot"
REASON_UNREADABLE = "the bot cannot read its details"
DIRECTORY_MISSING = (
    "The directory `{directory}` no longer exists. Bind this channel again with `!bind <path>`."
)
DIRECTORY_UNTRUSTED = (
    "Claude Code has not been trusted in `{directory}`, so its hooks and settings would run "
    "without asking. Open `claude` there in the terminal once and accept the trust dialog, then "
    "send your message again."
)
DIRECTORY_UNREADABLE = (
    "macOS does not let code-with-slack read `{directory}`. Grant access in System Settings, "
    "Privacy & Security, Full Disk Access (see docs/setup.md, Part 4)."
)
SESSION_GONE = (
    "This thread's session no longer exists: Claude Code deleted it or cannot find it. Send a "
    "new message in the channel to start one."
)
NOT_A_SESSION = "This thread is not a session: send a new message in the channel to start one."
WORD_IN_THREAD = "`!{word}` works in the channel, not inside a thread."
CLEAR_IN_THREAD = "One thread is one session: send a new message in the channel to start a new one."
UPGRADE_NOTICE = (
    "code-with-slack now runs one Claude Code session per thread. Send a new message in the "
    "channel to start a session; reply in its thread to continue it. The session this channel "
    "had is still in the folder: !resume brings it into a thread. Bypass is now set per "
    "session: send !bypass on inside a thread."
)
ERROR_REPLY = "Claude Code reported an error: `{error}`"
ENDED = "_This reply ended before an answer: {reason}._"
ENDED_SHUTDOWN = "code-with-slack stopped"
# Crash repair (issue #19): what a reply the daemon died in the middle of ends with.
STOPPED_BEFORE_ANSWER = "code-with-slack stopped before this answer."
# Messages a restart or a failure dropped from the queue (S3): they get no reply of their own,
# one note names them. `{because}` is one of the two phrases below.
NOT_SENT_ONE = "1 message was not sent because {because}: send it again."
NOT_SENT_MANY = "{count} messages were not sent because {because}: send them again."
BECAUSE_RESTARTED = "code-with-slack restarted"
BECAUSE_SHUTDOWN = "code-with-slack stopped"
BECAUSE_STOPPED = "Claude Code stopped"
ENDED_RESTARTING = "code-with-slack is restarting; send your message again in a moment"
# Never shown: an idle close (D9) always finds nothing running, sent or queued to end with it.
ENDED_IDLE = "code-with-slack closed this idle session"
RESTARTING = "code-with-slack is restarting; send this again in a moment."
# Under RESTARTING, and after a channel's `!status`, while a stop waits: one row per thread that
# holds it (issue #119). mrkdwn.
RESTART_WAITS_FOR = "It is waiting for:"
RESTART_WAITS_HEADER = "code-with-slack is restarting. " + RESTART_WAITS_FOR
RESTART_WAIT_ROW = "• <#{channel}>, {link}: {hold}"
RESTART_WAIT_SESSION = "Session"  # the link's label for a session with no title yet
RESTART_WAIT_STOP = "`!stop` in a thread ends the wait there."
RESTART_WAITS_MORE = "…and {count} more."
# In a channel's `!status`, a post every member reads: the threads of other channels are counted
# and not named.
RESTART_WAITS_ELSEWHERE = "{count} more in other channels."
RESTART_HOLD_OWNER = "an approval or a question is waiting for you"
RESTART_HOLD_TURN = "a turn is running"
RESTART_HOLD_TASKS = "{counts} running"
# Ends by itself (`INJECTED_TURN_WAIT`): `!stop` has nothing to stop there.
RESTART_HOLD_REPORT = "a background task is about to report, within 30 seconds"
BACKGROUND_NOTICE = "_Background task update_"
PROMPT_IMAGE = "an image"
COMPACTED = "Compacted the conversation: {before} → {after} tokens."
COMPACTED_PLAIN = "Compacted the conversation."
NO_OUTPUT = "_Done. Claude Code returned no text._"
BIND_OK = "Bound this channel to `{directory}`. The next message starts a new session there."
# D5: an old thread's session keeps the folder it was created in; `{old}` names each of them
# (comma-separated, backticked).
BIND_OK_ELSEWHERE = (
    "Bound this channel to `{directory}`. New messages start sessions there; existing threads "
    "keep working in {old}, where their sessions live."
)
BIND_UNAVAILABLE = (
    "Bound this channel to `{directory}`, but no session can start there yet. {reason}"
)
# D5: shown, ephemeral, before every prompt in a thread whose folder differs from the channel's.
OLD_THREAD_FOLDER = (
    "This session works in `{old}`, the folder it was created in: Claude Code resumes a session "
    "only there. New messages in the channel use `{new}`."
)
BIND_OUTSIDE = (
    "`{path}` is not a folder under `{root}`. Give its path relative to that folder, for "
    "example `!bind my-project`."
)
UPLOAD_FAILED = "Nothing was sent to Claude: {name} {reason}. Send the message again without it."
UPLOAD_IMAGE_TYPE = (
    "is an image of type `{mimetype}`, and Claude reads only JPEG, PNG, GIF and WebP images"
)
UPLOAD_IMAGE_SIZE = "is {size}, over the {limit} Claude accepts for an image"
UPLOAD_IMAGE_SIDE = "is {width}x{height} px, over the 8000x8000 px Claude accepts for an image"
UPLOAD_FILE_TYPE = (
    "is a `{mimetype}` file, and code-with-slack passes on only text, source code, PDF, JSON, "
    "XML, YAML and notebook files"
)
UPLOAD_FILE_SIZE = "is {size}, over the {limit} limit for a file"
UPLOAD_TOO_MANY = (
    "Nothing was sent to Claude: the message has {count} images, over the {limit} one message "
    "takes."
)
UPLOAD_TOO_HEAVY = (
    "Nothing was sent to Claude: its images total {size}, over the {limit} one message takes."
)
SESSION_CLOSED = (
    "Nothing was done: this session closed while it ran (idle for a while, or the daemon "
    "restarted). Send it again if it is still meant."
)
UPLOAD_NOT_SHARED = "is not a file shared in this channel that code-with-slack can download"
UPLOAD_DOWNLOAD = "could not be downloaded ({error})"
BIND_LIST = "Folders under `{root}` that Claude Code trusts:"
BIND_EMPTY = (
    "No folder under `{root}` is trusted by Claude Code yet. Open one in the terminal with "
    "`claude` and accept the trust dialog, then send `!bind` again."
)
BIND_MORE = "Only the first {rows} are shown: `!bind <folder>` binds any other."
BIND_CURRENT = " · _current_"
BIND_BUTTON = "Bind"
BIND_ALREADY = "This channel is already bound to `{directory}`."
BIND_BUSY = (
    "Sessions are running in this channel: binding another folder now would cut their work. "
    "Let them finish or send `!stop`, then bind again."
)
BYPASS_TOP_LEVEL = "Bypass belongs to one session: send `!bypass on` inside its thread."
# True with the setup on screen and without it (after a stop, a restart or a Cancel).
BYPASS_BEFORE_START = (
    "This session has not started yet: tick Bypass in its setup and press Start. "
    "If no setup is shown, send a message here first."
)
BYPASS_ON_THREAD = (
    "Bypass is on in this session: every tool runs without asking, until `!bypass off`. "
    "It survives a restart."
)
BYPASS_OFF_THREAD = (
    "Bypass is off in this session: Claude Code asks again before tools that need approval."
)
STOPPED = "Stopped the current turn."
# An answered question, as the terminal keeps it in the transcript.
ANSWERED = "User answered Claude's questions:"
# Slack drops plain spaces at the start of a line; no-break spaces stay and make the indent.
NESTED = "\u00a0" * 4 + "⎿ "
STOPPED_CHANNEL = "Stopped what was running in this channel."
# The thread's status line while a restart waits for background tasks only, and the same after
# the app's name. Never a message: it would notify and stay in the thread after the restart.
RESTART_WAITS = "Restart waits for {counts} · !stop ends {them} now"
RESTART_WAITS_STATUS = "is waiting to restart: {counts} still running"
# The same as a message, only where Slack refuses the app a thread status.
RESTART_WAITS_MESSAGE = (
    "code-with-slack is restarting once these background tasks end: {counts}. "
    "`!stop` ends them now."
)
NOTHING_TO_STOP = "Nothing is running in this channel."
# Both answers to `!stop` in a thread are posts that stay: an ephemeral line is gone on reload.
STOPPED_THREAD = "Stopped."
NOTHING_TO_STOP_THREAD = "Nothing is running in this session."
HOLD_UNPOSTED = (
    "code-with-slack could not show this question in Slack, so the message was not sent. "
    "Send it again."
)
HOLD_GONE = (
    "This question is no longer open: it was already answered, or code-with-slack restarted."
)
NOT_SENT = "Not sent."
# Session setup: asked once per top-level message, before the first prompt of a new session.
SETUP_FALLBACK = "Set up this session"
SETUP_HEADER = "*Choose how this session starts*"
SETUP_EFFORT_OPTION = "Effort: {level}"
SETUP_EFFORT_DEFAULT = "Default"
SETUP_BYPASS_OPTION = "Bypass permissions"
SETUP_BYPASS_DESCRIPTION = "Run every tool without asking, until `!bypass off`."
SETUP_START_BUTTON = "Start"
SETUP_SUMMARY = "Model: {model} · Effort: {effort} · Bypass: {bypass}"
STATUS = (
    "Directory: `{directory}`\nSession: `{session}`\nMode: `{mode}`\n"
    "Claude Code: `{version}`\nNow: {activity}"
)
ACTIVITY_IDLE = "idle"
VERSION_PENDING = "started, version shown after the first turn"
STATUS_BACKGROUND = "Background: `{counts}`"
STATUS_WORKING = "Working in: `{directory}`"
RUNNING = "⏳ {counts}"
# Slack's status line under a thread's last message (issues #83 and #95). A client shows the
# loading message: `THREAD_WORKING` while a prompt is on its way or a turn runs, and
# `STILL_RUNNING` once the turn has ended and a task it started still runs, the words the
# terminal ends such a turn with (`· 1 shell still running`, Claude Code 2.1.287, read
# 2026-10-02), with no hourglass: an emoji draws large and grey in a status line (seen on
# desktop, 2026-10-02). `THREAD_WORKING_STATUS` and
# `STILL_RUNNING_STATUS` say the same after the app's name, which is what a
# client that draws `<app name> <status>` shows (measured 2026-10-02, desktop and iOS).
THREAD_WORKING = "Working…"
STILL_RUNNING = "{counts} still running"
THREAD_WORKING_STATUS = "is working…"
STILL_RUNNING_STATUS = "has {counts} still running"
ACTIVITY_BUSY = "running a turn, {queued} queued"
# `!status` sent to the channel (top-level, or a thread that holds no session): the channel's
# folder, then one line per live session, each with a link to its thread.
STATUS_CHANNEL_HEADER = "Directory: `{directory}`"
STATUS_CHANNEL_EMPTY = "No live session in this channel."
STATUS_CHANNEL_ROW = "{link}: {activity}"
STATUS_CHANNEL_WAITING = "waiting for you"
STATUS_CHANNEL_BUSY = "busy"
STATUS_CHANNEL_IDLE = "idle"
STATUS_CHANNEL_BYPASS = " · ⚡ bypass"
STATUS_CHANNEL_FOLDER = " · folder `{directory}`"
STATUS_CHANNEL_LINK_FALLBACK = "thread `{thread_ts}`"
HELP_OWN = "**code-with-slack**"
HELP_WORDS = (
    "`!guide` how code-with-slack works, in a few lines; in the channel or inside a thread",
    "`!help [text]` this list, or only the lines that contain the text; in the channel or "
    "inside a thread",
    "`!status` in the channel: every session's state; inside a thread: that session's "
    "directory, mode and the footer's values",
    "`!stop` in the channel: every running session, its background tasks and its pending "
    "approvals; inside a thread: only that session",
    "`!bind [folder]` the folders Claude Code trusts, or bind this channel to one, its path "
    "relative to the allowed root; in the channel, refused inside a thread",
    "`!bypass on|off` run every tool without asking, until `!bypass off`; it survives a "
    "restart; inside a thread, refused in the channel",
    "`!resume [session]` this directory's sessions, or resume one by id or name in the "
    "thread of your `!resume` message; in the channel, refused inside a thread",
)
HELP_NO_MATCH = "No command matches `{query}`."
HELP_CLAUDE = (
    "\n**Claude Code** (this session, now). Any other `!name args` runs that command; "
    "these words above come first."
)
HELP_UNBOUND = "\nClaude Code's own commands are listed inside a session's thread."
APPROVAL_PROMPT = "Claude Code asks to use *{tool}*"
DENY_MESSAGE = "The owner denied this from Slack."
SKIP_MESSAGE = "The owner dismissed the question without answering."
QUESTIONS_ONE = "Claude Code has a question"
QUESTIONS_MANY = "Claude Code has {count} questions"
QUESTION_ANSWER = "Answer"
QUESTION_TITLE = "Claude Code asks"
QUESTION_CLOSE = "Later"
QUESTION_NOT_OPENED = "The form could not open (`{error}`). Click Answer again."
QUESTION_MISSING = "Choose an option or type your own answer."
QUESTION_WHERE = "{number} of {count}"
QUESTION_NEXT = "Next ({number}/{count})"
QUESTION_OTHER = "Other"
QUESTION_OTHER_HINT = "Or type your own answer"
APPROVAL_CUT = "_{count} characters of this request are not shown: Deny it unless you know them._"
APPROVAL_UNPOSTED = "code-with-slack could not show this request in Slack, so nobody approved it."
APPROVAL_GONE = "This request is no longer pending: the turn ended or code-with-slack restarted."
RESUME_LIST = "Sessions in `{directory}`, newest first:"
RESUME_EMPTY = "No sessions in `{directory}` yet."
# Every session of the folder is already open in a thread: the list has no row to offer.
RESUME_NONE_LEFT = "No session to resume in `{directory}`."
# Under the list: the sessions a thread already holds (D6), counted and not listed (issue #69).
RESUME_OPEN_ONE = "1 more is open in its own thread."
RESUME_OPEN_MANY = "{count} more are open in their own threads."
RESUME_BUTTON = "Resume"
RESUME_MORE = (
    "Only the newest {rows} are shown: `!resume <id>`, or `!resume <title>` for a session that "
    "has one, resumes an older session."
)
RESUME_OK = (
    "Resumed **{title}**: your next message continues it. If it is open in a terminal, close it "
    "there first, or the messages of both will mix in one conversation."
)
RESUME_NONE = "No session in `{directory}` has the id or name `{target}`: `!resume` lists them."
RESUME_AMBIGUOUS = (
    "More than one session in `{directory}` is named or starts with `{target}`: pick one from "
    "`!resume`."
)
RESUME_STALE = "This list is out of date: send `!resume` again for a current one."
RESUME_GONE = "That session is not in this channel's directory any more: `!resume` lists them."
RESUME_HELD = (
    "This thread already holds a session: send `!resume` again in the channel to pick another."
)
# What the list is rewritten to after a Resume click, only when it cannot be deleted.
RESUME_LISTED = "Resumed {title} in {link}."
# D6: a session held by any thread of any channel is never resumed a second time; `{link}` is the
# holding thread's permalink (a plain fallback when Slack would not give one).
RESUME_ELSEWHERE = "This session is already open in another thread: {link}."
# The app's Home tab: the sessions the threads hold, by channel, newest activity first, under
# four filters. `{time}` is Slack's own date token, shown in the reader's time zone.
HOME_HEADER = "Sessions by channel, newest first · updated {time}"
HOME_EMPTY = "No channel is bound yet: `!bind` in a private channel binds it to a folder."
HOME_NO_SESSIONS = "No sessions yet."
HOME_NO_MATCH = "No session matches these filters."
HOME_MORE = "Only the first {rows} sessions are shown: narrow the filters."
HOME_OPEN = "Open"
HOME_NEW_THREAD = "New thread"
HOME_SHOW_ALL = "Show all {count}"
HOME_UNTITLED = "Session {id}"
HOME_REPLY = "1 reply"
HOME_REPLIES = "{count} replies"
HOME_LAST_REPLY = "last reply {when}"
HOME_STARTED = "started {when}"
HOME_WORKING = "working"
HOME_WAITING = "waiting for you"
HOME_ENDED = "ended"
HOME_ERROR = "error"
HOME_ALL_CHANNELS = "All channels"
HOME_ALL_STATUSES = "All statuses"
HOME_ANY_TIME = "Any time"
HOME_LAST_48 = "Last 48 hours"
HOME_TODAY = "Today"
HOME_YESTERDAY = "Yesterday"
HOME_LAST_7 = "Last 7 days"
HOME_LAST_30 = "Last 30 days"
HOME_SEARCH_LABEL = "Search titles"
HOME_SEARCH_HINT = "Type a word and press Enter"
# `!guide`: how to use the bot, in the owner's words. tests/test_commands.py fails when a word of
# the daemon is missing here; keep the tone plain and every line true of the current behaviour.
GUIDE = """**code-with-slack**
This channel runs Claude Code on your Mac, in one folder, and answers you alone. One Slack \
thread is one Claude Code session: a top-level message starts a new one, and a reply inside its \
thread continues it, even days later.

**Get started**
1. `!bind` lists the folders Claude Code trusts, with a Bind button each; `!bind <folder>` \
binds one by its path relative to your allowed root: `!bind my-project`. Claude Code must trust \
that folder first: open `claude` there once in the terminal and accept. `!bind` works only as a \
top-level message in the channel.
2. Write a message in the channel: it opens a session, and its reply appears in a thread of its \
own, growing as Claude works, with a line for each tool it uses. Reply inside that thread to \
continue the same session. Attach images or files to a message: Claude sees a JPEG, PNG, GIF or \
WebP image directly (other image types are refused) and reads a text, code, PDF, JSON, XML, YAML \
or notebook file from a copy saved on this Mac; other files are refused.
3. Before that first message is sent, the thread asks for the **Model**, the **Effort** \
(`Default` leaves Claude Code's own choice) and **Bypass permissions**; **Start** sends your \
message with those choices, and `!stop` cancels it instead.

**Commands**
Claude Code's commands start with `!` instead of `/`: `!compact`, `!model opus`. They run inside \
a session's thread, where `!help` lists every command that session offers, and `!help <text>` \
filters the list; typed in the channel, `!help` lists code-with-slack's own words instead. \
`!clear` (`!reset`, `!new`) is refused inside a thread (one thread is one session): start a \
fresh session with a new top-level message instead.

**Approvals and questions**
When Claude Code asks permission, the request shows what will run, with **Approve** and \
**Deny**; a very long one shows its start and its end and says how much it leaves out. A \
question from Claude comes with **Answer**, which opens a short form, and **Skip**.

**Sessions**
`!status` typed in the channel lists the folder and every session still live, each linked to \
its thread; inside a thread it shows that session's directory, mode and the footer's values. \
`!stop` typed in the channel stops every running session and its background tasks; inside a \
thread it stops only that one. `!resume`, typed in the channel only, lists this folder's twenty \
newest sessions, from the terminal too, each with the start of its id and a **Resume** button; \
`!resume <id>` (that start is enough), or `!resume <title>` for a session that has one, resumes \
it in the thread of your `!resume` message. To continue a session in the terminal, run \
`claude --resume <id>` there with the full id `!status` shows.

**Bypass**
`!bypass on`, sent inside a session's thread, lets Claude Code run every tool without asking in \
that session, until `!bypass off`; a restart of code-with-slack keeps it. The footer shows ⚡ \
bypass while it is on.

`!guide` shows this text again."""
