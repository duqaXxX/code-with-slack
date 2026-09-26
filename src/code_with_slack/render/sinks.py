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
from code_with_slack.render.escape import mrkdwn_escape
from code_with_slack.render.renderer import STOPPED, TaskUpdate

logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 1.0
# The final write has no next rewrite to fix it: one that fails for any reason but its content
# is tried once more after this pause (slack-sdk has already retried a rate limit by then).
FINAL_RETRY_SECONDS = 10.0
# A markdown block holds at most 12,000 characters; the margin keeps a tool line that grows in
# place from pushing a full message over the limit.
MESSAGE_LIMIT = 11_000
FALLBACK_LIMIT = 3_000
# A context block's text holds at most 3,000 characters; a message, at most 50 blocks.
CONTEXT_LIMIT = 2_900
BLOCKS_LIMIT = 45
# chat.update errors that refuse the content itself (reference, read 2026-09-25): a plain retry
# can pass where the blocks did not. A transient error such as `ratelimited` is not one.
REFUSED_CONTENT = {"invalid_blocks", "invalid_blocks_format", "msg_too_long", "invalid_arguments"}
ICONS = {"pending": "·", "in_progress": "…", "complete": "✓", "error": "✗"}


def describe(exc: Exception) -> str:
    """Slack's error code, or the exception type: never the request or its content."""
    if isinstance(exc, SlackApiError):
        return str(exc.response.get("error"))
    return type(exc).__name__


