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
    outcome_blocks,
    question_blocks,
    to_permission,
)
from code_with_slack.footer import (
    FooterData,
    UsageCache,
    format_footer,
    git_branch,
    session_tokens,
)
from code_with_slack.guards import Identity
from code_with_slack.render.renderer import TurnRenderer, task_title
from code_with_slack.render.sinks import ReplySink, StreamingSwitch, describe
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
    streaming: StreamingSwitch
    client_factory: ClientFactory = default_client_factory


@dataclass
class Turn:
    prompt: str
    thread_ts: str
    done: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class ActiveTurn:
    turn: Turn | None
    thread_ts: str
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

    @property
    def busy(self) -> bool:
        return self._active is not None or bool(self._sent)

    async def submit(self, prompt: str, thread_ts: str) -> Turn:
        turn = Turn(prompt, thread_ts)
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
            await self._mark_outcome(pending.message_ts, texts.DENIED.format(title=pending.title))
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
                await self._post(turn.thread_ts, exc.message)
                turn.done.set()
            except Exception as exc:  # a failed turn must not stop the channel's queue
                logger.error("turn failed in %s: %s", self.channel_id, type(exc).__name__)
                if turn in self._sent:
                    self._sent.remove(turn)
                await self._post(turn.thread_ts, texts.ERROR_REPLY.format(error=type(exc).__name__))
                turn.done.set()

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
        if self._active is None:
            if isinstance(message, TASK_MESSAGES):
                self._held.append(message)
                if isinstance(message, TaskNotificationMessage):
                    self._expect_injected_turn()
                return
            if not isinstance(message, TURN_MESSAGES):
                return
            self._active = await self._start_turn()
        active = self._active
        await active.renderer.feed(message)
        if isinstance(message, ResultMessage):
            self._active = None
            await self._finish(active, message)

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
        thread_ts = turn.thread_ts if turn else await self._root(texts.BACKGROUND_ROOT)
        renderer = TurnRenderer(self._sink(thread_ts))
        if self._notice is not None:
            await renderer.feed_notice(self._notice)
            self._notice = None
        held, self._held = self._held, []
        for message in held:
            await renderer.feed(message)
        return ActiveTurn(turn, thread_ts, renderer)

    async def _standalone(self, messages: list[Message]) -> None:
        if not messages:
            return
        renderer = TurnRenderer(self._sink(await self._root(texts.BACKGROUND_ROOT)))
        for message in messages:
            await renderer.feed(message)
        await renderer.close(None)

    def _sink(self, thread_ts: str) -> ReplySink:
        return ReplySink(
            self._deps.slack,
            self._deps.streaming,
            channel=self.channel_id,
            thread_ts=thread_ts,
            team_id=self._deps.identity.team_id,
            user_id=self._deps.identity.owner_user_id,
        )

    async def _finish(self, active: ActiveTurn, result: ResultMessage) -> None:
        try:
            if result.session_id:
                self._deps.state.set_session(self.channel_id, result.session_id)
            await active.renderer.close(await self._footer(result))
        finally:
            self._settle(active.turn, result)

    def _settle(self, turn: Turn | None, result: ResultMessage) -> None:
        """Release whoever waits on this turn. The result's origin says whose turn it really was:
        when an owner query and a task notification cross, the guess made at the turn's start can
        be wrong, and this puts the queue back in order (that one reply is in the wrong thread)."""
        kind = result.origin.get("kind") if result.origin else None
        injected = kind not in (None, "human")
        if turn is None and not injected and self._sent:
            logger.warning("an owner reply in %s went to a background thread", self.channel_id)
            self._sent.popleft().done.set()
            self._expect_injected_turn()
        elif turn is not None and injected:
            logger.warning("a background reply in %s went to an owner thread", self.channel_id)
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
                    await active.renderer.close(None)
            for turn in sent:
                await self._post(turn.thread_ts, texts.ERROR_REPLY.format(error=error))
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
        thread_ts = (
            self._active.thread_ts
            if self._active
            else (self._sent[0].thread_ts if self._sent else None)
        )
        try:
            posted = await self._deps.slack.chat_postMessage(
                channel=self.channel_id, thread_ts=thread_ts, text=title, blocks=blocks
            )
            pending.message_ts = str(posted["ts"])
            decision = await pending.future
        finally:
            self._deps.approvals.discard(approval_id)
        return to_permission(decision, tool_input, questions)

    async def _root(self, text: str) -> str:
        posted = await self._deps.slack.chat_postMessage(channel=self.channel_id, text=text)
        return str(posted["ts"])

    async def _post(self, thread_ts: str, text: str) -> None:
        try:
            await self._deps.slack.chat_postMessage(
                channel=self.channel_id, thread_ts=thread_ts, text=text
            )
        except Exception as exc:
            logger.error("could not post in %s: %s", self.channel_id, describe(exc))

    async def _mark_outcome(self, message_ts: str | None, text: str) -> None:
        if message_ts is None:
            return
        try:
            await self._deps.slack.chat_update(
                channel=self.channel_id, ts=message_ts, text=text, blocks=outcome_blocks(text)
            )
        except Exception as exc:
            logger.error("could not update an approval in %s: %s", self.channel_id, describe(exc))


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
