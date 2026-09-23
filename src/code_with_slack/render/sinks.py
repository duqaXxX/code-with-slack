"""Write a rendered reply to Slack: one message in the channel's main window, rewritten with
chat.update as the reply grows.

Slack's native streaming works only inside a thread in an ordinary channel (`chat.startStream`
without `thread_ts` answers `invalid_thread_ts`, measured 2026-09-23), so the reply is rewritten
whole instead, at most once per DEBOUNCE_SECONDS: chat.update allows "50+ per minute" (Tier 3).
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from code_with_slack.render.renderer import TaskUpdate

logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 1.0
# A markdown block holds at most 12,000 characters; the margin keeps a tool line that grows in
# place from pushing a full message over the limit.
MESSAGE_LIMIT = 11_000
FALLBACK_LIMIT = 3_000
WRITING = "_Claude is writing…_"
ICONS = {"pending": "·", "in_progress": "…", "complete": "✓", "error": "✗"}


def describe(exc: Exception) -> str:
    """Slack's error code, or the exception type: never the request or its content."""
    if isinstance(exc, SlackApiError):
        return str(exc.response.get("error"))
    return type(exc).__name__


def context_block(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


@dataclass
class _Text:
    text: str


@dataclass
class _Tool:
    update: TaskUpdate

    def line(self) -> str:
        update = self.update
        line = f"{ICONS[update.status]} `{update.title}`"
        if update.status == "error" and update.output:
            line += f" · {update.output}"
        elif update.status == "in_progress" and update.details:
            line += f" · {update.details.splitlines()[-1]}"
        return line


def split(body: str) -> list[str]:
    """Cut a body into messages of at most MESSAGE_LIMIT characters, at line breaks if possible."""
    chunks: list[str] = []
    while len(body) > MESSAGE_LIMIT:
        cut = body.rfind("\n", 0, MESSAGE_LIMIT)
        if cut <= 0:
            chunks.append(body[:MESSAGE_LIMIT])
            body = body[MESSAGE_LIMIT:]
        else:
            chunks.append(body[:cut])
            body = body[cut + 1 :]
    chunks.append(body)
    return chunks


class ReplySink:
    """One reply in the channel, written in the order things happen: text, then a line per tool
    where it ran, updated in place. The last line says Claude is writing until the footer replaces
    it. A reply past MESSAGE_LIMIT continues in a new message. Never raises: a write Slack refuses
    is retried with the whole reply at the next flush, and the session goes on."""

    def __init__(self, slack: AsyncWebClient, *, channel: str) -> None:
        self._slack = slack
        self._channel = channel
        self._parts: list[_Text | _Tool] = []
        self._tools: dict[str, _Tool] = {}
        self._messages: list[str] = []  # ts of each message this reply has posted
        self._shown: list[list[dict[str, Any]]] = []  # the blocks each message shows now
        self._pending: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def text(self, markdown: str) -> None:
        if self._parts and isinstance(self._parts[-1], _Text):
            self._parts[-1].text += markdown
        else:
            self._parts.append(_Text(markdown))
        self._schedule()

    async def task(self, update: TaskUpdate) -> None:
        tool = self._tools.get(update.id)
        if tool is None:
            tool = self._tools[update.id] = _Tool(update)
            self._parts.append(tool)
        else:
            tool.update = update
        self._schedule()

    async def finish(self, closing: list[TaskUpdate], footer: str | None) -> None:
        for update in closing:
            await self.task(update)
        if self._pending is not None:
            self._pending.cancel()
        await self._flush(final=True, footer=footer)

    def _schedule(self) -> None:
        if self._pending is None or self._pending.done():
            self._pending = asyncio.create_task(self._later())

    async def _later(self) -> None:
        await asyncio.sleep(DEBOUNCE_SECONDS)
        await self._flush(final=False, footer=None)

    def _body(self) -> str:
        body = ""
        for part in self._parts:
            if isinstance(part, _Text):
                body += part.text
            else:
                if body and not body.endswith("\n"):
                    body += "\n"
                body += part.line() + "\n"
        return body.rstrip("\n")

    def _render(self, final: bool, footer: str | None) -> list[list[dict[str, Any]]]:
        chunks = split(self._body())
        messages: list[list[dict[str, Any]]] = [
            [{"type": "markdown", "text": chunk}] if chunk else [] for chunk in chunks
        ]
        tail = footer if final else WRITING
        if tail:
            messages[-1].append(context_block(tail))
        return messages

    async def _flush(self, *, final: bool, footer: str | None) -> None:
        async with self._lock:
            for index, blocks in enumerate(self._render(final, footer)):
                if not blocks:
                    continue
                if index < len(self._shown) and self._shown[index] == blocks:
                    continue
                markdown = next((b["text"] for b in blocks if b["type"] == "markdown"), "")
                fallback = markdown[:FALLBACK_LIMIT] or "…"
                try:
                    if index < len(self._messages):
                        await self._slack.chat_update(
                            channel=self._channel,
                            ts=self._messages[index],
                            text=fallback,
                            blocks=blocks,
                        )
                        self._shown[index] = blocks
                    else:
                        posted = await self._slack.chat_postMessage(
                            channel=self._channel, text=fallback, blocks=blocks
                        )
                        self._messages.append(str(posted["ts"]))
                        self._shown.append(blocks)
                except Exception as exc:
                    logger.warning("could not write a reply to Slack: %s", describe(exc))
                    return
