/** Every sentence the owner can read in Slack. English only; one place to review the wording. */

/**
 * Fills a template's `{name}` fields the way Python's `str.format` did: every template below keeps
 * its `{name}` fields as written, and a caller fills one with this. A field without a value throws,
 * as Python's `KeyError` did.
 */
export function fill(template: string, values: Readonly<Record<string, string | number>>): string {
  return template.replace(/\{([a-z_]+)\}/g, (_field, name: string) => {
    const value = values[name];
    if (value === undefined) throw new Error(`no value for the field {${name}}`);
    return String(value);
  });
}

export const UNBOUND =
  "This channel is not bound to a folder yet. Send `!bind` to choose one of the folders " +
  "Claude Code trusts, or `!bind <folder>` with the folder's path relative to `{root}`, for " +
  "example `!bind my-project`. `!guide` explains how awaydesk works.";
export const AUTH_FAILED =
  "Claude Code is not logged in on the host. On that machine, run `claude`, then `/login`. " +
  "The login is never done from Slack.";
export const LOGIN_ON_HOST =
  "Log in on the host: on that machine, run `claude`, then `/login`. " +
  "The login is never done from Slack.";
export const LOGOUT_ON_HOST =
  "Log out on the host: on that machine, run `claude`, then `/logout`. " +
  "It is never done from Slack.";
export const CHANNEL_REFUSED =
  "awaydesk does not work in this channel: {reason}. It answers only in a private " +
  "channel whose only members are you and the bot.";
export const REASON_NOT_PRIVATE = "it is not a private channel";
export const REASON_SHARED = "it is shared with another workspace";
export const REASON_MEMBERS = "it has members other than you and the bot";
export const REASON_UNREADABLE = "the bot cannot read its details";
export const DIRECTORY_MISSING =
  "The directory `{directory}` no longer exists. Restore it to keep using this thread, or, in " +
  "the channel, bind another folder with `!bind <path>` and send a new message to start a " +
  "session there.";
export const DIRECTORY_UNTRUSTED =
  "Claude Code has not been trusted in `{directory}`, so its hooks and settings would run " +
  "without asking. Open `claude` there in the terminal once and accept the trust dialog, then " +
  "send your message again.";
export const DIRECTORY_UNREADABLE =
  "macOS does not let awaydesk read `{directory}`. Grant access in System Settings, " +
  "Privacy & Security, Full Disk Access (see docs/setup.md, Part 4).";
export const SESSION_GONE =
  "This thread's session no longer exists: Claude Code deleted it or cannot find it. Send a " +
  "new message in the channel to start one.";
// Names no cause: the daemon keeps no record of a thread whose entry it dropped, so it cannot
// tell a thread that never held a session from one whose session may still be resumable.
export const NOT_A_SESSION =
  "This thread holds no session. In the channel, send a new message to start one, or " +
  "`!resume` to continue an earlier one.";
export const OPEN_TOP_LEVEL =
  "Opening a file belongs to one session: send `!open` inside its thread.";
export const OPEN_FALLBACK = "Open a file";
export const OPEN_TITLE = "*Open a file*";
export const OPEN_BUTTON = "Choose a file";
export const OPEN_BY_NAME = "Or type `!open setup` to open a file by name.";
export const OPEN_NO_MATCH = "No file matches `{words}`.";
export const OPEN_NO_MATCH_PARTIAL =
  "No file matching `{words}` was found, but the folder could not be listed in full.";
export const OPEN_MATCHES = "*{count} files match* `{words}`";
export const OPEN_MATCHES_ONE = "*1 file matches* `{words}`";
export const OPEN_PARTIAL =
  "The folder could not be listed in full, so some files may be missing here.";
export const OPEN_MATCHES_CAPPED =
  "The first {shown} of {count} are listed: type more of the name to narrow it.";
export const OPEN_MATCHES_TOO_LONG =
  "None of their paths is short enough to list: open one with `!open <path>`.";
