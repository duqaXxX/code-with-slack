"""Write a rendered reply to Slack: one message in the channel's main window, rewritten with
chat.update as the reply grows.

Slack's native streaming works only inside a thread in an ordinary channel (`chat.startStream`
without `thread_ts` answers `invalid_thread_ts`, measured 2026-09-23), so the reply is rewritten
whole instead, at most once per DEBOUNCE_SECONDS: chat.update allows "50+ per minute" (Tier 3).
"""

import asyncio
import itertools
import logging
from dataclasses import dataclass
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from code_with_slack import texts
from code_with_slack.render.renderer import STOPPED, TaskUpdate

logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 1.0
# A markdown block holds at most 12,000 characters; the margin keeps a tool line that grows in
# place from pushing a full message over the limit.
MESSAGE_LIMIT = 11_000
FALLBACK_LIMIT = 3_000
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
        if (update.status == "error" and update.output) or update.output == STOPPED:
            line += f" · {update.output}"
        elif update.status == "in_progress" and update.details:
            line += f" · {update.details.splitlines()[-1]}"
        return line


def tool_lines(tools: list[_Tool]) -> list[str]:
    """A run of tool lines as shown: calls that ended well fold into one summary line of tool
    names and counts, whatever the tool; a running or failed call, a task and a stopped line stay
    whole, since they carry something to read."""
    lines: list[str] = []
    folded: dict[str, int] = {}

    def fold() -> None:
        if folded:
            names = (name if n == 1 else f"{name} \u00d7{n}" for name, n in folded.items())
            lines.append(f"{ICONS['complete']} " + " · ".join(names))
            folded.clear()

    for tool in tools:
        update = tool.update
        if (
            update.status == "complete"
            and update.name
            and not update.task
            and update.output != STOPPED
        ):
            folded[update.name] = folded.get(update.name, 0) + 1
        else:
            fold()
            lines.append(tool.line())
    fold()
    return lines


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
    where it ran, updated in place. The last line shows a status (Claude is writing, or waiting
    for the previous reply) until a divider and the footer replace it. A reply past MESSAGE_LIMIT
    continues in a new message. Never raises: a write Slack refuses is retried with the whole
    reply at the next flush, and the session goes on."""

    def __init__(self, slack: AsyncWebClient, *, channel: str) -> None:
        self._slack = slack
        self._channel = channel
        self._parts: list[_Text | _Tool] = []
        self._tools: dict[str, _Tool] = {}
        self._messages: list[str] = []  # ts of each message this reply has posted
        self._shown: list[list[dict[str, Any]]] = []  # the blocks each message shows now
        self._pending: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._status = texts.WRITING
        self._finished = False
        self._footer: str | None = None
        self._running = ""

    async def open(self, status: str) -> None:
        """Post the reply at once, showing only its status: the owner sees an answer is coming."""
        self._status = status
        await self._flush(final=False, footer=None)

    async def announce(self, status: str) -> None:
        """Change the status line now, without waiting for the next rewrite."""
        self._status = status
        await self._flush(final=False, footer=None)

    async def text(self, markdown: str) -> None:
        if self._parts and isinstance(self._parts[-1], _Text):
            self._parts[-1].text += markdown
        else:
            self._parts.append(_Text(markdown))
        await self._changed()

    async def task(self, update: TaskUpdate) -> None:
        tool = self._tools.get(update.id)
        if tool is None:
            tool = self._tools[update.id] = _Tool(update)
            self._parts.append(tool)
        else:
            tool.update = update
        await self._changed()

    async def set_running(self, counts: str) -> None:
        """Show what still runs in the channel (`⏳ 1 shell · 1 agent`) after this reply's status
        or footer, or on a line of its own; empty removes it. Only the latest reply shows one."""
        if counts == self._running:
            return
        self._running = counts
        if self._finished or self._messages:
            await self._changed()

    async def finish(self, closing: list[TaskUpdate], footer: str | None) -> None:
        """End the reply. A line still in progress is a task that outlives the turn: `task`
        keeps updating it after the end."""
        for update in closing:
            await self.task(update)
        if self._pending is not None:
            self._pending.cancel()
        self._finished, self._footer = True, footer
        await self._flush(final=True, footer=footer)

    async def _changed(self) -> None:
        if self._finished:
            # A background task or subagent after the reply ended: rare, and possibly during
            # shutdown, so written at once in its final form rather than on a timer.
            await self._flush(final=True, footer=self._footer)
        else:
            self._schedule()

    def _schedule(self) -> None:
        if self._pending is None or self._pending.done():
            self._pending = asyncio.create_task(self._later())

    async def _later(self) -> None:
        await asyncio.sleep(DEBOUNCE_SECONDS)
        await self._flush(final=False, footer=None)

    def _body(self, final: bool) -> str:
        # A blank line between text and a run of tool lines makes them separate markdown
        # paragraphs; a run of tool lines stays one compact paragraph.
        paragraphs: list[str] = []
        for is_text, run in itertools.groupby(self._parts, key=lambda p: isinstance(p, _Text)):
            if is_text:
                paragraphs.append("".join(p.text for p in run if isinstance(p, _Text)).strip("\n"))
            else:
                tools = [p for p in run if isinstance(p, _Tool)]
                # Folded only once the reply is finished: while Claude works, nothing moves.
                paragraphs.append(
                    "\n".join(tool_lines(tools) if final else (t.line() for t in tools))
                )
        return "\n\n".join(p for p in paragraphs if p)

    def _render(self, final: bool, footer: str | None) -> list[list[dict[str, Any]]]:
        chunks = split(self._body(final))
        messages: list[list[dict[str, Any]]] = [
            [{"type": "markdown", "text": chunk}] if chunk else [] for chunk in chunks
        ]
        if not final:
            status = " · ".join(filter(None, (self._status, self._running)))
            messages[-1].append(context_block(status))
            return messages
        last_line = " · ".join(filter(None, (footer, self._running)))
        if last_line:
            messages[-1] += [{"type": "divider"}, context_block(last_line)]
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
