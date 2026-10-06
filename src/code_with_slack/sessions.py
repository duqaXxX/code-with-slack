"""One Claude Code session per Slack thread: its client, its queue of turns, and the reader
that turns the SDK's message stream into replies."""

import asyncio
import contextlib
import logging
import os
import shlex
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, cast, get_args
from uuid import uuid4

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    Message,
    ResultError,
    ResultMessage,
    SDKSessionInfo,
    StreamEvent,
    UserMessage,
    list_sessions,
)
from claude_agent_sdk.types import (
    TERMINAL_TASK_STATUSES,
    CanUseTool,
    EffortLevel,
    HookCallback,
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    PermissionMode,
    PermissionResult,
    PermissionResultDeny,
    RateLimitEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    ToolPermissionContext,
)
from slack_sdk.web.async_client import AsyncWebClient

from code_with_slack import texts
from code_with_slack.approvals import (
    Answer,
    Approvals,
    answered_blocks,
    approval_blocks,
    question_blocks,
    to_permission,
)
from code_with_slack.folders import bindable_folders
from code_with_slack.footer import (
    FooterData,
    UsageCache,
    effort_change,
    format_footer,
    format_status_fields,
    git_state,
    session_tokens,
)
from code_with_slack.guards import Identity
from code_with_slack.hold import Holds
from code_with_slack.prompt import Prompt, user_message
from code_with_slack.render.escape import markdown_escape, mrkdwn_escape
from code_with_slack.render.renderer import (
    INTERRUPTED,
    Sink,
    TaskFrame,
    TurnRenderer,
    ended_line,
    one_line,
    task_title,
)
from code_with_slack.render.sinks import (
    Clock,
    ReplySink,
    UpdateLimiter,
    context_block,
    delete_request,
    describe,
    notice_text,
)
from code_with_slack.render.status import Status, StatusReaction, ThreadStatus
from code_with_slack.resume import by_last_activity
from code_with_slack.setup import DEFAULT, Choice
from code_with_slack.state import StateStore
from code_with_slack.trust import Repository, trusted_repository, workspace_trusted

logger = logging.getLogger(__name__)

TURN_MESSAGES = (StreamEvent, AssistantMessage, UserMessage, ResultMessage)
TASK_MESSAGES = (
    TaskStartedMessage,
    TaskProgressMessage,
    TaskNotificationMessage,
    TaskUpdatedMessage,
)
# The tool Claude Code uses for clarifying questions: its answers travel back in updated_input
# (code.claude.com/docs/en/agent-sdk/user-input). This is the permission protocol, not rendering.
QUESTION_TOOL = "AskUserQuestion"
# After a background task's notification, the CLI starts a turn of its own to report it
# (measured on Claude Code 2.1.280). If that turn never comes, the owner's queue moves on.
INJECTED_TURN_WAIT = 30.0
# How long the answer to `!stop` waits for the reply it cut short to end. An interrupted turn
# ends within a second or two; a final write that is retried takes ten. Read at call time, so a
# test can lower it with monkeypatch.
STOP_TAIL_WAIT = 15.0
# Each task_type (measured on Claude Code 2.1.280: `local_bash` for a background command,
# `local_agent` for a subagent) as the footer counts it and as its end line names it, the way
# the terminal prints `Agent "..." finished`. A type not listed counts and reads as a task, so a
# new kind shows with no change.
TASK_KINDS = {"local_bash": ("shell", "Background command"), "local_agent": ("agent", "Agent")}
UNKNOWN_KIND = ("task", "Task")
# Tasks whose type and description the session keeps. A task can end with a terminal
# task_updated and no notification (SDK docstring), so past this many the oldest are dropped.
TASKS_KEPT = 200
# The task types whose notification summary is already the terminal's end line (measured: a
# command's reads `Background command "..." completed (exit code 0)`; an agent's is its result).
SUMMARY_IS_END_LINE = {"local_bash"}
# Tasks whose reply the session remembers. An ended task stays a while, since an agent can
# report again after its notification; past this many, the oldest ended ones are forgotten.
TASK_REPLIES_KEPT = 100
# How often a stopping daemon checks whether every channel's turns have ended.
DRAIN_POLL_SECONDS = 0.5
# D9: a session's Claude Code process closes after this long with nothing to do (idle, and no
# approval, question or background report pending); the next message rebuilds it and resumes.
# Read at fire time, like INJECTED_TURN_WAIT, so a test can lower it with monkeypatch.
IDLE_CLOSE_SECONDS = 3600.0
# D8: the one hold a thread can have open at once, in `_waiting` alongside real approval ids
# (`waiting_for_owner` and D9's idle-close timer treat every entry the same).
_HOLD_MARKER = "d8-hold"
# How much of the owner's question a note about a message that was not sent quotes.
ASKED_LIMIT = 100
# How much of each message a note about messages that were not sent quotes.
NOT_SENT_START = 60
# The levels `ClaudeAgentOptions.effort` accepts. A stored value outside this set (a CLI wording
# `footer.effort_change` parsed that the SDK's own type does not know, such as "auto") is never
# sent: `_connect` drops it rather than pass something the SDK was not built to validate.
VALID_EFFORT_LEVELS = frozenset(get_args(EffortLevel))


class DirectoryUnavailable(Exception):
    """The channel's directory cannot be used; `message` tells the owner what to do."""

    def __init__(self, directory: Path, message: str) -> None:
        super().__init__(message)
        self.directory = directory
        self.message = message


class SessionClosed(Exception):
    """The session closed (the daemon stopping, or D9's idle close, most likely) while something
    was using it: a daemon word (`!status`, `!bypass`), or `submit` queuing an owner's prompt
    (the caller retries once, against a freshly looked-up session)."""

    def __init__(self) -> None:
        super().__init__(texts.SESSION_CLOSED)
        self.message = texts.SESSION_CLOSED


class SessionGone(Exception):
    """A thread's stored session id could not be resumed: its entry was removed (D7), and the
    thread needs a new top-level message to start again."""

    def __init__(self) -> None:
        super().__init__(texts.SESSION_GONE)
        self.message = texts.SESSION_GONE


class DirectoryMissing(DirectoryUnavailable):
    def __init__(self, directory: Path) -> None:
        super().__init__(directory, texts.DIRECTORY_MISSING.format(directory=directory))


class DirectoryUntrusted(DirectoryUnavailable):
    """The owner has not trusted the folder in Claude Code: a session would run its hooks and
    apply its settings with no trust dialog (see `code_with_slack.trust`)."""

    def __init__(self, directory: Path) -> None:
        super().__init__(directory, texts.DIRECTORY_UNTRUSTED.format(directory=directory))


class DirectoryUnreadable(DirectoryUnavailable):
    """macOS privacy protection (TCC) denies the daemon a folder such as ~/Documents: a process
    started by launchd does not inherit the Terminal's permission, and the CLI fails to start."""

    def __init__(self, directory: Path) -> None:
        super().__init__(directory, texts.DIRECTORY_UNREADABLE.format(directory=directory))


class ClaudeClient(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    # The SDK's own signature; a turn sends prompt.user_message(), exactly one message.
    async def query(self, prompt: str | AsyncIterable[dict[str, Any]]) -> None: ...
    def receive_messages(self) -> AsyncIterator[Message]: ...
    async def set_permission_mode(self, mode: PermissionMode) -> None: ...
    async def set_model(self, model: str | None = None) -> None: ...
    async def interrupt(self) -> None: ...
    async def stop_task(self, task_id: str) -> None: ...
    async def get_server_info(self) -> dict[str, Any] | None: ...
    async def get_context_usage(self) -> Any: ...


ClientFactory = Callable[[ClaudeAgentOptions], ClaudeClient]


def default_client_factory(options: ClaudeAgentOptions) -> ClaudeClient:
    return ClaudeSDKClient(options)


def asked(prompt: Prompt) -> str:
    """The owner's question on one line, as a note about a message that was not sent quotes it."""
    if isinstance(prompt, str):
        text = prompt
    else:
        text = " ".join(block["text"] for block in prompt if block["type"] == "text")
    return one_line(text, ASKED_LIMIT) if text.strip() else texts.PROMPT_IMAGE


def injected_turn(result: ResultMessage) -> bool:
    """Whether Claude Code started this turn itself (a task notification, say), from the
    result's origin: None or `human` is the owner's own prompt."""
    kind = result.origin.get("kind") if result.origin else None
    return kind not in (None, "human")


def client_options(
    directory: Path,
    session_id: str | None,
    effort: EffortLevel | None,
    can_use_tool: CanUseTool,
    on_stop: HookCallback,
    on_tool_done: HookCallback,
) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        cwd=str(directory),
        resume=session_id,
        # D9: the model set with `/model` survives a resume by itself (measured); the effort
        # `/effort` set does not, so the daemon stores it per thread and passes it back here.
        # Validated against VALID_EFFORT_LEVELS by the caller, so this is always a level the SDK
        # itself would accept.
        effort=effort,
        setting_sources=["user", "project", "local"],
        include_partial_messages=True,
        can_use_tool=can_use_tool,
        # The Stop hook's input carries the effort level Claude Code runs at: the footer's
        # only source for it, since no message reports it. Every hook input carries the `cwd`
        # the session works in; PostToolUse reports it after each tool, so a turn stopped or
        # failed before its Stop still moves the footer's branch.
        hooks={
            "Stop": [HookMatcher(hooks=[on_stop])],
            "PostToolUse": [HookMatcher(hooks=[on_tool_done])],
        },
        # Makes bypass possible, not active: `!bypass on` switches it on the live client.
        # `replay-user-messages` (CLI reference): Claude Code re-emits each prompt with the uuid
        # the daemon sent it under, which tells a prompt it took into a running turn from one
        # that waits for a turn of its own (`ThreadSession._acknowledge`).
        extra_args={"allow-dangerously-skip-permissions": None, "replay-user-messages": None},
        # CLI stderr may quote the conversation: keep it out of the log unless debugging.
        stderr=lambda line: logger.debug("claude stderr: %d chars", len(line)),
    )


def resolve_directory(raw: str, root: Path) -> Path | None:
    """The real directory `raw` names, if it exists under `root`; guards against a typo only."""
    # A relative path is read under the root: the daemon's own working directory means nothing
    # to the owner (launchd starts it in `/`).
    path = (root / Path(raw).expanduser()).resolve()
    if not path.is_dir():
        return None
    return path if path == root or root in path.parents else None


def directory_sessions(directory: Path) -> list[SDKSessionInfo]:
    """The sessions of `directory` alone, newest first: the terminal's picker also starts from
    the current worktree (sessions reference, read 2026-09-25)."""
    return list_sessions(directory=str(directory), include_worktrees=False)


@dataclass
class SessionDeps:
    slack: AsyncWebClient
    identity: Identity
    state: StateStore
    approvals: Approvals
    usage: UsageCache
    # D8: kept in memory only, shared with slack_app.py's click handlers (Holds() default gives
    # every test its own, as `update_limiter` does).
    holds: Holds = field(default_factory=Holds)
    client_factory: ClientFactory = default_client_factory
    workspace_trusted: Callable[[Path], Awaitable[bool]] = workspace_trusted
    # The daemon's own git runs only on what this returns, for a directory and the folder the
    # session started in: a repository the owner trusted, or one inside that folder.
    trusted_repository: Callable[[Path, Path], Awaitable[Repository | None]] = trusted_repository
    sessions_of: Callable[[Path], list[SDKSessionInfo]] = directory_sessions
    # Shared by every ReplySink in the process, so their chat.update writes stay under one
    # app-wide budget together; a fresh default here gives each test its own.
    update_limiter: UpdateLimiter = field(default_factory=UpdateLimiter)
    # The clock of each reply's stream deadline (280 s): a test replaces it to cross the deadline
    # without waiting for it.
    stream_clock: Clock = field(default_factory=Clock)
    # The client replies are written with: one that does not retry a create or a stream call after
    # a reset (`ConnectionRetryUnlessCreating`), which every other caller of `slack` still gets.
    # None: `slack` itself.
    reply_slack: AsyncWebClient | None = None


@dataclass
class Turn:
    prompt: Prompt
    sink: ReplySink
    done: asyncio.Event = field(default_factory=asyncio.Event)
    # The daemon's own id for the prompt, which Claude Code replays it under.
    uuid: str = field(default_factory=lambda: str(uuid4()))


def terminal_line(directory: Path, session_id: str) -> str:
    """The `!status` line with the command that forks a session in the terminal, run from the
    thread's folder, where `!resume` in its channel then finds the fork."""
    command = texts.TERMINAL_COMMAND.format(
        directory=shlex.quote(str(directory)), session=session_id
    )
    # A code span cannot hold a backtick: such a command is plain text, escaped.
    shown = markdown_escape(command) if "`" in command else f"`{command}`"
    return texts.STATUS_TERMINAL.format(command=shown)


def not_sent(turns: list[Turn], because: str) -> str:
    """The note that names turns that will never be sent: how many, why, and the start of each,
    so the owner knows which to send again."""
    header = (texts.NOT_SENT_ONE if len(turns) == 1 else texts.NOT_SENT_MANY).format(
        count=len(turns), because=because
    )
    starts = (f'- "{one_line(asked(turn.prompt), NOT_SENT_START)}"' for turn in turns)
    return "\n".join([header, *starts])


def taken_note(count: int) -> str:
    """The note for the reply a turn that took `count` prompts into itself ends with."""
    return (texts.TAKEN_INTO_REPLY_ONE if count == 1 else texts.TAKEN_INTO_REPLY_MANY).format(
        count=count
    )


async def check_directory(directory: Path, trusted: Callable[[Path], Awaitable[bool]]) -> None:
    """Raise the `DirectoryUnavailable` that keeps a session from starting in `directory`, the
    first found in the order the owner can act on: missing, unreadable, untrusted."""
    # One stat and one directory open on a local folder, as a session start always made them.
    if not directory.is_dir():  # noqa: ASYNC240
        raise DirectoryMissing(directory)
    try:
        with os.scandir(directory):
            pass
    except PermissionError:
        raise DirectoryUnreadable(directory) from None
    if not await trusted(directory):
        raise DirectoryUntrusted(directory)