export const OPEN_MODAL_TITLE = "Open a file";
export const OPEN_MODAL_SUBMIT = "Open";
export const OPEN_MODAL_CLOSE = "Close";
export const OPEN_QUERY_LABEL = "Search any file";
export const OPEN_QUERY_HINT = "Type part of a name";
export const OPEN_ROWS_CHANGED = "Changed in this session ({count}), newest first";
export const OPEN_ROWS_MATCH = "{count} files match";
export const OPEN_ROWS_MATCH_ONE = "1 file matches";
export const OPEN_TYPE_A_NAME = "No changed files to show. Type part of a name to find a file.";
export const OPEN_LOADING = "Looking for files…";
export const OPEN_NONE_CHOSEN = "Choose a file first.";
export const OPEN_FORM_NOT_OPENED =
  "The file list could not open (`{error}`). Click Choose a file again.";
export const OPEN_NOT_A_FILE = "`{path}` is not a file inside this session's folder.";
export const OPEN_TOO_LARGE =
  "`{path}` is larger than 1 MB, the most Slack opens in its file viewer.";
export const OPEN_EMPTY = "`{path}` is empty: there is nothing to show.";
export const OPEN_NO_SCOPE =
  "Opening a file needs the `files:write` scope: add it to the app and reinstall it from " +
  "`slack-app-manifest.json` (docs/setup.md, Part 1).";
export const OPEN_FAILED = "Could not open `{path}`: {error}";
export const WORD_IN_THREAD = "`!{word}` works in the channel, not inside a thread.";
// `!stop` typed in a thread that holds no session: it would have stopped the whole channel.
export const STOP_OUTSIDE_SESSION =
  "This thread holds no session. Send `!stop` in the channel to stop every session, or inside " +
  "a session's thread to stop that one.";
export const CLEAR_IN_THREAD =
  "One thread is one session: send a new message in the channel to start a new one.";
export const UPGRADE_NOTICE =
  "awaydesk now runs one Claude Code session per thread. Send a new message in the " +
  "channel to start a session; reply in its thread to continue it. The session this channel " +
  "had is still in the folder: !resume brings it into a thread. Bypass is now set per " +
  "session: send !bypass on inside a thread.";
export const ERROR_REPLY = "Claude Code reported an error: `{error}`";
export const ENDED = "_This reply ended before an answer: {reason}._";
export const ENDED_SHUTDOWN = "awaydesk stopped";
// Crash repair (issue #19): what a reply the daemon died in the middle of ends with.
export const STOPPED_BEFORE_ANSWER = "awaydesk stopped before this answer.";
// Messages a restart or a failure dropped from the queue (S3): they get no reply of their own,
// one note names them. `{because}` is one of the two phrases below.
export const NOT_SENT_ONE = "1 message was not sent because {because}: send it again.";
export const NOT_SENT_MANY = "{count} messages were not sent because {because}: send them again.";
// Prompts Claude Code took into a running turn of its own (a report of a background task): no
// result of their own follows, so the reply of that turn says so at its end.
export const TAKEN_INTO_REPLY_ONE =
  "Claude Code took your message into this reply: send it again if it is not answered here.";
export const TAKEN_INTO_REPLY_MANY =
  "Claude Code took {count} messages into this reply: " +
  "send them again if they are not answered here.";
export const BECAUSE_RESTARTED = "awaydesk restarted";
export const BECAUSE_SHUTDOWN = "awaydesk stopped";
export const BECAUSE_STOPPED = "Claude Code stopped";
export const ENDED_RESTARTING = "awaydesk is restarting; send your message again in a moment";
// Never shown: an idle close (D9) always finds nothing running, sent or queued to end with it.
export const ENDED_IDLE = "awaydesk closed this idle session";
export const RESTARTING = "awaydesk is restarting; send this again in a moment.";
// Under RESTARTING, and after a channel's `!status`, while a stop waits: one row per thread that
// holds it (issue #119). mrkdwn.
export const RESTART_WAITS_FOR = "It is waiting for:";
export const RESTART_WAITS_HEADER = `awaydesk is restarting. ${RESTART_WAITS_FOR}`;
export const RESTART_WAIT_ROW = "• <#{channel}>, {link}: {hold}";
export const RESTART_WAIT_SESSION = "Session"; // the link's label for a session with no title yet
export const RESTART_WAIT_STOP = "`!stop` in a thread ends the wait there.";
export const RESTART_WAITS_MORE = "…and {count} more.";
// In a channel's `!status`, a post every member reads: the threads of other channels are counted
// and not named.
export const RESTART_WAITS_ELSEWHERE = "{count} more in other channels.";
export const RESTART_HOLD_OWNER = "an approval or a question is waiting for you";
export const RESTART_HOLD_TURN = "a turn is running";
export const RESTART_HOLD_TASKS = "{counts} running";
// Ends by itself (`INJECTED_TURN_WAIT`): `!stop` has nothing to stop there.
export const RESTART_HOLD_REPORT = "a background task is about to report, within 30 seconds";
export const BACKGROUND_NOTICE = "_Background task update_";
export const PROMPT_IMAGE = "an image";
export const COMPACTED = "Compacted the conversation: {before} → {after} tokens.";
export const COMPACTED_PLAIN = "Compacted the conversation.";
export const NO_OUTPUT = "_Done. Claude Code returned no text._";
export const BIND_OK =
  "Bound this channel to `{directory}`. The next message starts a new session there.";
