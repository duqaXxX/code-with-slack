"""Every sentence the owner can read in Slack. English only; one place to review the wording."""

UNBOUND = (
    "This channel is not bound to a directory yet. Run `/cc bind <path>` with a path under "
    "the allowed root."
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
    "The directory `{directory}` no longer exists. Bind this channel again with `/cc bind <path>`."
)
STALE_SESSION = (
    "The previous session could not be resumed ({error}). This reply starts a new session."
)
ERROR_REPLY = "Claude Code reported an error: `{error}`"
BACKGROUND_ROOT = "Background task update"
COMMAND_ROOT = "`/{command}`"
BIND_OK = "Bound this channel to `{directory}`. The next message starts a new session there."
BIND_OUTSIDE = "`{path}` is not a directory under the allowed root `{root}`."
BYPASS_ON = (
    "Bypass is on in this channel: Claude Code runs every tool without asking, until "
    "`/cc bypass off` or a restart of code-with-slack."
)
BYPASS_OFF = "Bypass is off. Claude Code is back in its `{mode}` mode."
STOPPED = "Stopped the current turn."
NOTHING_TO_STOP = "Nothing is running in this channel."
STATUS = (
    "Directory: `{directory}`\nSession: `{session}`\nMode: `{mode}`\n"
    "Claude Code: `{version}`\nNow: {activity}"
)
ACTIVITY_IDLE = "idle"
ACTIVITY_BUSY = "running a turn, {queued} queued"
USAGE = (
    "Usage: `/cc <command> [args]`, `/cc bind <path>`, `/cc bypass on|off`, `/cc status`, "
    "`/cc stop`, or `/cc` alone to choose a command."
)
PICKER_PROMPT = "Choose a Claude Code command to run in this channel's session."
PICKER_PLACEHOLDER = "Type to filter commands"
APPROVAL_PROMPT = "Claude Code asks to use *{tool}*"
APPROVED = "Approved: {title}"
DENIED = "Denied: {title}"
DENY_MESSAGE = "The owner denied this from Slack."
ANSWERED = "Answered: {title}"
SKIPPED = "Skipped: {title}"
SKIP_MESSAGE = "The owner dismissed the question without answering."
QUESTION_INCOMPLETE = "Answer every question before submitting."
APPROVAL_GONE = "This request is no longer pending: the turn ended or code-with-slack restarted."
