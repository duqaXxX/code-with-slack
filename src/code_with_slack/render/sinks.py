"""Write a rendered reply to Slack: one message in the channel's main window, rewritten with
chat.update as the reply grows.

Slack's native streaming works only inside a thread in an ordinary channel (`chat.startStream`
without `thread_ts` answers `invalid_thread_ts`, measured 2026-09-23), so the reply is rewritten
whole instead, at most once per DEBOUNCE_SECONDS: chat.update allows "50+ per minute" (Tier 3).
"""

import asyncio
import itertools
import logging
import re
from dataclasses import dataclass
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from code_with_slack import texts
from code_with_slack.render.escape import mrkdwn_escape
from code_with_slack.render.previews import folded
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
# ⏳ marks what is still running, as the footer marks running tasks (`⏳ 1 shell`).
ICONS = {"pending": "⏳", "in_progress": "⏳", "complete": "✓", "error": "✗"}
NESTED = texts.NESTED


def describe(exc: Exception) -> str:
    """Slack's error code, or the exception type: never the request or its content."""
    if isinstance(exc, SlackApiError):
        return str(exc.response.get("error"))
    return type(exc).__name__


def context_block(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


# A text object's limit, as a context block's mrkdwn element holds one (Block Kit reference).
CONTEXT_LIMIT = 3000


def notice_text(text: str) -> str:
    """A daemon notice fitted into one context element: cut with `…` past its limit."""
    return text if len(text) <= CONTEXT_LIMIT else text[: CONTEXT_LIMIT - 1] + "…"


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


def preview_blocks(body: str) -> list[dict[str, Any]]:
    """A new file's first lines as code blocks, split where a block would pass its limit. A fence
    inside the file must not close the block early: every run of three or more backticks is
    broken up, as `escape.shown_as_written` breaks every one."""
    body = re.sub(r"`{3,}", lambda run: "\u200b".join(run.group()), body)
    return [{"type": "markdown", "text": f"```\n{chunk}\n```"} for chunk in split(body) if chunk]


def diff_containers(icon: str, title: str, summary: str, body: str) -> list[dict[str, Any]]:
    """A diff as the terminal's call line, collapsed: a full-width container per MESSAGE_LIMIT
    piece of the body, titled with the call's line and its summary, closed until the owner opens
    it. The diff sits in the message itself, so it opens after a restart too."""
    # The plain title is the fallback of a client that does not draw the rich one, which shows
    # the call's name in code style as the tool line does. Both are cut to the plain one's 150.
    name = title[: 150 - len(icon) - 1]
    return [
        {
            "type": "container",
            "title": {"type": "plain_text", "text": f"{icon} {name}"},
            "rich_text_title": {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "text", "text": f"{icon} "},
                            {"type": "text", "text": name, "style": {"code": True}},
                        ],
                    }
                ],
            },
            "subtitle": {"type": "mrkdwn", "text": summary[:150]},
            "width": "full",
            "is_collapsible": True,
            "default_collapsed": True,
            # A `markdown` block is not allowed in a container: rich text is, and its text is
            # literal, so no fence to break. Slack desktop colours `diff`; mobile colours nothing.
            "child_blocks": [
                {
                    "type": "rich_text",
                    "elements": [
                        {
                            "type": "rich_text_preformatted",
                            "language": "diff",
                            "elements": [{"type": "text", "text": chunk}],
                        }
                    ],
                }
            ],
        }
        for chunk in split(body)
        if chunk
    ]


def block_text(block: dict[str, Any]) -> str:
    if block["type"] == "markdown":
        return str(block["text"])
    if block["type"] == "container":
        return "".join(
            e["text"]
            for child in block["child_blocks"]
            for pre in child["elements"]
            for e in pre["elements"]
        )
    return "".join(str(e.get("text", "")) for e in block.get("elements") or [])


def plain_text(blocks: list[dict[str, Any]]) -> str:
    """A message's text with no block: what a refused final write is retried with. Slack caps a
    text-only message at 4,000 characters (chat.update reference, 2026-09-25)."""
    body = "\n\n".join(str(b["text"]) for b in blocks if b["type"] == "markdown")
    if len(body) > FALLBACK_LIMIT:
        body = body[:FALLBACK_LIMIT] + "…"
    return body or "…"


@dataclass
class _Text:
    text: str


@dataclass
class _Tool:
    update: TaskUpdate

    def line(self, *, icon: bool = True) -> str:
        update = self.update
        title = update.shown_preview.title if update.shown_preview else update.title
        line = f"{ICONS[update.status]} `{title}`" if icon else f"`{title}`"
        if update.calls:
            # How much a subagent has done: its latest call alone does not say.
            line += f" · {update.calls} call{'' if update.calls == 1 else 's'}"
        if update.shown_preview is not None:
            # The terminal's sentence under the call (`⎿ Added 1 line, removed 1 line`); the
            # lines themselves follow the tool line in a block of their own.
            return f"{line}\n{NESTED}{update.shown_preview.summary}"
        if (update.status == "error" and update.output) or update.output == STOPPED:
            line += f" · {update.output}"
        elif update.status == "in_progress" and update.details:
            # What it is doing now, one level down, as the terminal nests it under `⎿`.
            line += f"\n{NESTED}{update.details.splitlines()[-1]}"
        return line