// D5: an old thread's session keeps the folder it was created in; `{old}` names each of them
// (comma-separated, backticked).
export const BIND_OK_ELSEWHERE =
  "Bound this channel to `{directory}`. New messages start sessions there; existing threads " +
  "keep working in {old}, where their sessions live.";
export const BIND_UNAVAILABLE =
  "Bound this channel to `{directory}`, but no session can start there yet. {reason}";
// D5: shown, ephemeral, before every prompt in a thread whose folder differs from the channel's.
export const OLD_THREAD_FOLDER =
  "This session works in `{old}`, the folder it was created in: Claude Code resumes a session " +
  "only there. New messages in the channel use `{new}`.";
export const BIND_OUTSIDE =
  "`{path}` is not a folder under `{root}`. Give its path relative to that folder, for " +
  "example `!bind my-project`.";
export const UPLOAD_FAILED =
  "Nothing was sent to Claude: {name} {reason}. Send the message again without it.";
// An audio clip waits for the transcript only the owner can ask Slack for (`voice.py`).
export const CLIP_WAITING =
  "Waiting for this clip's transcript: choose `Generate transcript` on the clip, and its text " +
  "is sent to Claude.";
export const CLIP_NOT_SENT =
  "Nothing was sent to Claude: this clip had no transcript after {minutes} minutes. Send it " +
  "again, or type the message.";
export const CLIP_NOT_YOURS =
  "Nothing was sent to Claude: this audio is a file somebody else uploaded.";
export const CLIP_EMPTY = "Nothing was sent to Claude: Slack's transcript of this clip is empty.";
export const CLIP_UNREADABLE =
  "Nothing was sent to Claude: the transcript of this clip could not be read ({reason}).";
export const UPLOAD_IMAGE_TYPE =
  "is an image of type `{mimetype}`, and Claude reads only JPEG, PNG, GIF and WebP images";
export const UPLOAD_IMAGE_SIZE = "is {size}, over the {limit} Claude accepts for an image";
export const UPLOAD_IMAGE_SIDE =
  "is {width}x{height} px, over the 8000x8000 px Claude accepts for an image";
export const UPLOAD_FILE_TYPE =
  "is a `{mimetype}` file, and awaydesk passes on only text, source code, PDF, JSON, " +
  "XML, YAML and notebook files";
export const UPLOAD_FILE_SIZE = "is {size}, over the {limit} limit for a file";
export const UPLOAD_TOO_MANY =
  "Nothing was sent to Claude: the message has {count} images, over the {limit} one message " +
  "takes.";
export const UPLOAD_TOO_HEAVY =
  "Nothing was sent to Claude: its images total {size}, over the {limit} one message takes.";
export const SESSION_CLOSED =
  "Nothing was done: this session closed while it ran (idle for a while, or the daemon " +
  "restarted). Send it again if it is still meant.";
export const UPLOAD_NOT_SHARED = "is not a file shared in this channel that awaydesk can download";
export const UPLOAD_DOWNLOAD = "could not be downloaded ({error})";
export const BIND_LIST = "Folders under `{root}` that Claude Code trusts:";
export const BIND_EMPTY =
  "No folder under `{root}` is trusted by Claude Code yet. Open one in the terminal with " +
  "`claude` and accept the trust dialog, then send `!bind` again.";
