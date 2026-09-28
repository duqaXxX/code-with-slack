"""Write a rendered reply to Slack: one message in the Claude Code session's Slack thread,
rewritten with chat.update as the reply grows, at most once per DEBOUNCE_SECONDS (chat.update
allows "50+ per minute", Tier 3), rather than through Slack's own native streaming
(`chat.startStream`). Every `chat.update` also waits its turn on an `UpdateLimiter` shared by
every reply in the process: a token bucket that paces writes evenly under the app's own budget,
rather than letting several busy threads exhaust it together and then freeze until it resets.
"""

import asyncio
import itertools
import logging
import re
import time
from collections.abc import Callable
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


# The reply's own status line (Claude is writing, or waiting): a fixed block_id, so crash repair
# can find and drop it by shape rather than by matching its rendered text (issue #19 fix round: a
# block posted with no id of its own comes back from Slack with one Slack assigned, so "a context
# block with no block_id" is not a shape a read-back message ever actually has).
STATUS_BLOCK_ID = "status"


def status_block(text: str) -> dict[str, Any]:
    return {**context_block(text), "block_id": STATUS_BLOCK_ID}


# A text object's limit, as a context block's mrkdwn element holds one (Block Kit reference).
CONTEXT_LIMIT = 3000


def notice_text(text: str) -> str:
    """A daemon notice fitted into one context element: cut with `…` past its limit."""
    return text if len(text) <= CONTEXT_LIMIT else text[: CONTEXT_LIMIT - 1] + "…"


# Invisible, so a block that holds only this one shows no text of its own; never pasted as a
# literal character in source, always this escape.
ZERO_WIDTH_SPACE = "\u200b"


