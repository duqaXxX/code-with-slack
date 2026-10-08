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

from code_with_slack import texts
from code_with_slack.render.escape import mrkdwn_escape, shown_as_written
from code_with_slack.render.fold import Fold
from code_with_slack.render.renderer import STOPPED, TaskUpdate

logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 1.0
# The final write has no next rewrite to fix it: one that fails for any reason but its content
# is tried once more after this pause (slack-sdk has already retried a rate limit by then).
FINAL_RETRY_SECONDS = 10.0
# A stream is closed by Slack 5 minutes after `chat.startStream` (measured 2026-09-28: refused at
# 300.3 s and 305 s); stopped by the daemon at this age, it leaves a margin for the round trip.
STREAM_SECONDS = 280.0
# A stream past this age is over, whatever the daemon did (M15, M32: refused at 300.3 s).
STREAM_LIFE = 300.0
# How far back a create of unknown outcome is looked for: the attempt's own clock, less this.
ADOPT_SKEW_SECONDS = 2.0
# How much of a message's first words is compared to know it is the one that was lost.
ADOPT_WORDS = 40
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
# A message holds at most 12,000 characters and 50 blocks or task cards (measured 2026-09-28),
# a text counting the blocks Slack makes of it (`markdown_starts`);
# the margins keep a preview that arrives after its card, and the footer, inside them. A stream
# and a post count the text of a collapsed container toward that cap; `chat.update` does not
# (measured 2026-10-06, slack-sdk 3.44.1: 50 containers of 10,000 characters taken, nothing ever
# refused). A message written by update therefore counts a container as its block only: at most
# BLOCKS_LIMIT containers of MESSAGE_LIMIT characters, 495,000, which is what was measured in the
# shape of a call with no card (45 of 11,000, taken).
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
CARD_TEXT_FIELDS = ("title", "details", "output")
# A stream keeps the details and the output of every `task_update` of a card, each added to what
# the card holds; an update that carries neither leaves them, and its title replaces the title
# (measured 2026-10-01 and 2026-10-08).
CARD_APPENDED = ("details", "output")
# What a card costs a streamed message besides its characters: the card, each of the two texts
# it holds, and each line of them. Slack stores a card's text as rich text, a line an element,
# and its cap follows what it stores. Replayed over the 15 streams of 2026-10-01 (slack-sdk
# 3.44.1), text and cards counted this way came to 13,514 at most in an accepted append and to
# 13,801 at least in a refused one; MESSAGE_LIMIT sits under both.
CARD_COST = 100
CARD_FIELD_COST = 150
CARD_LINE_COST = 50
# chat.update errors that refuse the content itself (reference, read 2026-09-25): a plain retry
# can pass where the blocks did not. A transient error such as `ratelimited` is not one.
REFUSED_CONTENT = {"invalid_blocks", "invalid_blocks_format", "msg_too_long", "invalid_arguments"}
# Slack's answers about a stream's state (measured 2026-09-28): it is over, or still open.
NOT_STREAMING = "message_not_in_streaming_state"
# The one refusal of an append's content measured on `chat.appendStream` (2026-10-01, slack-sdk
# 3.44.1), and the one a `chat.update` of the same message was seen to pass.
TOO_LONG = "msg_too_long"
STILL_STREAMING = "streaming_state_conflict"
# What stands where a preview arrived too late for its message.
PREVIEW_CUT = "Preview left out: it did not fit this message."
TERMINAL = ("complete", "error")


def describe(exc: Exception) -> str:
    """Slack's error code, or the exception type: never the request or its content."""
    if isinstance(exc, SlackApiError):
        return str(exc.response.get("error"))
    return type(exc).__name__


# What Slack says of a message whose blocks, once it has translated the markdown ones, pass 50
# (measured 2026-10-08 on `chat.update` and `chat.postMessage`, slack-sdk 3.45.0).
TOO_MANY_BLOCKS = "no more than 50 items allowed"
JSON_POINTER = re.compile(r"\[json-pointer:([^\]]*)\]")


def refusal_notes(exc: Exception) -> list[str]:
    """The sentences Slack adds to a refused write, which say what in the payload it refused."""
    if not isinstance(exc, SlackApiError):
        return []
    notes = (exc.response.get("response_metadata") or {}).get("messages") or []
    return [str(note) for note in notes]


def too_many_blocks(exc: Exception) -> bool:
    return any(TOO_MANY_BLOCKS in note for note in refusal_notes(exc))


def describe_refusal(exc: Exception) -> str:
    """`describe`, with where in the payload Slack pointed and whether it counted too many
    blocks: paths and a fixed phrase, never Slack's own sentence, which can quote a value."""
    words = describe(exc)
    where = sorted({path for note in refusal_notes(exc) for path in JSON_POINTER.findall(note)})
    if where:
        words += " at " + ", ".join(where)
    if too_many_blocks(exc):
        words += ", over 50 blocks once translated"
    return words


# Slack stores a `markdown` block, and a stream's `markdown_text`, as several blocks: a header
# per heading, a table per table, a divider per rule, and rich text for each run of anything
# else between them. A write by `chat.update` or `chat.postMessage` is refused when the message
# passes 50 of them; a stream is not, and the update that follows its stop is (measured
# 2026-10-08, slack-sdk 3.45.0). Quotes, lists, images, bold lines and code blocks stay in the
# rich text around them, and a `#` inside a code block is not a heading.
# Read as CommonMark and GFM define them where Slack was not measured (a heading underlined
# with `=`, a fence longer than three marks, a rule with spaces in it): a shape counted that
# Slack keeps in its rich text only ends a message early.
HEADING = re.compile(r" {0,3}#{1,6}(\s|$)")
UNDERLINE = re.compile(r" {0,3}=+\s*$")
RULE = re.compile(r" {0,3}([-*_])(\s*\1){2,}\s*$")
TABLE_RULE = re.compile(r"\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)$")


def markdown_starts(text: str) -> list[int]:
    """Where each block starts that Slack makes of a markdown text, as offsets into it."""
    starts: list[int] = []
    lines = text.split("\n")
    offset, fence, running, table = 0, "", False, False
    for number, line in enumerate(lines):
        here, offset = offset, offset + len(line) + 1
        line = line.rstrip("\r")
        mark = FENCE.match(line)
        if fence:
            # Closed by a run of the same mark, at least as long, with nothing after it.
            if mark and mark[1].startswith(fence) and not mark[2].strip():
                fence = ""
        elif mark and "`" not in mark[2]:
            fence, table = mark[1], False
        else:
            if not line.strip():
                table = False
                continue
            if table and "|" in line:
                continue
            table = False
            following = lines[number + 1].rstrip("\r") if number + 1 < len(lines) else ""
            if "|" in line and "|" in following and TABLE_RULE.match(following):
                starts.append(here)
                table, running = True, False
                continue
            if HEADING.match(line) or RULE.match(line) or (running and UNDERLINE.match(line)):
                starts.append(here)
                running = False
                continue
        if not running:
            starts.append(here)
            running = True
    return starts


def markdown_blocks(text: str) -> int:
    """How many blocks a markdown text with words in it counts in a message."""
    return max(1, len(markdown_starts(text)))


