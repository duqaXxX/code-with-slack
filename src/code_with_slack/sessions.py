"""One Claude Code session per bound channel: its client, its queue of turns, and the reader
that turns the SDK's message stream into replies."""

import asyncio
import contextlib
import logging
import os
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    Message,
    ResultError,
    ResultMessage,
    StreamEvent,
    UserMessage,
)
from claude_agent_sdk.types import (
    CanUseTool,
    PermissionMode,
    PermissionResult,
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
    Approvals,
    approval_blocks,
    question_blocks,
    to_permission,
)
from code_with_slack.footer import (
    FooterData,
    UsageCache,
    effort_change,
    effort_from_settings,
    format_footer,
    git_branch,
    session_tokens,
)
from code_with_slack.guards import Identity
from code_with_slack.render.renderer import TurnRenderer, task_title
from code_with_slack.render.sinks import ReplySink, describe
from code_with_slack.state import StateStore

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
# Tasks whose reply the session remembers. An ended task stays a while, since an agent can
# report again after its notification; past this many, the oldest ended ones are forgotten.
TASK_REPLIES_KEPT = 100


class DirectoryUnavailable(Exception):
    """The channel's directory cannot be used; `message` tells the owner what to do."""

    def __init__(self, directory: Path, message: str) -> None:
        super().__init__(message)
        self.directory = directory
        self.message = message


class DirectoryMissing(DirectoryUnavailable):
    def __init__(self, directory: Path) -> None:
        super().__init__(directory, texts.DIRECTORY_MISSING.format(directory=directory))


class DirectoryUnreadable(DirectoryUnavailable):
    """macOS privacy protection (TCC) denies the daemon a folder such as ~/Documents: a process
    started by launchd does not inherit the Terminal's permission, and the CLI fails to start."""

    def __init__(self, directory: Path) -> None:
        super().__init__(directory, texts.DIRECTORY_UNREADABLE.format(directory=directory))


class ClaudeClient(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def query(self, prompt: str) -> None: ...
    def receive_messages(self) -> AsyncIterator[Message]: ...
    async def set_permission_mode(self, mode: PermissionMode) -> None: ...
    async def interrupt(self) -> None: ...
    async def get_server_info(self) -> dict[str, Any] | None: ...
    async def get_context_usage(self) -> Any: ...


ClientFactory = Callable[[ClaudeAgentOptions], ClaudeClient]


def default_client_factory(options: ClaudeAgentOptions) -> ClaudeClient:
    return ClaudeSDKClient(options)


def client_options(
    directory: Path, session_id: str | None, can_use_tool: CanUseTool
) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        cwd=str(directory),
        resume=session_id,
        setting_sources=["user", "project", "local"],
        include_partial_messages=True,
        can_use_tool=can_use_tool,
        # Makes bypass possible, not active: `/cc bypass on` switches it on the live client.
        extra_args={"allow-dangerously-skip-permissions": None},
        # CLI stderr may quote the conversation: keep it out of the log unless debugging.
        stderr=lambda line: logger.debug("claude stderr: %d chars", len(line)),
    )


def resolve_directory(raw: str, root: Path) -> Path | None:
    """The real directory `raw` names, if it exists under `root`; guards against a typo only."""
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        return None
    return path if path == root or root in path.parents else None


@dataclass
class SessionDeps:
    slack: AsyncWebClient
    identity: Identity
    state: StateStore
    approvals: Approvals
    usage: UsageCache
    client_factory: ClientFactory = default_client_factory


@dataclass
class Turn:
    prompt: str
    sink: ReplySink
    done: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class ActiveTurn:
    turn: Turn | None
    renderer: TurnRenderer


