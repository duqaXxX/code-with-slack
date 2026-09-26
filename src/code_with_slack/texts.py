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
STALE_SESSION = (
    "The previous session could not be resumed ({error}). This reply starts a new session."
)
ERROR_REPLY = "Claude Code reported an error: `{error}`"
WRITING = "_Claude is writing…_"
WAITING = "_Waiting for the previous reply…_"
ENDED = "_This reply ended before an answer: {reason}._"
ENDED_REBOUND = "the channel was bound to another directory"
ENDED_SHUTDOWN = "code-with-slack stopped"
ENDED_RESUMED = "the channel resumed another session"
ENDED_RESTARTING = "code-with-slack is restarting; send your message again in a moment"
RESTARTING = "code-with-slack is restarting; send this again in a moment."
REPLY_ABOVE = "_This reply appeared in the background update above._"
BACKGROUND_NOTICE = "_Background task update_"
COMPACTED = "Compacted the conversation: {before} → {after} tokens."
COMPACTED_PLAIN = "Compacted the conversation."
NO_OUTPUT = "_Done. Claude Code returned no text._"
BIND_OK = "Bound this channel to `{directory}`. The next message starts a new session there."
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
PROMPT_REBOUND = (
    "Nothing was sent to Claude: the channel was bound to another folder while this message "
    "waited. Send it again if it is meant for the new folder."
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
    "A turn or a background task is running or waiting in this channel: binding another folder "
    "would end it. Let it finish or `!stop` it, then bind."
)
BYPASS_ON = (
    "Bypass is on in this channel: Claude Code runs every tool without asking, until "
    "`!bypass off` or a restart of code-with-slack."
)
BYPASS_OFF = "Bypass is off. Claude Code is back in its `{mode}` mode."
STOPPED = "Stopped the current turn."
NOTHING_TO_STOP = "Nothing is running in this channel."
STATUS = (
    "Directory: `{directory}`\nSession: `{session}`\nMode: `{mode}`\n"
    "Claude Code: `{version}`\nNow: {activity}"
)
ACTIVITY_IDLE = "idle"
VERSION_PENDING = "started, version shown after the first turn"
STATUS_BACKGROUND = "Background: `{counts}`"
RUNNING = "⏳ {counts}"
ACTIVITY_BUSY = "running a turn, {queued} queued"
HELP_OWN = "**code-with-slack**"
HELP_WORDS = (
    "`!guide` how code-with-slack works, in a few lines",
    "`!help [text]` this list, or only the lines that contain the text",
    "`!status` the channel's directory, session and mode, then the footer's values",
    "`!stop` stop the running turn and deny its pending approvals",
    "`!bind [folder]` the folders Claude Code trusts, or bind this channel to one, its path "
    "relative to the allowed root",
    "`!bypass on|off` run every tool without asking, until off or a restart",
    "`!resume [session]` this directory's sessions, or resume one by id or name",
)
HELP_NO_MATCH = "No command matches `{query}`."
HELP_CLAUDE = (
    "\n**Claude Code** (this session, now). Any other `!name args` runs that command; "
    "these words above come first."
)
HELP_UNBOUND = "\nClaude Code's own commands are listed here once the channel is bound."
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
RESUME_CURRENT = " · _current_"
RESUME_BUTTON = "Resume"
RESUME_MORE = (
    "Only the newest {rows} are shown: `!resume <title>` resumes an older session that has a "
    "title, and `claude --resume` in the terminal lists them all."
)
RESUME_ALREADY = "This channel is already on that session."
RESUME_OK = (
    "Resumed **{title}**: your next message continues it. If it is open in a terminal, close it "
    "there first, or the messages of both will mix in one conversation."
)
RESUME_BUSY = (
    "A turn or a background task is running or waiting in this channel: resuming would end it. "
    "Let it finish or `!stop` it, then resume."
)
RESUME_NONE = "No session in `{directory}` has the id or name `{target}`: `!resume` lists them."
RESUME_AMBIGUOUS = (
    "More than one session in `{directory}` is named `{target}`: pick one from `!resume`."
)
RESUME_GONE = "That session is not in this channel's directory any more: `!resume` lists them."
# `!guide`: how to use the bot, in the owner's words. tests/test_commands.py fails when a word of
# the daemon is missing here; keep the tone plain and every line true of the current behaviour.
GUIDE = """**code-with-slack**
This channel runs Claude Code on your Mac, in one folder, and answers you alone.

**Get started**
1. `!bind` lists the folders Claude Code trusts, with a Bind button each; `!bind <folder>` \
binds one by its path relative to your allowed root: `!bind my-project`. Claude Code must trust \
that folder first: open `claude` there once in the terminal and accept.
2. Write a message: it is a prompt. The reply appears below it and grows as Claude works, \
with a line for each tool it uses. Attach images or files to it: Claude sees a JPEG, PNG, GIF or \
WebP image directly (other image types are refused) and reads a text, code, PDF, JSON, XML, YAML \
or notebook file from a copy saved on this Mac; other files are refused.

**Commands**
Claude Code's commands start with `!` instead of `/`: `!compact`, `!model opus`, `!clear`. \
`!help` lists every command this session offers, and `!help <text>` filters the list.

**Approvals and questions**
When Claude Code asks permission, the request shows what will run, with **Approve** and \
**Deny**; a very long one shows its start and its end and says how much it leaves out. A \
question from Claude comes with **Answer**, which opens a short form, and **Skip**.

**Sessions**
`!status` shows the folder, the session, the permission mode and the footer's values. \
`!stop` stops the running turn. `!resume` lists this folder's twenty newest sessions, from \
the terminal too, with a **Resume** button each; `!resume <title>` resumes a session that has a \
title directly.

**Bypass**
`!bypass on` lets Claude Code run every tool without asking, until `!bypass off` or a restart. \
The footer shows ⚡ bypass while it is on.

`!guide` shows this text again."""