def markdown_cut(text: str, room: int, floor: int = 0) -> int | None:
    """Where to cut a markdown text so that it makes at most `room` blocks, at a block's start
    no earlier than `floor` (what a stream was already sent); None when nothing is to cut. A
    heading is never the last block before the cut, nor the last of a text that fills the room:
    it opens the next message, with the text it heads."""
    starts = markdown_starts(text)
    index: int | None = len(starts) if len(starts) == room else None
    if len(starts) > room:
        index = next((i for i in range(room, len(starts)) if starts[i] >= floor), None)
    if index is None:
        return None
    while index > 1 and starts[index - 1] >= floor and HEADING.match(text, starts[index - 1]):
        index -= 1
    return starts[index] if index < len(starts) else None


def blocks_count(blocks: list[dict[str, Any]]) -> int:
    """The blocks of a message as Slack counts them."""
    return sum(markdown_blocks(b["text"]) if b["type"] == "markdown" else 1 for b in blocks)


CREATING_METHODS = (
    "chat.startStream",
    "chat.appendStream",
    "chat.stopStream",
    "chat.postMessage",
)


class ConnectionRetryUnlessCreating(AsyncConnectionErrorRetryHandler):
    """slack-sdk's retry of a call that failed on the connection, except for the calls that
    create or grow a message: a start, an append, a stop and a post are not idempotent, and a
    reset can come after Slack applied the call, so a retry would duplicate a message or its
    text, or stop a stream twice. The sink reads the thread back instead, and adopts what
    landed."""

    async def _can_retry_async(
        self,
        *,
        state: RetryState,
        request: HttpRequest,
        response: Any = None,
        error: Exception | None = None,
    ) -> bool:
        if request.url.rstrip("/").endswith(CREATING_METHODS):
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

    def time(self) -> float:
        """Wall-clock seconds, as a Slack ts counts them."""
        return time.time()


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
            return
    # Issue #71: a message ts is its post time in epoch seconds, so the age of a request when it
    # goes is read from the log with no timer kept for it. Ids and a duration only.
    try:
        age = f"{time.time() - float(ts):.1f}s after it was posted"
    except ValueError:
        age = "age unknown"
    logger.info("removed a request in %s: message %s, %s", channel, ts, age)