export const BIND_MORE = "Only the first {rows} are shown: `!bind <folder>` binds any other.";
export const BIND_CURRENT = " · _current_";
export const BIND_BUTTON = "Bind";
export const BIND_ALREADY = "This channel is already bound to `{directory}`.";
export const BIND_BUSY =
  "Sessions are running in this channel: binding another folder now would cut their work. " +
  "Let them finish or send `!stop`, then bind again.";
export const BYPASS_TOP_LEVEL =
  "Bypass belongs to one session: send `!bypass on` inside its thread.";
// True with the setup on screen and without it (after a stop, a restart or a Cancel).
export const BYPASS_BEFORE_START =
  "This session has not started yet: tick Bypass in its setup and press Start. " +
  "If no setup is shown, send a message here first.";
export const BYPASS_ON_THREAD =
  "Bypass is on in this session: every tool runs without asking, until `!bypass off`. " +
  "It survives a restart.";
// Says nothing about what is asked: that is the mode the owner's own settings give.
export const BYPASS_OFF_THREAD =
  "Bypass is off in this session: Claude Code follows your permission settings again.";
export const STOPPED = "Stopped the current turn.";
// An answered question, as the terminal keeps it in the transcript.
export const ANSWERED = "User answered Claude's questions:";
// Slack drops plain spaces at the start of a line; no-break spaces stay and make the indent.
export const NESTED = `${"\u00a0".repeat(4)}⎿ `;
export const STOPPED_CHANNEL = "Stopped what was running in this channel.";
// The thread's status line while a restart waits for background tasks only, and the same after
// the app's name. Never a message: it would notify and stay in the thread after the restart.
export const RESTART_WAITS = "Restart waits for {counts} · !stop ends {them} now";
export const RESTART_WAITS_STATUS = "is waiting to restart: {counts} still running";
// The same as a message, only where Slack refuses the app a thread status.
export const RESTART_WAITS_MESSAGE =
  "awaydesk is restarting once these background tasks end: {counts}. `!stop` ends them now.";
export const NOTHING_TO_STOP = "Nothing is running in this channel.";
// Both answers to `!stop` in a thread are posts that stay: an ephemeral line is gone on reload.
export const STOPPED_THREAD = "Stopped.";
export const NOTHING_TO_STOP_THREAD = "Nothing is running in this session.";
// D8: two sessions in one folder at once.
export const HOLD_QUESTION = "Another session is working in this folder: {link}. Send anyway?";
// The words of the question itself, and close in length: Slack sizes a button by its text, so
// `Continue` beside `Cancel` made the second look the lesser choice (issue #76).
export const HOLD_CONTINUE_BUTTON = "Send anyway";
export const HOLD_CANCEL_BUTTON = "Don't send";
export const HOLD_UNPOSTED =
  "awaydesk could not show this question in Slack, so the message was not sent. Send it again.";
export const HOLD_GONE =
  "This question is no longer open: it was already answered, or awaydesk restarted.";
