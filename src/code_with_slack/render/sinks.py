"""Write a rendered reply to Slack: native streaming in the thread, or chat.update when the
workspace refuses the streaming API."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.models.messages.chunk import TaskUpdateChunk
from slack_sdk.web.async_chat_stream import AsyncChatStream
from slack_sdk.web.async_client import AsyncWebClient

from code_with_slack.render.renderer import Sink, TaskUpdate

logger = logging.getLogger(__name__)

APPEND_LIMIT = 12_000
BUFFER_SIZE = 256
# A buffered remainder plus one piece must stay under Slack's per-call limit.
PIECE = APPEND_LIMIT - BUFFER_SIZE
DEBOUNCE_SECONDS = 1.0
MARKDOWN_BLOCK_LIMIT = 12_000
ICONS = {"pending": "·", "in_progress": "…", "complete": "✓", "error": "✗"}


class StreamingSwitch:
    """Process-wide: once Slack refuses to start a stream, later replies use chat.update."""

    def __init__(self) -> None:
        self.enabled = True


def footer_blocks(footer: str) -> list[dict[str, Any]]:
    return [{"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]}]


def to_chunk(update: TaskUpdate) -> TaskUpdateChunk:
    return TaskUpdateChunk(
        id=update.id,
        title=update.title,
        status=update.status,
        details=update.details,
        output=update.output,
    )


class StreamSink:
    def __init__(
        self, slack: AsyncWebClient, *, channel: str, thread_ts: str, team_id: str, user_id: str
    ) -> None:
        self._slack = slack
        self._args = {
            "channel": channel,
            "thread_ts": thread_ts,
            "recipient_team_id": team_id,
            "recipient_user_id": user_id,
        }
        self._stream: AsyncChatStream | None = None

    @property
    def started(self) -> bool:
        return self._stream is not None and self._stream.ts is not None

    async def _open(self) -> AsyncChatStream:
        if self._stream is None:
            self._stream = await self._slack.chat_stream(
                buffer_size=BUFFER_SIZE, task_display_mode="timeline", **self._args
            )
        return self._stream

    async def text(self, markdown: str) -> None:
        stream = await self._open()
        for start in range(0, len(markdown), PIECE):
            await stream.append(markdown_text=markdown[start : start + PIECE])

    async def task(self, update: TaskUpdate) -> None:
        await (await self._open()).append(chunks=[to_chunk(update)])

    async def finish(self, closing: list[TaskUpdate], footer: str | None) -> None:
        stream = await self._open()
        await stream.stop(
            chunks=[to_chunk(u) for u in closing] or None,
            blocks=footer_blocks(footer) if footer else None,
        )


class UpdateSink:
    """One message in the thread, rewritten at most once per DEBOUNCE_SECONDS."""

    def __init__(self, slack: AsyncWebClient, *, channel: str, thread_ts: str) -> None:
        self._slack = slack
        self._channel = channel
        self._thread_ts = thread_ts
        self._text = ""
        self._cards: dict[str, TaskUpdate] = {}
        self._ts: str | None = None
        self._pending: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def text(self, markdown: str) -> None:
        self._text += markdown
        self._schedule()

    async def task(self, update: TaskUpdate) -> None:
        self._cards[update.id] = update
        self._schedule()

    async def finish(self, closing: list[TaskUpdate], footer: str | None) -> None:
        for update in closing:
            self._cards[update.id] = update
        if self._pending is not None:
            self._pending.cancel()
        await self._flush(footer)

    def _schedule(self) -> None:
        if self._pending is None or self._pending.done():
            self._pending = asyncio.create_task(self._later())

    async def _later(self) -> None:
        await asyncio.sleep(DEBOUNCE_SECONDS)
        try:
            await self._flush(None)
        except Exception as exc:  # the next flush retries with the whole reply
            logger.warning("could not update a reply: %s", type(exc).__name__)

    def _blocks(self, footer: str | None) -> list[dict[str, Any]]:
        body = self._text
        if len(body) > MARKDOWN_BLOCK_LIMIT:
            body = "…\n" + body[-(MARKDOWN_BLOCK_LIMIT - 2) :]
        blocks: list[dict[str, Any]] = [{"type": "markdown", "text": body}] if body else []
        if self._cards:
            lines = "\n".join(f"{ICONS[c.status]} {c.title}" for c in self._cards.values())
            blocks.append(
                {"type": "context", "elements": [{"type": "mrkdwn", "text": lines[:3000]}]}
            )
        if footer:
            blocks += footer_blocks(footer)
        return blocks

    async def _flush(self, footer: str | None) -> None:
        async with self._lock:
            blocks = self._blocks(footer)
            if not blocks:
                return
            fallback = self._text[:3000] or "…"
            if self._ts is None:
                posted = await self._slack.chat_postMessage(
                    channel=self._channel, thread_ts=self._thread_ts, text=fallback, blocks=blocks
                )
                self._ts = str(posted["ts"])
            else:
                await self._slack.chat_update(
                    channel=self._channel, ts=self._ts, text=fallback, blocks=blocks
                )


class ReplySink:
    """The sink a turn writes to. Streams while Slack allows it, and replays into UpdateSink when
    it does not, so the owner never loses a reply to a refused API call."""

    def __init__(
        self,
        slack: AsyncWebClient,
        switch: StreamingSwitch,
        *,
        channel: str,
        thread_ts: str,
        team_id: str,
        user_id: str,
    ) -> None:
        self._switch = switch
        self._stream: StreamSink | None = (
            StreamSink(
                slack, channel=channel, thread_ts=thread_ts, team_id=team_id, user_id=user_id
            )
            if switch.enabled
            else None
        )
        self._update = UpdateSink(slack, channel=channel, thread_ts=thread_ts)
        self._log: list[Callable[[Sink], Awaitable[None]]] = []
        self._lost = False

    async def text(self, markdown: str) -> None:
        await self._do(lambda sink: sink.text(markdown))

    async def task(self, update: TaskUpdate) -> None:
        await self._do(lambda sink: sink.task(update))

    async def finish(self, closing: list[TaskUpdate], footer: str | None) -> None:
        await self._do(lambda sink: sink.finish(closing, footer))

    async def _do(self, op: Callable[[Sink], Awaitable[None]]) -> None:
        """Never raises: a reply Slack cannot take is lost, the Claude Code session goes on."""
        self._log.append(op)
        if self._lost:
            return
        pending = [op]
        if self._stream is not None:
            try:
                await op(self._stream)
                return
            except Exception as exc:
                self._stream_failed(exc)
                pending = list(self._log)
        try:
            for logged in pending:
                await logged(self._update)
        except Exception as exc:
            logger.warning("could not write a reply to Slack: %s", describe(exc))
            self._lost = True

    def _stream_failed(self, exc: Exception) -> None:
        assert self._stream is not None
        if isinstance(exc, SlackApiError) and not self._stream.started:
            logger.warning(
                "Slack refused streaming (%s); replies use chat.update from now on", describe(exc)
            )
            self._switch.enabled = False
        else:
            logger.warning(
                "streaming failed mid-reply (%s); this reply continues as a new message",
                describe(exc),
            )
        self._stream = None


def describe(exc: Exception) -> str:
    """Slack's error code, or the exception type: never the request or its content."""
    if isinstance(exc, SlackApiError):
        return str(exc.response.get("error"))
    return type(exc).__name__