def context_block(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def plain_text_object(text: str, *, emoji: bool | None = None) -> dict[str, Any]:
    """A `plain_text` text object. `emoji` False says `:name:` in `text` is not to be turned into
    an emoji (text object reference, read 2026-10-06); left out when None."""
    shown: dict[str, Any] = {"type": "plain_text", "text": text}
    if emoji is not None:
        shown["emoji"] = emoji
    return shown


# A text object's limit, as a context block's mrkdwn element holds one (Block Kit reference).
CONTEXT_LIMIT = 3000


def notice_text(text: str) -> str:
    """A daemon notice fitted into one context element: cut with `…` past its limit."""
    return text if len(text) <= CONTEXT_LIMIT else text[: CONTEXT_LIMIT - 1] + "…"


def plain_lines(body: str) -> str:
    """A preview's lines of words as one context element's text: each indented under its card
    and shown as written, the whole cut with `…` past the element's limit."""
    lines = (texts.NESTED + shown_as_written(line) for line in body.split("\n"))
    return notice_text("\n".join(lines))


# Invisible, so a block that holds only this one shows no text of its own; never pasted as a
# literal character in source, always this escape.
ZERO_WIDTH_SPACE = "\u200b"


def preview_blocks(body: str) -> list[dict[str, Any]]:
    """A new file's first lines as code blocks, split where a block would pass its limit. A fence
    inside the file must not close the block early: every run of three or more backticks is
    broken up, as `escape.shown_as_written` breaks every one."""
    body = re.sub(r"`{3,}", lambda run: "\u200b".join(run.group()), body)
    return [{"type": "markdown", "text": f"```\n{chunk}\n```"} for chunk in split(body) if chunk]


def preview_containers(
    title: str, body: str, *, subtitle: str = "", language: str = "diff", as_code: bool = False
) -> list[dict[str, Any]]:
    """A preview's body, collapsed: a full-width container per MESSAGE_LIMIT piece of it, closed
    until the owner opens it. A call with no card is titled with its line, in code style as a
    tool line is (`as_code`), and says the preview's sentence under it; under a card, which is
    the call's line, the title is the sentence alone. The body sits in the message itself, so it
    opens after a restart too."""
    title = title[:150]
    # The plain title is the fallback of a client that does not draw the rich one.
    rich = {
        "rich_text_title": {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_section",
                    "elements": [{"type": "text", "text": title, "style": {"code": True}}],
                }
            ],
        }
    }
    return [
        {
            "type": "container",
            "title": {"type": "plain_text", "text": title},
            **(rich if as_code else {}),
            **({"subtitle": {"type": "plain_text", "text": subtitle[:150]}} if subtitle else {}),
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
                            **({"language": language} if language else {}),
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


def blocks_sizes(blocks: list[dict[str, Any]]) -> tuple[int, int, int]:
    """What a refused write sent, for the log, never its content: the characters of text, the
    blocks, and how many of them are task cards."""
    cards = sum(1 for b in blocks if b["type"] == "task_card")
    return sum(len(block_text(b)) for b in blocks), len(blocks), cards


def plain_text(blocks: list[dict[str, Any]]) -> str:
    """A message's text with no block: what a refused final write is retried with. Slack caps a
    text-only message at 4,000 characters (chat.update reference, 2026-09-25)."""
    body = "\n\n".join(str(b["text"]) for b in blocks if b["type"] == "markdown")
    if len(body) > FALLBACK_LIMIT:
        body = body[:FALLBACK_LIMIT] + "…"
    return body or "…"


def banner_text(paragraph: str, *, limit: int | None = None) -> str:
    """A paragraph of Claude's markdown as the plain `text` of a notification: the markers gone
    (headings, quotes, list bullets, emphasis, code ticks, link targets) and `&`, `<`, `>`
    escaped, since Slack reads them as markup there too. With `limit` it is cut before it is
    escaped, so the result holds at most that many characters and no half an entity."""
    plain = strip_markdown(paragraph)
    if limit is None:
        return mrkdwn_escape(plain)
    out: list[str] = []
    size = 0
    for char in plain:
        piece = mrkdwn_escape(char)
        if size + len(piece) > limit:
            break
        out.append(piece)
        size += len(piece)
    return "".join(out)


def plain_words(text: str) -> str:
    """The words of a text with the markup, the punctuation and the spacing gone: what two
    renderings of one text (Claude's markdown and Slack's converted read-back of it, with `*b*`
    for `**b**`, `<url|label>` for a link, `•` for a bullet) have in common."""
    text = re.sub(r"<[^|>]*\|([^>]*)>", r"\1", text)
    return " ".join(re.findall(r"\w+", strip_markdown(text))).lower()


def strip_markdown(text: str) -> str:
    """The markdown markers of `text` removed, nothing escaped, in a time linear in the text: a
    banner is cut from a paragraph as long as Claude wrote it."""
    # A pattern that reads ahead and then fails is tried again from every later start, which is
    # quadratic on a long run of `[`, of blank lines or of `_` inside a word. So the three that
    # read ahead also match what they read where no marker is (a `[` no link closes, space no
    # marker follows, `_` between two letters), and the replacement puts that back as it was.
    plain = re.sub(r"\[([^\]]*)(?:\](?:\([^)]*(\))?)?)?", lambda m: m[1] if m[2] else m[0], text)
    plain = re.sub(
        r"^\s*((?:#+|>|[-+*]|\d+\.)\s+)?", lambda m: "" if m[1] else m[0], plain, flags=re.M
    )
    plain = re.sub(r"\*\*|__|~~|`+", "", plain)
    plain = re.sub(r"(?<!\w)[*_]+|[*_]+(?!\w)|(_+)", r"\1", plain)
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
    rev: int = 0  # bumped when it changes: a message whose parts kept theirs is not rendered again


@dataclass
class _Tool:
    update: TaskUpdate
    rev: int = 0
    _pieces: list[str] = field(default_factory=list, init=False, repr=False)
    _carded: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.set(self.update)

    def set(self, update: TaskUpdate) -> None:
        """A new state of the call. What follows its card is worked out once, here."""
        self.update = update
        view = update.shown_preview
        if view is None or not view.body:
            self._pieces = []
        elif view.plain:
            # One context block, as it is drawn: a piece counts for what the message holds.
            self._pieces = [plain_lines(view.body)]
        else:
            self._pieces = [c for c in split(view.body) if c]
        if view is None or view.plain or not self._pieces:
            self._carded = True

    @property
    def cardless(self) -> bool:
        """Whether the tool is its preview alone: an Edit or a Write that reached the reply
        already ended well. A state that needs a card (running, failed, stopped, a preview with
        no body) gives it one for good, since a stream cannot take a card back."""
        return not self._carded

    def pieces(self) -> list[str]:
        """What follows the tool's card: the terminal's preview of a call that ended well, cut
        into pieces that fit a message each. None until the call ends, and for any other call."""
        return self._pieces

    @property
    def extent(self) -> int:
        """The elements of the tool: its card (counted even when `cardless` draws none, so a
        cursor reads the same either way), then its preview pieces."""
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
        if view.summary:
            fields["output"] = view.summary[:CARD_TEXT_LIMIT]
    elif output and (update.status == "error" or output == STOPPED):
        fields["output"] = output[:CARD_TEXT_LIMIT]
    elif update.status == "in_progress" and update.details:
        fields["details"] = update.details[:CARD_TEXT_LIMIT]
    return fields


def card_chunk(update: TaskUpdate) -> dict[str, Any]:
    """What a tool's card says, as a `task_update` chunk. A stream is sent `card_addition` of
    it: the card is updated in place by its id, and its details and output only grow."""
    return {"type": "task_update", "id": update.id, **card_fields(update)}


def lacking(held: str, wanted: str) -> str:
    """What to add to the text a card holds so that it ends with `wanted`: the lines of `wanted`
    past those the card already ends with, a line break first. Empty when it lacks nothing.
    Ten equal lines followed by an eleventh read as nothing new."""
    if not held:
        return wanted
    has, wants = held.split("\n"), wanted.split("\n")
    shared = next((n for n in range(min(len(has), len(wants)), 0, -1) if has[-n:] == wants[:n]), 0)
    return "".join(f"\n{line}" for line in wants[shared:])


def card_addition(
    chunk: dict[str, Any], sent: dict[str, Any] | None, held: dict[str, str], room: int | None
) -> tuple[dict[str, Any], dict[str, str], int]:
    """The chunk that brings a stream's card to `chunk`, what the card holds after it, and what
    it costs the message. `sent` is the card's last chunk (None for a new card) and `held` the
    details and the output Slack keeps for it. A card already in the message leaves out the
    details that do not fit `room`, and keeps those it holds; its output, which says how the
    call ended, is sent whatever the room."""
    told = {key: chunk[key] for key in ("type", "id", "title", "status")}
    after = dict(held)
    if sent is None:
        cost = CARD_COST + len(chunk["title"])
    else:
        cost = max(0, len(chunk["title"]) - len(sent["title"]))
    for key in CARD_APPENDED:
        if key not in chunk:
            continue  # nothing to say: the card keeps what it holds
        more = lacking(held.get(key, ""), chunk[key])
        if not more:
            continue
        price = len(more) + CARD_LINE_COST * (more.count("\n") + (key not in held))
        if key not in held:
            price += CARD_FIELD_COST
        if key == "details" and sent is not None and room is not None and cost + price > room:
            continue
        told[key] = more
        after[key] = held.get(key, "") + more
        cost += price
    return told, after, cost


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
    """Piece `index` (1 is the first) of a tool's preview as blocks. With no card: a collapsed
    container titled with the call's line. Under a card: a collapsed container for a diff, code
    blocks for a new file's first lines, a context block for lines of words (a question's
    answers), each indented under its card and shown as written."""
    view = tool.update.shown_preview
    assert view is not None
    body = tool.pieces()[index - 1]
    if view.plain:
        return [context_block(body)]
    if not piece_collapsed(tool):
        return preview_blocks(body)
    if tool.cardless:
        return preview_containers(
            view.title, body, subtitle=view.summary, language=view.language, as_code=True
        )
    return preview_containers(view.summary, body)


def piece_collapsed(tool: _Tool) -> bool:
    """Whether `piece_blocks` draws the tool's preview as collapsed containers, whose text a
    `chat.update` does not count toward MESSAGE_LIMIT. A context block and code blocks (markdown)
    always count."""
    view = tool.update.shown_preview
    assert view is not None
    return not view.plain and (tool.cardless or view.language == "diff")


def piece_chunk(tool: _Tool, index: int) -> dict[str, Any]:
    """The same piece for a stream: a `blocks` chunk (measured 2026-09-28 for a diff's container
    and 2026-09-29 for a markdown block, which reads back as rich text; a context block in one
    was seen drawn on the daemon on 2026-10-03)."""
    return {"type": "blocks", "blocks": piece_blocks(tool, index)}


@dataclass
class _Plan:
    """What a stream still has to be told, and where the reply goes on if it does not all fit."""

    size: int  # characters the message holds once these chunks are in
    count: int  # elements it holds
    card_cost: int = 0  # what its cards count toward MESSAGE_LIMIT beside `size`
    chunks: list[dict[str, Any]] = field(default_factory=list)
    overflow: Cursor | None = None  # where the next message starts, if the rest does not fit
    text: dict[int, int] = field(default_factory=dict)  # part -> characters sent up to
    pieces: set[tuple[int, int]] = field(default_factory=set)
    cards: dict[str, dict[str, Any]] = field(default_factory=dict)
    card_held: dict[str, dict[str, str]] = field(default_factory=dict)
    text_blocks: dict[int, int] = field(default_factory=dict)  # part -> blocks its text makes


@dataclass
class _Held:
    """What the reply's last message showed when its end was written: nothing of it is removed
    by a late update."""

    text: dict[int, int] = field(default_factory=dict)  # part -> length of its text shown
    cards: set[str] = field(default_factory=set)
    pieces: set[tuple[int, int]] = field(default_factory=set)

    def blocks(self) -> int:
        """The blocks they take, a card or a piece counting one."""
        return len(self.text) + len(self.cards) + len(self.pieces)


def plan_card_text(plan: _Plan) -> int:
    """The characters of card text a plan's chunks carry."""
    return sum(
        len(chunk.get(key, ""))
        for chunk in plan.chunks
        if chunk["type"] == "task_update"
        for key in CARD_TEXT_FIELDS
    )


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
    cards: dict[str, dict[str, Any]] = field(default_factory=dict)  # what each card was told
    # The details and the output Slack holds per card: those of every chunk sent, joined.
    card_held: dict[str, dict[str, str]] = field(default_factory=dict)
    # Characters of card text (title, details, output) in every chunk sent, for the log, and
    # what the cards count toward MESSAGE_LIMIT (`card_addition`); `size` counts none of it.
    card_text: int = 0
    card_cost: int = 0
    size: int = 0
    count: int = 0
    text_blocks: dict[int, int] = field(default_factory=dict)  # part -> blocks its text makes
    # The blocks the message may hold: BLOCKS_LIMIT, until Slack refuses a write of it for
    # counting more of them than the daemon did.
    blocks_room: int = BLOCKS_LIMIT
    # Stopped: whether what it shows is its stream as sent, which needs no write while the
    # model still says the same; else `shown` is the blocks of its last post or update.
    exact: bool = False
    shown: list[dict[str, Any]] | None = None
    footer: list[dict[str, Any]] = field(default_factory=list)  # the footer it shows
    deadline: asyncio.Task[None] | None = None
    # An append whose outcome is unknown: the stream is no longer told anything, it is stopped
    # and the message goes on by update, from the model.
    blind: bool = False
    # Its stream was refused as too long (`refused`), and an update refused in turn left it
    # showing less than the model (`short`), until an update passes: while a message is short
    # the reply's end has not landed.
    refused: bool = False
    short: bool = False
    started: float = 0.0  # when its stream started, by the clock: a stream is over at 5 minutes
    # (span revision, end) of the last write of a stopped message that has a successor: while it
    # holds, nothing in the message changed and it is not rendered again.
    checked: tuple[int, Cursor | None] | None = None
    # The footer a stop of unknown outcome carried (a footerless stop records nothing): if the
    # next stop finds the stream over, that stop is the one that landed.
    stop_unknown: list[dict[str, Any]] | None = None
    # Slack refused an update of this message whose containers were not counted: from then on
    # they count toward MESSAGE_LIMIT, as in a post.
    containers_counted: bool = False


class ReplySink:
    """One reply in a Slack thread, as a native stream: Claude's text as it is written, and
    task cards for its tools, updated in place, in the order things happen: two per run of calls
    (`render.fold`), which a silent update folds into a line of counts once the stream has
    stopped at the reply's end. The stream starts with the first content (never a placeholder)
    and stops with the reply's end, the footer at the bottom.
    It stops on its own at STREAM_SECONDS, since Slack closes a stream at 5 minutes, and when
    Slack refuses an append as too long, which it would refuse again: from then on
    the same message grows by `chat.update`, and the end posts the reply's ending (the text
    after its last call) with the footer as a new message.
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
        bot_user_id: str,
        limiter: UpdateLimiter,
        clock: Clock | None = None,
        on_open_reply: Callable[[str | None, str | None], None] | None = None,
        on_write: Callable[[], None] | None = None,
    ) -> None:
        self._slack = slack
        self._channel = channel
        self._thread_ts = thread_ts
        # A stream is addressed to a user of a workspace (as the recordings passed them).
        self._team_id = team_id
        self._user_id = user_id
        # The daemon's own bot: whose messages are read back to find one a create left unknown.
        self._bot_user_id = bot_user_id
        self._limiter = limiter
        self._clock = clock or Clock()
        # Crash repair (issue #19): `(old_ts, new_ts)`, this sink's own transition in the
        # thread's open-replies list (more than one sink can be open at once: a background
        # task's own reply can outlive the turn that started it, so each sink owns exactly one
        # entry and must never touch another's). A plain sync callback (`StateStore`'s setters
        # are sync file writes), never awaited here.
        self._on_open_reply = on_open_reply
        # Called after every pass that wrote this reply, or tried to: Slack clears a thread's
        # status line when the app replies (`ThreadStatus.wrote`). Sync, never awaited here.
        self._on_write = on_write
        self._wrote = False  # a Slack write was attempted since `on_write` was last called
        # This sink's own entries in that list: the messages a crash would leave unfinished.
        self._tracked: set[str] = set()
        self._ended = False  # the end landed: nothing is left for a repair to close
        # The last message's body is whole, and only the closing message is owed.
        self._body_landed = False
        self._parts: list[_Text | _Tool] = []
        self._tools: dict[str, _Tool] = {}  # by the id of the card that shows them
        self._fold = Fold()  # which cards show the calls: two per run of calls
        self._messages: list[_Message] = []
        self._pending: asyncio.Task[None] | None = None
        self._retry: asyncio.Task[None] | None = None
        self._ending: asyncio.Task[bool] | None = None
        self._lock = asyncio.Lock()
        self._finished = False
        self._footer: str | None = None
        self._running = ""
        self._latest = True
        self._closed_out = False  # whether close_out has run; a second call is a no-op
        # Where the footer went once the reply ended: "inline", on the stream's own stop (and
        # on the message's updates after it); "moved", under the reply's ending, posted as its
        # last message; or "post", in a closing message that holds nothing else.
        self._end_mode: str | None = None
        # Set once the end has landed on Slack (`_end_landed`): the reply's messages are then a
        # fixed set, and a late update only edits them, keeping what its last message showed
        # (`_blocks`). Not `_closed_out`, nor `_end_mode` alone: a close whose write
        # failed still owes messages, and its retry must open them.
        self._held: _Held | None = None
        self._closing: str | None = None  # ts of the closing message, once posted
        self._closing_shown: list[dict[str, Any]] = []
        # Resolves once the reply is known to have ended on Slack (True) or its one retry
        # failed too (False): what the session waits on before it shows a checkmark.
        self._landed: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        # Bumped by `_changed`: `_later` compares it before and after a pass to notice a change
        # that arrived while the pass wrote or waited its turn, and runs another pass for it.
        self._version = 0
        self._rev = 0

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
        if markdown.strip():
            self._fold.text()
        last = self._parts[-1] if self._parts else None
        if isinstance(last, _Text) and last.notice == notice:
            last.text += markdown
            last.rev = self._next_rev()
        else:
            self._parts.append(_Text(markdown, notice, self._next_rev()))
        await self._changed()

    async def task(self, update: TaskUpdate) -> None:
        """A call's new state, on the cards that show it: a run of calls shares two cards
        (`render.fold`), a call with a view of its own has one."""
        cards = self._fold.task(update)
        for card in cards:
            tool = self._tools.get(card.id)
            if tool is None:
                tool = self._tools[card.id] = _Tool(card)
                self._parts.append(tool)
            else:
                tool.set(card)
            tool.rev = self._next_rev()
        if cards:
            await self._changed()

    def _next_rev(self) -> int:
        self._rev += 1
        return self._rev

    async def set_running(self, counts: str) -> None:
        """Show what still runs in this thread (`⏳ 1 shell · 1 agent`) after the footer, or on a
        line of its own; empty removes it. Only the thread's latest reply shows one."""
        if counts == self._running:
            return
        self._running = counts
        if self._closed_out:
            await self._changed()

    @property
    def footer_shown(self) -> bool:
        """Whether the reply's end has landed on Slack: its footer is then what says the
        running counts, and no status line under it repeats them."""
        return self._ended and bool(self._messages)

    async def set_latest(self, latest: bool) -> None:
        """Only the thread's latest reply shows the running counts, at the bottom of the
        thread; an older one drops them and keeps its footer, a record of how its turn ended."""
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
        # What a card of a run of calls shows changes here, to its folded line: its message is
        # rendered again even when nothing else in it changed.
        for tool in self._tools.values():
            if tool.update.folded is not None:
                tool.rev = self._next_rev()
        await self._flush()

    async def close_out(self, footer: str | None) -> bool:
        """End the reply, once: stop its stream with the footer at the bottom (one push), or,
        when the stream already stopped at STREAM_SECONDS, post its ending and the footer as a
        new message (the second push). True when the reply ended on Slack (or nothing was
        owed); False when a write failed and one retry is scheduled: `wait_landed` then says how
        it ended. A second call is a no-op: the reply has already ended."""
        if self._closed_out:
            return self._landed.done() and self._landed.result()
        self._closed_out = True
        self._footer = footer
        # The end is its own task, shielded from the caller: a caller cancelled while Slack
        # takes the write (a timer, a turn starting, a shutdown) must not leave `_landed`
        # unresolved with no retry, which no later `close_out` could repair.
        self._ending = asyncio.create_task(self._end_out())
        return await asyncio.shield(self._ending)

    async def _end_out(self) -> bool:
        """The end's first flush: whatever happens, `_landed` resolves (an end that began always
        does), with the retry when Slack refused the write."""
        try:
            ok = await self._flush()
        except BaseException:
            self._resolve(False)
            raise
        if ok:
            self._resolve(True)
            return True
        if not self._landed.done():  # `settle` may have decided it already
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
        if self._ending is not None and not self._ending.done():
            # The end in flight may still schedule its retry: let it, so the cancel below sees it.
            # `wait` hands back neither its exception nor its cancellation: only a cancel of
            # this very call ends the wait early.
            await asyncio.wait([self._ending])
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

    def _span_rev(self, message: _Message, end: Cursor | None) -> int:
        return max((part.rev for _, part, _, _ in self._span(message.start, end)), default=0)

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
        """Whether a span takes a tool's card, if it has one, and which of its preview pieces."""
        extent = tool.extent
        top = extent if ceil is None else min(extent, ceil)
        return floor == 0 and top > 0 and not tool.cardless, range(max(floor, 1), top)

    def _closing_blocks(self) -> list[dict[str, Any]]:
        """The footer below a divider, and after it, on the thread's latest reply only, what
        still runs; empty when there is nothing to show."""
        last_line = " · ".join(filter(None, (self._footer, self._running if self._latest else "")))
        if not last_line:
            return []
        return [{"type": "divider"}, context_block(last_line)]

    def _footer_of(self, message: _Message) -> list[dict[str, Any]]:
        """The footer a message shows: only the reply's last, when the footer rode on its
        stream's stop or when it is the ending posted as a message of its own (`_end`)."""
        if message is self._messages[-1] and self._end_mode in ("inline", "moved"):
            return self._closing_blocks()
        return []

    def _ending_cursor(self, message: _Message) -> Cursor | None:
        """Where the reply's ending starts in its last message: the text Claude wrote after its
        last call, whole (a part of the model is never cut: half a list or a heading without
        its body is no ending), with whatever follows it. None when the message holds no such
        text from its start, or would keep nothing of the answer before it (an answer that is
        text alone): the ending is then the footer alone."""
        index = next(
            (
                i
                for i in range(len(self._parts) - 1, message.start[0] - 1, -1)
                if isinstance(part := self._parts[i], _Text)
                and not part.notice
                and part.text.strip()
            ),
            None,
        )
        if index is None or (index, 0) <= message.start:
            return None
        cursor = (index, 0)
        # Something of the answer must stay before it, and show: a card that draws (a folded
        # run's second card draws nothing), a preview, or words of Claude's. A message left
        # with a line of the daemon's alone, or with nothing, is no answer.
        for _, part, floor, ceil in self._span(message.start, cursor):
            if isinstance(part, _Tool):
                has_card, pieces = self._tool_elements(part, floor, ceil)
                if (has_card and self._card_blocks(part.update)) or pieces:
                    return cursor
            elif not part.notice and part.text[floor:ceil].strip():
                return cursor
        return None

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
                    return banner_text(words.split("\n\n", 1)[0], limit=BANNER_LIMIT) or "…"
                notice = notice or words
        for _, part, _, _ in span:
            if isinstance(part, _Tool):
                return banner_text(card_fields(part.update)["title"], limit=BANNER_LIMIT) or "…"
        return banner_text(notice.split("\n\n", 1)[0], limit=BANNER_LIMIT) or "…"

    # What a stream is told.

    def _plan(self, message: _Message) -> _Plan:
        """What the message's stream lacks: the cards that changed since they were sent, then
        what the model has past what it was sent, in order, as far as the message holds."""
        plan = _Plan(size=message.size, count=message.count, card_cost=message.card_cost)
        for tool_id, sent in message.cards.items():
            chunk = card_chunk(self._tools[tool_id].update)
            if chunk != sent:
                self._plan_card(message, plan, chunk)
        for index, part, floor, _ in self._span(message.start, None):
            if isinstance(part, _Text):
                more = self._plan_text(message, plan, index, part, floor)
            else:
                more = self._plan_tool(message, plan, index, part, floor)
            if not more:
                break
        return plan

    @staticmethod
    def _plan_card(message: _Message, plan: _Plan, chunk: dict[str, Any]) -> bool:
        """Add a card's chunk to the plan as a stream takes it; False when the card is new and
        the message has no room for it. A message that holds nothing yet takes any card."""
        tool_id = chunk["id"]
        room = MESSAGE_LIMIT - plan.size - plan.card_cost
        told, held, cost = card_addition(
            chunk, message.cards.get(tool_id), message.card_held.get(tool_id, {}), room
        )
        if tool_id not in message.cards and cost > room and plan.size + plan.card_cost > 0:
            return False
        plan.chunks.append(told)
        plan.cards[tool_id] = chunk
        plan.card_held[tool_id] = held
        plan.card_cost += cost
        return True

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
        had = message.text_blocks.get(index, 0)
        others = plan.count - had  # the blocks of everything else in the message
        if not had and others >= message.blocks_room:
            plan.overflow = (index, sent)
            return False
        room = max(0, MESSAGE_LIMIT - plan.size - plan.card_cost)
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
        # The part's text in this message, as the update that follows the stream will write
        # it: cut where the blocks Slack makes of it would pass the message's room.
        whole = part.text[floor : sent + lead + len(piece)]
        opening = floor + len(whole) - len(whole.lstrip("\n"))
        words = whole.strip("\n")
        # Text already sent can gain a block when its next line arrives (a line that turns
        # out to head a table): the cut is then the next block's start, never inside a line.
        over = markdown_cut(
            words, max(1, message.blocks_room - others), max(0, sent + lead - opening)
        )
        if over is not None:
            stop = opening + over
            piece = part.text[sent + lead : stop]
            plan.text[index] = stop
            plan.overflow = (index, stop)
            words = part.text[opening:stop].strip("\n")
        if piece.strip():
            plan.chunks.append({"type": "markdown_text", "text": piece})
            plan.size += len(piece)
            plan.text_blocks[index] = markdown_blocks(words)
            plan.count = others + plan.text_blocks[index]
        return plan.overflow is None

    def _plan_tool(
        self, message: _Message, plan: _Plan, index: int, tool: _Tool, floor: int
    ) -> bool:
        """Add a tool's card, if this message holds it and has not sent it, and the pieces of its
        preview it has not sent; False when the plan is complete."""
        update = tool.update
        has_card, pieces = self._tool_elements(tool, floor, None)
        if has_card and update.id not in message.cards and update.id not in plan.cards:
            if plan.count >= message.blocks_room or not self._plan_card(
                message, plan, card_chunk(update)
            ):
                plan.overflow = (index, 0)
                return False
            plan.count += 1
        for piece in pieces:
            if (index, piece) in message.pieces_sent:
                continue
            size = len(tool.pieces()[piece - 1])
            if (
                plan.count >= message.blocks_room
                or plan.size + plan.card_cost + size > MESSAGE_LIMIT
            ):
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
        message.card_held.update(plan.card_held)
        message.card_text += plan_card_text(plan)
        message.size, message.count, message.card_cost = plan.size, plan.count, plan.card_cost
        message.text_blocks.update(plan.text_blocks)

    def _stream_shows(self, message: _Message, end: Cursor | None) -> bool:
        """Whether the message's stream, as sent, shows what the model says for its span: every
        card as it is now, and ended (a card left in progress in a stopped stream is stored as
        an error until it is updated: measured 2026-09-28), every preview piece, all the text.
        Never once the body has ended, for a message with cards of a run of calls: the model
        then says the folded line, which a stream cannot show."""
        for tool_id, sent in message.cards.items():
            update = self._tools[tool_id].update
            if self._finished and update.folded is not None:
                return False
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
        self, message: _Message, end: Cursor | None, *, posting: bool = False
    ) -> tuple[list[dict[str, Any]], Cursor | None]:
        """The blocks of the message's span: Claude's text as markdown, the tools' task cards
        (`_card_blocks`), the preview blocks after a card. With no `end` (the reply's last
        message) only as many as fit MESSAGE_LIMIT and BLOCKS_LIMIT, and where the reply goes on
        if it does not all fit. Once the reply's end has landed nothing opens a message after
        it: what arrives late is shown when it all fits, and else not at all, the last message
        keeping what it showed (`_Held`) and no cursor being returned. The blocks are counted for
        a `chat.update`, which takes a collapsed container's text without counting it; `posting`
        counts it, as a `chat.postMessage` does."""
        blocks, overflow = self._render(message, end, None, posting=posting)
        if overflow is None or end is not None or self._held is None:
            return blocks, overflow
        return self._render(message, None, self._held)[0], None

    def _render(
        self, message: _Message, end: Cursor | None, held: _Held | None, *, posting: bool = False
    ) -> tuple[list[dict[str, Any]], Cursor | None]:
        """`_blocks` for one reading of the span: with `held`, only what the message showed when
        the reply's end landed, as the span is now, and the note for a preview of a card of it
        that was left out, if a block is free for it."""
        blocks: list[dict[str, Any]] = []
        size = count = 0  # `count` is the blocks as Slack counts them, `markdown_blocks` a text
        limit = message.blocks_room
        fixed = end is not None or held is not None
        # A message whose span is fixed: what its cards and text take, and what previews took.
        base_blocks, base_size = self._base(message, end) if end is not None else (0, 0)
        left_out = (
            self._cut_pieces(message, end, base_blocks, base_size) if end is not None else set()
        )
        note_room = held is not None and held.blocks() < BLOCKS_LIMIT
        noted = False
        for index, part, floor, ceil in self._span(message.start, end):
            if isinstance(part, _Text):
                if held is not None:
                    ceil = held.text.get(index, floor)
                raw = part.text[floor:ceil]
                lead = len(raw) - len(raw.lstrip("\n"))
                tail = raw[lead:]
                words = tail.strip("\n")
                if not words:
                    continue
                if not fixed and count >= limit:
                    return blocks, (index, floor + lead)
                room = MESSAGE_LIMIT - size
                if not fixed:
                    stop, skip = None, 0
                    if len(words) > room:
                        cut = words.rfind("\n", 0, room) if room > 0 else -1
                        stop, skip = (room, 0) if cut <= 0 else (cut, 1)
                    over = markdown_cut(words, limit - count)
                    if over is not None and (stop is None or over < stop):
                        stop, skip = over, 0  # a line's start: the next message opens on it
                    if stop is not None:
                        if words[:stop].strip("\n"):
                            blocks.append({"type": "markdown", "text": words[:stop].rstrip("\n")})
                        return blocks, (index, floor + lead + stop + skip)
                blocks.append({"type": "markdown", "text": words})
                size += len(words)
                count += markdown_blocks(words)
            else:
                has_card, pieces = self._tool_elements(part, floor, ceil)
                if has_card and (held is None or part.update.id in held.cards):
                    if not fixed and count >= limit:
                        return blocks, (index, 0)
                    card = self._card_blocks(part.update)
                    blocks += card
                    count += len(card)
                for piece in pieces:
                    length = self._weight(message, part, piece, posting)
                    if held is not None and (index, piece) not in held.pieces:
                        # Late: left out, with the note when its card was shown, once.
                        if note_room and not noted and part.update.id in held.cards:
                            blocks.append(context_block(PREVIEW_CUT))
                            noted, count = True, count + 1
                        continue
                    if not fixed:
                        if count + 1 > limit or size + length > MESSAGE_LIMIT:
                            return blocks, (index, piece)
                    elif (index, piece) in left_out:
                        # A preview that arrives after its card, in a message whose span is
                        # fixed and full: it is cut, and says so once, rather than pass the
                        # limit (Slack would refuse the whole update, the card with it).
                        if not noted:
                            blocks.append(context_block(PREVIEW_CUT))
                            noted, count = True, count + 1
                        continue
                    shown = piece_blocks(part, piece)
                    if not fixed and count + blocks_count(shown) > limit:
                        return blocks, (index, piece)
                    blocks += shown
                    size += length
                    count += blocks_count(shown)
        return blocks, None

    def _card_blocks(self, update: TaskUpdate) -> list[dict[str, Any]]:
        """A tool's card, or once the reply's body has ended what a card of a run of calls
        folds to: a line of counts, as the channel model drew it, or nothing."""
        if not self._finished or update.folded is None:
            return [card_block(update)]
        return [context_block(mrkdwn_escape(update.folded))] if update.folded else []

    def _hold(self, message: _Message) -> _Held:
        """What the message shows of the model as it is now: as much of it as fits. Words can
        reach the model while the end is being written; past the limits they were never shown,
        and holding them would have every later edit of the message refused."""
        held = _Held()
        fits = self._render(message, None, None)[1]
        for index, part, floor, ceil in self._span(message.start, fits):
            if isinstance(part, _Text):
                if part.text[floor:ceil].strip("\n"):
                    held.text[index] = len(part.text) if ceil is None else ceil
            else:
                has_card, pieces = self._tool_elements(part, floor, ceil)
                if has_card:
                    held.cards.add(part.update.id)
                held.pieces |= {(index, piece) for piece in pieces}
        return held

    @staticmethod
    def _weight(message: _Message, tool: _Tool, piece: int, posting: bool) -> int:
        """The characters a preview piece counts toward MESSAGE_LIMIT in a message: its text,
        unless it is drawn as collapsed containers, the message is written by `chat.update` and
        Slack has not refused an update of it for them."""
        if not posting and not message.containers_counted and piece_collapsed(tool):
            return 0
        return len(tool.pieces()[piece - 1])

    def _cut_pieces(
        self, message: _Message, end: Cursor, base_blocks: int, base_size: int
    ) -> set[tuple[int, int]]:
        """The preview pieces a fixed span has no room for, once the text and the cards, and the
        one note that says pieces were left out, have theirs. Pieces are taken in order."""
        cut: set[tuple[int, int]] = set()
        for reserved in (0, 1):  # a note is owed as soon as one piece is cut
            cut, used_blocks, used_size = set(), 0, 0
            for index, part, floor, ceil in self._span(message.start, end):
                if not isinstance(part, _Tool):
                    continue
                for piece in self._tool_elements(part, floor, ceil)[1]:
                    length = self._weight(message, part, piece, False)
                    if (
                        base_blocks + used_blocks + 1 + reserved > message.blocks_room
                        or base_size + used_size + length > MESSAGE_LIMIT
                    ):
                        cut.add((index, piece))
                    else:
                        used_blocks, used_size = used_blocks + 1, used_size + length
            if not cut:
                break
        return cut

    def _base(self, message: _Message, end: Cursor) -> tuple[int, int]:
        """The blocks and characters the text and the cards of a fixed span take: what is left of
        the limits is what its previews may."""
        blocks = size = 0
        for _, part, floor, ceil in self._span(message.start, end):
            if isinstance(part, _Text):
                words = part.text[floor:ceil].strip("\n")
                if words:
                    blocks, size = blocks + markdown_blocks(words), size + len(words)
            elif self._tool_elements(part, floor, ceil)[0]:
                blocks += 1
        return blocks, size

    async def _update_step(
        self, message: _Message, end: Cursor | None, *, split: bool = False, room: int = 0
    ) -> tuple[bool, Cursor | None]:
        """Bring a stopped message to what the model says, with a `chat.update` when it shows
        something else: (written, where the reply goes on past the message). An update never
        notifies (measured 2026-09-29). Nothing is written for a message whose stream already
        shows the model. An update refused for content that held a container, which counted for
        nothing, is tried once more with the containers counted, as a post counts them. With
        `split`, for a caller that opens the next message at the cursor returned: an update
        Slack refuses for too many blocks is tried again with less of the span (`_tighten`);
        `room` is the room the message had before the first of those tries."""
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
            self._wrote = True
            await self._slack.chat_update(
                channel=self._channel,
                ts=message.ts,
                text=self._banner(self._span(message.start, end)),
                blocks=blocks,
            )
        except Exception as exc:
            code = describe(exc)
            logger.warning(
                "chat.update failed (%s) with text %d, elements %d, cards %d",
                describe_refusal(exc),
                *blocks_sizes(blocks),
            )
            if code == STILL_STREAMING:
                # The daemon's stop never reached Slack: stopped now, the next write passes.
                await self._stop(message, None, end)
            elif code in REFUSED_CONTENT:
                if not message.containers_counted and any(b["type"] == "container" for b in blocks):
                    message.containers_counted = True
                    return await self._update_step(message, end, split=split, room=room)
                if split and end is None and self._held is None and too_many_blocks(exc):
                    before = room or message.blocks_room
                    if self._tighten(message, blocks):
                        return await self._update_step(message, end, split=True, room=before)
                    # Refused down to one block: nothing was written, so nothing is cut.
                    message.blocks_room = before
                    blocks, overflow = self._blocks(message, end)
                    blocks += footer
                # The message already shows what it showed: never replaced by a plainer one.
                # The change is dropped; the next one is tried. After a refused append nothing
                # else would say the message is short of the model, so that is kept.
                message.shown, message.exact, message.footer = blocks, False, footer
                message.short = message.refused
                return True, overflow
            return False, None
        message.shown, message.exact, message.footer = blocks, False, footer
        message.short = False
        return True, overflow

    @staticmethod
    def _tighten(message: _Message, blocks: list[dict[str, Any]]) -> bool:
        """Slack counted more blocks in a write of the message than the daemon did: halve the
        room the message has, so that its next write holds less and the reply goes on in a new
        message. False, with the room as it was, when one block was already too many."""
        if message.blocks_room <= 1:
            return False
        message.blocks_room = max(1, min(message.blocks_room, blocks_count(blocks)) // 2)
        return True

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
            self._wrote = True
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

    async def _post_step(
        self, message: _Message, *, split: bool = False
    ) -> tuple[bool, Cursor | None]:
        """Post the message that continues a stopped one, with the blocks of its span as a post
        takes them, then bring it to what an update takes: a container's text counts toward a
        post's limit and not toward an update's, so what the post left out of its span reaches
        the same message and no further one is opened for it."""
        blocks, overflow = self._blocks(message, None, posting=True)
        footer = self._footer_of(message)
        blocks += footer
        if not blocks:
            return True, overflow
        attempted = self._clock.time()
        banner = self._banner(self._span(message.start, None))
        try:
            # Claude's text can carry a link built to leak data when Slack fetches it for a
            # preview: no previews for anything the daemon posts.
            self._wrote = True
            posted = await self._slack.chat_postMessage(
                channel=self._channel,
                thread_ts=self._thread_ts,
                text=banner,
                blocks=blocks,
                unfurl_links=False,
                unfurl_media=False,
            )
        except Exception as exc:
            logger.warning(
                "chat.postMessage failed (%s) with text %d, elements %d, cards %d",
                describe_refusal(exc),
                *blocks_sizes(blocks),
            )
            if split and too_many_blocks(exc) and self._tighten(message, blocks):
                return await self._post_step(message, split=True)
            if unknown_outcome(exc):
                ts = await self._adopt(
                    attempted, stream=False, probe=plain_words(banner)[:ADOPT_WORDS]
                )
                if ts is not None:
                    message.ts, message.shown, message.footer = ts, blocks, footer
                    self._retrack()
                    return True, overflow
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
        return await self._update_step(message, None, split=split)

    @staticmethod
    def _first_words(plan: _Plan) -> str:
        """The start of what a stream's first chunks say, as plain words: empty when there are
        none to compare (then nothing is adopted). A stream's `text` reads back with a card's
        title and with a container's title in it (recorded 2026-09-28, slack-sdk 3.44.1)."""
        first = plan.chunks[0]
        if first["type"] == "blocks":
            block = first["blocks"][0]
            words = str(block.get("title", {}).get("text") or block_text(block))
        else:
            words = str(first.get("text") or first.get("title") or "")
        return plain_words(words)[:ADOPT_WORDS]

    async def _adopt(self, attempted: float, *, stream: bool, probe: str) -> str | None:
        """A create that failed on the connection may have landed. Read the thread back, and
        return the ts of the daemon's own message newer than the attempt that carries `probe`
        (a stream: `streaming_state`, and the start of its text), or None. Writing again first
        would make it twice."""
        try:
            read = await self._slack.conversations_replies(
                channel=self._channel,
                ts=self._thread_ts,
                oldest=f"{attempted - ADOPT_SKEW_SECONDS:.6f}",
                limit=200,
            )
        except Exception as exc:
            logger.warning("could not read the thread back for a lost write: %s", describe(exc))
            return None
        known = {m.ts for m in self._messages if m.ts} | {self._closing}
        for found in read.get("messages") or []:
            ts = str(found.get("ts"))
            if ts == self._thread_ts or ts in known or found.get("user") != self._bot_user_id:
                continue
            if stream and "streaming_state" not in found:
                continue
            if probe and probe in plain_words(str(found.get("text", ""))):
                return ts
        return None

    async def _stream_step(self, message: _Message) -> tuple[bool, Cursor | None]:
        """Tell the message's stream what it lacks, starting it with the first content: (written,
        where the reply goes on past the message)."""
        plan = self._plan(message)
        if not plan.chunks:
            return True, plan.overflow
        if message.ts is None:
            attempted = self._clock.time()
            try:
                self._wrote = True
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
                    " (outcome unknown: the thread is read back for a stream it may have made)"
                    if unknown_outcome(exc)
                    else "",
                )
                ts = None
                if unknown_outcome(exc):
                    ts = await self._adopt(attempted, stream=True, probe=self._first_words(plan))
                if ts is None:
                    return False, None
                stream_ts = ts  # it landed: this is that stream
            else:
                stream_ts = str(started["ts"])
            message.ts, message.streaming = stream_ts, True
            message.started = attempted
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
            self._wrote = True
            await self._slack.chat_appendStream(
                channel=self._channel, ts=message.ts, chunks=plan.chunks
            )
        except Exception as exc:
            code = describe(exc)
            if code == TOO_LONG:
                # Slack's cap follows what it stores, which the plan only estimates, and it
                # would refuse the same append again. The
                # stream is stopped bare, as at STREAM_SECONDS, and the message goes on by
                # update, from the model; the end then posts the closing message. Only this
                # code: any other refusal stays a failed write, which the session shows.
                logger.warning(
                    "chat.appendStream refused (%s) with text %d, elements %d, cards %d, "
                    "card text sent %d and %d more in this append: "
                    "the stream is stopped and the message goes on by update",
                    code,
                    plan.size,
                    plan.count,
                    len(message.cards | plan.cards),
                    message.card_text,
                    plan_card_text(plan),
                )
                message.refused = True
                if await self._stop(message, None, None) == "failed":
                    return False, None
                return await self._update_step(message, None, split=True)
            logger.warning("could not write a reply to Slack: %s", code)
            if code == NOT_STREAMING:
                # Slack ended the stream first: the message goes on by update.
                self._gone(message)
                return await self._update_step(message, None, split=True)
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
            self._wrote = True
            await self._slack.chat_stopStream(**args)
        except Exception as exc:
            if describe(exc) != NOT_STREAMING:
                logger.warning("could not stop a reply's stream: %s", describe(exc))
                if unknown_outcome(exc) and blocks and message.stop_unknown is None:
                    message.stop_unknown = list(blocks)
                return "failed"
            if message.stop_unknown is not None and not self._past_life(message):
                # The stop that failed on the connection is the one that landed, footer and all.
                blocks, result = message.stop_unknown, "stopped"
            else:
                # Over without a footer of ours, or past the stream's life, when Slack's own end
                # cannot be told from our stop landing: the end posts a footer of its own.
                result = "gone"
            message.stop_unknown = None
        self._gone(message)
        message.stop_unknown = None
        message.exact = self._stream_shows(message, end)
        message.footer = list(blocks or []) if result == "stopped" else []
        return result

    def _past_life(self, message: _Message) -> bool:
        return self._clock.time() - message.started >= STREAM_LIFE

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
            if self._wrote:
                self._wrote = False
                if self._on_write is not None:
                    self._on_write()

    async def _sync_messages(self) -> bool:
        if not self._messages:
            if not self._has_content():
                return await self._end() if self._closed_out else True
            self._messages.append(_Message((0, 0), "stream"))
        for message, following in itertools.pairwise(self._messages):
            if message.ts is None:
                continue
            stamp = (self._span_rev(message, following.start), following.start)
            if message.checked == stamp:
                continue  # nothing in it changed since it was last brought to the model
            if not (await self._update_step(message, following.start))[0]:
                return False
            message.checked = stamp
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
                if message.mode == "stream":
                    ok, overflow = await self._stream_step(message)
                else:
                    ok, overflow = await self._post_step(message, split=True)
            elif message.streaming:
                ok, overflow = await self._stream_step(message)
            else:
                ok, overflow = await self._update_step(message, None, split=True)
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
        if not self._closed_out:
            return True
        # The end is written either way; a message left short of the model makes it one that
        # did not land, which the session shows.
        ended = await self._end()
        if self._held is None and self._end_landed():
            self._held = self._hold(self._messages[-1])
        return ended and not any(m.short for m in self._messages)

    async def _end(self) -> bool:
        """The reply's end, on Slack: the footer on the last stream's stop; once the stream is
        over, under the reply's ending, posted as a new message, or in a closing message of its
        own when there is no ending to move (`_ending_cursor`)."""
        if not self._messages:
            return True  # nothing was ever shown: nothing to end
        if self._end_mode in ("inline", "moved"):
            # The stream's stop carried the footer, or the ending posted as the reply's last
            # message does: a later change edits that message (`_sync_messages`).
            return True
        message = self._messages[-1]
        if message.streaming:
            result = await self._stop(message, self._closing_blocks(), None)
            if result == "failed":
                return False
            if result == "stopped":
                self._end_mode = "inline"
            # A stream cannot fold its cards: the update that follows its stop does, silently.
            # The reply has ended whatever becomes of that write: one that fails is tried once
            # more with the next pass, and the cards stay if that fails too.
            if not (await self._update_step(message, None))[0]:
                self._version += 1
                self._schedule()
            if result == "stopped":
                return True
        if self._end_mode is None:
            cursor = self._ending_cursor(message)
            if cursor is not None:
                # The reply's ending, with the footer, as a new message: the one that notifies,
                # so the notification says how the work ended. Posted first, then taken out
                # of the message it grew in: for a moment it shows twice, never nowhere.
                ending = _Message(cursor, "post")
                self._messages.append(ending)
                self._end_mode = "moved"  # the footer goes under it, in the same post
                try:
                    await self._post_step(ending)
                finally:
                    if ending.ts is None:
                        # Not posted, or cut off by a cancellation: as if never tried, so the
                        # next pass posts it before it shortens anything.
                        self._messages.pop()
                        self._end_mode = None
                if self._end_mode is None:
                    return False
                message.exact, message.checked = False, None  # its stream said more than its span
                return (await self._update_step(message, cursor))[0]
        self._end_mode = "post"
        self._body_landed = True  # every message is written: only the closing message is owed
        return await self._write_closing()

    def _end_landed(self) -> bool:
        """Whether the reply's end is on Slack: the footer rode on the last stream's stop
        ("inline", set after a stop that landed) or sits in the ending posted as the last
        message ("moved", reset when that post fails), or the closing message was posted. Not
        `_end_mode == "post"`, which `_end` sets before the closing message is written."""
        return self._end_mode in ("inline", "moved") or self._closing is not None

    async def _write_closing(self) -> bool:
        """Post the closing message of a reply whose stream stopped early and that has no ending
        to move into a message of its own (`_end`), or bring it to the footer as it stands: it
        stays once posted, since it is what notified. Its text is Claude's own words, as a
        banner: never a line of the daemon's."""
        blocks = self._closing_blocks() or [context_block(ZERO_WIDTH_SPACE)]
        attempted = self._clock.time()
        try:
            if self._closing is None:
                self._wrote = True
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
                self._wrote = True
                await self._slack.chat_update(
                    channel=self._channel,
                    ts=self._closing,
                    text=self._banner(),
                    blocks=blocks,
                )
        except Exception as exc:
            logger.warning("could not write a reply's closing message: %s", describe(exc))
            if self._closing is None and unknown_outcome(exc):
                self._closing = await self._adopt(
                    attempted, stream=False, probe=plain_words(self._banner())[:ADOPT_WORDS]
                )
                if self._closing is not None:
                    self._closing_shown = blocks
                    return True
            return False
        self._closing_shown = blocks
        return True