export const NOT_SENT = "Not sent.";
// Session setup: asked once per top-level message, before the first prompt of a new session.
export const SETUP_FALLBACK = "Set up this session";
export const SETUP_HEADER = "*Choose how this session starts*";
export const SETUP_EFFORT_OPTION = "Effort: {level}";
export const SETUP_EFFORT_DEFAULT = "Default";
export const SETUP_BYPASS_OPTION = "Bypass permissions";
export const SETUP_BYPASS_DESCRIPTION = "Run every tool without asking, until `!bypass off`.";
export const SETUP_START_BUTTON = "Start";
export const SETUP_SUMMARY = "Model: {model} · Effort: {effort} · Bypass: {bypass}";
export const STATUS = "Directory: `{directory}`\nSession: `{session}`";
export const STATUS_STATE = "Mode: `{mode}`\nClaude Code: `{version}`\nNow: {activity}";
// The line between the two once the thread has a session id (issue #12). Claude Code leaves a
// session created through the Agent SDK out of the terminal's picker; a fork made in the
// terminal gets an id of its own and a row there (sessions reference, read 2026-10-04; the
// picker listing such a fork measured on CLI 2.1.282). `claude --resume <id>` finds a session
// from any folder since 2.1.223 (same reference): the `cd` chooses where the fork works and
// whose picker lists it. `directory` arrives quoted for the shell, and `command` as a code
// span, or escaped as plain text when it holds a backtick.
export const TERMINAL_COMMAND = "cd {directory} && claude --resume {session} --fork-session";
export const STATUS_TERMINAL = "Terminal: {command}";
export const ACTIVITY_IDLE = "idle";
export const VERSION_PENDING = "started, version shown after the first turn";
export const STATUS_BACKGROUND = "Background: `{counts}`";
export const STATUS_WORKING = "Working in: `{directory}`";
export const RUNNING = "⏳ {counts}";
// Slack's status line under a thread's last message (issues #83 and #95). A client shows the
// loading message: `THREAD_WORKING` while a prompt is on its way or a turn runs, and
// `STILL_RUNNING` once the turn has ended and a task it started still runs, the words the
// terminal ends such a turn with (`· 1 shell still running`, Claude Code 2.1.287, read
// 2026-10-02), with no hourglass: an emoji draws large and grey in a status line (seen on
// desktop, 2026-10-02). `THREAD_WORKING_STATUS` and
// `STILL_RUNNING_STATUS` say the same after the app's name, which is what a
// client that draws `<app name> <status>` shows (measured 2026-10-02, desktop and iOS).
export const THREAD_WORKING = "Working…";
// The terminal's words while it compacts (`Compacting conversation`, read in the CLI 2.1.292).
export const THREAD_COMPACTING = "Compacting conversation…";
export const THREAD_COMPACTING_STATUS = "is compacting the conversation…";
export const STILL_RUNNING = "{counts} still running";
export const THREAD_WORKING_STATUS = "is working…";
export const STILL_RUNNING_STATUS = "has {counts} still running";
export const ACTIVITY_BUSY = "running a turn, {queued} queued";
// `!status` sent to the channel (top-level, or a thread that holds no session): the channel's
// folder, then one line per live session, each with a link to its thread.
export const STATUS_CHANNEL_HEADER = "Directory: `{directory}`";
export const STATUS_CHANNEL_EMPTY = "No live session in this channel.";
export const STATUS_CHANNEL_ROW = "{link}: {activity}";
export const STATUS_CHANNEL_WAITING = "waiting for you";
export const STATUS_CHANNEL_BUSY = "busy";
export const STATUS_CHANNEL_IDLE = "idle";
// After `idle`, how long ago the session's last message was written, when that is known.
export const STATUS_CHANNEL_SINCE = " · {ago}";
export const STATUS_CHANNEL_SESSION = "Session"; // the link's label for a session with no title yet
export const STATUS_CHANNEL_BYPASS = " · ⚡ bypass";
export const STATUS_CHANNEL_FOLDER = " · folder `{directory}`";
export const STATUS_CHANNEL_LINK_FALLBACK = "thread `{thread_ts}`";
export const HELP_OWN = "**awaydesk**";
export const HELP_RULE =
  "A message that starts with `!` is a command, in code or bold too. To send it as text, put " +
  "anything before the `!`, as in `\\!goal`.";
export const HELP_WORDS: readonly string[] = [
  "`!guide` how awaydesk works, in a few lines; in the channel or inside a thread",
  "`!help [text]` this list, or only the lines that contain the text; in the channel or " +
    "inside a thread",
  "`!status` in the channel: every session's state; inside a thread: that session's " +
    "directory, mode and the footer's values",
  "`!stop` in the channel: every running session, its background tasks and its pending " +
    "approvals; inside a thread: only that session",
  "`!bind [folder]` the folders Claude Code trusts, or bind this channel to one, its path " +
    "relative to the allowed root; in the channel, refused inside a thread",
  "`!bypass on|off` run every tool without asking, until `!bypass off`; it survives a " +
    "restart; inside a thread, refused in the channel",
  "`!resume [session]` this directory's sessions, or resume one by id or name in the " +
    "thread of your `!resume` message; in the channel, refused inside a thread",
  "`!open [file]` share a file of the session's folder into the thread, where Slack opens it in " +
    "its file viewer: a picker alone, or a path or part of a name; inside a thread, refused in " +
    "the channel",
];
export const HELP_NO_MATCH = "No command matches `{query}`.";
export const HELP_CLAUDE =
  "\n**Claude Code** (this session, now). Any other `!name args` runs that command; " +
  "these words above come first.";