class ChannelSession:
    def __init__(self, channel_id: str, directory: Path, deps: SessionDeps) -> None:
        self.channel_id = channel_id
        self.directory = directory
        self._deps = deps
        self._client: ClaudeClient | None = None
        self._reader: asyncio.Task[None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[Turn] = asyncio.Queue()
        self._sent: deque[Turn] = deque()
        self._active: ActiveTurn | None = None
        self._connect_lock = asyncio.Lock()
        self._notice: str | None = None
        self._background: set[asyncio.Task[None]] = set()
        # Task messages that arrived between turns, shown in the next turn's reply.
        self._held: list[Message] = []
        # Tasks that outlived their turn, and the reply whose line each one keeps up to date,
        # for as long as the Claude Code process lives: an agent can report more than once.
        self._task_replies: dict[str, TurnRenderer] = {}
        # The channel's newest reply: the only one that shows what is still running.
        self._latest: ReplySink | None = None
        # Clear while a background notification's own turn is expected or running: the owner's
        # next query waits, so the two replies never share a thread.
        self._settled = asyncio.Event()
        self._settled.set()
        self._injected_expected = False
        self._expiry: asyncio.Task[None] | None = None
        self.commands: list[dict[str, Any]] = []
        self.native_mode = "default"
        self.cli_version: str | None = None
        self.bypass = False
        # The effort level set in this session, as its command output reported it (the SDK
        # reports none); None falls back to the settings.
        self.effort: str | None = None

    @property
    def busy(self) -> bool:
        return self._active is not None or bool(self._sent)

    async def submit(self, prompt: str) -> Turn:
        """Queue a prompt; its reply appears at once, saying Claude is writing or waiting."""
        waiting = self.busy or not self._queue.empty() or not self._settled.is_set()
        sink = await self._sink()
        await sink.open(texts.WAITING if waiting else texts.WRITING)
        turn = Turn(prompt, sink)
        self._queue.put_nowait(turn)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work(), name=f"worker-{self.channel_id}")
        return turn

    async def ensure_connected(self) -> ClaudeClient:
        async with self._connect_lock:
            if self._client is not None:
                return self._client
            if not self.directory.is_dir():
                raise DirectoryMissing(self.directory)
            try:
                with os.scandir(self.directory):
                    pass
            except PermissionError:
                raise DirectoryUnreadable(self.directory) from None
            stored = self._deps.state.get(self.channel_id)
            session_id = stored.session_id if stored else None
            try:
                client = await self._connect(session_id)
            except ResultError as exc:
                if session_id is None:
                    raise
                # The stored session is gone (its transcript was deleted): start fresh.
                logger.warning("could not resume the stored session in %s", self.channel_id)
                self._deps.state.set_session(self.channel_id, None)
                reason = exc.errors[0] if exc.errors else (exc.subtype or "unknown error")
                self._notice = texts.STALE_SESSION.format(error=reason)
                client = await self._connect(None)
            info = await client.get_server_info() or {}
            self.commands = list(info.get("commands") or [])
            self.native_mode = str(info.get("current_permission_mode") or "default")
            if self.bypass:
                await client.set_permission_mode("bypassPermissions")
            self._client = client
            self.effort = None  # a new Claude Code process starts from the settings' level
            self._reader = asyncio.create_task(self._read(client), name=f"reader-{self.channel_id}")
            return client

    async def set_bypass(self, on: bool) -> None:
        client = await self.ensure_connected()
        mode = "bypassPermissions" if on else self.native_mode
        await client.set_permission_mode(mode)  # type: ignore[arg-type]
        self.bypass = on

    async def stop(self) -> bool:
        """Interrupt the running turn and deny its pending approvals; queued turns stay queued."""
        if self._client is None or not self.busy:
            return False
        for pending in self._deps.approvals.deny_all(self.channel_id):
            await self._delete_request(pending.message_ts)
        await self._client.interrupt()
        return True

    def status(self) -> str:
        stored = self._deps.state.get(self.channel_id)
        activity = (
            texts.ACTIVITY_BUSY.format(queued=self._queue.qsize())
            if self.busy
            else texts.ACTIVITY_IDLE
        )
        return texts.STATUS.format(
            directory=self.directory,
            session=(stored.session_id if stored else None) or "new",
            mode="bypassPermissions" if self.bypass else self.native_mode,
            version=self.cli_version or "not started",
            activity=activity,
        )

    async def close(self) -> None:
        for task in (self._worker, self._reader, self._expiry):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._deps.approvals.deny_all(self.channel_id)
        await self._stop_task_replies()
        if self._client is not None:
            client, self._client = self._client, None
            with contextlib.suppress(Exception):
                await client.disconnect()

    async def _connect(self, session_id: str | None) -> ClaudeClient:
        options = client_options(self.directory, session_id, self._can_use_tool)
        client = self._deps.client_factory(options)
        await client.connect()
        return client

    async def _work(self) -> None:
        while True:
            turn = await self._queue.get()
            try:
                client = await self.ensure_connected()
                refresh = asyncio.create_task(self._deps.usage.refresh_if_stale())
                self._background.add(refresh)
                refresh.add_done_callback(self._background.discard)
                await self._settled.wait()
                self._sent.append(turn)
                await client.query(turn.prompt)
                await turn.done.wait()
            except DirectoryUnavailable as exc:
                await self._fail(turn, exc.message)
            except Exception as exc:  # a failed turn must not stop the channel's queue
                logger.error("turn failed in %s: %s", self.channel_id, type(exc).__name__)
                if turn in self._sent:
                    self._sent.remove(turn)
                await self._fail(turn, texts.ERROR_REPLY.format(error=type(exc).__name__))

    async def _read(self, client: ClaudeClient) -> None:
        """Follow the client's stream until it ends; whatever ends it, release the channel."""
        reason = "the Claude Code process exited"
        try:
            async for message in client.receive_messages():
                try:
                    await self._dispatch(message)
                except Exception as exc:  # one message that fails to render must not end it all
                    logger.error(
                        "could not render a message in %s: %s", self.channel_id, describe(exc)
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = type(exc).__name__
        logger.error("session reader stopped in %s: %s", self.channel_id, reason)
        if self._client is client:
            self._client = None
        try:
            await self._abandon(reason)
        finally:
            with contextlib.suppress(Exception):
                await client.disconnect()

    async def _dispatch(self, message: Message) -> None:
        if isinstance(message, RateLimitEvent):
            self._deps.usage.invalidate()
            return
        if isinstance(message, SystemMessage) and message.subtype == "init":
            self.cli_version = message.data.get("claude_code_version")
        if isinstance(message, TASK_MESSAGES) and message.task_id in self._task_replies:
            # A task that outlived its turn shows only on its own line, wherever it started.
            await self._task_replies[message.task_id].feed(message)
            await self._show_running()
            if self._active is None and isinstance(message, TaskNotificationMessage):
                self._notified()
            return
        parent = getattr(message, "parent_tool_use_id", None)
        origin = self._origin_of(parent) if parent else None
        if origin is not None:
            # A background subagent at work after its turn: its calls belong under its line.
            await origin.feed(message)
            return
        if self._active is None:
            if isinstance(message, TASK_MESSAGES):
                self._held.append(message)
                if isinstance(message, TaskNotificationMessage):
                    self._notified()
                return
            if not isinstance(message, TURN_MESSAGES):
                return
            self._active = await self._start_turn()
        active = self._active
        await active.renderer.feed(message)
        if isinstance(message, ResultMessage):
            self._active = None
            await self._finish(active, message)

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
        logger.warning("no turn followed a task notification in %s", self.channel_id)
        self._injected_expected = False
        held, self._held = self._held, []
        try:
            await self._standalone(held)
        except Exception as exc:
            logger.warning(
                "could not post a background update in %s: %s", self.channel_id, describe(exc)
            )
        finally:
            self._settled.set()

    async def _start_turn(self) -> ActiveTurn:
        injected = self._injected_expected or not self._sent
        self._injected_expected = False
        if self._expiry is not None:
            self._expiry.cancel()
        turn = None if injected else self._sent.popleft()
        if turn is None:
            renderer = TurnRenderer(await self._sink())
            await renderer.feed_notice(texts.BACKGROUND_NOTICE)
        else:
            renderer = TurnRenderer(turn.sink)
            await turn.sink.announce(texts.WRITING)
        if self._notice is not None:
            await renderer.feed_notice(self._notice)
            self._notice = None
        held, self._held = self._held, []
        for message in held:
            await renderer.feed(message)
        return ActiveTurn(turn, renderer)

    async def _standalone(self, messages: list[Message]) -> None:
        if not messages:
            return
        renderer = TurnRenderer(await self._sink())
        await renderer.feed_notice(texts.BACKGROUND_NOTICE)
        for message in messages:
            await renderer.feed(message)
        await self._close_reply(renderer, None)

    async def _close_reply(self, renderer: TurnRenderer, footer: str | None) -> None:
        for task_id in renderer.running_tasks:
            self._task_replies[task_id] = renderer
        ended = [t for t, r in self._task_replies.items() if t not in r.running_tasks]
        for task_id in ended[: max(0, len(self._task_replies) - TASK_REPLIES_KEPT)]:
            del self._task_replies[task_id]
        # Before the close, so the reply's last write already carries the list.
        await self._show_running()
        await renderer.close(footer)

    async def _stop_task_replies(self) -> None:
        """The Claude Code process is going away with its tasks: no reply keeps showing one."""
        renderers = set(self._task_replies.values())
        self._task_replies.clear()
        for renderer in renderers:
            with contextlib.suppress(Exception):
                await renderer.stop_running()
        await self._show_running()

    def _origin_of(self, tool_use_id: str) -> TurnRenderer | None:
        return next((r for r in self._task_replies.values() if r.owns(tool_use_id)), None)

    def _running_lines(self) -> list[str]:
        titles = (r.running_title(task_id) for task_id, r in self._task_replies.items())
        return [f"… `{title}`" for title in titles if title is not None]

    async def _show_running(self) -> None:
        if self._latest is not None:
            await self._latest.set_running(self._running_lines())

    async def _sink(self) -> ReplySink:
        """A new reply, which becomes the channel's latest and takes over the running list."""
        sink = ReplySink(self._deps.slack, channel=self.channel_id)
        previous, self._latest = self._latest, sink
        await sink.set_running(self._running_lines())
        if previous is not None:
            await previous.set_running([])
        return sink

    async def _finish(self, active: ActiveTurn, result: ResultMessage) -> None:
        try:
            if result.session_id:
                self._deps.state.set_session(self.channel_id, result.session_id)
            changed, effort = effort_change(result.result or "")
            if changed:
                self.effort = effort
            await self._close_reply(active.renderer, await self._footer(result))
        finally:
            await self._settle(active.turn, result)

    async def _settle(self, turn: Turn | None, result: ResultMessage) -> None:
        """Release whoever waits on this turn. The result's origin says whose turn it really was:
        when an owner query and a task notification cross, the guess made at the turn's start can
        be wrong; this puts the queue back in order (that one reply carries the other's text)."""
        kind = result.origin.get("kind") if result.origin else None
        injected = kind not in (None, "human")
        if turn is None and not injected and self._sent:
            logger.warning("an owner reply in %s went to a background reply", self.channel_id)
            owner = self._sent.popleft()
            await self._fail(owner, texts.REPLY_ABOVE)
            self._expect_injected_turn()
        elif turn is not None and injected:
            logger.warning("a background reply in %s went to an owner reply", self.channel_id)
            # That reply is spent: the owner's own turn gets a fresh one.
            turn.sink = await self._sink()
            await turn.sink.open(texts.WRITING)
            self._sent.appendleft(turn)
            self._settled.set()
        elif turn is not None:
            turn.done.set()
        else:
            self._settled.set()

    async def _abandon(self, error: str) -> None:
        """The client is gone: end the reply that was open and release every waiting turn."""
        active, self._active = self._active, None
        sent, self._sent = list(self._sent), deque()
        waiting = ([active.turn] if active and active.turn else []) + sent
        self._injected_expected = False
        try:
            if active is not None:
                with contextlib.suppress(Exception):
                    await active.renderer.feed_error(texts.ERROR_REPLY.format(error=error))
                    await self._close_reply(active.renderer, None)
            await self._stop_task_replies()
            for turn in sent:
                await self._fail(turn, texts.ERROR_REPLY.format(error=error))
        finally:
            for turn in waiting:
                turn.done.set()
            self._settled.set()

    async def _footer(self, result: ResultMessage) -> str | None:
        context: dict[str, Any] = {}
        if self._client is not None:
            with contextlib.suppress(Exception):
                context = dict(await self._client.get_context_usage())
        data = FooterData(
            bypass=self.bypass,
            branch=await git_branch(self.directory),
            model=context.get("model"),
            context_percent=context.get("percentage"),
            session_tokens=session_tokens(result),
            usage=self._deps.usage.current,
            effort=self.effort or effort_from_settings(self.directory) or "default",
        )
        return format_footer(data, datetime.now().astimezone()) or None

    async def _can_use_tool(
        self, tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResult:
        questions = tool_input.get("questions") if tool_name == QUESTION_TOOL else None
        title = context.title or task_title(tool_name, tool_input)
        approval_id, pending = self._deps.approvals.open(self.channel_id, title, questions)
        blocks = (
            question_blocks(approval_id, questions)
            if questions
            else approval_blocks(approval_id, tool_name, tool_input, context)
        )
        try:
            posted = await self._deps.slack.chat_postMessage(
                channel=self.channel_id, text=title, blocks=blocks
            )
            pending.message_ts = str(posted["ts"])
            decision = await pending.future
        finally:
            self._deps.approvals.discard(approval_id)
        return to_permission(decision, tool_input, questions)

    async def _fail(self, turn: Turn, text: str) -> None:
        """End a turn's reply with a line saying why, and release whoever waits on it."""
        try:
            await turn.sink.text(text)
            await turn.sink.finish([], None)
        finally:
            turn.done.set()

    async def _post(self, text: str) -> None:
        try:
            await self._deps.slack.chat_postMessage(channel=self.channel_id, text=text)
        except Exception as exc:
            logger.error("could not post in %s: %s", self.channel_id, describe(exc))

    async def _delete_request(self, message_ts: str | None) -> None:
        """Remove a decided request: the tool's line in the reply records what happened."""
        if message_ts is None:
            return
        try:
            await self._deps.slack.chat_delete(channel=self.channel_id, ts=message_ts)
        except Exception as exc:
            logger.error("could not remove a request in %s: %s", self.channel_id, describe(exc))


class SessionManager:
    def __init__(self, deps: SessionDeps) -> None:
        self._deps = deps
        self._sessions: dict[str, ChannelSession] = {}

    def get(self, channel_id: str) -> ChannelSession | None:
        stored = self._deps.state.get(channel_id)
        if stored is None:
            return None
        session = self._sessions.get(channel_id)
        if session is None or session.directory != stored.directory:
            session = ChannelSession(channel_id, stored.directory, self._deps)
            self._sessions[channel_id] = session
        return session

    async def bind(self, channel_id: str, directory: Path) -> None:
        """Claude Code keeps sessions per directory, so a new directory means a new session."""
        old = self._sessions.pop(channel_id, None)
        if old is not None:
            await old.close()
        self._deps.state.bind(channel_id, directory)

    async def close_all(self) -> None:
        for session in list(self._sessions.values()):
            await session.close()
        self._sessions.clear()
