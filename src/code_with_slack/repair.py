"""Repair what a crashed daemon left open (issue #19): a reply still saying Claude is writing,
an approval, question or D8 hold request still carrying buttons, and the ⏳/✋ reaction a turn
mid-flight left on its root. Runs once on start, right after `auth.test` and before opening the
Socket Mode connection (the web client works without it) and before `state.prune` (a pruned
thread's leftovers must still be repaired); a graceful stop clears these same fields itself, so a
second start finds nothing to do.

`conversations.replies` (docs.slack.dev/reference/methods/conversations.replies, read 2026-09-28,
confirmed against a real `conversations.replies` response recorded from a real workspace on
2026-09-28, scrubbed and kept as `tests/fixtures/slack/api-conversations-replies-by-ts.json`):
`ts` set to the reply's own ts, with `limit=1`, returns only that one message (an `oldest`/
`latest`/`inclusive` range built around the same ts instead returned the thread's root too).
"""

import logging
from collections.abc import Callable

from slack_sdk.web.async_client import AsyncWebClient

from code_with_slack import texts
from code_with_slack.render.sinks import (
    BLOCKS_LIMIT,
    STATUS_BLOCK_ID,
    UpdateLimiter,
    delete_request,
    describe,
)
from code_with_slack.render.status import Status, StatusReaction
from code_with_slack.state import StateStore, ThreadState

logger = logging.getLogger(__name__)

# What a shutdown appends to a reply cut short (`ThreadSession.close`'s own `line`, texts.ENDED
# with its default reason): reused verbatim, so a repaired reply reads exactly as one closed by
# a graceful stop would.
_STOPPED_LINE = texts.ENDED.format(reason=texts.ENDED_SHUTDOWN)
_STOPPED_BLOCK = {"type": "markdown", "text": _STOPPED_LINE}


async def repair_crash(slack: AsyncWebClient, state: StateStore, limiter: UpdateLimiter) -> None:
    """Repair every thread state.json still shows as left open. One thread's failure (a
    deleted message, a Slack error) is logged and does not stop the others; each of a thread's
    three fields is cleared once its own repair has been attempted, successfully or not, so a
    second start never retries what this one already gave up on."""
    for channel_id, thread_ts, thread in state.repairs_pending():
        try:
            await _repair_thread(slack, limiter, state, channel_id, thread_ts, thread)
        except Exception as exc:
            logger.error(
                "could not repair a crashed thread's state in %s/%s: %s",
                channel_id,
                thread_ts,
                describe(exc),
            )


async def _repair_thread(
    slack: AsyncWebClient,
    limiter: UpdateLimiter,
    state: StateStore,
    channel_id: str,
    thread_ts: str,
    thread: ThreadState,
) -> None:
    for message_ts in thread.open_replies:
        await _repair_reply(slack, limiter, channel_id, thread_ts, message_ts)
        _safe(state.replace_open_reply, channel_id, thread_ts, message_ts, None)
    for message_ts in thread.requests:
        await delete_request(slack, channel=channel_id, ts=message_ts)
        _safe(state.remove_request, channel_id, thread_ts, message_ts)
    if thread.status is not None:
        await _repair_status(slack, channel_id, thread_ts, thread.status)
        _safe(state.set_status_pending, channel_id, thread_ts, None)


def _safe(write: Callable[..., None], *args: object) -> None:
    """A `StateStore` write is a plain synchronous file write, so it can raise like any other
    (disk full, a permission problem): best-effort here, since repair itself must never crash the
    daemon's startup over its own bookkeeping. Logged with ids only."""
    try:
        write(*args)
    except Exception as exc:
        logger.warning("could not update state.json during crash repair: %s", describe(exc))


async def _repair_reply(
    slack: AsyncWebClient, limiter: UpdateLimiter, channel_id: str, thread_ts: str, message_ts: str
) -> None:
    """Rewrite the reply's last message: its body blocks, minus the daemon's own transient status
    line (identified by `STATUS_BLOCK_ID`, the fixed block_id `ReplySink` gives it: a block posted
    with no id of its own comes back from Slack with one Slack assigned, so this is the only shape
    that survives a round trip), plus the line a shutdown writes today. Kept within
    `BLOCKS_LIMIT` (Slack's own 50-block cap, with the sink's own margin): the status line is
    dropped first, and if the body is still at the limit the stopped line replaces its last block
    rather than push the message over it. A failed read or a message already gone is logged and
    left alone: never replaced with a shorter form that would lose its content."""
    try:
        reply = await slack.conversations_replies(channel=channel_id, ts=message_ts, limit=1)
    except Exception as exc:
        logger.warning(
            "could not read a crashed reply's message in %s/%s: %s",
            channel_id,
            thread_ts,
            describe(exc),
        )
        return
    messages = reply.get("messages") or []
    message = next((m for m in messages if str(m.get("ts")) == message_ts), None)
    if message is None:
        logger.info("a crashed reply's message is gone in %s/%s", channel_id, thread_ts)
        return
    blocks = list(message.get("blocks") or [])
    if blocks and blocks[-1].get("block_id") == STATUS_BLOCK_ID:
        blocks = blocks[:-1]
    if len(blocks) >= BLOCKS_LIMIT:
        blocks[-1] = _STOPPED_BLOCK
    else:
        blocks.append(_STOPPED_BLOCK)
    try:
        await limiter.acquire()
        await slack.chat_update(
            channel=channel_id, ts=message_ts, text=_STOPPED_LINE, blocks=blocks
        )
    except Exception as exc:
        logger.warning(
            "could not rewrite a crashed reply in %s/%s: %s", channel_id, thread_ts, describe(exc)
        )


async def _repair_status(slack: AsyncWebClient, channel_id: str, thread_ts: str, name: str) -> None:
    """Set ❌ on a root left ⏳ or ✋, through the same `StatusReaction` a live session uses (issue
    #19 fix round item 9): a fresh instance's first `show` also strips every other stray
    reaction from the root on its own (D10), which is strictly more thorough than removing just
    the one name state.json recorded."""
    if name not in (Status.WORKING.value, Status.WAITING.value):
        return
    await StatusReaction(slack, channel=channel_id, root_ts=thread_ts).show(Status.ERROR)