export const HELP_UNBOUND = "\nClaude Code's own commands are listed inside a session's thread.";
export const APPROVAL_PROMPT = "Claude Code asks to use *{tool}*";
export const DENY_MESSAGE = "The owner denied this from Slack.";
export const SKIP_MESSAGE = "The owner dismissed the question without answering.";
export const QUESTIONS_ONE = "Claude Code has a question";
export const QUESTIONS_MANY = "Claude Code has {count} questions";
export const QUESTION_ANSWER = "Answer";
export const QUESTION_TITLE = "Claude Code asks";
export const QUESTION_CLOSE = "Later";
export const QUESTION_NOT_OPENED = "The form could not open (`{error}`). Click Answer again.";
export const QUESTION_MISSING = "Choose an option or type your own answer.";
export const QUESTION_WHERE = "{number} of {count}";
export const QUESTION_NEXT = "Next ({number}/{count})";
export const QUESTION_OTHER = "Other";
export const QUESTION_OTHER_HINT = "Or type your own answer";
export const APPROVAL_CUT =
  "_{count} characters of this request are not shown: Deny it unless you know them._";
export const APPROVAL_UNPOSTED =
  "awaydesk could not show this request in Slack, so nobody approved it.";
export const APPROVAL_GONE =
  "This request is no longer pending: the turn ended or awaydesk restarted.";
export const RESUME_LIST = "Sessions in `{directory}`, newest first:";
export const RESUME_EMPTY = "No sessions in `{directory}` yet.";
// Every session of the folder is already open in a thread: the list has no row to offer.
export const RESUME_NONE_LEFT = "No session to resume in `{directory}`.";
// Under the list: the sessions a thread already holds (D6), counted and not listed (issue #69).
export const RESUME_OPEN_ONE = "1 more is open in its own thread.";
export const RESUME_OPEN_MANY = "{count} more are open in their own threads.";
export const RESUME_BUTTON = "Resume";
export const RESUME_MORE =
  "Only the newest {rows} are shown: `!resume <id>`, or `!resume <title>` for a session that " +
  "has one, resumes an older session.";
// Two processes on one session share its file and not its conversation (measured 2026-10-03,
// issue #41, SDK 0.2.163 beside a host CLI 2.1.288 process): each keeps the session as it loaded
// it plus its own turns, and a later resume continues one of the two branches.
export const RESUME_OK =
  "Resumed **{title}**: your next message continues it. If it is open in a terminal, close it " +
  "there first: while it is open in both, neither sees the other's messages, and a later " +
  "resume keeps only one side's.";
export const RESUME_NONE =
  "No session in `{directory}` has the id or name `{target}`: `!resume` lists them.";
export const RESUME_AMBIGUOUS =
  "More than one session in `{directory}` is named or starts with `{target}`: pick one from " +
  "`!resume`.";
export const RESUME_STALE = "This list is out of date: send `!resume` again for a current one.";
export const RESUME_GONE =
  "That session is not in this channel's directory any more: `!resume` lists them.";
export const RESUME_HELD =
  "This thread already holds a session: send `!resume` again in the channel to pick another.";
// What the list is rewritten to after a Resume click, only when it cannot be deleted.
export const RESUME_LISTED = "Resumed {title} in {link}.";
// D6: a session held by any thread of any channel is never resumed a second time; `{link}` is the
// holding thread's permalink (a plain fallback when Slack would not give one).
export const RESUME_ELSEWHERE = "This session is already open in another thread: {link}.";
// The app's Home tab: the sessions the threads hold, by channel, newest activity first, under
// four filters. `{time}` is Slack's own date token, shown in the reader's time zone.
export const HOME_HEADER = "Sessions by channel, newest first · updated {time}";
export const HOME_EMPTY =
  "No channel is bound yet: `!bind` in a private channel binds it to a folder.";