def tool_lines(tools: list[_Tool], *, latest: bool = False) -> list[str]:
    """A run of tool lines as shown: the calls that ended fold into one first line, in the
    terminal's words for the tools it has words for (`✓ Ran 2 shell commands · Read 1 file ·
    WebFetch · ✗ Ran 1 shell command`), as the terminal folds them. A call with a preview
    never folds. A running call, a task and a stopped line stay whole
    below it, in order: they outlive the moment or say why they ended. With `latest`, for the
    run Claude is still in, its last call stays whole too until it is no longer the last: a
    call that ends within a second of starting would otherwise never show."""
    counts: dict[str, dict[str, int]] = {"complete": {}, "error": {}}
    whole: list[str] = []
    for index, tool in enumerate(tools):
        update = tool.update
        last = latest and index == len(tools) - 1
        if (
            update.status in counts
            and update.name
            and not update.task
            and update.shown_preview is None
            and update.output != STOPPED
            and not last
        ):
            names = counts[update.status]
            names[update.name] = names.get(update.name, 0) + 1
        elif last and update.status == "complete" and not update.task and update.output != STOPPED:
            # What Claude just did, not an outcome: the outcome goes to the counts once another
            # call follows. A running call keeps ⏳, a failure its icon and output.
            whole.append(tool.line(icon=False))
        else:
            whole.append(tool.line())
    groups = [
        f"{ICONS[status]} " + " · ".join(folded(name, n) for name, n in names.items())
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
    for the previous reply) until the reply ends. A reply past MESSAGE_LIMIT continues in a new
    message. The end is a closing message of its own, posted then (a divider and the footer), so
    that a reply that must reach the owner rings once, when it is complete: only a new message
    notifies. Never raises: a write that fails is retried with the whole reply at the next flush,
    the final one after FINAL_RETRY_SECONDS, and the session goes on."""

    def __init__(self, slack: AsyncWebClient, *, channel: str, thread_ts: str) -> None:
        self._slack = slack
        self._channel = channel
        self._thread_ts = thread_ts
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
        self._closing: str | None = None  # ts of the closing message, once posted
        self._closing_shown: list[dict[str, Any]] = []
        self._reply_to: str | None = None  # the owner's question: the closing message's text
        self._notify_kept = False  # whether the closing message still owes its notification

    async def open(self, status: str) -> None:
        """Post the reply at once, showing only its status: the owner sees an answer is coming."""
        self._status = status
        await self._flush(final=False)

    async def announce(self, status: str) -> None:
        """Change the status line now, without waiting for the next rewrite."""
        self._status = status
        await self._flush(final=False)

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

    async def finish(
        self, closing: list[TaskUpdate], footer: str | None, *, reply_to: str | None = None
    ) -> None:
        """End the reply. A line still in progress is a task that outlives the turn: `task`
        keeps updating it after the end. With `reply_to` (the owner's question, one line) the
        closing message notifies the owner; without it the end is silent."""
        for update in closing:
            await self.task(update)
        if self._pending is not None:
            self._pending.cancel()
        self._finished, self._footer, self._reply_to = True, footer, reply_to
        # A reply already superseded when it ends still owes its notification: nothing later
        # takes that back, even once `set_latest` drops the footer for good.
        self._notify_kept = not self._latest
        if not await self._flush(final=True):
            self._retry = asyncio.create_task(self._retry_final())

    async def _retry_final(self) -> None:
        await asyncio.sleep(FINAL_RETRY_SECONDS)
        await self._flush(final=True)

    async def _changed(self) -> None:
        if self._finished:
            # A background task or subagent after the reply ended: rare, and possibly during
            # shutdown, so written at once in its final form rather than on a timer.
            await self._flush(final=True)
        else:
            self._schedule()

    def _schedule(self) -> None:
        if self._pending is None or self._pending.done():
            self._pending = asyncio.create_task(self._later())

    async def _later(self) -> None:
        await asyncio.sleep(DEBOUNCE_SECONDS)
        # Shielded: `finish` cancels a pending rewrite, and a write cancelled after Slack took it
        # would lose the message's ts. `finish` waits for the lock instead.
        await asyncio.shield(self._flush(final=False))

    def _blocks(self) -> list[dict[str, Any]]:
        """The reply's body in order: Claude's text as markdown, each run of tool lines as
        secondary text, folded by `tool_lines`. The last run of a reply still being written is
        the one Claude is in: its last call stays whole."""
        blocks: list[dict[str, Any]] = []
        runs = [
            (is_text, list(run))
            for is_text, run in itertools.groupby(self._parts, key=lambda p: isinstance(p, _Text))
        ]
        for position, (is_text, run) in enumerate(runs):
            if is_text:
                text = "".join(p.text for p in run if isinstance(p, _Text)).strip("\n")
                blocks += [{"type": "markdown", "text": c} for c in split(text) if c]
            else:
                tools = [p for p in run if isinstance(p, _Tool)]
                latest = not self._finished and position == len(runs) - 1
                # A call with a preview splits the fold, as in the terminal: the calls before it
                # fold on their own, then its line and its preview, then the calls after it.
                segments = [
                    (shown, list(group))
                    for shown, group in itertools.groupby(
                        tools, key=lambda t: t.update.shown_preview is not None
                    )
                ]
                for index, (shown, group) in enumerate(segments):
                    last = latest and index == len(segments) - 1
                    for tool in group if shown else [None]:
                        view = tool.update.shown_preview if tool else None
                        if tool and view and view.body and view.language == "diff":
                            # The call's line heads its diff's container: no line of its own.
                            icon = ICONS[tool.update.status]
                            blocks += diff_containers(icon, view.title, view.summary, view.body)
                            continue
                        lines = [tool.line()] if tool else tool_lines(group, latest=last)
                        blocks += self._tool_blocks(lines, len(blocks))
                        if view and view.body:
                            blocks += preview_blocks(view.body)
        return blocks

    @staticmethod
    def _tool_blocks(lines: list[str], start: int) -> list[dict[str, Any]]:
        """Tool lines as context blocks, each under Slack's size limit."""
        # Escaped first: Slack's limit counts the text it receives.
        lines = [mrkdwn_escape(line) for line in lines]
        blocks: list[dict[str, Any]] = []
        chunk: list[str] = []
        for line in lines:
            if chunk and sum(len(x) + 1 for x in chunk) + len(line) > CONTEXT_LIMIT:
                blocks.append(tools_block(chunk, start + len(blocks)))
                chunk = []
            chunk.append(line[:CONTEXT_LIMIT])
        if chunk:
            blocks.append(tools_block(chunk, start + len(blocks)))
        return blocks

    def _render(self, final: bool) -> list[list[dict[str, Any]]]:
        """The reply's body, message by message; while it is written, the status line ends it."""
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

    def _closing_blocks(self) -> list[dict[str, Any]]:
        """The closing message: the footer and what still runs. Only the channel's latest reply
        shows them, so a newer reply removes the closing message, unless it was already newer
        when this one ended: a reply the owner asked something still has to post, since a new
        message in the thread is what notifies here, so a bare line stands in for the footer.
        Empty: no closing message."""
        notifies = self._reply_to is not None and (self._latest or self._notify_kept)
        footer = (self._footer, self._running) if self._latest else ()
        last_line = " · ".join(filter(None, footer))
        if not last_line:
            return [context_block("​")] if notifies else []
        return [SPACER_ABOVE, {"type": "divider"}, context_block(last_line), SPACER_BELOW]

    def _closing_text(self) -> str:
        """What the notification shows: the question a ringing reply answers, or the footer."""
        if self._reply_to is not None:
            return texts.REPLY_TO.format(prompt=mrkdwn_escape(self._reply_to))
        return " · ".join(filter(None, (self._footer, self._running)))[:FALLBACK_LIMIT] or "…"

    async def _write_closing(self) -> bool:
        """Post, rewrite or remove the closing message to match the reply's end."""
        blocks = self._closing_blocks()
        try:
            if blocks and self._closing is None:
                posted = await self._slack.chat_postMessage(
                    channel=self._channel,
                    thread_ts=self._thread_ts,
                    text=self._closing_text(),
                    blocks=blocks,
                    unfurl_links=False,
                    unfurl_media=False,
                )
                self._closing = str(posted["ts"])
            elif blocks and self._closing is not None and blocks != self._closing_shown:
                # An edit never rings (measured 2026-09-27): the text can stay as posted.
                await self._slack.chat_update(
                    channel=self._channel,
                    ts=self._closing,
                    text=self._closing_text(),
                    blocks=blocks,
                )
            elif not blocks and self._closing is not None:
                await self._slack.chat_delete(channel=self._channel, ts=self._closing)
                self._closing = None
        except Exception as exc:
            logger.warning("could not write a reply's closing message: %s", describe(exc))
            return False
        self._closing_shown = blocks
        return True

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

    async def _flush(self, *, final: bool) -> bool:
        """Write what changed; False when a write failed and the reply is not as rendered."""
        async with self._lock:
            if self._finished and not final:
                return True  # a draft that waited for the lock must not undo the final form
            rendered = self._render(final)
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
                            thread_ts=self._thread_ts,
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
            # After the body, so the closing message is posted below it.
            return await self._write_closing() if final else True