def context_block(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


# An empty line before the footer's divider and after the footer, so replies stand apart: Slack
# blocks have no margin setting, so a context block holding only a zero-width space makes the gap.
def spacer(where: str) -> dict[str, Any]:
    # Each spacer has its own block_id: Slack refuses a message that repeats one (invalid_blocks).
    return {
        "type": "context",
        "block_id": f"spacer-{where}",
        "elements": [{"type": "mrkdwn", "text": "\u200b"}],
    }


SPACER_ABOVE = spacer("above")
SPACER_BELOW = spacer("below")


def tools_block(lines: list[str], index: int) -> dict[str, Any]:
    """Tool lines, already escaped, as secondary text, small and grey like the footer, as the
    terminal dims them. The block_id marks the reply's body, as opposed to its status or footer."""
    block = context_block("\n".join(lines))
    return {**block, "block_id": f"tools-{index}"}


def block_text(block: dict[str, Any]) -> str:
    if block["type"] == "markdown":
        return str(block["text"])
    return "".join(str(e.get("text", "")) for e in block.get("elements") or [])


def plain_text(blocks: list[dict[str, Any]]) -> str:
    """A message's text and footer with no block: what a refused final write is retried with.
    Slack caps a text-only message at 4,000 characters (chat.update reference, 2026-09-25)."""
    body = "\n\n".join(str(b["text"]) for b in blocks if b["type"] == "markdown")
    if len(body) > FALLBACK_LIMIT:
        body = body[:FALLBACK_LIMIT] + "…"
    types = [b["type"] for b in blocks]
    footer = block_text(blocks[types.index("divider") + 1]) if "divider" in types else ""
    return "\n\n".join(filter(None, (body, footer))) or "…"


@dataclass
class _Text:
    text: str


@dataclass
class _Tool:
    update: TaskUpdate

    def line(self) -> str:
        update = self.update
        line = f"{ICONS[update.status]} `{update.title}`"
        if update.calls:
            # How much a subagent has done: its latest call alone does not say.
            line += f" · {update.calls} call{'' if update.calls == 1 else 's'}"
        if (update.status == "error" and update.output) or update.output == STOPPED:
            line += f" · {update.output}"
        elif update.status == "in_progress" and update.details:
            line += f" · {update.details.splitlines()[-1]}"
        return line


def tool_lines(tools: list[_Tool]) -> list[str]:
    """A run of tool lines as shown, while the turn runs and once it ends: the calls that ended
    fold into one first line of tool names, each counted when it ran more than once, whatever
    the tool (`✓ Bash · Read · ✗ Bash`), as the terminal folds them. A running call, a task and
    a stopped line stay whole below it, in order: they outlive the moment or say why they
    ended. A running call moves into the counts when it ends, so the lines above never move."""
    counts: dict[str, dict[str, int]] = {"complete": {}, "error": {}}
    whole: list[str] = []
    for tool in tools:
        update = tool.update
        if update.status in counts and update.name and not update.task and update.output != STOPPED:
            names = counts[update.status]
            names[update.name] = names.get(update.name, 0) + 1
        else:
            whole.append(tool.line())
    groups = [
        f"{ICONS[status]} "
        + " · ".join(name if n == 1 else f"{name} \u00d7{n}" for name, n in names.items())
        for status, names in counts.items()
        if names
    ]
    return ([" · ".join(groups)] if groups else []) + whole


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
    continues in a new message. Never raises: a write that fails is retried with the whole reply
    at the next flush, the final one after FINAL_RETRY_SECONDS, and the session goes on."""

    def __init__(self, slack: AsyncWebClient, *, channel: str) -> None:
        self._slack = slack
        self._channel = channel
        self._parts: list[_Text | _Tool] = []
        self._tools: dict[str, _Tool] = {}
        self._messages: list[str] = []  # ts of each message this reply has posted
        self._shown: list[list[dict[str, Any]]] = []  # the blocks each message shows now
        self._pending: asyncio.Task[None] | None = None
        self._retry: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._status = texts.WRITING
        self._finished = False
        self._footer: str | None = None
        self._running = ""
        self._latest = True

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

    async def set_latest(self, latest: bool) -> None:
        """Only the channel's latest reply shows the footer and the running counts, at the
        bottom of the channel as the terminal's status line; an older one drops them."""
        if latest == self._latest:
            return
        self._latest = latest
        if self._finished:
            await self._changed()

    async def finish(self, closing: list[TaskUpdate], footer: str | None) -> None:
        """End the reply. A line still in progress is a task that outlives the turn: `task`
        keeps updating it after the end."""
        for update in closing:
            await self.task(update)
        if self._pending is not None:
            self._pending.cancel()
        self._finished, self._footer = True, footer
        if not await self._flush(final=True, footer=footer):
            self._retry = asyncio.create_task(self._retry_final())

    async def _retry_final(self) -> None:
        await asyncio.sleep(FINAL_RETRY_SECONDS)
        await self._flush(final=True, footer=self._footer)

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
        # Shielded: `finish` cancels a pending rewrite, and a write cancelled after Slack took it
        # would lose the message's ts. `finish` waits for the lock instead.
        await asyncio.shield(self._flush(final=False, footer=None))

    def _blocks(self) -> list[dict[str, Any]]:
        """The reply's body in order: Claude's text as markdown, each run of tool lines as
        secondary text, folded by `tool_lines` whether the reply is finished or not."""
        blocks: list[dict[str, Any]] = []
        for is_text, run in itertools.groupby(self._parts, key=lambda p: isinstance(p, _Text)):
            if is_text:
                text = "".join(p.text for p in run if isinstance(p, _Text)).strip("\n")
                blocks += [{"type": "markdown", "text": c} for c in split(text) if c]
            else:
                tools = [p for p in run if isinstance(p, _Tool)]
                lines = tool_lines(tools)
                # Escaped first: Slack's limit counts the text it receives.
                lines = [mrkdwn_escape(line) for line in lines]
                chunk: list[str] = []
                for line in lines:
                    if chunk and sum(len(x) + 1 for x in chunk) + len(line) > CONTEXT_LIMIT:
                        blocks.append(tools_block(chunk, len(blocks)))
                        chunk = []
                    chunk.append(line[:CONTEXT_LIMIT])
                if chunk:
                    blocks.append(tools_block(chunk, len(blocks)))
        return blocks

    def _render(self, final: bool, footer: str | None) -> list[list[dict[str, Any]]]:
        messages: list[list[dict[str, Any]]] = [[]]
        size = 0
        for block in self._blocks():
            length = len(block_text(block))
            if messages[-1] and (
                size + length > MESSAGE_LIMIT or len(messages[-1]) >= BLOCKS_LIMIT
            ):
                messages.append([])
                size = 0
            messages[-1].append(block)
            size += length
        if not final:
            status = " · ".join(filter(None, (self._status, self._running)))
            messages[-1].append(context_block(status))
            return messages
        last_line = " · ".join(filter(None, (footer, self._running))) if self._latest else ""
        if last_line:
            messages[-1] += [
                SPACER_ABOVE,
                {"type": "divider"},
                context_block(last_line),
                SPACER_BELOW,
            ]
        return messages

    async def _write_plain(self, index: int, blocks: list[dict[str, Any]]) -> bool:
        """Slack refused the final form of a message: without this one retry it would keep
        saying Claude is writing. An empty `blocks` makes Slack drop the old ones and render
        the text (chat.update reference, 2026-09-25)."""
        try:
            await self._slack.chat_update(
                channel=self._channel, ts=self._messages[index], text=plain_text(blocks), blocks=[]
            )
        except Exception as exc:
            logger.warning("could not write a reply to Slack as plain text: %s", describe(exc))
            return False
        self._shown[index] = []
        return True

    async def _flush(self, *, final: bool, footer: str | None) -> bool:
        """Write what changed; False when a write failed and the reply is not as rendered."""
        async with self._lock:
            if self._finished and not final:
                return True  # a draft that waited for the lock must not undo the final form
            rendered = self._render(final, footer)
            if rendered == [[]]:
                rendered = []  # nothing left to show: the extra-message removal below takes it
            for index, blocks in enumerate(rendered):
                if not blocks:
                    continue
                if index < len(self._shown) and self._shown[index] == blocks:
                    continue
                fallback = block_text(blocks[0])[:FALLBACK_LIMIT] or "…"
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
                        # Claude's text can carry a link built to leak data when Slack fetches
                        # it for a preview: no previews for anything the daemon posts.
                        posted = await self._slack.chat_postMessage(
                            channel=self._channel,
                            text=fallback,
                            blocks=blocks,
                            unfurl_links=False,
                            unfurl_media=False,
                        )
                        self._messages.append(str(posted["ts"]))
                        self._shown.append(blocks)
                except Exception as exc:
                    logger.warning("could not write a reply to Slack: %s", describe(exc))
                    refused = final and describe(exc) in REFUSED_CONTENT
                    if (
                        refused
                        and index < len(self._messages)
                        and await self._write_plain(index, blocks)
                    ):
                        continue  # the later messages still need their final form
                    return False
            # Folding at the end can make the reply shorter: a message it no longer needs goes.
            while len(self._messages) > len(rendered):
                try:
                    await self._slack.chat_delete(channel=self._channel, ts=self._messages[-1])
                except Exception as exc:
                    logger.warning("could not remove a reply's extra message: %s", describe(exc))
                    return False
                self._messages.pop()
                self._shown.pop()
        return True