export const HOME_NO_SESSIONS = "No sessions yet.";
export const HOME_NO_MATCH = "No session matches these filters.";
export const HOME_MORE = "Only the first {rows} sessions are shown: narrow the filters.";
export const HOME_OPEN = "Open";
export const HOME_NEW_THREAD = "New thread";
export const HOME_EDIT = "Edit";
export const HOME_DONE = "Done";
export const HOME_DELETE = "Delete";
// Bold, behind the hourglass the other two notes carry: mrkdwn has no colour, and the plain
// word was missed among the row's details.
export const HOME_DELETING = ":hourglass_flowing_sand: *deleting…*";
export const HOME_CHANNEL_DELETING_ONE = ":hourglass_flowing_sand: deleting 1 thread…";
export const HOME_CHANNEL_DELETING_MANY = ":hourglass_flowing_sand: deleting {count} threads…";
export const HOME_DELETING_ONE =
  ":hourglass_flowing_sand: *Deleting 1 thread.* Slack limits how fast messages are deleted: " +
  "a thread takes about a minute, and leaves this page when it is gone.";
export const HOME_DELETING_MANY =
  ":hourglass_flowing_sand: *Deleting {count} threads, one at a time.* Slack limits how fast " +
  "messages are deleted: each takes about a minute, and leaves this page when it is gone.";
export const HOME_DELETE_TITLE = "Delete this thread?";
export const HOME_DELETE_NAMED = "“{title}” in #{channel}, {replies}. ";
export const HOME_DELETE_NAMED_BARE = "“{title}” in #{channel}. ";
export const HOME_DELETE_TEXT =
  "Every message of the thread is deleted from Slack, yours and the bot's. This cannot be " +
  "undone. The session stays in Claude Code and can be resumed with !resume. Deleting takes " +
  "about a minute.";
export const HOME_DELETE_CONFIRM = "Delete thread";
export const HOME_DELETE_DENY = "Cancel";
export const HOME_CLEAN = "Clean up";
export const HOME_CLEAN_TITLE = "Clean up this channel?";
export const HOME_CLEAN_TEXT =
  "Deletes from #{channel} what sits outside a thread: your messages there that have no " +
  "reply, commands like !stop included, and the bot's own messages. Every thread that has a " +
  "reply stays. This cannot be undone and can take a few minutes.";
export const HOME_CLEAN_CONFIRM = "Clean up";
export const HOME_CHANNEL_CLEANING = ":hourglass_flowing_sand: cleaning up…";
export const HOME_CLEANING_ONE =
  ":hourglass_flowing_sand: *Cleaning up 1 channel.* Slack limits how fast messages are " +
  "deleted: this can take a few minutes.";
export const HOME_CLEANING_MANY =
  ":hourglass_flowing_sand: *Cleaning up {count} channels, one at a time.* Slack limits how " +
  "fast messages are deleted: this can take a few minutes.";
export const HOME_CLEAN_FAILED =
  "Could not clean up that channel (`{error}`). Clean it up again to continue.";
export const HOME_DELETE_REFUSED =
  "Slack refused to delete {count} of that thread's messages (`cant_delete_message`): they " +
  "are not yours or the bot's, or your workspace does not let you delete them. Every other " +
  "message is gone and the thread is still listed.";
export const HOME_CLEAN_REFUSED =
  "Slack refused to delete {count} of that channel's messages (`cant_delete_message`): your " +
  "workspace does not let you delete them. Every other one is gone.";
export const HOME_DELETE_BUSY = "Not deleted: that thread is working or waiting for you.";
export const HOME_DELETE_FAILED =
  "Could not delete every message of that thread (`{error}`). Delete it again to continue.";
