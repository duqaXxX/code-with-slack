"""Write a rendered reply to Slack as a native stream: `chat.startStream` with the reply's first
content, `chat.appendStream` as it grows, `chat.stopStream` at its end, in the session's thread.
A stream in a thread the owner started notifies once, when it stops, with its first text as the
banner, and never at its start (measured 2026-09-29). Slack closes a stream 5 minutes after it
started (measured), so a reply that runs longer stops its stream at STREAM_SECONDS and goes on in
the same message with `chat.update`, which never notifies; its end then posts a closing message.
Every `chat.appendStream` and `chat.update` also waits its turn on an `UpdateLimiter` shared by
every reply in the process: a token bucket that paces writes evenly under the app's own budget,
rather than letting several busy threads exhaust it together and then freeze until it resets.
"""

import asyncio
import itertools
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_async_handlers import AsyncConnectionErrorRetryHandler
from slack_sdk.http_retry.request import HttpRequest
from slack_sdk.http_retry.state import RetryState
from slack_sdk.web.async_client import AsyncWebClient

from code_with_slack.render.escape import mrkdwn_escape
from code_with_slack.render.renderer import STOPPED, TaskUpdate

logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 1.0
# The final write has no next rewrite to fix it: one that fails for any reason but its content
# is tried once more after this pause (slack-sdk has already retried a rate limit by then).
FINAL_RETRY_SECONDS = 10.0
# A stream is closed by Slack 5 minutes after `chat.startStream` (measured 2026-09-28: refused at
# 300.3 s and 305 s); stopped by the daemon at this age, it leaves a margin for the round trip.
STREAM_SECONDS = 280.0
# chat.update is Tier 3, "50+ per minute" per app (chat.update reference, read 2026-09-28): 40
# per 60 s plus a burst of 5, worst case 45 in one window, a real margin under the documented
# floor. Paced evenly (a token bucket, not a sliding window) past the burst, so a busy minute is
# a steady trickle rather than every reply racing through the budget together and then freezing
# until it resets.
UPDATE_LIMIT = 40
UPDATE_WINDOW_SECONDS = 60.0
# How many writes the budget lets through at once before pacing kicks in: enough for a reply
# that just started to show its first few lines without waiting on threads that were already busy.
UPDATE_BURST = 5
# A message holds at most 12,000 characters and 50 blocks or task cards (measured 2026-09-28);
# the margins keep a preview that arrives after its card, and the footer, inside them.
MESSAGE_LIMIT = 11_000
BLOCKS_LIMIT = 45
FALLBACK_LIMIT = 3_000
# What a message's `text` says: the banner of a notification, short, since a `chat.update` whose
# `text` is long fails `msg_too_long` (measured 2026-09-28).
BANNER_LIMIT = 300
# A task card's details and output are text of a rich text element; a context block's text
# holds at most 3,000 characters.
CARD_TEXT_LIMIT = 2_900
CARD_TITLE_LIMIT = 150
# chat.update errors that refuse the content itself (reference, read 2026-09-25): a plain retry
# can pass where the blocks did not. A transient error such as `ratelimited` is not one.
REFUSED_CONTENT = {"invalid_blocks", "invalid_blocks_format", "msg_too_long", "invalid_arguments"}
# Slack's answers about a stream's state (measured 2026-09-28): it is over, or still open.
NOT_STREAMING = "message_not_in_streaming_state"
STILL_STREAMING = "streaming_state_conflict"
# The icon of a diff container's title.
ICONS = {"pending": "⏳", "in_progress": "⏳", "complete": "✓", "error": "✗"}
TERMINAL = ("complete", "error")


def describe(exc: Exception) -> str:
    """Slack's error code, or the exception type: never the request or its content."""
    if isinstance(exc, SlackApiError):
        return str(exc.response.get("error"))
    return type(exc).__name__


STREAM_METHODS = ("chat.startStream", "chat.appendStream", "chat.stopStream")


class ConnectionRetryUnlessStream(AsyncConnectionErrorRetryHandler):
    """slack-sdk's retry of a call that failed on the connection, except for the stream calls:
    a start, an append and a stop are not idempotent, and a reset can come after Slack applied
    the call, so a retry would duplicate text, or stop a stream twice."""

    async def _can_retry_async(
        self,
        *,
        state: RetryState,
        request: HttpRequest,
        response: Any = None,
        error: Exception | None = None,
    ) -> bool:
        if request.url.rstrip("/").endswith(STREAM_METHODS):
            return False
        return await super()._can_retry_async(
            state=state, request=request, response=response, error=error
        )


def unknown_outcome(exc: Exception) -> bool:
    """Whether a failed call may have been applied: Slack answering with an error says it was
    not, anything else (a reset, a timeout) says nothing."""
    return not isinstance(exc, SlackApiError)


class Clock:
    """The stream's deadline: a sleep the tests replace, so a test crosses 280 seconds without
    waiting them."""

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


async def delete_request(slack: AsyncWebClient, *, channel: str, ts: str) -> None:
    """Delete a request (an approval, a question, a D8 hold) once it is decided or stale:
    `message_not_found` counts as done, as everywhere else this project deletes one. Shared by
    `ThreadSession._delete_request`, `slack_app.py`'s `remove_request` and `repair.py` (issue #19
    fix round item 9), so the one behaviour lives in one place."""
    try:
        await slack.chat_delete(channel=channel, ts=ts)
    except Exception as exc:
        if describe(exc) != "message_not_found":
            logger.warning("could not remove a request in %s: %s", channel, describe(exc))


