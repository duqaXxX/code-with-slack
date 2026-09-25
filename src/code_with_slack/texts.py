"""Every sentence the owner can read in Slack. English only; one place to review the wording."""

UNBOUND = (
    "This channel is not bound to a directory yet. Send `!bind <path>` with a path under "
    "the allowed root, or `!help` for every command."
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
REPLY_ABOVE = "_This reply appeared in the background update above._"
BACKGROUND_NOTICE = "_Background task update_"
COMPACTED = "Compacted the conversation: {before} → {after} tokens."
COMPACTED_PLAIN = "Compacted the conversation."
NO_OUTPUT = "_Done. Claude Code returned no text._"
BIND_OK = "Bound this channel to `{directory}`. The next message starts a new session there."
BIND_OUTSIDE = "`{path}` is not a directory under the allowed root `{root}`."
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
RUNNING = "⏳ {counts}"
ACTIVITY_BUSY = "running a turn, {queued} queued"
HELP_OWN = "*code-with-slack*"
HELP_WORDS = (
    "`!help [text]` this list, or only the lines that contain the text",
    "`!status` the channel's directory, session and mode",
    "`!stop` stop the running turn and deny its pending approvals",
    "`!bind <path>` bind this channel to a directory under the allowed root",
    "`!bypass on|off` run every tool without asking, until off or a restart",
)
HELP_NO_MATCH = "No command matches `{query}`."
HELP_CLAUDE = (
    "\n*Claude Code* (this session, now). Any other `!name args` runs that command; "
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