@dataclass
class ActiveTurn:
    turn: Turn | None
    renderer: TurnRenderer
    # Prompts whose replay arrived while this turn ran: Claude Code took them into it.
    taken: list[Turn] = field(default_factory=list)


class ThreadSession:
    def __init__(
        self,
        channel_id: str,
        thread_ts: str,
        directory: Path,
        deps: SessionDeps,
        *,
        predecessor: asyncio.Event | None = None,
    ) -> None:
        self.channel_id = channel_id
        self.thread_ts = thread_ts
        self.directory = directory
        # D8: resolved once here, not by `SessionManager.working_in` on every live session for
        # every message (`directory` never changes after this object is built: a rebind only
        # takes effect for a thread not open yet).
        self._resolved_directory = directory.resolve()
        self._deps = deps
        # D10: one reaction on the session's root message, which `thread_ts` always is (a
        # top-level owner message, or the root of a `!resume` thread, the owner's own message).
        self._status = StatusReaction(deps.slack, channel=channel_id, root_ts=thread_ts)
        # Issues #83 and #95: Slack's status line at the bottom of the thread (`_thread_line`).
        self._thread_status = ThreadStatus(deps.slack, channel=channel_id, thread_ts=thread_ts)
        # D10: set by `stop()` while it denies pending approvals, so `_can_use_tool`'s own
        # finally does not race its closing ✅ back to working; `_finish` and `_abandon`
        # clear it, once that turn's own tail ends, whichever way.
        self._interrupting = False
        # Set once the turn `stop()` interrupted has closed its reply (`stop_landed`).
        self._stop_tail: asyncio.Event | None = None
        # D10: true from `_react_error` until new work starts (`submit`, `_start_turn`), so a
        # standing ❌ is never mistaken for idle-and-done. `StatusReaction.current` cannot serve
        # this alone: it only updates once its own `reactions.add` returns, which a quick turn
        # can easily outrun.
        self._error_standing = False
        self._client: ClaudeClient | None = None
        self._reader: asyncio.Task[None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[Turn] = asyncio.Queue()
        self._taken: Turn | None = None  # out of the queue, not sent yet
        self._sent: deque[Turn] = deque()
        self._active: ActiveTurn | None = None
        self._connect_lock = asyncio.Lock()
        # Set once close starts: no client starts after it, and no word stores a setting.
        self._closed = False
        # The predecessor this object replaced at its key (D9: the manager evicts a closed
        # session as soon as closing starts, not once it finishes). Awaited once, inside the
        # connect lock, before this session's own first connect: the SDK's transport needs real
        # time to flush the old CLI process after EOF, and a resume of the same session id must
        # not start while that is still in flight.
        self._predecessor = predecessor
        # Set once close() (or the SessionGone branch) has fully finished: the predecessor signal
        # above, for whoever replaces this session next, and what `close_all` waits on.
        self.done_closing: asyncio.Event = asyncio.Event()
        # Set by the manager that created this session; called once, right when `done_closing`
        # is set, so a session nobody ever looks up again does not sit in `_sessions` forever.
        self.on_closed: Callable[[], None] | None = None
        self._notice: str | None = None
        # Fire-and-forget tasks this session never waits on, chiefly a usage refresh: never
        # cancelled by `close`, since a usage refresh shares one `UsageProbe` (and its one
        # client) across every session, and cancelling it mid-query would leave that client
        # answering the next lookup, of any session, late.
        self._background: set[asyncio.Task[None]] = set()
        # `_expire_unreported`'s own timers only: this session's alone, so `close` can cancel
        # them without touching `_background`'s shared-client tasks.
        self._expiring: set[asyncio.Task[None]] = set()
        # Approval ids open right now, waiting on the owner's decision (`waiting_for_owner`); a
        # D8 hold adds `_HOLD_MARKER` here too.
        self._waiting: set[str] = set()
        # Task messages that arrived between turns, shown in the next turn's reply.
        self._held: list[Message] = []
        # Tasks that outlived their turn, and the reply whose line each one keeps up to date,
        # for as long as the Claude Code process lives: an agent can report more than once.
        self._task_replies: dict[str, TurnRenderer] = {}
        # The channel's newest reply: the only one that shows what is still running.
        self._latest: ReplySink | None = None
        # Replies whose end did not land: no checkmark until the one retry does, a cross if it
        # fails too. Each is settled once more when the session closes.
        self._unlanded: set[Sink] = set()
        # What the running turn's reply says when it ends: how many queued messages a restart
        # dropped (S3), since they get no reply of their own.
        self._end_notes: list[str] = []
        # Each task's type and description, for the footer's counts and its end line, and the
        # end lines that open Claude Code's next turn of its own, the one that reports them.
        self._tasks: dict[str, tuple[str, str]] = {}
        # Ended tasks not yet opened into a report turn's reply: (task_id, its formatted end
        # line). The task_id finds the reply that started it (D1: the report renders there).
        self._ended: list[tuple[str, str]] = []
        # Tasks whose end arrived without their notification yet, by the loop time it arrived:
        # the notification starts the turn that reports them. The CLI can suppress it (SDK
        # TaskUpdatedMessage docstring), so a stop waits for it only INJECTED_TURN_WAIT.
        self._unreported: dict[str, float] = {}
        # Tasks `!stop` ended. Claude Code starts no turn to report such a task: its notification
        # stays queued (measured twice on 2026-09-27, Claude Code 2.1.283), so none is awaited.
        self._stopped: set[str] = set()
        # Clear while a background notification's own turn is expected or running: the owner's
        # next query waits, so the two replies never share a thread.
        self._settled = asyncio.Event()
        self._settled.set()
        self._injected_expected = False
        self._expiry: asyncio.Task[None] | None = None
        # D9: armed while idle with nothing pending; any turn, task frame, approval or question
        # cancels it, and being idle with nothing pending again (re)starts it.
        self._idle_expiry: asyncio.Task[None] | None = None
        # D9: a count, not a flag, since two `submit` calls can overlap. Nonzero makes `idle`
        # false for the span between a `submit` call starting and its turn actually being
        # queued, so the timer is genuinely cancelled there, not reset to fire again mid-await.
        self._pending_submits = 0
        self.commands: list[dict[str, Any]] = []
        # The CLI's own model list (value, displayName, supportedEffortLevels), for the setup.
        self.models: list[dict[str, Any]] = []
        # A prompt was ever queued on this object; with no stored session id it tells a thread
        # that never ran a turn from one whose first turn is still starting.
        self._submitted = False
        # `apply_setup` changed something that `forget_setup` has not undone.
        self._setup_applied = False
        # Held while Start is applied, so a `!bypass` typed meanwhile acts after it.
        self._setup_lock = asyncio.Lock()
        # What the live client was built with or switched to: the effort option (no runtime
        # setter exists) and whether it effectively runs in bypass (the switch, or the folder's own
        # settings starting it so). `apply_setup` compares the owner's choice with these, not with
        # what was stored, which a restart or a `!bypass on` can have moved.
        self._client_effort: str | None = None
        self._client_bypass = False
        self.native_mode = "default"
        self.cli_version: str | None = None
        # The effort level Claude Code last reported: its Stop hook, or the output of `/effort`
        # and `/model`, which run no hook. None for a model without effort.
        self.effort: str | None = None
        # Whether Claude Code has reported the level since the client started: until then it is
        # not known, since the settings do not decide it (measured 2026-09-25).
        self.effort_reported = False
        # Where the session works as its hooks last reported (`cwd` follows a `cd` and a worktree;
        # Stop and PostToolUse measured 2026-09-27 on 2.1.283); None until then: `directory`.
        self.working_directory: Path | None = None
        # The session's token count as the client's last result reported it, for `!status`.
        self.session_tokens: int | None = None
        # Set when the daemon stops: the turns already sent finish, no other one starts.
        self.draining = False
        # Whether this stop has said, in a message, which background tasks it waits for: only
        # where the thread's status line cannot (`show_restart_wait`).
        self._told_waiting = False
        # Set when a stop begins while this session has a turn running. The signal names no
        # sender, and the session that sent it has its turn running then; a background task it
        # starts from that point is often its own wait for the new process, which cannot end
        # before this one exits (issue #87). Those tasks are not waited for: the shutdown ends
        # them with the Claude Code process. "After" means read after: a task the turn started
        # just before the signal, whose start message the reader had not reached yet, counts as
        # started after it. A single background command that sends the signal and then waits
        # started before it, and is still waited for.
        self.may_have_ordered_restart = False
        self._after_restart: set[str] = set()

    @property
    def bypass_choice(self) -> bool | None:
        """The owner's last word on bypass in this thread, as state.json holds it (a restart keeps
        it): True on, False off, None never chosen."""
        stored = self._deps.state.thread(self.channel_id, self.thread_ts)
        return stored.bypass if stored is not None else None

    def _bypass_runs(self, choice: bool | None) -> bool:
        """Whether a client runs in bypass given `choice`: what the owner chose, else what Claude
        Code itself started in (`native_mode`, as it reported it)."""
        return choice if choice is not None else self.native_mode == "bypassPermissions"

    @property
    def bypass(self) -> bool:
        """Whether this thread runs in bypass: the one answer the footer's ⚡, `!status`, the
        channel list and the setup's checkbox read."""
        return self._bypass_runs(self.bypass_choice)

    def _mode_for(self, on: bool) -> str:
        """The permission mode that means `on`; off is the native mode, or `default` when the
        process itself started in bypass."""
        if on:
            return "bypassPermissions"
        return "default" if self.native_mode == "bypassPermissions" else self.native_mode

    @property
    def resolved_directory(self) -> Path:
        """`directory`, resolved once at construction and cached (D8's `working_in` reads this
        across sessions, instead of resolving every live session's folder on every message)."""
        return self._resolved_directory

    @property
    def busy(self) -> bool:
        return self._active is not None or bool(self._sent)

    @property
    def waiting_for_owner(self) -> bool:
        """An approval or a question is open in this thread, waiting on the owner's answer."""
        return bool(self._waiting)

    def _thread_line(self) -> tuple[str, str]:
        """What the thread's status line says, empty for nothing, and the same after the app's
        name (`ThreadStatus.show`). `Working…` while a prompt is on its way to Claude Code or a
        turn runs (issue #83). Once the turn has ended, what it left running
        (`1 shell still running`, issue #95): a count that changes cannot live in a reply,
        whose stream only grows. Nothing while an approval, a question or a hold waits on the
        owner, while `!stop` winds a turn down, and once the session is closed."""
        if self._closed or self.waiting_for_owner or self._interrupting:
            return "", ""
        if self.busy or self._taken is not None or not self._queue.empty():
            return texts.THREAD_WORKING, texts.THREAD_WORKING_STATUS
        if self.draining and (held := self._running_kinds(awaited_only=True)):
            # A stop that only background tasks hold: said here whatever the footer shows,
            # since `!stop` is what ends the wait.
            them = "it" if held.startswith("1 ") and " · " not in held else "them"
            return (
                texts.RESTART_WAITS.format(counts=held, them=them),
                texts.RESTART_WAITS_STATUS.format(counts=held),
            )
        kinds = self._running_kinds()
        if not kinds or (self._latest is not None and self._latest.footer_shown):
            # Nothing runs, or the end of the thread's last reply is on Slack: its footer
            # counts them, and a second line under a footer would say the same twice.
            return "", ""
        return (
            texts.STILL_RUNNING.format(counts=kinds),
            texts.STILL_RUNNING_STATUS.format(counts=kinds),
        )

    def _show_thread_status(self) -> None:
        """Bring the thread's status line to `_thread_line`, after anything that can change it:
        it never waits on Slack (`ThreadStatus.show`)."""
        self._thread_status.show(*self._thread_line())

    @property
    def running_kinds(self) -> str:
        """`1 shell · 2 agents`, the tasks that outlived their turn and still run; empty when
        none does. The channel-level `!status` shows this beside a session's own state."""
        return self._running_kinds()

    @property
    def idle(self) -> bool:
        """Nothing running, sent, taken or queued, no `submit` call still on its way to queuing
        one, and no background task still working: the session can be closed without a loss
        (closing it ends its Claude Code process). `bind` (D5) waits for every thread of a
        channel to be idle before it stores a new folder."""
        return self._quiet(awaited_only=False)

    @property
    def restart_ready(self) -> bool:
        """`idle` as a stop reads it: a background task started after the stop began, in a
        session that may have sent the signal (`may_have_ordered_restart`), does not count."""
        return self._quiet(awaited_only=True)

    @property
    def restart_hold(self) -> str:
        """What a stop still waits for in this thread, in words the owner can act on; empty
        when the thread does not hold it (`SessionManager.drain` reads the same two flags)."""
        if self.restart_ready and not self.reporting:
            return ""
        if self.waiting_for_owner:
            return texts.RESTART_HOLD_OWNER
        if held := self._running_kinds(awaited_only=True):
            if not self.busy:
                return texts.RESTART_HOLD_TASKS.format(counts=held)
        elif not self.busy and self.reporting:
            return texts.RESTART_HOLD_REPORT
        return texts.RESTART_HOLD_TURN

    def _quiet(self, *, awaited_only: bool) -> bool:
        return (
            not self.busy
            and not self._pending_submits
            and not self._running_kinds(awaited_only=awaited_only)
            and self._taken is None
            and self._queue.empty()
            and self._settled.is_set()
        )

    @property
    def closed(self) -> bool:
        """Closing has started (`close()`'s first line, a shutdown, D9's idle close, or the
        SessionGone branch of `ensure_connected`); the teardown itself may still be running.
        The manager discards this object rather than hand it out again, from its next lookup
        (`sessions_of` already leaves it out); `done_closing` says when the teardown is over."""
        return self._closed

    @property
    def reporting(self) -> bool:
        """A task ended less than INJECTED_TURN_WAIT ago and the notification that makes Claude
        Code report it has not come yet."""
        now = asyncio.get_running_loop().time()
        return any(now - ended < INJECTED_TURN_WAIT for ended in self._unreported.values())

    def touch(self) -> None:
        """Cancel or (re)arm the idle-close timer for the state right now (D9). The manager calls
        this wherever a session is handed out (`get`/`open`), synchronously, before any await a
        caller might do on the way to its own `submit`: otherwise the timer could still fire, and
        close the session, in the gap between the lookup and the turn actually being queued."""
        self._idle_timer_check()

    def hold_start(self) -> None:
        """D8: mark this thread's 'send anyway?' question as waiting on the owner, the same way
        an approval does (`waiting_for_owner` covers both, so D9's idle-close timer stays off and
        D10 shows ✋, including through any report turn a background task starts while held:
        `_start_turn` reacts WAITING, not WORKING, whenever `waiting_for_owner` is true). At most
        one hold is ever open on a thread at once: `submit_to_session` waits on it inside the
        thread's own arrival lock, so a caller of this always pairs it with one `hold_end`.
        `_react` would otherwise read WAITING as `_error_standing = False`: a hold is not new
        work, so a standing ❌ must outlive it (Cancel restores it; Continue's own `submit`
        clears it, same as any new work)."""
        standing = self._error_standing
        self._waiting.add(_HOLD_MARKER)
        self._idle_timer_check()
        self._react(Status.WAITING)
        self._error_standing = standing

    async def hold_end(self, *, continued: bool) -> None:
        """Undo `hold_start`. `continued`: Continue was chosen, so the reaction is left alone (the
        `submit()` right after this returns reacts ⏳ on its own); otherwise (Cancel, `!stop`, a
        drain) the root reflects the session's live state now, not a snapshot from when the hold
        started (a report turn during the hold, most likely a background task's, can have changed
        it in the meantime)."""
        self._waiting.discard(_HOLD_MARKER)
        self._idle_timer_check()
        if not continued:
            await self._react_after_hold()

    async def _react_after_hold(self) -> None:
        """The reaction a hold not continued restores: bare for a thread that never ran a turn
        (`_client is None` alone is not "never ran" — a D9 idle close or a restart evicts the
        object and hands the thread a fresh one on its next lookup, with `_client` reset but its
        history intact, so "never ran" is read from the stored session id instead, state.json's
        only record of it, set once Claude Code's own init message reports one; nothing else is
        stored for this); otherwise ❌ when an error stands (`hold_start` kept it standing through
        the hold; this is what restores it, since `hold_start`'s own WAITING reaction already
        overwrote whatever ❌ was showing), ✅ through `_react_done_if_idle` when idle and no
        error stands, ⏳/✋ (whichever this thread still holds) otherwise."""
        stored = self._deps.state.thread(self.channel_id, self.thread_ts)
        if stored is None or stored.session_id is None:
            self._deps.state.set_status_pending(self.channel_id, self.thread_ts, None)
            await self._status.clear()
            return
        if self._error_standing:
            self._react_error()
        elif self.idle:
            await self._react_done_if_idle()
        else:
            self._react_waiting_or_working()

    async def react_hold_abandoned(self, *, error: bool) -> None:
        """A Continue whose `submit()` never ran: `hold_end(continued=True)` already discarded
        `_HOLD_MARKER` and left the ✋ standing, betting on that `submit()` to show ⏳ right after;
        a caller that instead exits without ever calling it (`sessions.draining` turning true,
        or `ensure_connected` raising `DirectoryUnavailable`/`SessionClosed`/`SessionGone`) must
        restore the reaction itself, or the ✋ stands forever. `error`: ❌, the same reaction a
        turn that reached the queue and then failed this way gets from `_fail(..., error=True)`;
        otherwise whatever a Cancel would show."""
        if error:
            self._react_error()
            return
        await self._react_after_hold()

    async def cancel_hold(self) -> bool:
        """Cancel a D8 hold open in this thread, as the owner's own Cancel would: `stop()` and
        `SessionManager.drain` both call this (a drain, unlike an approval or a question, never
        leaves a hold open: nobody could still be typing a reply to a question that names a
        session about to close). The button message is removed here, silently; `submit_to_session`
        is the one waiting on the future, and it tells the owner `Not sent.` once it wakes."""
        pending = self._deps.holds.cancel(self.channel_id, self.thread_ts)
        if pending is None:
            return False
        await self._delete_request(pending.message_ts)
        return True

    async def fail_queued(self, line: str) -> None:
        """End every queued turn's reply with `line`, and release whoever waits on them: the
        turns a thread whose session is gone could not start."""
        while not self._queue.empty():
            with contextlib.suppress(Exception):
                await self._fail(self._queue.get_nowait(), line)

    async def drop_queued(self, *, error: bool = False) -> None:
        """A restart will never send the queued turns: release whoever waits on them. They get
        no reply of their own; one note names them, at the end of the running turn's reply, or,
        with nothing running, as a message of its own (S3). `error` reacts D10's ❌ (a restart
        drain dropping a queued turn): a plain `close()` never needs it here, since its own
        `cut_short` already reacts once for the whole close."""
        queued: list[Turn] = []
        while not self._queue.empty():
            queued.append(self._queue.get_nowait())
        await self._drop(queued, texts.BECAUSE_RESTARTED, error=error)

    async def _drop(self, turns: list[Turn], because: str, *, error: bool = False) -> None:
        """Release `turns` that will never be sent, and say so once: see `drop_queued`."""
        if not turns:
            return
        try:
            if error:
                self._react_error()
            note = not_sent(turns, because)
            if self.busy:
                self._end_notes.append(note)
            else:
                await self._post(mrkdwn_escape(note))
        finally:
            for turn in turns:
                turn.done.set()
            self._show_thread_status()

    async def _feed_end_notes(self, renderer: TurnRenderer) -> None:
        """Say, at the end of the reply that just ran, what a restart dropped meanwhile."""
        notes, self._end_notes = self._end_notes, []
        if notes:
            # The dropped turns' replies are never written, and the newest was the latest: the
            # reply that says so is the one that has the footer.
            await self._hand_latest_to(renderer.sink)
        for note in notes:
            await renderer.feed_error(note)

    async def submit(self, prompt: Prompt) -> Turn:
        """Queue a prompt; its reply starts when Claude has something to show, not before.
        Raises `SessionClosed` if the session closed (an idle close, most likely) between the
        caller's lookup and this call; the caller retries once, against a freshly looked-up
        session (D9)."""
        if self._closed:
            raise SessionClosed
        # Counted, not just checked-and-cancelled: `idle` reads false for as long as this stays
        # above zero, so the timer is genuinely cancelled here, not reset only to fire again
        # during the awaits below (D9).
        self._pending_submits += 1
        self._submitted = True
        try:
            self._idle_timer_check()
            turn = Turn(prompt, await self._sink())
            if self._closed:
                # The session closed (an idle close or a restart, or `ensure_connected`'s
                # SessionGone branch, which already drained the queue) while the reply's sink
                # was made: no worker will ever take this turn from the queue, so it is resolved
                # right here instead of left waiting for one that will not come. Only a
                # SessionGone close also removes the thread's own entry (D7): that alone is what
                # tells the two apart, since both leave `self._closed` the same.
                gone = self._deps.state.thread(self.channel_id, self.thread_ts) is None
                await self._fail(
                    turn, texts.SESSION_GONE if gone else texts.SESSION_CLOSED, error=True
                )
                return turn
            self._queue.put_nowait(turn)
            # D10: a submitted turn is working, unless an approval or question already open in
            # this thread still holds it (that state stands until it is answered). New work,
            # so any standing ❌ ends here.
            if not self.waiting_for_owner:
                self._error_standing = False
                self._react(Status.WORKING)
        finally:
            self._pending_submits -= 1
        if self._worker is None or self._worker.done():
            name = f"worker-{self.channel_id}-{self.thread_ts}"
            self._worker = asyncio.create_task(self._work(), name=name)
        self._idle_timer_check()  # a queued turn is not idle (D9)
        return turn

    async def ensure_connected(self) -> ClaudeClient:
        async with self._connect_lock:
            if self._closed:
                raise SessionClosed
            if self._client is not None:
                return self._client
            if self._predecessor is not None:
                # The Claude Code process this thread had before may still be exiting: the SDK's
                # transport notes it needs real time to flush the session file after EOF (up to
                # ~20 s). Connecting with `resume=<the same id>` before that is done would race it.
                await self._predecessor.wait()
                self._predecessor = None
            await check_directory(self.directory, self._deps.workspace_trusted)
            stored = self._deps.state.thread(self.channel_id, self.thread_ts)
            session_id = stored.session_id if stored else None
            effort = stored.effort if stored else None
            try:
                client = await self._connect(session_id, effort)
            except ResultError as exc:
                if session_id is None:
                    raise
                # The stored session is gone (its transcript was deleted): the thread cannot
                # continue (D7). Its entry is dropped and the session ends itself here, so no
                # later turn (already queued, or the next one taken by the worker) starts a
                # second, unrecorded session in its place: it fails the same way instead.
                logger.warning(
                    "could not resume the stored session in %s/%s", self.channel_id, self.thread_ts
                )
                self._deps.state.remove_thread(self.channel_id, self.thread_ts)
                self._closed = True
                # No client was ever connected in this branch (`_connect` raised): nothing to
                # wait on, so the "fully closed" signal fires at once, same as `close()`'s tail.
                self.done_closing.set()
                if self.on_closed is not None:
                    self.on_closed()
                self._idle_timer_check()  # cancels any armed timer; closed now, so none rearms
                worker = self._worker
                if worker is not None and worker is not asyncio.current_task():
                    # Called directly (`!status`, `!bypass`), not from this session's own
                    # worker: an idle worker left blocked on the queue (most likely on this very
                    # `_connect_lock`, still held here) would never learn to stop otherwise. It
                    # is cancelled and awaited first, and its `_taken` turn (if any) rescued only
                    # once it is confirmed stopped: cancelling it while it still held a turn,
                    # with nothing rescuing that turn, left that reply saying "writing" forever
                    # (a real regression, caught by
                    # test_a_direct_gone_call_rescues_the_worker_s_taken_turn).
                    # When `worker` IS the current task (the common case: this branch runs inside
                    # the worker's own call to `ensure_connected`), its own `except SessionGone`
                    # clause up the stack already fails `self._taken` itself; failing it again
                    # here too would double the reply's text.
                    worker.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await worker
                    taken, self._taken = self._taken, None
                    if taken is not None:
                        with contextlib.suppress(Exception):
                            await self._fail(taken, texts.SESSION_GONE, error=True)
                await self.fail_queued(texts.SESSION_GONE)
                raise SessionGone from exc
            try:
                info = await client.get_server_info() or {}
                self.commands = list(info.get("commands") or [])
                # The setup keys every option on `value`: an entry without one is left out here,
                # so a CLI that lists one cannot stop every first prompt.
                self.models = [
                    m
                    for m in info.get("models") or []
                    if isinstance(m, dict) and isinstance(m.get("value"), str) and m["value"]
                ]
                self.native_mode = str(info.get("current_permission_mode") or "default")
                # Read once: a Start writing the switch meanwhile must not make the client's
                # mode and what is recorded for it disagree.
                choice = self.bypass_choice
                if choice is True:
                    await client.set_permission_mode("bypassPermissions")
                elif choice is False and self.native_mode == "bypassPermissions":
                    # An explicit off outlives a rebuild: the folder's own bypass would return.
                    await client.set_permission_mode("default")
            except BaseException:
                # A client nobody holds would leave its Claude Code process running.
                await self._disconnect(client)
                raise
            self._client = client
            self._client_effort = effort
            self._client_bypass = self._bypass_runs(choice)
            # A resumed session runs at the settings' level (measured), unknown until reported,
            # unless the daemon itself just asked for a stored level: that request is shown at
            # once, until Claude Code's own report (every turn ends with one) corrects it.
            requested = effort if effort in VALID_EFFORT_LEVELS else None
            self.effort, self.effort_reported = requested, requested is not None
            self.working_directory = None  # the new process starts in the bound folder
            self.session_tokens = None  # counted by the client process, which starts at zero
            name = f"reader-{self.channel_id}-{self.thread_ts}"
            self._reader = asyncio.create_task(self._read(client), name=name)
            self._idle_timer_check()  # a daemon word (`!status`, say) connects with no turn (D9)
            return client

    async def set_bypass(self, on: bool) -> None:
        client = await self.ensure_connected()
        mode = self._mode_for(on)
        try:
            await client.set_permission_mode(mode)  # type: ignore[arg-type]
        except Exception:
            if self._closed:  # the close disconnected the client under the call
                raise SessionClosed from None
            raise
        self._client_bypass = on
        # The session closed while the mode was being set: the thread this would write to may
        # already be gone, so the switch is never recorded after the fact.
        if self._closed:
            raise SessionClosed
        self._deps.state.set_bypass(self.channel_id, self.thread_ts, on)

    async def switch_bypass(self, on: bool) -> bool:
        """`!bypass on` or `off` typed in the thread: switch and store it, True; or False,
        changing nothing, before Start (`before_start`). A Start being applied is waited for,
        so the word, typed after the click, is what holds; one that failed has undone itself
        by then, and the word is refused."""
        async with self._setup_lock:
            if self.before_start:
                return False
            await self.set_bypass(on)
            return True

    @property
    def never_ran(self) -> bool:
        """No turn was ever queued in this thread: its next message is a first prompt, which asks
        for the setup again (after a cancelled setup or a D8 Cancel, nothing was sent)."""
        stored = self._deps.state.thread(self.channel_id, self.thread_ts)
        return not self._submitted and (stored is None or stored.session_id is None)

    @property
    def before_start(self) -> bool:
        """No first prompt was sent and no Start applied: the setup's Bypass box is what sets
        bypass, and `apply_setup` writes it over anything set before."""
        return self.never_ran and not self._setup_applied

    async def apply_setup(self, choice: Choice) -> None:
        """Apply the owner's session setup before the first prompt. Start is authoritative: the
        effort and the bypass switch are written from the choice whatever state.json held
        (a restart can have left either; `!bypass` is refused before Start), so what
        runs is what the summary line says. An effort the live client was not built with goes
        through a fresh client (the SDK has no runtime effort setter; no query has been sent, so
        no session is lost), then the model on the live client, then the permission mode: when
        the choice differs from what the client effectively runs (the switch, or a folder whose
        own settings start it in bypass), `set_bypass` moves it. Unticking in such a folder is an
        explicit off with the semantics of `!bypass off`: the mode it returns to is `default`.
        Measured 2026-09-30 (CLI 2.1.285): `set_model` survives a resume and leaves the owner's
        own default alone, so the daemon does not store the model. A failure undoes what was
        applied (`forget_setup`) and goes on. All of it under `_setup_lock`, which a word's
        `switch_bypass` waits on."""
        async with self._setup_lock:
            self._setup_applied = True
            effort = None if choice.effort == DEFAULT else choice.effort
            try:
                self._deps.state.set_effort(self.channel_id, self.thread_ts, effort)
                self._deps.state.set_bypass(self.channel_id, self.thread_ts, choice.bypass)
                client = await self.ensure_connected()
                if effort != self._client_effort:
                    client = await self._reconnect()
                if choice.model != DEFAULT:
                    await client.set_model(choice.model)
                if choice.bypass != self._client_bypass:
                    # A client connected above applies the stored switch itself: this only moves one
                    # that was already live, off meaning the mode it would have had without bypass.
                    await self.set_bypass(choice.bypass)
            except BaseException:
                try:
                    await self.forget_setup()
                except Exception as undo:
                    logger.warning(
                        "could not undo a failed setup in %s/%s: %s",
                        self.channel_id,
                        self.thread_ts,
                        describe(undo),
                    )
                raise

    async def forget_setup(self) -> None:
        """Undo a Start whose message was not sent (a failure, a stop, a D8 Cancel, or a restart
        that kept what it wrote): the stored effort and bypass go back to never chosen, and the
        client is dropped, since it carries the effort option, the model and the mode, which the
        next connect rebuilds. A no-op for a thread that ran a turn (its choices are the
        owner's) and when there is nothing to undo."""
        stored = self._deps.state.thread(self.channel_id, self.thread_ts)
        left = stored is not None and (stored.effort is not None or stored.bypass is not None)
        if not self.never_ran or not (self._setup_applied or left):
            return
        self._setup_applied = False
        self._deps.state.set_effort(self.channel_id, self.thread_ts, None)
        self._deps.state.set_bypass(self.channel_id, self.thread_ts, None)
        await self._drop_client()

    async def _reconnect(self) -> ClaudeClient:
        """Close the live client and connect again, so `ensure_connected` builds the options
        from state as they are now."""
        await self._drop_client()
        return await self.ensure_connected()

    async def _drop_client(self) -> None:
        async with self._connect_lock:
            reader, self._reader = self._reader, None
            client, self._client = self._client, None
            if reader is not None:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader
            if client is not None:
                await self._disconnect(client)

    async def show_restart_wait(self) -> None:
        """During a stop: bring the thread's status line up to date. Once only background tasks
        are left it says which ones the restart waits for (`_thread_line`), since only the
        owner knows whether a task (a dev server, a watcher) ever ends. A status line and not a
        message: a message would notify, and would stay in the thread after the restart. Where
        Slack refuses this app a thread status, a message says it, once: a restart that waits
        without a word is worse than one notification."""
        self._show_thread_status()
        if not ThreadStatus.refused() or self._told_waiting or self.busy:
            return
        if held := self._running_kinds(awaited_only=True):
            self._told_waiting = True
            await self._post(texts.RESTART_WAITS_MESSAGE.format(counts=held))

    def thread_written(self) -> None:
        """A message of the app's was posted in this thread outside the session: Slack clears a
        thread's status line when the app replies, so it is set again."""
        self._thread_status.wrote()

    async def stop(self) -> bool | None:
        """Interrupt the running turn, deny its pending approvals and stop this thread's
        background tasks; queued turns stay queued. A D8 hold open in this thread is always
        cancelled too, silently (`submit_to_session` tells the owner `Not sent.` once it wakes).
        None when that hold was the only thing here: the caller adds no further notice of its
        own then, since a hold with nothing else running stops nothing Claude Code itself was
        doing, and `Not sent.` alone already answers `!stop`."""
        held = await self.cancel_hold()
        if self._client is None:
            return None if held else False
        tasks = self._running_task_ids()
        if not self.busy and not tasks:
            return None if held else False
        if self.busy:
            # D10: a denial `deny_all` triggers below resolves `_can_use_tool`'s own future, whose
            # `finally` would otherwise race this method's own closing ✅ back to working; this
            # flag makes it skip that instead. `_finish` or `_abandon` clears it once this
            # turn's own tail ends.
            self._interrupting = True
            self._stop_tail = self._stop_tail or asyncio.Event()
            for pending in self._deps.approvals.deny_all(self.channel_id, self.thread_ts):
                await self._delete_request(pending.message_ts)
            await self._client.interrupt()
        for task_id in tasks:
            self._stopped.add(task_id)
            try:
                await self._client.stop_task(task_id)
            except Exception as exc:  # it may have ended meanwhile; the others still stop
                logger.warning(
                    "could not stop a task in %s/%s: %s",
                    self.channel_id,
                    self.thread_ts,
                    describe(exc),
                )
        # D10: a stop the owner gave is not an error. `_react` clears `_error_standing`, so a
        # later idle sweep shows the same ✅ and `_note_status` drops the persisted ⏳.
        self._react(Status.DONE)
        return True

    async def stop_landed(self) -> None:
        """Return once the reply `stop()` cut short has ended, so what is posted next sits under
        its ending and its footer. Waits at most STOP_TAIL_WAIT, and not at all when the stop
        interrupted no turn (it stopped background tasks only)."""
        tail = self._stop_tail
        if tail is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(tail.wait(), STOP_TAIL_WAIT)

    def _stop_tail_ended(self) -> None:
        tail, self._stop_tail = self._stop_tail, None
        if tail is not None:
            tail.set()

    async def status(self) -> str:
        """The channel's directory, session and mode, then the footer's values one per line, or
        why Claude Code cannot start.

        Starts the client like `!help` does, since model and context come from it.
        """
        data: FooterData | None = None
        unavailable: list[str] = []
        gone = False
        usable = True
        try:
            await self.ensure_connected()
            self._refresh_usage()
            data = await self._footer_data(self.session_tokens)
        except DirectoryUnavailable as exc:
            unavailable, usable = [exc.message], False
        except SessionGone as exc:
            # This call is the one that found it gone: `ensure_connected` already closed the
            # session over it, which is not the race the check below guards against.
            unavailable, gone = [exc.message], True
        except Exception as exc:  # the status still answers, with what a prompt would get
            logger.warning(
                "could not read the footer's values for the status in %s/%s: %s",
                self.channel_id,
                self.thread_ts,
                describe(exc),
            )
            unavailable = [texts.ERROR_REPLY.format(error=type(exc).__name__)]
        # The daemon closed the session (shutdown, an idle close) while this call was reading it.
        if self._closed and not gone:
            raise SessionClosed
        fields = format_status_fields(data, datetime.now().astimezone()) if data else []
        here = self.working_directory
        if data and here and here != self.directory:
            # The branch and the changes below describe this folder, not the thread's own.
            fields.insert(0, texts.STATUS_WORKING.format(directory=here))
        if running := self._running_kinds():
            fields.append(texts.STATUS_BACKGROUND.format(counts=running))
        stored = self._deps.state.thread(self.channel_id, self.thread_ts)
        activity = (
            texts.ACTIVITY_BUSY.format(queued=self._queue.qsize())
            if self.busy
            else texts.ACTIVITY_IDLE
        )
        session_id = stored.session_id if stored else None
        head = [texts.STATUS.format(directory=self.directory, session=session_id or "new")]
        # Left out with a folder the line above cannot use: its `cd` would fail there.
        if session_id and usable:
            head.append(terminal_line(self.directory, session_id))
        state = texts.STATUS_STATE.format(
            mode=self._mode_for(self.bypass),
            # Only a turn's `init` message carries the version, never the connect (measured).
            version=self.cli_version
            or (texts.VERSION_PENDING if self._client is not None else "not started"),
            activity=activity,
        )
        return "\n".join([*head, state, *fields, *unavailable])

    def _refresh_usage(self) -> None:
        """Refresh the usage limits in the background when stale; the next footer shows them."""
        refresh = asyncio.create_task(self._deps.usage.refresh_if_stale())
        self._background.add(refresh)
        refresh.add_done_callback(self._background.discard)

    async def close(self, reason: str = texts.ENDED_SHUTDOWN) -> None:
        """Stop the session. The running reply ends with a line saying why, and one note names the
        messages that were sent or queued and will never be answered, so no stream is left open.

        `done_closing` always fires, from a `finally`: a step below raising must not leave a
        rebuilt session's `ensure_connected` waiting on it forever, or `close_all` hanging at
        shutdown, or the manager's map holding a session nothing can ever evict.
        """
        # D10: read before anything below settles it back to idle. A close that cuts anything
        # short (a shutdown or a restart's drain, most likely: `busy` alone misses a queued or
        # taken turn and a task that outlived its own turn, which `idle` already accounts for)
        # gets ❌; D9's idle close, always called on an idle session, never does, and only
        # finishes a change of the reaction still on its way (`StatusReaction.settle`).
        cut_short = not self.idle
        # Issue #87: at the end of a stop, a session whose only unfinished work is a background
        # task the stop does not wait for (`may_have_ordered_restart`) ended its turn well; the
        # task is most likely its own wait for the new process. ✅, as `!stop` would show.
        only_skipped = cut_short and self.draining and self.restart_ready
        self._closed = True
        try:
            # Issue #19 fix round item 8: a snapshot taken before any cancellation below, not
            # `deny_all`'s own return list read afterward. `_cancel_tasks` cancels the reader
            # task, which throws `CancelledError` into a request genuinely being awaited inside
            # `_can_use_tool` regardless of whether its future was resolved first (asyncio does
            # this once `cancel()` is requested before the task's next resume actually runs), and
            # `_can_use_tool`'s own `finally` discards the approval from `Approvals` either way
            # (cancelled or resolved): by the time `deny_all` ran after that, there would be
            # nothing left in it. Deleting each message here does not depend on `_can_use_tool`
            # ever seeing a decision, so the cancel-first order below (kept exactly as it was, for
            # its own reason) stays untouched.
            requests_to_delete = [
                pending.message_ts
                for pending in self._deps.approvals.pending_in(self.channel_id, self.thread_ts)
            ]
            # The worker may hold the connect lock while the CLI starts: cancelled first.
            await self._cancel_tasks()
            # A daemon word may be starting the client in its own task. Its connect finishes,
            # then the reader it started is cancelled and its client closed below: no process
            # outlives the session, and none resumes the session id the next one will resume.
            async with self._connect_lock:
                await self._cancel_tasks()
            self._deps.approvals.deny_all(self.channel_id, self.thread_ts)
            for message_ts in requests_to_delete:
                await self._delete_request(message_ts)
            # A turn the worker took but had not sent yet is in no queue.
            taken, self._taken = self._taken, None
            unsent = [taken] if taken is not None else []
            while not self._queue.empty():
                unsent.append(self._queue.get_nowait())
            await self._abandon(
                texts.ENDED.format(reason=reason), more=unsent, because=texts.BECAUSE_SHUTDOWN
            )
            if self._client is not None:
                client, self._client = self._client, None
                await self._disconnect(client)
        finally:
            if self._predecessor is not None:
                # This object's own predecessor may still be mid-teardown: `ensure_connected`
                # never ran here (this session closed with no turn ever submitted), so nothing
                # else has waited on it yet. `done_closing` must never fire before it does, or a
                # session built after this one could resume the same id while an earlier one is
                # still exiting (the chain, not just the direct predecessor, must be honoured).
                await self._predecessor.wait()
                self._predecessor = None
            # Only now, with every task cancelled: a Slack round trip must not hold those up.
            await self._thread_status.close()
            # The last chance a change still only debounced gets: `asyncio.run`'s own exit never
            # lets a `_later` still waiting on its own timer run. Also the last chance of a
            # reply whose end failed and whose retry is still waiting: each sink once.
            lost = False
            latest = [self._latest] if self._latest is not None else []
            for reply in {*self._unlanded, *latest}:
                try:
                    lost = not await reply.settle() or lost
                except Exception:
                    lost = True
            if only_skipped and not lost:
                self._note_status(Status.DONE)
                await self._status.show(Status.DONE)
            elif cut_short or lost:
                # Awaited, not `_react`: `_cancel_tasks` above has already run, so a task added
                # to `_background` now would never be cancelled or awaited by anything again.
                # Before `done_closing`/`on_closed`, not after: those let the manager evict this
                # session and hand the same root to a freshly built one right away, whose own
                # `StatusReaction` strips every other name on its own first `show` (D10) — this
                # ❌ must already be on the root before that race can even start. An answer that
                # never reached Slack keeps the persisted status (below), so the next start's
                # repair still says so.
                if not lost:
                    self._note_status(Status.ERROR)
                await self._status.show(Status.ERROR)
            else:
                # Issue #104: a session reads idle as soon as its turn ends, while its reader
                # may still be between the two calls that move ⏳ to ✅, and `_cancel_tasks`
                # above stops it there. No call when the reaction already reads as asked.
                await self._status.settle()
            # Crash repair (issue #19): a graceful close is not what repair is for, whatever is
            # still live in Slack when it ends (an approval message `_cancel_tasks` cut off
            # mid-await, most likely: `_can_use_tool`'s own finally never reached the point that
            # would have cleared it), so this thread's fields end up empty either way. Best-effort
            # (fix round item 7): a failed write here must never skip `done_closing`/`on_closed`.
            try:
                self._deps.state.clear_repair(self.channel_id, self.thread_ts, keep_open=lost)
                if lost:
                    self._deps.state.set_status_pending(
                        self.channel_id, self.thread_ts, Status.WORKING.value, Status.ERROR.value
                    )
            except Exception as exc:
                logger.warning(
                    "could not clear state.json's crash-repair fields for %s/%s: %s",
                    self.channel_id,
                    self.thread_ts,
                    describe(exc),
                )
            # Only now: the CLI process (if any) has had its chance to flush and exit, or closing
            # failed partway through and there is nothing left worth waiting for either way.
            self.done_closing.set()
            if self.on_closed is not None:
                self.on_closed()

    async def _cancel_tasks(self) -> None:
        # Every task is cancelled before any is awaited: a reader left running while the worker
        # stops could still end a turn and record its session after a rebind. `_expiring`'s own
        # timers go too: none should outlive the session, and a stray one firing
        # after would try to post a closing through one that is already gone. `_background`'s
        # tasks are not cancelled here: a usage refresh shares one
        # `UsageProbe` client across every session, and cancelling it mid-query (`UsageProbe`
        # does not catch `CancelledError`) would leave that client answering the next lookup,
        # of any session, late.
        tasks = [
            t
            for t in (
                self._worker,
                self._reader,
                self._expiry,
                self._idle_expiry,
                *self._expiring,
            )
            if t is not None
        ]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _disconnect(self, client: ClaudeClient) -> None:
        try:
            await client.disconnect()
        except Exception as exc:
            logger.warning(
                "could not close Claude Code in %s/%s: %s",
                self.channel_id,
                self.thread_ts,
                describe(exc),
            )

    async def _connect(self, session_id: str | None, effort: str | None) -> ClaudeClient:
        valid_effort: EffortLevel | None = None
        if effort in VALID_EFFORT_LEVELS:
            valid_effort = cast(EffortLevel, effort)
        elif effort is not None:
            logger.warning(
                "dropped an unrecognized stored effort level in %s/%s",
                self.channel_id,
                self.thread_ts,
            )
        options = client_options(
            self.directory,
            session_id,
            valid_effort,
            self._can_use_tool,
            self._on_stop,
            self._on_tool_done,
        )
        client = self._deps.client_factory(options)
        await client.connect()
        return client

    async def _work(self) -> None:
        # Closed (the SessionGone branch of `ensure_connected`, most likely) ends the loop
        # instead of looping back to an empty queue nothing will ever fill again: an orphan
        # worker task, blocked forever, is not what a closed session leaves behind.
        while not self._closed:
            turn = await self._queue.get()
            self._taken = turn
            try:
                client = await self.ensure_connected()
                self._refresh_usage()
                await self._settled.wait()
                if self.draining:
                    self._taken = None
                    # D10: a prompt genuinely dropped by the drain, reacting ❌; like the queued
                    # ones, it gets no reply of its own, only a note.
                    await self._drop([turn], texts.BECAUSE_RESTARTED, error=True)
                    self._idle_timer_check()  # only cancels: draining itself blocks it from arming
                    continue
                self._sent.append(turn)
                self._taken = None
                await client.query(user_message(turn.prompt, turn.uuid))
                await turn.done.wait()
            except (DirectoryUnavailable, SessionGone, SessionClosed) as exc:
                self._taken = None
                await self._fail(turn, exc.message, error=True)
                self._idle_timer_check()  # a no-op if this also closed the session (D9)
            except Exception as exc:  # a failed turn must not stop this thread's queue
                self._taken = None
                logger.error(
                    "turn failed in %s/%s: %s", self.channel_id, self.thread_ts, type(exc).__name__
                )
                if turn in self._sent:
                    self._sent.remove(turn)
                await self._fail(
                    turn, texts.ERROR_REPLY.format(error=type(exc).__name__), error=True
                )
                self._idle_timer_check()  # the session stays alive; may need arming again (D9)

    async def _read(self, client: ClaudeClient) -> None:
        """Follow the client's stream until it ends; whatever ends it, release the thread."""
        reason = "the Claude Code process exited"
        try:
            async for message in client.receive_messages():
                try:
                    await self._dispatch(message)
                except Exception as exc:  # one message that fails to render must not end it all
                    logger.error(
                        "could not render a message in %s/%s: %s",
                        self.channel_id,
                        self.thread_ts,
                        describe(exc),
                    )
                # A task frame or a turn's own end (through `_dispatch`) may leave the session
                # idle again, or may end it: re-armed or cancelled here either way (D9).
                self._idle_timer_check()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = type(exc).__name__
        logger.error("session reader stopped in %s/%s: %s", self.channel_id, self.thread_ts, reason)
        if self._client is client:
            self._client = None
        try:
            await self._abandon(
                texts.ERROR_REPLY.format(error=reason), error=True, because=texts.BECAUSE_STOPPED
            )
        finally:
            with contextlib.suppress(Exception):
                await client.disconnect()
        # The session stays alive (not closed): the next turn reconnects. `_abandon` settles it
        # from outside this loop's own per-message check above, so it needs its own call (D9).
        self._idle_timer_check()

    def _acknowledge(self, message: UserMessage) -> bool:
        """Whether `message` is the replay of a prompt this session sent (`replay-user-messages`),
        which renders nothing and starts no turn: a replay with no turn running comes right
        before the stream of the turn that prompt gets of its own. One that arrives inside a
        running turn means Claude Code took the prompt into that turn, which then ends with one
        result for both (measured 2026-10-06, CLI 2.1.286): `_finish` releases it. A uuid that
        names no prompt of ours (a resumed session's) is no acknowledgement."""
        if message.uuid is None:
            return False
        active = self._active
        sent = [*self._sent, *([active.turn] if active and active.turn else [])]
        turn = next((t for t in sent if t.uuid == message.uuid), None)
        if turn is None:
            return False
        if active is not None and turn not in active.taken:
            active.taken.append(turn)
        return True

    async def _dispatch(self, message: Message) -> None:
        if isinstance(message, RateLimitEvent):
            self._deps.usage.invalidate()
            return
        if isinstance(message, UserMessage) and self._acknowledge(message):
            return
        if isinstance(message, SystemMessage) and message.subtype == "init":
            self.cli_version = message.data.get("claude_code_version")
            # D6: recorded as soon as Claude Code reports it, not only at the turn's
            # ResultMessage, so a still-running first turn is already this session id's
            # `holder()` and `!resume` cannot put a second process on the same transcript.
            init_session_id = message.data.get("session_id")
            if isinstance(init_session_id, str):
                self._deps.state.set_session(self.channel_id, self.thread_ts, init_session_id)
        if isinstance(message, TASK_MESSAGES) and (inner := self._nesting_reply(message)):
            # A task of a call inside another call (a long command a subagent runs) is that
            # call's root's work: Claude Code reports it to the subagent, so no turn follows it.
            # None of what a task of the conversation sets up (its record, its end line, a wait
            # for its report, a held frame) applies; the reply keeps the frame off its cards.
            await inner.feed(message)
            return
        if isinstance(message, TaskStartedMessage):
            self._record_task(message)
        stopped = isinstance(message, TASK_MESSAGES) and message.task_id in self._stopped
        if (
            isinstance(message, TaskNotificationMessage)
            and self._active is None
            and not self._sent
            and not stopped
        ):
            self._ended.append((message.task_id, self._ended_line(message)))
        if (
            isinstance(message, TaskUpdatedMessage)
            and message.status in TERMINAL_TASK_STATUSES
            and message.task_id not in self._unreported
            and not stopped
        ):
            self._unreported[message.task_id] = asyncio.get_running_loop().time()
            # D1: the CLI can suppress the notification altogether (SDK
            # TaskUpdatedMessage docstring); nothing else rechecks this reply once the wait
            # that holds it passes, so this schedules that recheck itself.
            expiry = asyncio.create_task(self._expire_unreported(message.task_id))
            self._expiring.add(expiry)
            expiry.add_done_callback(self._expiring.discard)
        if isinstance(message, TaskNotificationMessage):
            self._unreported.pop(message.task_id, None)
            self._stopped.discard(message.task_id)
            # Read above for the end line, which comes after the terminal task_updated (recorded
            # order); a task whose notification never comes goes with the process.
            self._tasks.pop(message.task_id, None)
        if isinstance(message, TASK_MESSAGES) and message.task_id in self._task_replies:
            # A task that outlived its turn shows only on its own line, wherever it started.
            holder = self._task_replies[message.task_id]
            await holder.feed(message)
            await self._show_running()
            if (
                self._active is None
                and isinstance(message, TaskNotificationMessage)
                and not stopped
            ):
                self._notified()
            # D1: the task that just ended may have been the last thing keeping this reply's
            # stream open; `_notified` above, if it fired, already re-armed the wait.
            owed = self._still_owed(holder) or self._injected_expected or not self._settled.is_set()
            if not owed:
                self._track_landing(await holder.close_out(), holder.sink)
                self._show_thread_status()  # its footer may be what counts the tasks now
                # D10: `!stop` already reacted ✅ itself; skipped so a stopped task's end does
                # not show it a second time.
                if not stopped:
                    await self._react_done_if_idle()
            return
        # A stream message names the call it runs under (`parent_tool_use_id`), a task frame
        # the call that started it (`tool_use_id`): either leads to the reply holding that call
        # when a background subagent works on after its turn.
        call = getattr(message, "parent_tool_use_id", None) or (
            getattr(message, "tool_use_id", None) if isinstance(message, TASK_MESSAGES) else None
        )
        origin = self._origin_of(call) if call else None
        if origin is not None:
            await origin.feed(message)
            await self._adopt_promoted(origin)
            if isinstance(message, TaskStartedMessage):
                self._task_replies[message.task_id] = origin
                await self._show_running()
            return
        if self._active is None:
            # A task started by no call, while an owner prompt is sent and no report turn is
            # expected, is that prompt's: a skill with `context: fork` typed as a command
            # (`!review` → `/review`) runs its agent before the turn's first message (recorded:
            # `skill-fork-command.jsonl`, CLI 2.1.283, the only recording with no
            # `tool_use_id`). Its turn starts now, so its line shows while it works.
            starts_owner_turn = (
                isinstance(message, TaskStartedMessage)
                and message.tool_use_id is None
                and bool(self._sent)
                and not self._injected_expected
            )
            if not starts_owner_turn:
                if isinstance(message, TASK_MESSAGES):
                    self._held.append(message)
                    if isinstance(message, TaskNotificationMessage) and not stopped:
                        self._notified()
                    return
                if not isinstance(message, TURN_MESSAGES):
                    return
                if call:
                    # A subagent's own frame, whose call no reply holds: an agent continued with
                    # `SendMessage` in a daemon that never saw the call that first started it
                    # (recorded: `report-turn-agent-resume.jsonl`, CLI 2.1.286, whose frames name
                    # that first call, not the `SendMessage` one). It is no turn of the
                    # conversation's, so it starts none and takes no owner prompt's.
                    return
            self._active = await self._start_turn()
            # A report turn has no prompt behind it: only now does `_thread_line` read it.
            self._show_thread_status()
        active = self._active
        if isinstance(message, AssistantMessage) and message.error is not None:
            # The category only: the words Claude Code wrote about it go to the thread.
            logger.warning(
                "Claude Code reported an error in %s/%s: %s",
                self.channel_id,
                self.thread_ts,
                message.error,
            )
        await active.renderer.feed(message)
        await self._adopt_promoted(active.renderer)
        if isinstance(message, ResultMessage):
            # The turn stays active until its reply is closed: the footer is read first, and a
            # drain that saw this thread idle meanwhile would exit before the reply's end,
            # leaving its stream open (#25).
            try:
                stopped = await self._finish(active, message)
            finally:
                self._active = None
                self._show_thread_status()  # whichever way it ended, `!stop` included
            # D10: checked only now, with `_active` cleared: `_finish` alone still reads busy.
            if not stopped:
                await self._react_done_if_idle()

    def _record_task(self, message: TaskStartedMessage) -> None:
        self._tasks[message.task_id] = (message.task_type or "", message.description)
        if self.may_have_ordered_restart:
            self._after_restart.add(message.task_id)
        while len(self._tasks) > TASKS_KEPT:
            del self._tasks[next(iter(self._tasks))]

    async def _adopt_promoted(self, renderer: TurnRenderer) -> None:
        """Tasks that just outlived the nested call that started them are ordinary background
        tasks from now on: recorded and counted as one that outlived its turn."""
        promoted = renderer.take_promoted()
        for started in promoted:
            self._record_task(started)
            self._task_replies[started.task_id] = renderer
        if promoted:
            await self._show_running()

    def _ended_line(self, message: TaskNotificationMessage) -> str:
        """The terminal's line for a task's end: a command's own summary, or `Agent "..."
        finished` from the task's description, plus the duration when the task reports one."""
        task_type, description = self._tasks.get(message.task_id, ("", ""))
        if task_type in SUMMARY_IS_END_LINE and message.summary:
            text = one_line(message.summary, 200)
        else:
            origin = self._task_replies.get(message.task_id)
            label = description or (origin.task_title(message.task_id) if origin else None)
            name = TASK_KINDS.get(task_type, UNKNOWN_KIND)[1]
            outcome = {"completed": "finished"}.get(message.status, message.status)
            text = f'{name} "{one_line(label or message.task_id, 120)}" {outcome}'
        duration = message.usage["duration_ms"] if message.usage else None
        return ended_line(text, message.status, duration)

    def _opening_target(self) -> tuple[str, TurnRenderer | None]:
        """What opens a report of Claude Code's own turn (the end of each task it reports), and
        the reply that started the first of those tasks (D1: the report renders there), if it is
        still tracked. `None` when it is not (a restart or an idle close dropped it, or it already
        ended: a notification later than INJECTED_TURN_WAIT let `_expire_unreported` end it
        first): the report then gets a reply of its own, as it always has, rather than an edit of
        a reply that ended and will not notify again."""
        lines, self._ended = self._ended, []
        target = self._task_replies.get(lines[0][0]) if lines else None
        if target is not None and target.closed_out:
            target = None
        text = "\n".join(line for _, line in lines) or texts.BACKGROUND_NOTICE
        return text, target

    def _notified(self) -> None:
        """A task ended while no turn runs. Claude Code starts a turn to report it, unless an
        owner query already sits in its queue: that turn comes first and carries the report
        (measured 2026-09-24, Claude Code 2.1.280), and no turn of its own follows."""
        if not self._sent:
            self._expect_injected_turn()

    def _expect_injected_turn(self) -> None:
        self._injected_expected = True
        self._settled.clear()
        if self._expiry is None or self._expiry.done():
            self._expiry = asyncio.create_task(self._expire_injected_turn())

    async def _expire_injected_turn(self) -> None:
        await asyncio.sleep(INJECTED_TURN_WAIT)
        if not self._injected_expected or self._active is not None:
            return
        logger.warning(
            "no turn followed a task notification in %s/%s", self.channel_id, self.thread_ts
        )
        self._injected_expected = False
        held, self._held = self._held, []
        text, target = self._opening_target()
        try:
            if held:
                renderer = target or TurnRenderer(await self._sink(), str(self.directory))
                await self._standalone(held, text, renderer)
        except Exception as exc:
            logger.warning(
                "could not post a background update in %s/%s: %s",
                self.channel_id,
                self.thread_ts,
                describe(exc),
            )
        finally:
            # A notification that arrived while this wrote is expected on a wait of its own, armed
            # below: `_expect_injected_turn` found this task still running and started none, and
            # it cleared `_settled`, which stays so. (`_start_turn` cancelling this task leaves
            # `_injected_expected` False.)
            if not self._injected_expected:
                self._settled.set()
            try:
                # D1: catches `target` (nothing more coming for it either,
                # when `held` was empty) and any other reply a joint report turn left stranded,
                # since a report only ever renders into the first of the tasks it covers.
                await self._sweep_closed_out()
                await self._react_done_if_idle()
            finally:
                # Whatever the sweep did: a notification that arrived during its own writes is
                # waiting on a timer this task alone can arm, since nothing awaits between here
                # and its end. None once the session is closed: `close` has cancelled this task
                # and a timer made now would outlive `_cancel_tasks`.
                if self._injected_expected and not self._closed:
                    self._expiry = asyncio.create_task(self._expire_injected_turn())
                # Runs outside `_read`'s loop (its own INJECTED_TURN_WAIT timer), so nothing else
                # re-checks idleness for this transition: it may need to arm the timer itself
                # (D9).
                self._idle_timer_check()

    def _react(self, state: Status) -> None:
        """Show `state` on the root message reaction as a task this session tracks (D10): a
        reaction must never delay a turn, and `StatusReaction.show` already serializes its own
        calls and swallows their errors, so nothing here waits on it. `_error_standing` follows
        the state asked for last, set here synchronously, since `StatusReaction.current` changes
        only once Slack has answered."""
        self._error_standing = state is Status.ERROR
        self._note_status(state)
        self._show_thread_status()
        task = asyncio.create_task(self._status.show(state))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _note_status(self, state: Status) -> None:
        """Crash repair (issue #19): the root's reaction while it is ⏳ or ✋, cleared once ✅ or
        ❌ is requested; that one is kept as the thread's ended reaction, which the Home tab
        shows. Called from `_react` and from the two other places that ask `StatusReaction` for
        a state directly (`close`'s ❌, `_react_done_if_idle`'s ✅)."""
        pending = state in (Status.WORKING, Status.WAITING)
        self._deps.state.set_status_pending(
            self.channel_id,
            self.thread_ts,
            state.value if pending else None,
            None if pending else state.value,
        )

    def _note_cross_over_status(self) -> None:
        """❌ is shown while the persisted ⏳ or ✋ stays for crash repair (an answer never reached
        Slack): kept as the ended reaction, which is what the session index shows."""
        stored = self._deps.state.thread(self.channel_id, self.thread_ts)
        if stored is not None:
            self._deps.state.set_status_pending(
                self.channel_id, self.thread_ts, stored.status, Status.ERROR.value
            )

    def _react_error(self) -> None:
        """❌ (D10). It stands until another state is asked for: `_react` records it in
        `_error_standing` at once, before Slack answers, so a quick turn's own idle sweep cannot
        mistake this session for done."""
        self._react(Status.ERROR)

    def _react_waiting_or_working(self) -> None:
        """Back to waiting, or working, right after an approval or question settles (D10):
        whichever the thread still holds, since a parallel tool call can leave another one open."""
        self._react(Status.WAITING if self.waiting_for_owner else Status.WORKING)

    async def _react_done_if_idle(self) -> None:
        """✅, once the reply that just ended turns out to have been the last thing the session
        owed (D10): awaited, unlike `_react`, so the checkmark never shows before the reply's
        end has landed. A no-op while another prompt, a running task or an unreported one still
        keeps the session going, or while `_error_standing` says a ❌ from
        `_abandon(error=True)` already stands: that lasts until new work starts (a submit or a
        report turn shows ⏳ again, both clearing it), not until some unrelated task's own sweep
        decides the session reads idle again. Gated on `_error_standing`, not
        `StatusReaction.current`: the reaction only updates once its own `reactions.add`
        returns, which a quick turn can easily outrun."""
        self._show_thread_status()
        if (
            not self.waiting_for_owner
            and self.idle
            and not self._error_standing
            and not self._unlanded
        ):
            self._note_status(Status.DONE)
            await self._status.show(Status.DONE)

    def _track_landing(self, landed: bool, reply: Sink) -> None:
        """A reply whose end failed is not done: no ✅ until its one retry lands, ❌ if that
        fails too. The persisted status stays as it is meanwhile (and after a final failure), so
        a crash still gets `repair`'s notice."""
        if landed:
            return
        self._unlanded.add(reply)
        if self._closed:
            return  # `close` settles every unlanded reply itself: nobody would await a task
        task = asyncio.create_task(self._await_landing(reply))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _await_landing(self, reply: Sink) -> None:
        landed = await reply.wait_landed()
        self._unlanded.discard(reply)
        if self._closed:
            return
        if landed:
            await self._react_done_if_idle()
        else:
            # ❌ shown, but `state.json` keeps ⏳ or ✋: the answer never reached Slack.
            self._error_standing = True
            self._note_cross_over_status()
            task = asyncio.create_task(self._status.show(Status.ERROR))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    def _idle_timer_check(self) -> None:
        """(Re)arm the idle-close timer (D9) when the session is now idle with no approval or
        question pending; cancel it otherwise. Cheap and idempotent, so every place that could
        change either just calls it again."""
        if self._idle_expiry is not None:
            self._idle_expiry.cancel()
            self._idle_expiry = None
        if self._closed or self.draining or self.waiting_for_owner or not self.idle:
            return
        name = f"idle-close-{self.channel_id}-{self.thread_ts}"
        self._idle_expiry = asyncio.create_task(self._close_when_idle(), name=name)

    async def _close_when_idle(self) -> None:
        """Close a Claude Code process nothing has needed for IDLE_CLOSE_SECONDS (D9). Silent:
        idle means nothing is running, sent or queued for `close()` to end with a line."""
        await asyncio.sleep(IDLE_CLOSE_SECONDS)
        if self._closed or self.draining or self.waiting_for_owner or not self.idle:
            return  # something started again before the hour was up
        logger.info("closing an idle session in %s/%s", self.channel_id, self.thread_ts)
        # Cleared first: `close()` cancels this very task through `_cancel_tasks`, and a task
        # cannot await itself.
        self._idle_expiry = None
        await self.close(reason=texts.ENDED_IDLE)

    async def _start_turn(self) -> ActiveTurn:
        # D10: skipped while `stop()` is still winding an interrupt down (`_finish` clears the
        # flag once that very turn's own terminal result says so): this can be that turn's own
        # trailing messages, not a new one, and `stop()`'s own ✅ must stand.
        if not self._interrupting:
            self._error_standing = False  # a turn is sent, or a report turn starts: new work
            # D8: a background task's own report turn can start while a hold waits on this very
            # session; ✋ must stand through it, not be overwritten by ⏳ (`waiting_for_owner`
            # covers a hold the same way it covers an open approval or question).
            self._react(Status.WAITING if self.waiting_for_owner else Status.WORKING)
        injected = self._injected_expected or not self._sent
        self._injected_expected = False
        if self._expiry is not None:
            self._expiry.cancel()
        turn = None if injected else self._sent.popleft()
        if turn is None:
            # D1: a report turn renders into the reply that started the task it reports, so no
            # new message follows for it; only when that reply is no longer tracked does it get
            # one of its own, as every reply always has.
            text, target = self._opening_target()
            renderer = target or TurnRenderer(await self._sink(), str(self.directory))
            await renderer.feed_notice(text)
        else:
            renderer = TurnRenderer(turn.sink, str(self.directory))
        if self._notice is not None:
            await renderer.feed_notice(self._notice)
            self._notice = None
        held, self._held = self._held, []
        for message in held:
            await renderer.feed(message)
        return ActiveTurn(turn, renderer)

    async def _standalone(self, messages: list[Message], text: str, renderer: TurnRenderer) -> None:
        """The turn the CLI never started to report an ended task, on its own reply (D1: usually
        the one that started the task, `renderer`; `_expire_injected_turn` resolves it)."""
        await renderer.feed_notice(text)
        for message in messages:
            await renderer.feed(message)
        await self._close_reply(renderer, None)

    async def _close_reply(
        self,
        renderer: TurnRenderer,
        footer: str | None,
        *,
        force: bool = False,
    ) -> None:
        for task_id in renderer.running_tasks:
            self._task_replies[task_id] = renderer
        # Forgetting one whose reply has not ended yet (D1) would
        # strand it exactly as a joint report turn can: `_still_owed`, and the sweep that acts
        # on it, both read this map.
        ended = [
            t for t, r in self._task_replies.items() if t not in r.running_tasks and r.closed_out
        ]
        for task_id in ended[: max(0, len(self._task_replies) - TASK_REPLIES_KEPT)]:
            del self._task_replies[task_id]
        # Before the close, so the reply's last write already carries the list.
        await self._show_running()
        await renderer.close(footer)
        # D1: the reply ends at once unless a task this renderer started outlives this very turn;
        # `force` is a stop, an error or a restart, which never waits for one.
        if force or not self._still_owed(renderer):
            self._track_landing(await renderer.close_out(), renderer.sink)

    async def _stop_task_replies(self) -> None:
        """The Claude Code process is going away with its tasks: no reply keeps showing one, and
        none is left waiting on an end that will now never come. D1: each ends at once, with
        whatever footer its own turn already decided."""
        renderers = set(self._task_replies.values())
        self._task_replies.clear()
        self._tasks.clear()
        self._ended.clear()
        self._unreported.clear()
        self._stopped.clear()
        for renderer in renderers:
            with contextlib.suppress(Exception):
                await renderer.stop_running()
        # Before close_out (D1): the latest reply's own `_running` must already
        # read empty, or its end would still show a stale `⏳ 1 shell`.
        await self._show_running()
        for renderer in renderers:
            with contextlib.suppress(Exception):
                self._track_landing(await renderer.close_out(), renderer.sink)
        # The cards `stop_running` just ended, and `set_running` above, only debounce (item 7):
        # a caller closing everything down cannot wait a whole `DEBOUNCE_SECONDS` for `_later`
        # to get around to them on its own. The latest reply may not itself be one of
        # `renderers` (its own turn need not have started any task).
        replies = [renderer.sink for renderer in renderers]
        if self._latest is not None:
            replies.append(self._latest)
        for reply in replies:
            with contextlib.suppress(Exception):
                await reply.settle()

    def _nesting_reply(self, message: TaskFrame) -> TurnRenderer | None:
        """The reply that holds the root of the call this task frame's task belongs to, when a
        call inside another call started it. `None` when no tracked reply can tell (a restart
        dropped it): the task is then treated as any other, since a top-level task of a call no
        reply holds looks the same."""
        replies = set(self._task_replies.values())
        if self._active is not None:
            replies.add(self._active.renderer)
        return next((r for r in replies if r.nests(message)), None)

    def _origin_of(self, tool_use_id: str) -> TurnRenderer | None:
        return next((r for r in self._task_replies.values() if r.owns(tool_use_id)), None)

    def _still_owed(self, renderer: TurnRenderer) -> bool:
        """D1: whether one of `renderer`'s own tasks still keeps its reply's stream open:
        one still runs, or ended less than INJECTED_TURN_WAIT ago with no notification yet (its
        report, if any, not in yet). The CLI can suppress the notification altogether (SDK
        TaskUpdatedMessage docstring), so past that wait this stops counting it:
        `_expire_unreported` rechecks then, since nothing else would."""
        if renderer.running_tasks:
            return True
        now = asyncio.get_running_loop().time()
        return any(
            now - ended < INJECTED_TURN_WAIT
            for task_id, ended in self._unreported.items()
            if self._task_replies.get(task_id) is renderer
        )

    async def _sweep_closed_out(self) -> None:
        """D1: a report turn's opening names only the reply of the
        first task it covers when several end together, so every other reply whose own tasks
        also finished is checked here instead, since nothing else rechecks it once the wait
        that deferred it lifts. Only while nothing is still expected or running session-wide:
        `_close_reply` already handles the renderer whose own turn or report just ended, and a
        currently active one is skipped outright, whatever it reads:
        its own turn has not closed it yet, so nothing here is its call to make."""
        active = self._active.renderer if self._active is not None else None
        for renderer in set(self._task_replies.values()):
            if self._injected_expected or not self._settled.is_set():
                return  # a notification arrived meanwhile: its report may render into this reply
            if renderer is not active and not self._still_owed(renderer):
                self._track_landing(await renderer.close_out(), renderer.sink)

    async def _expire_unreported(self, task_id: str) -> None:
        """D1: give up waiting on a task's notification after
        INJECTED_TURN_WAIT (the CLI can suppress it) and sweep for whatever that frees."""
        await asyncio.sleep(INJECTED_TURN_WAIT)
        self._unreported.pop(task_id, None)
        await self._sweep_closed_out()
        await self._react_done_if_idle()

    def _running_counts(self) -> str:
        """`⏳ 1 shell · 2 agents`: the tasks that outlived their turn and still run."""
        kinds = self._running_kinds()
        return texts.RUNNING.format(counts=kinds) if kinds else ""

    def _running_task_ids(self) -> list[str]:
        """The tasks still running: those that outlived their turn, and the running turn's."""
        outlived = [t for t, r in self._task_replies.items() if t in r.running_tasks]
        current = self._active.renderer.running_tasks if self._active is not None else []
        return list(dict.fromkeys(outlived + current))

    def _running_kinds(self, *, awaited_only: bool = False) -> str:
        """`1 shell · 2 agents`, or empty when no task outlived its turn. `awaited_only` leaves
        out the tasks a stop does not wait for (`may_have_ordered_restart`)."""
        counts: dict[str, int] = {}
        for task_id, renderer in self._task_replies.items():
            if awaited_only and task_id in self._after_restart:
                continue
            if task_id in renderer.running_tasks:
                kind = TASK_KINDS.get(self._tasks.get(task_id, ("", ""))[0], UNKNOWN_KIND)[0]
                counts[kind] = counts.get(kind, 0) + 1
        return " · ".join(f"{n} {kind}{'s' if n > 1 else ''}" for kind, n in counts.items())

    async def _show_running(self) -> None:
        self._show_thread_status()  # the thread's status line counts them too, once the turn ended
        if self._latest is not None:
            await self._latest.set_running(self._running_counts())

    async def _sink(self) -> ReplySink:
        """A new reply, which becomes this thread's latest and takes over the running list. It
        writes nothing until Claude has something to show."""
        sink = ReplySink(
            self._deps.reply_slack or self._deps.slack,
            channel=self.channel_id,
            thread_ts=self.thread_ts,
            team_id=self._deps.identity.team_id,
            user_id=self._deps.identity.owner_user_id,
            bot_user_id=self._deps.identity.bot_user_id,
            limiter=self._deps.update_limiter,
            clock=self._deps.stream_clock,
            on_open_reply=lambda old, new: self._deps.state.replace_open_reply(
                self.channel_id, self.thread_ts, old, new
            ),
            on_write=self._thread_status.wrote,
        )
        previous, self._latest = self._latest, sink
        await sink.set_running(self._running_counts())
        if previous is not None:
            await previous.set_running("")
            await previous.set_latest(False)
        return sink

    async def _hand_latest_to(self, sink: Sink) -> None:
        """Make `sink` the thread's latest reply again, taking over the footer and the running
        list, when the reply that was the latest will never be written."""
        if self._latest is sink or not isinstance(sink, ReplySink):
            return
        previous, self._latest = self._latest, sink
        await sink.set_running(self._running_counts())
        await sink.set_latest(True)
        if previous is not None:
            await previous.set_running("")
            await previous.set_latest(False)

    async def _finish(self, active: ActiveTurn, result: ResultMessage) -> bool:
        """Close the turn's reply and release it; True when `!stop` cut it short, which the
        caller reads once `_active` (still this turn, for `_close_reply`'s own checks) is clear,
        to skip D10's ✅, since `stop()` already showed its own."""
        # Default until the try below settles them; read in the `finally` even if something
        # above raises first.
        stopped = False
        # Claude Code ends a turn it took prompts into with one injected result for all of them.
        released = active.taken if injected_turn(result) else []
        try:
            if result.session_id:
                self._deps.state.set_session(self.channel_id, self.thread_ts, result.session_id)
            changed, effort = effort_change(result.result or "")
            if changed:
                self.effort, self.effort_reported = effort, True
                # D9: stored so the next connect can pass it back (the model set with `/model`
                # survives a resume by itself; effort does not). effort_change's own None means
                # "unknown" (a `/model` change with no effort in its output), not "back to
                # default": the stored override, if any, is left alone. "auto" is the CLI's own
                # word for the default (its EffortLevel has no such value): that IS an explicit
                # reset, stored as None.
                if effort is not None:
                    self._deps.state.set_effort(
                        self.channel_id, self.thread_ts, None if effort == "auto" else effort
                    )
            # Kept for `!status`, even when this result reports none (`/usage`, `/clear`): the two
            # lines never disagree.
            self.session_tokens = session_tokens(result)
            try:
                footer = await self._footer(self.session_tokens)
            except Exception as exc:  # the reply still ends, with no footer
                logger.warning(
                    "could not build the footer in %s/%s: %s",
                    self.channel_id,
                    self.thread_ts,
                    describe(exc),
                )
                footer = None
            stopped = result.terminal_reason in INTERRUPTED
            self._interrupting = False  # D10: this turn's own tail, whichever way it ended
            if active.turn is None and not injected_turn(result) and self._sent:
                # An owner query crossed a notification: this reply carries its answer, and
                # `_settle` discards the owner turn's own reply, which is the latest now. The
                # footer belongs under the answer.
                await self._hand_latest_to(active.renderer.sink)
            if released:
                self._end_notes.append(taken_note(len(released)))
            # What a restart dropped meanwhile is said where the running turn ends.
            await self._feed_end_notes(active.renderer)
            # `!stop` cut this turn short, the owner's own or a report's: its reply ends like any
            # other, the footer on its stream's stop, and never waits on a task it may still owe
            # (`force`: `!stop` never does).
            await self._close_reply(active.renderer, footer, force=stopped)
        finally:
            await self._settle(active.turn, result, released)
            # D1: a report turn's own reply is `_close_reply`'s
            # concern above; this catches every other reply a joint one left stranded.
            await self._sweep_closed_out()
            self._stop_tail_ended()
        return stopped

    async def _settle(self, turn: Turn | None, result: ResultMessage, released: list[Turn]) -> None:
        """Release whoever waits on this turn. The result's origin says whose turn it really was:
        when an owner query and a task notification cross, the guess made at the turn's start can
        be wrong; this puts the queue back in order (that one reply carries the other's text).
        `released`: prompts Claude Code took into this turn; no result of their own follows."""
        injected = injected_turn(result)
        for taken in released:
            if taken in self._sent:
                self._sent.remove(taken)
            taken.done.set()
        if turn is None and not injected and self._sent:
            logger.warning(
                "an owner reply in %s/%s went to a background reply",
                self.channel_id,
                self.thread_ts,
            )
            # D10: only misrouted, not failed, so no ❌. Its answer is already in the reply that
            # was guessed to be the report: the owner turn's own reply is never written.
            self._sent.popleft().done.set()
            self._expect_injected_turn()
        elif turn is not None and injected and turn not in released:
            logger.warning(
                "a background reply in %s/%s went to an owner reply",
                self.channel_id,
                self.thread_ts,
            )
            # That reply is spent: the owner's own turn gets a fresh one.
            turn.sink = await self._sink()
            self._sent.appendleft(turn)
            self._settled.set()
        elif turn is not None and not injected:
            turn.done.set()
        else:
            self._settled.set()

    async def _abandon(
        self,
        line: str,
        *,
        error: bool = False,
        more: list[Turn] | None = None,
        because: str = texts.BECAUSE_STOPPED,
    ) -> None:
        """The client is gone: end the reply that was open with `line`, and release whoever
        waits on it, on the turns sent and on `more` (queued ones, when the session closes). Those
        get no reply of their own: one note says they were not sent, `because` of what."""
        active, self._active = self._active, None
        sent, self._sent = list(self._sent), deque()
        dropped = [*sent, *(more or [])]
        waiting = ([active.turn] if active and active.turn else []) + dropped
        self._injected_expected = False
        self._interrupting = False  # D10: whatever it was waiting on, this ends it
        self._show_thread_status()
        if error:
            self._react_error()
        try:
            if active is not None:
                with contextlib.suppress(Exception):
                    await active.renderer.feed_error(line)
                    if dropped:
                        await self._hand_latest_to(active.renderer.sink)
                        await active.renderer.feed_error(not_sent(dropped, because))
                    await self._feed_end_notes(active.renderer)
                    await self._close_reply(active.renderer, None, force=True)
            elif dropped or self._end_notes:
                notes, self._end_notes = self._end_notes, []
                if dropped:
                    notes.append(not_sent(dropped, because))
                with contextlib.suppress(Exception):
                    await self._post(mrkdwn_escape("\n".join(notes)))
            await self._stop_task_replies()
        finally:
            for turn in waiting:
                turn.done.set()
            self._settled.set()
            self._stop_tail_ended()

    async def _footer(self, tokens: int | None) -> str | None:
        data = await self._footer_data(tokens)
        return format_footer(data, datetime.now().astimezone()) or None

    async def _repository(self, directory: Path) -> Repository | None:
        """The footer's git lookup: for the folder this session started in (`self.directory`)."""
        return await self._deps.trusted_repository(directory, self.directory)

    async def _footer_data(self, tokens: int | None) -> FooterData:
        """The footer's values from what the session knows now and `tokens`: the one source for
        both the footer and `!status`, so the two never disagree."""
        context: dict[str, Any] = {}
        if self._client is not None:
            try:
                context = dict(await self._client.get_context_usage())
            except Exception as exc:  # model and context are left out of the footer
                logger.warning(
                    "could not read the context usage in %s: %s", self.channel_id, describe(exc)
                )
        here = self.working_directory or self.directory
        branch, changes = await git_state(here, self._repository)
        return FooterData(
            bypass=self.bypass,
            branch=branch,
            model=context.get("model"),
            context_percent=context.get("percentage"),
            session_tokens=tokens,
            usage=self._deps.usage.current,
            effort=(self.effort or "default") if self.effort_reported else None,
            directory=self.directory,
            changes=changes,
        )

    async def _on_stop(
        self, hook_input: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        # `effort` is in the CLI's Stop input (Claude Code 2.1.280) though not in the SDK's
        # StopHookInput; absent when the model takes no effort parameter.
        effort = cast(dict[str, Any], hook_input).get("effort")
        self.effort = effort.get("level") if isinstance(effort, dict) else None
        self.effort_reported = True
        self._note_cwd(hook_input)
        return {}

    async def _on_tool_done(
        self, hook_input: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        self._note_cwd(hook_input)
        return {}

    def _note_cwd(self, hook_input: HookInput) -> None:
        cwd = hook_input.get("cwd")
        if cwd:
            self.working_directory = Path(cwd)

    async def _can_use_tool(
        self, tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResult:
        questions = tool_input.get("questions") if tool_name == QUESTION_TOOL else None
        title = context.title or task_title(tool_name, tool_input)
        approval_id, pending = self._deps.approvals.open(
            self.channel_id, self.thread_ts, title, questions
        )
        self._waiting.add(approval_id)
        self._idle_timer_check()  # an open approval or question holds the session (D9)
        self._react(Status.WAITING)
        blocks = (
            question_blocks(approval_id, questions)
            if questions
            else approval_blocks(approval_id, tool_name, tool_input, context)
        )
        try:
            try:
                posted = await self._deps.slack.chat_postMessage(
                    channel=self.channel_id,
                    thread_ts=self.thread_ts,
                    text=title,
                    blocks=blocks,
                    unfurl_links=False,
                    unfurl_media=False,
                )
            except Exception as exc:
                # Nobody can answer a request that was never shown: deny it, and say why.
                logger.error(
                    "could not post an approval request in %s/%s: %s",
                    self.channel_id,
                    self.thread_ts,
                    describe(exc),
                )
                return PermissionResultDeny(message=texts.APPROVAL_UNPOSTED)
            message_ts = str(posted["ts"])
            if self._deps.approvals.posted(approval_id, message_ts):
                # Crash repair (issue #19): still carrying buttons, until it is answered.
                # Best-effort (fix round item 7): the message is already live either way, so a
                # failed write here is logged loudly rather than left silently unrecorded.
                try:
                    self._deps.state.add_request(self.channel_id, self.thread_ts, message_ts)
                except Exception as exc:
                    logger.error(
                        "posted a request in %s/%s that state.json could not record: %s",
                        self.channel_id,
                        self.thread_ts,
                        describe(exc),
                    )
            else:
                await self._delete_request(message_ts)  # decided while it was posted
            decision = await pending.future
        finally:
            self._deps.approvals.discard(approval_id)
            self._waiting.discard(approval_id)
            self._idle_timer_check()  # may (re)start the idle-close timer (D9)
            if not self._interrupting:
                self._react_waiting_or_working()
        if questions and isinstance(decision, Answer):
            await self._keep_answers(context.tool_use_id, message_ts, questions, decision.answers)
        return to_permission(decision, tool_input, questions)

    async def _keep_answers(
        self,
        tool_use_id: str | None,
        message_ts: str,
        questions: list[dict[str, Any]],
        answers: dict[str, str | list[str]],
    ) -> None:
        """Where an answered question's answers stay. In the reply, under the line of the call,
        where the question was asked (issue #82): the request has done its job and goes. When
        the reply has no line of its own for that call, in the request itself, rewritten with no
        buttons, so the answers are never lost. The permission request names its call
        (`ToolPermissionContext.tool_use_id`, measured on Claude Code 2.1.286, 2026-10-03)."""
        active = self._active
        if (
            tool_use_id is not None
            and active is not None
            and active.renderer.answered(tool_use_id, questions, answers)
        ):
            await self._delete_request(message_ts)
            return
        try:
            # One more chat.update against the app's budget, which every reply draws from.
            await self._deps.update_limiter.acquire()
            await self._deps.slack.chat_update(
                channel=self.channel_id,
                ts=message_ts,
                text=texts.ANSWERED,
                blocks=answered_blocks(questions, answers),
            )
        except Exception as exc:
            # The request must not keep buttons that no longer work: remove it.
            logger.warning(
                "could not record an answer in %s/%s: %s",
                self.channel_id,
                self.thread_ts,
                describe(exc),
            )
            await self._delete_request(message_ts)
            return
        # Crash repair (issue #19): it carries no buttons now, so it leaves the tracked list.
        # Outside the try above: a failed write here must never delete a message just rewritten.
        try:
            self._deps.state.remove_request(self.channel_id, self.thread_ts, message_ts)
        except Exception as exc:
            logger.warning(
                "could not clear an answered request from state.json in %s/%s: %s",
                self.channel_id,
                self.thread_ts,
                describe(exc),
            )

    async def _fail(self, turn: Turn, text: str, *, error: bool = False) -> None:
        """End a turn's reply with a line saying why, and release whoever waits on it. `error`
        reacts D10's ❌."""
        try:
            await turn.sink.text(text, notice=True)
            await turn.sink.finish([])
            self._track_landing(await turn.sink.close_out(None), turn.sink)
            if error:
                self._react_error()
        finally:
            turn.done.set()
            self._show_thread_status()

    async def _post(self, text: str) -> None:
        """A notice of the daemon's own, small and grey as the footer."""
        try:
            await self._deps.slack.chat_postMessage(
                channel=self.channel_id,
                thread_ts=self.thread_ts,
                text=text,
                blocks=[context_block(notice_text(text))],
                unfurl_links=False,
                unfurl_media=False,
            )
            self._thread_status.wrote()  # a post of the app's clears the thread's status
        except Exception as exc:
            logger.error(
                "could not post in %s/%s: %s", self.channel_id, self.thread_ts, describe(exc)
            )

    async def _delete_request(self, message_ts: str | None) -> None:
        """Remove a decided request: the tool's line in the reply records what happened."""
        if message_ts is None:
            return
        await delete_request(self._deps.slack, channel=self.channel_id, ts=message_ts)
        try:
            # Crash repair (issue #19): the request is spoken for either way, so a startup
            # repair never retries a delete this call already made (or gave up on). Best-effort
            # (fix round item 7): a failed write here must never surface past this point.
            self._deps.state.remove_request(self.channel_id, self.thread_ts, message_ts)
        except Exception as exc:
            logger.warning(
                "could not clear a deleted request from state.json in %s/%s: %s",
                self.channel_id,
                self.thread_ts,
                describe(exc),
            )


class SessionManager:
    def __init__(self, deps: SessionDeps) -> None:
        self._deps = deps
        self._sessions: dict[tuple[str, str], ThreadSession] = {}
        # The threads being deleted (`release`): no session is handed out or built in one.
        self._held: set[tuple[str, str]] = set()
        self.draining = False

    @property
    def update_limiter(self) -> UpdateLimiter:
        """The chat.update budget every ReplySink in the process draws from, so a chat.update
        made outside a reply (slack_app.py's own edits) can share the same one."""
        return self._deps.update_limiter

    def open(self, channel_id: str, thread_ts: str) -> ThreadSession | None:
        """A top-level owner message: creates the thread's entry, in the channel's current
        folder, and its live session. None when the channel is unbound."""
        if self._deps.state.channel(channel_id) is None:
            return None
        thread = self._deps.state.open_thread(channel_id, thread_ts)
        return self._session(channel_id, thread_ts, thread.directory)

    async def repository(self, directory: Path, session_folder: Path) -> Repository | None:
        """The repository holding `directory` when the daemon's git may run in it for a session
        started in `session_folder`: the footer's own lookup, so what runs git for `!open` is what
        runs it for the footer."""
        return await self._deps.trusted_repository(directory, session_folder)

    def wrote(self, channel_id: str, thread_ts: str) -> None:
        """Something of the app's was posted in this thread outside its session (the answer to a
        word, a notice): the live session's status line, which that post cleared, is set again.
        Nothing for a thread with no live session: it shows no status."""
        session = self._sessions.get((channel_id, thread_ts))
        if session is not None:
            session.thread_written()

    def get(self, channel_id: str, thread_ts: str) -> ThreadSession | None:
        """The live session of a thread, or one rebuilt from its stored entry (after a restart,
        D9's idle close, or a resume whose session turned out gone); None when the thread is not
        a session. A closed session (`close()` has started: shutdown, an idle close, or the
        SessionGone branch of `ensure_connected`) is never handed back here: it is discarded and
        replaced, so a message never reaches a client that is going, or gone. The session handed
        back has just been touched (D9): its idle-close timer cannot fire before the caller's own
        next await, however slow (a download, a slow Slack call)."""
        thread = self._deps.state.thread(channel_id, thread_ts)
        if thread is None or (channel_id, thread_ts) in self._held:
            return None
        return self._session(channel_id, thread_ts, thread.directory)

    def _session(self, channel_id: str, thread_ts: str, directory: Path) -> ThreadSession:
        key = (channel_id, thread_ts)
        session = self._sessions.get(key)
        predecessor: asyncio.Event | None = None
        if session is not None and session.closed:
            # Discarded as soon as closing has started, not once it finishes: a message must
            # never reach an object mid-teardown (every operation on it raises SessionClosed).
            # The replacement waits on the old session's own `done_closing` before it connects,
            # so a rebuild never resumes the same id while the old process is still exiting.
            del self._sessions[key]
            predecessor = session.done_closing
            session = None
        if session is None:
            session = ThreadSession(
                channel_id, thread_ts, directory, self._deps, predecessor=predecessor
            )
            session.draining = self.draining
            session.on_closed = lambda: self._evict_if_current(key, session)
            self._sessions[key] = session
        # About to be handed to a caller: never let the idle timer close it out from under
        # whatever slow step (a download, a Slack call) the caller does before its own submit.
        session.touch()
        return session

    def _evict_if_current(self, key: tuple[str, str], session: ThreadSession) -> None:
        """Drop a fully-closed session from the live map, but only if nothing has already
        replaced it at this key (D9: keeps a long-idle thread's dead object from piling up)."""
        if self._sessions.get(key) is session:
            del self._sessions[key]

    def live_threads(self) -> set[tuple[str, str]]:
        """The (channel_id, thread_ts) of every thread with a session object that is not closed:
        what the cleanup of `state.json` must not remove under a running session."""
        return {key for key, session in self._sessions.items() if not session.closed}

    def sessions_of(self, channel_id: str) -> list[ThreadSession]:
        """The live sessions of a channel, across its threads; a closed one (D9's idle close, or
        a gone resume) is left out even before the next lookup evicts it."""
        return [s for (c, _), s in self._sessions.items() if c == channel_id and not s.closed]

    def restart_holds(self) -> list[tuple[ThreadSession, str]]:
        """While a stop waits: each thread that holds it, of any channel, with what it waits
        for there (`ThreadSession.restart_hold`). Empty when no stop is under way."""
        if not self.draining:
            return []
        held = ((s, s.restart_hold) for s in self._sessions.values() if not s.closed)
        return [(session, hold) for session, hold in held if hold]

    def working_in(self, *, besides: ThreadSession) -> ThreadSession | None:
        """D8: a live session of any channel, other than `besides`, whose folder resolves to the
        same one as `besides`'s own and is not idle (a background task counts as working, same
        as `idle` already treats it). Each session's own `resolved_directory` is cached once, at
        construction: comparing it avoids a `Path.resolve()` syscall per live session on every
        message. The first one found is enough: the question links to it."""
        resolved = besides.resolved_directory
        return next(
            (
                s
                for s in self._sessions.values()
                if s is not besides
                and not s.closed
                and not s.idle
                and s.resolved_directory == resolved
            ),
            None,
        )

    async def bind(self, channel_id: str, directory: Path) -> bool:
        """Bind the channel to `directory`, for the next thread it opens; False, and nothing
        changed, while any live session of the channel is not idle (D5). A thread already open
        keeps the folder it started in."""
        if any(not s.idle for s in self.sessions_of(channel_id)):
            return False
        self._deps.state.bind(channel_id, directory)
        return True

    async def release(self, channel_id: str, thread_ts: str) -> bool:
        """Close the thread's live session so the thread can be deleted, and wait for its
        teardown. False, with nothing closed, when the session is not idle or waits for the
        owner: the same test D9's idle close makes, so the close is as silent. True holds the
        thread until `free`: `get` answers None for it meanwhile and `held` says so, so a
        message sent in a thread that is being deleted is told it holds no session."""
        key = (channel_id, thread_ts)
        session = self._sessions.get(key)
        in_use = (
            session is not None
            and not session.closed
            and (session.waiting_for_owner or not session.idle)
        )
        if in_use:
            return False
        # Held before the first await: a message that arrives while the session closes, or
        # while the thread's messages are deleted, must not build a session in it.
        self._held.add(key)
        if session is not None:
            if not session.closed:
                await session.close(reason=texts.ENDED_IDLE)
            await session.done_closing.wait()
        return True

    def held(self, channel_id: str, thread_ts: str) -> bool:
        """Whether the thread is being deleted: it still has its entry, and takes no session."""
        return (channel_id, thread_ts) in self._held

    def free(self, channel_id: str, thread_ts: str) -> None:
        """End the hold `release` put on a thread: its delete ended, either way."""
        self._held.discard((channel_id, thread_ts))

    async def stop_channel(self, channel_id: str) -> bool | None:
        """`stop()` on every live session of the channel; True if any stopped something. None
        when nothing did except cancel a D8 hold: the caller adds no further notice of its own
        then, the same as `ThreadSession.stop`'s own `None`, since the held thread already got
        `Not sent.`."""
        stopped = False
        held_only = False
        for session in self.sessions_of(channel_id):
            result = await session.stop()
            if result:
                stopped = True
            elif result is None:
                held_only = True
        if stopped:
            return True
        return None if held_only else False

    async def resume(
        self, channel_id: str, thread_ts: str, session_id: str
    ) -> ThreadSession | None:
        """Phase 1 form of `!resume`: opens a thread at `thread_ts` (the `!resume` message's own
        ts) in the channel's folder, already on `session_id`. None when the channel is unbound."""
        if self._deps.state.channel(channel_id) is None:
            return None
        thread = self._deps.state.open_thread(channel_id, thread_ts, session_id=session_id)
        return self._session(channel_id, thread_ts, thread.directory)

    async def sessions_in(self, directory: Path, *, dated: bool = False) -> list[SDKSessionInfo]:
        """Every session of `directory`, the one the caller read for its channel; `dated`, by
        their last message, newest first, as the terminal's picker shows them."""
        found = await asyncio.to_thread(self._deps.sessions_of, directory)
        return await self.dated(directory, found) if dated else found

    async def dated(self, directory: Path, found: list[SDKSessionInfo]) -> list[SDKSessionInfo]:
        """`found`, sessions of `directory`, by their last message, newest first. Dating reads
        files, so a caller that shows only some of a folder's sessions passes those alone."""
        return await asyncio.to_thread(by_last_activity, directory, found)

    async def unavailable(self, directory: Path) -> DirectoryUnavailable | None:
        """What would keep a session from starting in `directory`, by the checks a start makes;
        None when nothing would."""
        try:
            await check_directory(directory, self._deps.workspace_trusted)
        except DirectoryUnavailable as exc:
            return exc
        return None

    async def folders_in(self, root: Path) -> list[Path]:
        """The folders under `root` where a session can start, as `bindable_folders` finds them."""
        return await bindable_folders(root, self._deps.workspace_trusted)

    async def drain(self, cut_short: asyncio.Event) -> None:
        """Let the turns already sent finish and send no other, then return: when every channel is
        idle, or when `cut_short` is set. Idle includes the background commands and agents, which
        die with the Claude Code process, and the turn Claude Code starts to report each one,
        except a background task started after the signal by a session whose turn was running
        when it came (`ThreadSession.restart_ready`, issue #87). Queued turns end at once, asking
        to be sent again. An approval or a question stays open: the Slack connection lives until
        the drain ends, so the owner can still answer it. A D8 hold does not: it asks about a
        session the restart is about to touch, so it is cancelled here exactly as `!stop` would
        (its own waiter tells the owner `Not sent.`)."""
        self.draining = True
        sessions = list(self._sessions.values())
        # Every flag before the first await: no worker sends a queued turn in between.
        for session in sessions:
            session.draining = True
            session.may_have_ordered_restart = session.busy
        for session in sessions:
            await session.cancel_hold()
            # D10: a queued turn genuinely dropped by the drain reacts ❌.
            await session.drop_queued(error=True)
        while not cut_short.is_set():
            if all(s.restart_ready and not s.reporting for s in self._sessions.values()):
                return
            for session in list(self._sessions.values()):
                with contextlib.suppress(Exception):  # a failed post must not end the wait
                    await session.show_restart_wait()
            # Polled: a turn ends in several places, and a stop needs no finer timing.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(cut_short.wait(), DRAIN_POLL_SECONDS)

    async def close_all(self) -> None:
        """Close every session and wait for each to be fully torn down, including one an idle
        close (D9) already had in flight: `close()` is safe to call again on a session already
        closing, but returning as soon as this call's own no-op finds the client already gone
        would not wait for whichever call is actually disconnecting it."""
        sessions = list(self._sessions.values())
        for session in sessions:
            if not session.closed:
                try:
                    await session.close()
                except Exception as exc:  # one session's failure must not skip the rest
                    logger.error(
                        "could not close %s/%s: %s",
                        session.channel_id,
                        session.thread_ts,
                        describe(exc),
                    )
        for session in sessions:
            await session.done_closing.wait()
        self._sessions.clear()