def context_block(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


# A text object's limit, as a context block's mrkdwn element holds one (Block Kit reference).
CONTEXT_LIMIT = 3000


def notice_text(text: str) -> str:
    """A daemon notice fitted into one context element: cut with `…` past its limit."""
    return text if len(text) <= CONTEXT_LIMIT else text[: CONTEXT_LIMIT - 1] + "…"


# Invisible, so a block that holds only this one shows no text of its own; never pasted as a
# literal character in source, always this escape.
ZERO_WIDTH_SPACE = "\u200b"


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


def banner_text(paragraph: str) -> str:
    """A paragraph of Claude's markdown as the plain `text` of a notification: the markers gone
    (headings, quotes, list bullets, emphasis, code ticks, link targets) and `&`, `<`, `>`
    escaped, since Slack reads them as markup there too."""
    return mrkdwn_escape(strip_markdown(paragraph))


def strip_markdown(text: str) -> str:
    """The markdown markers of `text` removed, nothing escaped."""
    plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    plain = re.sub(r"^\s*(?:#+|>|[-+*]|\d+\.)\s+", "", plain, flags=re.M)
    plain = re.sub(r"\*\*|__|~~|`+", "", plain)
    plain = re.sub(r"(?<!\w)[*_]+|[*_]+(?!\w)", "", plain)
    return plain.strip()


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


class UpdateLimiter:
    """One instance shared by every `ReplySink` in the process, so their chat.update writes stay
    under Slack's app-wide budget: a token bucket refilling at `limit` tokens per `window`
    seconds (evenly, one token every `window / limit`), holding at most `burst` at once. A caller
    that has to wait keeps its place in line, since it holds `_lock` for as long as it waits, so
    the next caller queues up behind it. A retry `AsyncRateLimitErrorRetryHandler` makes under the
    hood, inside one `chat.update` call, spends no extra token here: the limiter only gates the
    call itself, not what slack-sdk does while it is in flight."""

    def __init__(
        self,
        *,
        limit: int = UPDATE_LIMIT,
        window: float = UPDATE_WINDOW_SECONDS,
        burst: int = UPDATE_BURST,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate = limit / window  # tokens regained per second
        self._burst = burst
        self._clock = clock
        self._tokens = float(burst)
        self._checked = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until a token is available, then spend it."""
        async with self._lock:
            while True:
                now = self._clock()
                self._tokens = min(self._burst, self._tokens + (now - self._checked) * self._rate)
                self._checked = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / self._rate)

    async def refund(self) -> None:
        """Give back a token `acquire` spent on a write that, once inside the caller's own lock,
        turned out not to be needed after all (the reply caught up to what it now shows while
        this one waited its turn): capped at `burst`, as a token earned by waiting would be.
        Never takes `_lock`: a concurrent `acquire` can hold it for as long as its own wait
        takes, and this must land at once regardless; a plain attribute write is safe without
        it (no `await` in between, so nothing else can run mid-assignment), and `acquire`
        always rereads `_tokens` fresh on its own next pass."""
        self._tokens = min(self._burst, self._tokens + 1)


Cursor = tuple[int, int]  # (part, offset): characters into a text part, elements into a tool


@dataclass
class _Text:
    text: str
    notice: bool = False  # a line of the daemon's own: never the banner while Claude has words


@dataclass
class _Tool:
    update: TaskUpdate

    def pieces(self) -> list[str]:
        """What follows the tool's card: the terminal's preview of a call that ended well, cut
        into pieces that fit a message each. None until the call ends, and for any other call."""
        view = self.update.shown_preview
        return [c for c in split(view.body) if c] if view and view.body else []

    @property
    def extent(self) -> int:
        """The elements of the tool: its card, then its preview pieces."""
        return 1 + len(self.pieces())


def card_fields(update: TaskUpdate) -> dict[str, str]:
    """What a tool's card says, in the terminal's words: the title, the state, what it is doing
    now while it runs, and its output when it failed, was stopped, or has a preview's sentence.
    A running call, a task and a stopped call keep the words their tool line had."""
    view = update.shown_preview
    title = view.title if view else update.title
    if update.calls:
        # How much a subagent has done: its latest call alone does not say.
        title += f" · {update.calls} call{'' if update.calls == 1 else 's'}"
    fields = {"title": title[:CARD_TITLE_LIMIT], "status": update.status}
    output = update.output
    if view:
        fields["output"] = view.summary[:CARD_TEXT_LIMIT]
    elif output and (update.status == "error" or output == STOPPED):
        fields["output"] = output[:CARD_TEXT_LIMIT]
    elif update.status == "in_progress" and update.details:
        fields["details"] = update.details[:CARD_TEXT_LIMIT]
    return fields


def card_chunk(update: TaskUpdate) -> dict[str, Any]:
    """The `task_update` chunk of a tool: a stream updates a card in place by its id."""
    return {"type": "task_update", "id": update.id, **card_fields(update)}


def rich_text(text: str) -> dict[str, Any]:
    return {
        "type": "rich_text",
        "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": text}]}],
    }


def card_block(update: TaskUpdate) -> dict[str, Any]:
    """The `task_card` block of a tool, for a message that is no longer a stream (recorded
    shape: `details` and `output` are rich text)."""
    fields = card_fields(update)
    block: dict[str, Any] = {
        "type": "task_card",
        "task_id": update.id,
        "title": fields["title"],
        "status": fields["status"],
    }
    for key in ("details", "output"):
        if key in fields:
            block[key] = rich_text(fields[key])
    return block


def piece_blocks(tool: _Tool, index: int) -> list[dict[str, Any]]:
    """Piece `index` (1 is the first) of a tool's preview as blocks: a collapsed container for a
    diff, code blocks for a new file's first lines."""
    view = tool.update.shown_preview
    assert view is not None
    body = tool.pieces()[index - 1]
    if view.language == "diff":
        return diff_containers(ICONS[tool.update.status], view.title, view.summary, body)
    return preview_blocks(body)


def piece_chunk(tool: _Tool, index: int) -> dict[str, Any]:
    """The same piece for a stream: a `blocks` chunk (measured 2026-09-28 for a diff's container
    and 2026-09-29 for a markdown block, which reads back as rich text)."""
    return {"type": "blocks", "blocks": piece_blocks(tool, index)}


@dataclass
class _Plan:
    """What a stream still has to be told, and where the reply goes on if it does not all fit."""

    size: int  # characters the message holds once these chunks are in
    count: int  # elements it holds
    chunks: list[dict[str, Any]] = field(default_factory=list)
    overflow: Cursor | None = None  # where the next message starts, if the rest does not fit
    text: dict[int, int] = field(default_factory=dict)  # part -> characters sent up to
    pieces: set[tuple[int, int]] = field(default_factory=set)
    cards: dict[str, dict[str, Any]] = field(default_factory=dict)
    counted: set[int] = field(default_factory=set)


@dataclass
class _Message:
    """One Slack message of a reply: the span of the reply's model from `start` to the next
    message's start, first written as a stream (a reply's first message, and one that
    continues a stream that is still open) or as a post (one that continues a stopped message).
    While it streams, what it has been sent is remembered here, since a stream only grows."""

    start: Cursor
    mode: str  # "stream" or "post"
    ts: str | None = None
    streaming: bool = False
    text_sent: dict[int, int] = field(default_factory=dict)
    pieces_sent: set[tuple[int, int]] = field(default_factory=set)
    cards: dict[str, dict[str, Any]] = field(default_factory=dict)  # last chunk sent per tool
    size: int = 0
    count: int = 0
    counted: set[int] = field(default_factory=set)
    # Stopped: whether what it shows is its stream as sent, which needs no write while the
    # model still says the same; else `shown` is the blocks of its last post or update.
    exact: bool = False
    shown: list[dict[str, Any]] | None = None
    footer: list[dict[str, Any]] = field(default_factory=list)  # the footer it shows
    deadline: asyncio.Task[None] | None = None
    # An append whose outcome is unknown: the stream is no longer told anything, it is stopped
    # and the message goes on by update, from the model.
    blind: bool = False
    # The footer a stop of unknown outcome carried (a footerless stop records nothing): if the
    # next stop finds the stream over, that stop is the one that landed.
    stop_unknown: list[dict[str, Any]] | None = None


class ReplySink:
    """One reply in a Slack thread, as a native stream: Claude's text as it is written, and a
    task card per tool, updated in place, in the order things happen. The stream starts with the
    first content (never a placeholder) and stops with the reply's end, the footer at the bottom.
    It stops on its own at STREAM_SECONDS, since Slack closes a stream at 5 minutes: from then on
    the same message grows by `chat.update`, and the end posts a closing message with the footer.
    A reply past MESSAGE_LIMIT or BLOCKS_LIMIT continues in a new message (a new stream while
    the message still streams, else a post). `finish` ends the body only: a task that outlives
    the turn keeps updating its own card after that, in place. Never raises: a write that fails
    is sent again with the next one, the final one once more after FINAL_RETRY_SECONDS."""

    def __init__(
        self,
        slack: AsyncWebClient,
        *,
        channel: str,
        thread_ts: str,
        team_id: str,
        user_id: str,
        limiter: UpdateLimiter,
        clock: Clock | None = None,
        on_open_reply: Callable[[str | None, str | None], None] | None = None,
    ) -> None:
        self._slack = slack
        self._channel = channel
        self._thread_ts = thread_ts
        # A stream is addressed to a user of a workspace (as the recordings passed them).
        self._team_id = team_id
        self._user_id = user_id
        self._limiter = limiter
        self._clock = clock or Clock()
        # Crash repair (issue #19): `(old_ts, new_ts)`, this sink's own transition in the
        # thread's open-replies list (more than one sink can be open at once: a background
        # task's own reply can outlive the turn that started it, so each sink owns exactly one
        # entry and must never touch another's). A plain sync callback (`StateStore`'s setters
        # are sync file writes), never awaited here.
        self._on_open_reply = on_open_reply
        # This sink's own entries in that list: the messages a crash would leave unfinished.
        self._tracked: set[str] = set()
        self._ended = False  # the end landed: nothing is left for a repair to close
        # The last message's body is whole, and only the closing message is owed.
        self._body_landed = False
        self._parts: list[_Text | _Tool] = []
        self._tools: dict[str, _Tool] = {}
        self._messages: list[_Message] = []
        self._pending: asyncio.Task[None] | None = None
        self._retry: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._finished = False
        self._footer: str | None = None
        self._running = ""
        self._latest = True
        self._closed_out = False  # whether close_out has run; a second call is a no-op
        # Where the footer went once the reply ended: "inline", on the stream's own stop (and
        # on the message's updates after it), or "post", in a closing message of its own.
        self._end_mode: str | None = None
        self._closing: str | None = None  # ts of the closing message, once posted
        self._closing_shown: list[dict[str, Any]] = []
        # Resolves once the reply is known to have ended on Slack (True) or its one retry
        # failed too (False): what the session waits on before it shows a checkmark.
        self._landed: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        # Bumped by `_changed`: `_later` compares it before and after a pass to notice a change
        # that arrived while the pass wrote or waited its turn, and runs another pass for it.
        self._version = 0

    def _track(self, old: str | None, new: str | None) -> bool:
        """One transition of this sink's own entries in the thread's open-replies list: add
        `new`, or drop `old`; every other sink's entry is left alone. Best-effort (issue #19 fix
        round item 7): a failed `StateStore` write is logged (ids only) and swallowed, since the
        reply itself must never fail over crash-repair bookkeeping. False then: the caller keeps
        its set as it was (fix round 2 item 3), or it would believe a ts no longer needs removing
        and orphan it in state.json forever, since nothing else ever asks to remove a ts this
        sink no longer remembers."""
        if self._on_open_reply is None:
            return True
        try:
            self._on_open_reply(old, new)
        except Exception as exc:
            logger.warning(
                "could not update the open-reply tracking for %s/%s: %s",
                self._channel,
                self._thread_ts,
                describe(exc),
            )
            return False
        return True

    def _keeps_open(self, message: _Message, end: Cursor | None) -> bool:
        """Whether a crash would leave the message unfinished: its stream still open, or a card
        of it still running (a stopped message stores such a card as an error until it is
        updated), or, for the reply's last message, the end not yet landed. Once the body is
        whole and only the closing message is owed, there is nothing of the answer left to fix."""
        if message.ts is None:
            return False
        if message.streaming:
            return True
        if any(
            self._tool_elements(part, floor, ceil)[0] and part.update.status not in TERMINAL
            for _, part, floor, ceil in self._span(message.start, end)
            if isinstance(part, _Tool)
        ):
            return True
        return message is self._messages[-1] and not self._ended and not self._body_landed

    def _retrack(self) -> None:
        """Bring this sink's open-reply entries to the messages that are unfinished now: one is
        added the moment it is written, and dropped once nothing is left for a repair to close."""
        wanted = set()
        for index, message in enumerate(self._messages):
            following = self._messages[index + 1 :]
            end = following[0].start if following else None
            if message.ts is not None and self._keeps_open(message, end):
                wanted.add(message.ts)
        for ts in sorted(wanted - self._tracked):
            if self._track(None, ts):
                self._tracked.add(ts)
        for ts in sorted(self._tracked - wanted):
            if self._track(ts, None):
                self._tracked.discard(ts)

    def _settle_open_reply(self) -> None:
        """The reply's end landed (issue #19 fix round item 6): from then on nothing is left
        open for a repair to close, but a card still running."""
        self._ended = True
        self._retrack()

    async def text(self, markdown: str, *, notice: bool = False) -> None:
        """Claude's words, or with `notice` a line of the daemon's own (never a banner)."""
        last = self._parts[-1] if self._parts else None
        if isinstance(last, _Text) and last.notice == notice:
            last.text += markdown
        else:
            self._parts.append(_Text(markdown, notice))
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
        """Show what still runs in this thread (`⏳ 1 shell · 1 agent`) after the footer, or on a
        line of its own; empty removes it. Only the thread's latest reply shows one."""
        if counts == self._running:
            return
        self._running = counts
        if self._closed_out:
            await self._changed()

    async def set_latest(self, latest: bool) -> None:
        """Only the thread's latest reply shows the footer and the running counts, at the
        bottom of the thread as the terminal's status line; an older one drops them."""
        if latest == self._latest:
            return
        self._latest = latest
        if self._closed_out:
            await self._changed()

    async def finish(self, closing: list[TaskUpdate]) -> None:
        """End the reply's body: what was written is sent now, not at the next debounce. A card
        still in progress is a task that outlives the turn: `task` keeps updating it after
        this. The stream stays open: it ends with `close_out`, once the caller knows nothing
        more is coming (a task can still outlive this very turn)."""
        for update in closing:
            await self.task(update)
        if self._pending is not None:
            self._pending.cancel()
            # Cleared now, not left for `_schedule` to find: `cancel` only requests it, so the
            # task can still read `.done()` as False for a while yet.
            self._pending = None
        self._finished = True
        await self._flush()

    async def close_out(self, footer: str | None) -> bool:
        """End the reply, once: stop its stream with the footer at the bottom (one push), or,
        when the stream already stopped at STREAM_SECONDS, post a closing message with it (the
        second push). True when the reply ended on Slack (or nothing was owed); False when a
        write failed and one retry is scheduled: `wait_landed` then says how it ended. A second
        call is a no-op: the reply has already ended."""
        if self._closed_out:
            return self._landed.done() and self._landed.result()
        self._closed_out = True
        self._footer = footer
        if await self._flush():
            self._resolve(True)
            return True
        self._retry = asyncio.create_task(self._retry_final())
        return False

    def _resolve(self, landed: bool) -> None:
        if landed:
            self._settle_open_reply()
        if not self._landed.done():
            self._landed.set_result(landed)

    async def wait_landed(self) -> bool:
        """Whether the reply ends up on Slack: waits for the retry a failed end schedules."""
        return await asyncio.shield(self._landed)

    async def _retry_final(self) -> None:
        await asyncio.sleep(FINAL_RETRY_SECONDS)
        # Shielded: a shutdown's `settle` cancels this task, and a write cancelled after Slack
        # took it would lose what it did.
        self._resolve(await asyncio.shield(self._flush()))

    async def settle(self) -> bool:
        """Force this reply to its current, true form right now, cancelling any debounce still
        pending: a shutdown's very last chance, since `asyncio.run`'s own exit never lets a
        `_later` still waiting on its own timer get to run. Cheap when nothing changed since
        the last write. A retry still waiting is tried now. True when the reply is on Slack (or
        nothing was owed): a shutdown that gets False has lost it."""
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None
        if self._retry is not None and not self._retry.done():
            self._retry.cancel()
        ok = await self._flush()
        if self._closed_out and not self._landed.done():
            self._resolve(ok)
        return ok or (self._landed.done() and self._landed.result())

    async def _changed(self) -> None:
        """A background task or subagent can still update its own card after the reply itself
        ended (rare, and possibly during shutdown): debounced through `_later`, like any other
        change, rather than flushed here and now, which would spend a limiter wait (real time,
        under a busy process's shared budget) inline on the caller, the SDK reader loop among
        them."""
        self._version += 1
        self._schedule()

    def _schedule(self) -> None:
        if self._pending is None or self._pending.done():
            self._pending = asyncio.create_task(self._later())

    async def _later(self) -> None:
        while True:
            await asyncio.sleep(DEBOUNCE_SECONDS)
            version = self._version
            # Shielded: `finish` cancels a pending write, and a write cancelled after Slack
            # took it would lose the message's ts. `finish` waits for the lock instead.
            if not await asyncio.shield(self._flush()):
                return
            if self._version == version:
                return
            # The reply changed again while that call wrote or waited its turn, in a way it
            # never saw: another debounce, then flush again, so it still catches up rather than
            # waiting for the next unrelated event to notice. The debounce stays inside the
            # loop: while text keeps streaming, the version moves during every round trip, and
            # without it here a reply would be written after every round trip instead of at
            # most once per DEBOUNCE_SECONDS.

    # The reply's model: the parts in order, cut into messages at cursors.

    def _has_content(self) -> bool:
        return any(isinstance(p, _Tool) or p.text.strip() for p in self._parts)

    def _span(
        self, start: Cursor, end: Cursor | None
    ) -> list[tuple[int, _Text | _Tool, int, int | None]]:
        """The parts from `start` to `end` (the model's end when None), each with the first and
        last element the span takes of it."""
        last = len(self._parts) if end is None else min(len(self._parts), end[0] + 1)
        return [
            (
                index,
                self._parts[index],
                start[1] if index == start[0] else 0,
                end[1] if end is not None and index == end[0] else None,
            )
            for index in range(start[0], last)
        ]

    @staticmethod
    def _tool_elements(tool: _Tool, floor: int, ceil: int | None) -> tuple[bool, range]:
        """Whether a span takes a tool's card, and which of its preview pieces."""
        extent = tool.extent
        top = extent if ceil is None else min(extent, ceil)
        return floor == 0 and top > 0, range(max(floor, 1), top)

    def _closing_blocks(self) -> list[dict[str, Any]]:
        """The footer and what still runs, below a divider. Only the thread's latest reply shows
        them; empty when there is nothing to show."""
        footer = (self._footer, self._running) if self._latest else ()
        last_line = " · ".join(filter(None, footer))
        if not last_line:
            return []
        return [{"type": "divider"}, context_block(last_line)]

    def _footer_of(self, message: _Message) -> list[dict[str, Any]]:
        """The footer a stopped message shows: only the reply's last, and only when the footer
        rode on its stream's stop."""
        if self._end_mode == "inline" and message is self._messages[-1]:
            return self._closing_blocks()
        return []

    def _banner(self, span: list[tuple[int, _Text | _Tool, int, int | None]] | None = None) -> str:
        """The notification's text, plain: the first paragraph of Claude's own words in the
        span; else the first tool's title; else a line of the daemon's. Never a daemon line
        while there is something of Claude's to show. Cut to BANNER_LIMIT."""
        span = self._span((0, 0), None) if span is None else span
        notice = ""
        for _, part, floor, ceil in span:
            if isinstance(part, _Text):
                words = part.text[floor:ceil].strip()
                if words and not part.notice:
                    return banner_text(words.split("\n\n", 1)[0])[:BANNER_LIMIT] or "…"
                notice = notice or words
        for _, part, _, _ in span:
            if isinstance(part, _Tool):
                return banner_text(card_fields(part.update)["title"])[:BANNER_LIMIT] or "…"
        return banner_text(notice.split("\n\n", 1)[0])[:BANNER_LIMIT] or "…"

    # What a stream is told.

    def _plan(self, message: _Message) -> _Plan:
        """What the message's stream lacks: the cards that changed since they were sent, then
        what the model has past what it was sent, in order, as far as the message holds."""
        plan = _Plan(size=message.size, count=message.count)
        for tool_id, sent in message.cards.items():
            chunk = card_chunk(self._tools[tool_id].update)
            if chunk != sent:
                plan.chunks.append(chunk)
                plan.cards[tool_id] = chunk
        for index, part, floor, _ in self._span(message.start, None):
            if isinstance(part, _Text):
                more = self._plan_text(message, plan, index, part, floor)
            else:
                more = self._plan_tool(message, plan, index, part, floor)
            if not more:
                break
        return plan

    def _plan_text(
        self, message: _Message, plan: _Plan, index: int, part: _Text, floor: int
    ) -> bool:
        """Add the unsent tail of a text part to the plan; False when the plan is complete."""
        sent = max(message.text_sent.get(index, 0), floor)
        tail = part.text[sent:]
        lead = 0
        if plan.size == 0:  # a message opens on its words, not on the blank lines before them
            lead = len(tail) - len(tail.lstrip("\n"))
            tail = tail[lead:]
        if not tail.strip():
            if index == len(self._parts) - 1 and not self._finished:
                return False  # more of it may come: the blanks go with it
            plan.text[index] = len(part.text)
            return True
        new = index not in message.counted and index not in plan.counted
        if new and plan.count >= BLOCKS_LIMIT:
            plan.overflow = (index, sent)
            return False
        room = MESSAGE_LIMIT - plan.size
        if len(tail) <= room or not tail[room:].strip():
            # All of it, or all but blanks, which go with what follows if anything does.
            piece = tail[:room]
            plan.text[index] = sent + lead + len(piece)
        else:
            cut = tail.rfind("\n", 0, room) if room > 0 else -1
            end = room if cut <= 0 else cut
            piece = tail[:end]
            plan.text[index] = sent + lead + end + (1 if cut > 0 else 0)
            plan.overflow = (index, plan.text[index])
        if piece.strip():
            plan.chunks.append({"type": "markdown_text", "text": piece})
            plan.size += len(piece)
            if new:
                plan.count += 1
                plan.counted.add(index)
        return plan.overflow is None

    def _plan_tool(
        self, message: _Message, plan: _Plan, index: int, tool: _Tool, floor: int
    ) -> bool:
        """Add a tool's card, if this message holds it and has not sent it, and the pieces of its
        preview it has not sent; False when the plan is complete."""
        update = tool.update
        has_card, pieces = self._tool_elements(tool, floor, None)
        if has_card and update.id not in message.cards and update.id not in plan.cards:
            if plan.count >= BLOCKS_LIMIT:
                plan.overflow = (index, 0)
                return False
            plan.cards[update.id] = card_chunk(update)
            plan.chunks.append(plan.cards[update.id])
            plan.count += 1
        for piece in pieces:
            if (index, piece) in message.pieces_sent:
                continue
            size = len(tool.pieces()[piece - 1])
            if plan.count >= BLOCKS_LIMIT or plan.size + size > MESSAGE_LIMIT:
                plan.overflow = (index, piece)
                return False
            plan.chunks.append(piece_chunk(tool, piece))
            plan.pieces.add((index, piece))
            plan.size += size
            plan.count += 1
        return True

    @staticmethod
    def _sent(message: _Message, plan: _Plan) -> None:
        message.text_sent.update(plan.text)
        message.pieces_sent |= plan.pieces
        message.cards.update(plan.cards)
        message.size, message.count = plan.size, plan.count
        message.counted |= plan.counted

    def _stream_shows(self, message: _Message, end: Cursor | None) -> bool:
        """Whether the message's stream, as sent, shows what the model says for its span: every
        card as it is now, and ended (a card left in progress in a stopped stream is stored as
        an error until it is updated: measured 2026-09-28), every preview piece, all the text."""
        for tool_id, sent in message.cards.items():
            update = self._tools[tool_id].update
            if update.status not in TERMINAL or card_chunk(update) != sent:
                return False
        for index, part, floor, ceil in self._span(message.start, end):
            if isinstance(part, _Text):
                sent_to = max(message.text_sent.get(index, 0), floor)
                if part.text[sent_to:ceil].strip():
                    return False
            else:
                has_card, pieces = self._tool_elements(part, floor, ceil)
                if has_card and part.update.id not in message.cards:
                    return False
                if any((index, piece) not in message.pieces_sent for piece in pieces):
                    return False
        return True

    # What a message shows once it is no longer a stream: blocks.

    def _blocks(
        self, message: _Message, end: Cursor | None
    ) -> tuple[list[dict[str, Any]], Cursor | None]:
        """The blocks of the message's span: Claude's text as markdown, a task card per tool,
        the preview blocks after it. With no `end` (the reply's last message) only as many as fit
        MESSAGE_LIMIT and BLOCKS_LIMIT, and where the reply goes on if it does not all fit."""
        blocks: list[dict[str, Any]] = []
        size = 0
        for index, part, floor, ceil in self._span(message.start, end):
            if isinstance(part, _Text):
                raw = part.text[floor:ceil]
                lead = len(raw) - len(raw.lstrip("\n"))
                tail = raw[lead:]
                words = tail.strip("\n")
                if not words:
                    continue
                if end is None and len(blocks) >= BLOCKS_LIMIT:
                    return blocks, (index, floor + lead)
                room = MESSAGE_LIMIT - size
                if end is None and len(words) > room:
                    cut = words.rfind("\n", 0, room) if room > 0 else -1
                    stop = room if cut <= 0 else cut
                    if stop > 0:
                        blocks.append({"type": "markdown", "text": words[:stop]})
                    return blocks, (index, floor + lead + stop + (1 if cut > 0 else 0))
                blocks.append({"type": "markdown", "text": words})
                size += len(words)
            else:
                has_card, pieces = self._tool_elements(part, floor, ceil)
                if has_card:
                    if end is None and len(blocks) >= BLOCKS_LIMIT:
                        return blocks, (index, 0)
                    blocks.append(card_block(part.update))
                for piece in pieces:
                    shown = piece_blocks(part, piece)
                    length = len(part.pieces()[piece - 1])
                    if end is None and (
                        len(blocks) + len(shown) > BLOCKS_LIMIT or size + length > MESSAGE_LIMIT
                    ):
                        return blocks, (index, piece)
                    blocks += shown
                    size += length
        return blocks, None

    async def _update_step(
        self, message: _Message, end: Cursor | None
    ) -> tuple[bool, Cursor | None]:
        """Bring a stopped message to what the model says, with a `chat.update` when it shows
        something else: (written, where the reply goes on past the message). An update never
        notifies (measured 2026-09-29). Nothing is written for a message whose stream already
        shows the model."""
        assert message.ts is not None
        blocks, overflow = self._blocks(message, end)
        footer = self._footer_of(message)
        blocks += footer
        if not blocks or self._current(message, blocks, footer, end):
            return True, overflow
        try:
            await self._limiter.acquire()
            # A change can arrive while this write waits its turn: send what the reply looks
            # like right now rather than the snapshot taken before the wait.
            fresh, overflow = self._blocks(message, end)
            footer = self._footer_of(message)
            fresh += footer
            if not fresh or self._current(message, fresh, footer, end):
                await self._limiter.refund()  # caught up: no write follows
                return True, overflow
            blocks = fresh
            await self._slack.chat_update(
                channel=self._channel,
                ts=message.ts,
                text=self._banner(self._span(message.start, end)),
                blocks=blocks,
            )
        except Exception as exc:
            logger.warning("could not write a reply to Slack: %s", describe(exc))
            code = describe(exc)
            if code == STILL_STREAMING:
                # The daemon's stop never reached Slack: stopped now, the next write passes.
                await self._stop(message, None, end)
            elif code in REFUSED_CONTENT:
                # The message already shows what it showed: never replaced by a plainer one.
                # The change is dropped; the next one is tried.
                message.shown, message.exact, message.footer = blocks, False, footer
                return True, overflow
            return False, None
        message.shown, message.exact, message.footer = blocks, False, footer
        return True, overflow

    def _current(
        self,
        message: _Message,
        blocks: list[dict[str, Any]],
        footer: list[dict[str, Any]],
        end: Cursor | None,
    ) -> bool:
        """Whether the message already shows `blocks`."""
        if message.exact:
            return message.footer == footer and self._stream_shows(message, end)
        return message.shown == blocks

    async def _write_plain(self, message: _Message, blocks: list[dict[str, Any]]) -> bool:
        """Slack refused the blocks of a message not yet posted: without this one retry it would
        never show, so it is posted as text alone. Never used on a message that shows something
        already, which a plainer form would replace."""
        try:
            posted = await self._slack.chat_postMessage(
                channel=self._channel,
                thread_ts=self._thread_ts,
                text=plain_text(blocks),
                unfurl_links=False,
                unfurl_media=False,
            )
        except Exception as exc:
            logger.warning("could not write a reply to Slack as plain text: %s", describe(exc))
            return False
        message.ts = str(posted["ts"])
        message.shown, message.exact, message.footer = [], False, []
        self._retrack()
        return True

    async def _post_step(self, message: _Message) -> tuple[bool, Cursor | None]:
        """Post the message that continues a stopped one, with the blocks of its span."""
        blocks, overflow = self._blocks(message, None)
        footer = self._footer_of(message)
        blocks += footer
        if not blocks:
            return True, overflow
        try:
            # Claude's text can carry a link built to leak data when Slack fetches it for a
            # preview: no previews for anything the daemon posts.
            posted = await self._slack.chat_postMessage(
                channel=self._channel,
                thread_ts=self._thread_ts,
                text=self._banner(self._span(message.start, None)),
                blocks=blocks,
                unfurl_links=False,
                unfurl_media=False,
            )
        except Exception as exc:
            logger.warning("could not write a reply to Slack: %s", describe(exc))
            if (
                self._closed_out
                and describe(exc) in REFUSED_CONTENT
                and await self._write_plain(message, blocks)
            ):
                return True, overflow
            return False, None
        message.ts = str(posted["ts"])
        message.shown, message.footer = blocks, footer
        self._retrack()
        return True, overflow

    async def _stream_step(self, message: _Message) -> tuple[bool, Cursor | None]:
        """Tell the message's stream what it lacks, starting it with the first content: (written,
        where the reply goes on past the message)."""
        plan = self._plan(message)
        if not plan.chunks:
            return True, plan.overflow
        if message.ts is None:
            try:
                started = await self._slack.chat_startStream(
                    channel=self._channel,
                    thread_ts=self._thread_ts,
                    recipient_team_id=self._team_id,
                    recipient_user_id=self._user_id,
                    chunks=plan.chunks,
                    task_display_mode="timeline",
                )
            except Exception as exc:
                logger.warning(
                    "could not start a reply's stream: %s%s",
                    describe(exc),
                    " (outcome unknown: a stream may be open that this reply never learned of)"
                    if unknown_outcome(exc)
                    else "",
                )
                return False, None
            message.ts, message.streaming = str(started["ts"]), True
            self._sent(message, plan)
            self._retrack()
            message.deadline = asyncio.create_task(self._expire(message))
            return True, plan.overflow
        try:
            await self._limiter.acquire()
            # A change can arrive while this write waits its turn: tell the stream what it
            # lacks right now, not the snapshot taken before the wait.
            plan = self._plan(message)
            if not plan.chunks:
                await self._limiter.refund()  # caught up: no write follows
                return True, plan.overflow
            await self._slack.chat_appendStream(
                channel=self._channel, ts=message.ts, chunks=plan.chunks
            )
        except Exception as exc:
            logger.warning("could not write a reply to Slack: %s", describe(exc))
            if describe(exc) == NOT_STREAMING:
                # Slack ended the stream first: the message goes on by update.
                self._gone(message)
                return await self._update_step(message, None)
            if unknown_outcome(exc):
                # An append is not idempotent: sent again it may show twice. The stream is told
                # nothing more; the message is stopped and goes on by update, from the model.
                logger.warning(
                    "a stream append's outcome is unknown: the message goes on by update"
                )
                message.blind = True
            return False, None
        self._sent(message, plan)
        return True, plan.overflow

    def _gone(self, message: _Message) -> None:
        """The message's stream is over, however it ended."""
        message.streaming = False
        if message.deadline is not None and message.deadline is not asyncio.current_task():
            message.deadline.cancel()
        message.deadline = None

    async def _stop(
        self, message: _Message, blocks: list[dict[str, Any]] | None, end: Cursor | None
    ) -> str:
        """Stop the message's stream, with `blocks` at its bottom: "stopped", "gone" (Slack had
        ended it already) or "failed". A stop notifies; what the stream still lacks is left for
        the update that follows."""
        assert message.ts is not None
        args: dict[str, Any] = {"channel": self._channel, "ts": message.ts}
        if blocks:
            args["blocks"] = blocks
        result = "stopped"
        try:
            await self._slack.chat_stopStream(**args)
        except Exception as exc:
            if describe(exc) != NOT_STREAMING:
                logger.warning("could not stop a reply's stream: %s", describe(exc))
                if unknown_outcome(exc) and blocks and message.stop_unknown is None:
                    message.stop_unknown = list(blocks)
                return "failed"
            if message.stop_unknown is not None:
                # The stop that failed on the connection is the one that landed, footer and all.
                blocks, result = message.stop_unknown, "stopped"
            else:
                result = "gone"  # over without a footer of ours: the end posts one
            message.stop_unknown = None
        self._gone(message)
        message.exact = self._stream_shows(message, end)
        message.footer = list(blocks or []) if result == "stopped" else []
        return result

    async def _expire(self, message: _Message) -> None:
        """STREAM_SECONDS after the stream started: stop it before Slack does, and go on by
        update. The stop notifies (accepted); the updates after it never do."""
        await self._clock.sleep(STREAM_SECONDS)
        async with self._lock:
            if not message.streaming:
                return
            if await self._stop(message, None, None) == "failed":
                return  # Slack ends it at 5 minutes: the next append finds it over
            await self._sync()

    # The one place a reply is written.

    async def _flush(self) -> bool:
        """Write what changed; False when a write failed and the reply is not as it should be.
        A write can take real time (the limiter, a round trip), during which the reply can
        change again: what is written is what the reply looks like right now, never the
        snapshot taken before the wait, so a wait never drops a change. `_later` runs this
        again when `self._version` moved during it."""
        async with self._lock:
            ok = await self._sync()
            if ok and self._closed_out:
                self._settle_open_reply()
            return ok

    async def _sync(self) -> bool:
        """Bring every message of the reply to the model, then, once the reply has ended, end
        it, and follow which messages a crash would leave unfinished. The lock is held."""
        try:
            return await self._sync_messages()
        finally:
            self._retrack()

    async def _sync_messages(self) -> bool:
        if not self._messages:
            if not self._has_content():
                return await self._end() if self._closed_out else True
            self._messages.append(_Message((0, 0), "stream"))
        for message, following in itertools.pairwise(self._messages):
            if (
                message.ts is not None
                and not (await self._update_step(message, following.start))[0]
            ):
                return False
        while True:
            message = self._messages[-1]
            if message.streaming and message.blind:
                # Told nothing more: stopped now (with the footer, if the reply has ended), and
                # the update below writes the whole message from the model.
                footer = self._closing_blocks() if self._closed_out else None
                result = await self._stop(message, footer, None)
                if result == "failed":
                    return False
                if self._closed_out and result == "stopped":
                    self._end_mode = "inline"
                message.exact = False
            if message.ts is None:
                step = self._stream_step if message.mode == "stream" else self._post_step
                ok, overflow = await step(message)
            elif message.streaming:
                ok, overflow = await self._stream_step(message)
            else:
                ok, overflow = await self._update_step(message, None)
            if not ok:
                return False
            if overflow is None:
                break
            if overflow == message.start:
                logger.error("a reply's message cannot hold its first element")
                return False
            mode = "stream" if message.streaming else "post"
            if message.streaming and await self._stop(message, None, overflow) == "failed":
                return False
            self._messages.append(_Message(overflow, mode))
        return await self._end() if self._closed_out else True

    async def _end(self) -> bool:
        """The reply's end, on Slack: the footer on the last stream's stop, or in a closing
        message once the stream is over."""
        if not self._messages:
            return True  # nothing was ever shown: nothing to end
        if self._end_mode == "inline":
            return True  # the stream's stop carried the footer; a later change edits the message
        message = self._messages[-1]
        if message.streaming:
            result = await self._stop(message, self._closing_blocks(), None)
            if result == "failed":
                return False
            if result == "stopped":
                self._end_mode = "inline"
                return True
        self._end_mode = "post"
        self._body_landed = True  # every message is written: only the closing message is owed
        return await self._write_closing()

    async def _write_closing(self) -> bool:
        """Post the closing message of a reply whose stream stopped early, or bring it to the
        footer as it stands: it stays once posted, since it is what notified. Its text is
        Claude's own words, as a banner: never a line of the daemon's."""
        blocks = self._closing_blocks() or [context_block(ZERO_WIDTH_SPACE)]
        try:
            if self._closing is None:
                posted = await self._slack.chat_postMessage(
                    channel=self._channel,
                    thread_ts=self._thread_ts,
                    text=self._banner(),
                    blocks=blocks,
                    unfurl_links=False,
                    unfurl_media=False,
                )
                self._closing = str(posted["ts"])
            elif blocks != self._closing_shown:
                # An edit never notifies (measured 2026-09-27): the text stays as posted.
                await self._limiter.acquire()
                await self._slack.chat_update(
                    channel=self._channel, ts=self._closing, text=self._banner(), blocks=blocks
                )
        except Exception as exc:
            logger.warning("could not write a reply's closing message: %s", describe(exc))
            return False
        self._closing_shown = blocks
        return True