# An empty line before the footer's divider and after the footer, so replies stand apart: Slack
# blocks have no margin setting, so a context block holding only a zero-width space makes the gap.
def spacer(where: str) -> dict[str, Any]:
    # Each spacer has its own block_id: Slack refuses a message that repeats one (invalid_blocks).
    return {
        "type": "context",
        "block_id": f"spacer-{where}",
        "elements": [{"type": "mrkdwn", "text": ZERO_WIDTH_SPACE}],
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


class ReplySink:
    """One reply in a Slack thread, written in the order things happen: text, then a line per
    tool where it ran, updated in place. The last line shows a status (Claude is writing, or
    waiting for the previous reply) until the reply ends. A reply past MESSAGE_LIMIT continues
    in a new message. `finish` writes the body's final form only: a task that outlives the turn
    keeps updating its own line after that, in place, since Claude Code can report on it again.
    The end is a closing message of its own (a divider and the footer), posted by `close_out`
    once the caller knows nothing more is coming: a new message is what notifies here, so a
    reply that must reach the owner still posts one even with nothing else to show. Never
    raises: a write that fails is retried with the whole reply at the next flush, the final one
    after FINAL_RETRY_SECONDS, and the session goes on."""

    def __init__(
        self,
        slack: AsyncWebClient,
        *,
        channel: str,
        thread_ts: str,
        limiter: UpdateLimiter,
        on_open_reply: Callable[[str | None, str | None], None] | None = None,
    ) -> None:
        self._slack = slack
        self._channel = channel
        self._thread_ts = thread_ts
        self._limiter = limiter
        # Crash repair (issue #19): `(old_ts, new_ts)`, this sink's own transition in the
        # thread's open-replies list (more than one sink can be open at once: a background
        # task's own reply can outlive the turn that started it, so each sink owns exactly one
        # entry and must never touch another's). A plain sync callback (`StateStore`'s setters
        # are sync file writes), never awaited here.
        self._on_open_reply = on_open_reply
        # This sink's own current entry in that list, or None once it has none (not yet posted,
        # or already settled/given up).
        self._own_open_reply: str | None = None
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
        self._closed_out = False  # whether close_out has run; a second call is a no-op
        self._closing_retry: asyncio.Task[None] | None = None
        # D1: a silent close's footer, if any, joins the body's own last
        # message instead of a message of its own: any new message in a thread the owner
        # started notifies, whatever it says, but an edit never does.
        self._silent_closed = False
        # Bumped by `_changed`: `_flush` compares it before and after a pass to notice a change
        # that arrived while the pass wrote or waited its turn, and runs another pass for it.
        self._version = 0

    def _track_open_reply(self, new_ts: str | None) -> None:
        """This sink's own transition: drop its current entry (if any), add `new_ts` (if not
        None), leave every other sink's entry alone. Best-effort (issue #19 fix round item 7): a
        failed `StateStore` write is logged (ids only) and swallowed, since the reply itself must
        never fail over crash-repair bookkeeping; this sink's own local view of `new_ts` still
        updates either way, so it stays correct even if the file write did not land."""
        old = self._own_open_reply
        self._own_open_reply = new_ts
        if self._on_open_reply is None:
            return
        try:
            self._on_open_reply(old, new_ts)
        except Exception as exc:
            logger.warning(
                "could not update the open-reply tracking for %s/%s: %s",
                self._channel,
                self._thread_ts,
                describe(exc),
            )

    def _settle_open_reply(self) -> None:
        """Stop tracking once the body's final write is known to have landed, or the sink has
        given up retrying it for good (issue #19 fix round item 6): from then on the message
        shows no status line to repair, whatever else may still change on it."""
        if self._own_open_reply is not None:
            self._track_open_reply(None)

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
        """Show what still runs in this thread (`⏳ 1 shell · 1 agent`) after this reply's status
        or footer, or on a line of its own; empty removes it. Only the thread's latest reply
        shows one."""
        if counts == self._running:
            return
        self._running = counts
        if self._finished or self._messages:
            await self._changed()

    async def set_latest(self, latest: bool) -> None:
        """Only the thread's latest reply shows the footer and the running counts, at the
        bottom of the thread as the terminal's status line; an older one drops them."""
        if latest == self._latest:
            return
        self._latest = latest
        if self._finished:
            await self._changed()

    async def finish(self, closing: list[TaskUpdate]) -> None:
        """End the reply's body. A line still in progress is a task that outlives the turn:
        `task` keeps updating it after this, in place. The closing message (the footer, the
        notification) is not written here: it comes from `close_out`, once the caller knows
        nothing more is coming (a task can still outlive this very turn)."""
        for update in closing:
            await self.task(update)
        if self._pending is not None:
            self._pending.cancel()
            # Cleared now, not left for `_schedule` to find: `cancel` only requests it, so the
            # task can still read `.done()` as False for a while yet (real production code
            # always has an await in between; a change right after `finish`, with none, would
            # not). `_schedule` must not mistake it for a debounce already in flight.
            self._pending = None
        self._finished = True
        if await self._flush(final=True):
            self._settle_open_reply()
        else:
            self._retry = asyncio.create_task(self._retry_final())

    async def _retry_final(self) -> None:
        await asyncio.sleep(FINAL_RETRY_SECONDS)
        await self._flush(final=True)
        # This is the reply's one and only retry for its final write: whether it just landed or
        # failed again, nothing else will try again, so tracking stops either way (issue #19 fix
        # round item 6, "or when the sink gives up for good").
        self._settle_open_reply()

    async def close_out(
        self, footer: str | None, reply_to: str | None = None, *, silent: bool = False
    ) -> None:
        """Post the closing message: the footer and what still runs, once, below the body.
        With `reply_to` (the owner's question, one line) it notifies the owner; without it the
        end is silent. With `silent` the closing never becomes a message of its own either
        (D1): even a footer with no notification to carry would still be a NEW
        message, and any new message in a thread the owner started notifies, whatever it says;
        an edit never does (measured 2026-09-27). The footer, if any, joins the body's own last
        message instead, through the ordinary flush path (`_render`, the limiter, retries). A
        second call, silent or not, is a no-op: the reply has already closed."""
        if self._closed_out:
            return
        self._closed_out = True
        self._footer = footer
        if silent:
            self._silent_closed = True
            # Crash repair (issue #19): only once this write (which folds the footer into the
            # body's own last message) is known to have landed does the message stop showing a
            # status line to repair; a failed write keeps tracking it, same as `finish`'s own.
            if await self._flush(final=True):
                self._settle_open_reply()
            else:
                self._retry = asyncio.create_task(self._retry_final())
            return
        self._reply_to = reply_to
        # A reply already superseded by the time it closes out still owes its notification:
        # nothing later takes that back, even once `set_latest` drops the footer for good.
        self._notify_kept = not self._latest
        async with self._lock:
            if not await self._write_closing():
                self._closing_retry = asyncio.create_task(self._retry_closing())

    async def settle(self) -> None:
        """Force this reply to its current, true form right now, cancelling any debounce still
        pending: a shutdown's very last chance, since `asyncio.run`'s own exit never lets a
        `_later` still waiting on its own timer get to run. Cheap when nothing changed since
        the last write: `_flush` itself no-ops then."""
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None
        if await self._flush(final=self._finished) and self._finished:
            self._settle_open_reply()

    async def _retry_closing(self) -> None:
        await asyncio.sleep(FINAL_RETRY_SECONDS)
        async with self._lock:
            await self._write_closing()

    async def _changed(self) -> None:
        """A background task or subagent can still update its own line after the reply itself
        finished (rare, and possibly during shutdown): debounced through `_later`, like any
        other change, rather than flushed here and now, which would spend a limiter wait (real
        time, under a busy process's shared budget) inline on the caller, the SDK reader loop
        among them. `finish` and `close_out` write the final form themselves, right when each
        decides there is one."""
        self._version += 1
        self._schedule()

    def _schedule(self) -> None:
        if self._pending is None or self._pending.done():
            self._pending = asyncio.create_task(self._later())

    async def _later(self) -> None:
        while True:
            await asyncio.sleep(DEBOUNCE_SECONDS)
            version = self._version
            # `final` is read now, not fixed at `_changed`'s own time: once the reply has
            # finished, every further debounced pass writes the current, final form (no status
            # line; the closing blocks too, once a silent close folded them into the body).
            final = self._finished
            # Shielded: `finish` cancels a pending rewrite, and a write cancelled after Slack
            # took it would lose the message's ts. `finish` waits for the lock instead.
            if not await asyncio.shield(self._flush(final=final)):
                return
            if final:
                self._settle_open_reply()
            if self._version == version:
                return
            # The reply changed again while that call wrote or waited its turn, in a way it
            # never saw (e.g. it grew into a message it did not know it would need): another
            # debounce, then flush again, so it still catches up rather than waiting for the
            # next unrelated event to notice. The debounce stays inside the loop: while text
            # keeps streaming, the version moves during every round trip, and without it here
            # a reply would be rewritten after every round trip instead of at most once per
            # DEBOUNCE_SECONDS.

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
            if status:  # neither set (rare, before the first `open`): no status line at all
                messages[-1].append(status_block(status))
        elif self._silent_closed:
            # A silent close (D1): the footer, if any, joins the body's own
            # last message instead of a message of its own, through the ordinary flush below.
            messages[-1] += self._closing_blocks()
        return messages

    def _closing_blocks(self) -> list[dict[str, Any]]:
        """The closing message: the footer and what still runs. Only the thread's latest reply
        shows them, so a newer reply removes the closing message, unless it was already newer
        when this one ended: a reply the owner asked something still has to post, since a new
        message in the thread is what notifies here, so a bare line stands in for the footer.
        Empty: no closing message."""
        notifies = self._reply_to is not None and (self._latest or self._notify_kept)
        footer = (self._footer, self._running) if self._latest else ()
        last_line = " · ".join(filter(None, footer))
        if not last_line:
            return [context_block(ZERO_WIDTH_SPACE)] if notifies else []
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
                await self._limiter.acquire()
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
            await self._limiter.acquire()
            await self._slack.chat_update(
                channel=self._channel, ts=self._messages[index], text=plain_text(blocks), blocks=[]
            )
        except Exception as exc:
            logger.warning("could not write a reply to Slack as plain text: %s", describe(exc))
            return False
        self._shown[index] = []
        return True

    async def _flush(self, *, final: bool) -> bool:
        """Write what changed; False when a write failed and the reply is not as rendered. A
        write can take real time (the limiter, a chat.update round trip), during which the reply
        can change again: send what it looks like right now for that index, not the snapshot
        taken before the wait, so a wait never drops a change. `_later` reschedules this call
        when `self._version` moved during it, which is how a change too big for this pass's own
        `rendered` to have known about (growing into a message it did not expect to need) still
        gets discovered and sent, on the next call's fresh render."""
        async with self._lock:
            if self._finished and not final:
                return True  # a draft that waited for the lock must not undo the final form
            rendered = self._render(final)
            if rendered == [[]]:
                rendered = []  # nothing left to show: the extra-message removal below takes it
            for index, blocks in enumerate(rendered):
                if not blocks:
                    continue
                # Compared before the limiter is ever asked for a token below: nothing awaits
                # between this render and that ask, so nothing else can change `_shown` in
                # between, and a message already showing this stays free.
                if index < len(self._shown) and self._shown[index] == blocks:
                    continue
                fallback = block_text(blocks[0])[:FALLBACK_LIMIT] or "…"
                try:
                    if index < len(self._messages):
                        await self._limiter.acquire()
                        if not final and self._finished:
                            await self._limiter.refund()  # no write follows: not spent for real
                            return True  # finish() ran while this draft waited its turn
                        # A change can arrive while this write waits its turn: send what the
                        # reply looks like right now rather than the snapshot taken before the
                        # wait. If it grew into a message this pass never saw coming, the version
                        # check below has `_later` call `_flush` again for that.
                        fresh = self._render(final)
                        if index < len(fresh) and fresh[index]:
                            blocks = fresh[index]
                        if blocks == self._shown[index]:
                            await self._limiter.refund()  # caught up: no write follows either
                            continue  # caught up while it waited: nothing left to send
                        fallback = block_text(blocks[0])[:FALLBACK_LIMIT] or "…"
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
                        # Crash repair (issue #19 fix round item 5): only while the reply is not
                        # already closed out. A closed-out reply's final render never carries a
                        # status line (`_render`'s not-final branch is the only place one is
                        # added), so a continuation posted after close_out has nothing repair
                        # would need to fix; tracking it anyway would leave a dangling entry
                        # nothing ever clears again.
                        if not self._closed_out:
                            self._track_open_reply(self._messages[-1])
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
                if not self._closed_out:
                    self._track_open_reply(self._messages[-1] if self._messages else None)
            # After the body, so the closing message is posted below it. Only once `close_out`
            # has run: before that, nothing is known about the footer or the notification yet.
            # A silent close has none of its own: `_render` already folded it into the body
            # above (D1).
            if final and self._closed_out and not self._silent_closed:
                return await self._write_closing()
            return True