export const HOME_SHOW_ALL = "Show all {count}";
export const HOME_UNTITLED = "Session {id}";
export const HOME_REPLY = "1 reply";
export const HOME_REPLIES = "{count} replies";
export const HOME_LAST_REPLY = "last reply {when}";
export const HOME_STARTED = "started {when}";
export const HOME_WORKING = "working";
export const HOME_WAITING = "waiting for you";
export const HOME_ENDED = "ended";
export const HOME_ERROR = "error";
export const HOME_ALL_CHANNELS = "All channels";
export const HOME_ALL_STATUSES = "All statuses";
export const HOME_ANY_TIME = "Any time";
export const HOME_LAST_48 = "Last 48 hours";
export const HOME_TODAY = "Today";
export const HOME_YESTERDAY = "Yesterday";
export const HOME_LAST_7 = "Last 7 days";
export const HOME_LAST_30 = "Last 30 days";
export const HOME_SEARCH_LABEL = "Search titles";
export const HOME_SEARCH_HINT = "Type a word and press Enter";
// `!guide`: how to use the bot, in the owner's words. tests/test_commands.py fails when a word of
// the daemon is missing here; keep the tone plain and every line true of the current behaviour.
export const GUIDE =
  "**awaydesk**\n" +
  "This channel runs Claude Code on your Mac, in one folder, and answers you alone. One Slack " +
  "thread is one Claude Code session: a top-level message starts a new one, and a reply inside its " +
  "thread continues it, even days later.\n" +
  "\n" +
  "**Get started**\n" +
  "1. `!bind` lists the folders Claude Code trusts, with a Bind button each; `!bind <folder>` " +
  "binds one by its path relative to your allowed root: `!bind my-project`. Claude Code must trust " +
  "that folder first: open `claude` there once in the terminal and accept. `!bind` works only as a " +
  "top-level message in the channel.\n" +
  "2. Write a message in the channel: it opens a session, and its reply appears in a thread of its " +
  "own, growing as Claude works, with a line for each tool it uses. Reply inside that thread to " +
  "continue the same session. Attach images or files to a message: Claude sees a JPEG, PNG, GIF or " +
  "WebP image directly (other image types are refused) and reads a text, code, PDF, JSON, XML, YAML " +
  "or notebook file from a copy saved on this Mac; other files are refused.\n" +
  "3. Before that first message is sent, the thread asks for the **Model**, the **Effort** " +
  "(`Default` leaves Claude Code's own choice) and **Bypass permissions**; **Start** sends your " +
  "message with those choices, and `!stop` cancels it instead.\n" +
  "\n" +
  "**Commands**\n" +
  "Claude Code's commands start with `!` instead of `/`: `!compact`, `!model opus`. They run inside " +
  "a session's thread, where `!help` lists every command that session offers, and `!help <text>` " +
  "filters the list; typed in the channel, `!help` lists awaydesk's own words instead. " +
  "`!clear` (`!reset`, `!new`) is refused inside a thread (one thread is one session): start a " +
  "fresh session with a new top-level message instead.\n" +
  "\n" +
  "**Approvals and questions**\n" +
  "When Claude Code asks permission, the request shows what will run, with **Approve** and " +
  "**Deny**; a very long one shows its start and its end and says how much it leaves out. A " +
  "question from Claude comes with **Answer**, which opens a short form, and **Skip**.\n" +
  "\n" +
  "**Sessions**\n" +
  "`!status` typed in the channel lists the folder and every session still live, each linked to " +
  "its thread; inside a thread it shows that session's directory, mode and the footer's values. " +
  "`!stop` typed in the channel stops every running session and its background tasks; inside a " +
  "thread it stops only that one. `!resume`, typed in the channel only, lists this folder's twenty " +
  "newest sessions, from the terminal too, each with the start of its id and a **Resume** button; " +
  "`!resume <id>` (that start is enough), or `!resume <title>` for a session that has one, resumes " +
  "it in the thread of your `!resume` message. To continue a session in the terminal, run the " +
  "`Terminal:` command `!status` shows in its thread.\n" +
  "\n" +
  "**Files**\n" +
  "`!open`, sent inside a session's thread, offers the files changed in that session and a search " +
  "over the folder's files; choosing one shares it into the thread, where Slack opens it in its " +
  "file viewer, with Markdown rendered. `!open <path>` opens that file, and `!open <words>` the file " +
  "whose path contains them, or lists the files that do. The search reads the folder from disk and " +
  "leaves out what `.gitignore` excludes inside a git repository. A file over 1 MB is not opened.\n" +
  "\n" +
  "**Bypass**\n" +
  "`!bypass on`, sent inside a session's thread, lets Claude Code run every tool without asking in " +
  "that session, until `!bypass off`; a restart of awaydesk keeps it. The footer shows ⚡ " +
  "bypass while it is on.\n" +
  "\n" +
  "`!guide` shows this text again.";
