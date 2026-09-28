"""Repair what a crashed daemon left open (issue #19): a reply still saying Claude is writing,
an approval, question or D8 hold request still carrying buttons, and the ⏳/✋ reaction a turn
mid-flight left on its root. Runs once on start, after connecting to Slack and before
`state.prune` (a pruned thread's leftovers must still be repaired); a graceful stop clears these
same fields itself, so a second start finds nothing to do.

`conversations.replies` (docs.slack.dev/reference/methods/conversations.replies, read 2026-09-28):
`oldest`/`latest` set to the same ts with `inclusive=True` and `limit=1` isolates one message by
its own ts.
"""

import logging

from slack_sdk.web.async_client import AsyncWebClient

from code_with_slack import texts
from code_with_slack.render.sinks import UpdateLimiter, describe
from code_with_slack.render.status import Status
from code_with_slack.state import StateStore, ThreadState

logger = logging.getLogger(__name__)

# What a shutdown appends to a reply cut short (`ThreadSession.close`'s own `line`, texts.ENDED
# with its default reason): reused verbatim, so a repaired reply reads exactly as one closed by
# a graceful stop would.
_STOPPED_LINE = texts.ENDED.format(reason=texts.ENDED_SHUTDOWN)


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
    if thread.open_reply is not None:
        await _repair_reply(slack, limiter, channel_id, thread_ts, thread.open_reply)
        state.set_open_reply(channel_id, thread_ts, None)
    for message_ts in thread.requests:
        await _repair_request(slack, channel_id, thread_ts, message_ts)
        state.remove_request(channel_id, thread_ts, message_ts)
    if thread.status is not None:
        await _repair_status(slack, channel_id, thread_ts, thread.status)
        state.set_status_pending(channel_id, thread_ts, None)


async def _repair_reply(
    slack: AsyncWebClient, limiter: UpdateLimiter, channel_id: str, thread_ts: str, message_ts: str
) -> None:
    """Rewrite the reply's last message: its body blocks, minus the daemon's own transient
    status line (a context block with no block_id, the shape `ReplySink._render` gives it, never
    identified by matching its rendered text), plus the line a shutdown writes today. A failed
    read or a message already gone is logged and left alone: never replaced with a shorter form
    that would lose its content."""
    try:
        reply = await slack.conversations_replies(
            channel=channel_id,
            ts=thread_ts,
            oldest=message_ts,
            latest=message_ts,
            inclusive=True,
            limit=1,
        )
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
    if blocks and blocks[-1].get("type") == "context" and "block_id" not in blocks[-1]:
        blocks = blocks[:-1]
    blocks.append({"type": "markdown", "text": _STOPPED_LINE})
    try:
        await limiter.acquire()
        await slack.chat_update(
            channel=channel_id, ts=message_ts, text=_STOPPED_LINE, blocks=blocks
        )
    except Exception as exc:
        logger.warning(
            "could not rewrite a crashed reply in %s/%s: %s", channel_id, thread_ts, describe(exc)
        )


async def _repair_request(
    slack: AsyncWebClient, channel_id: str, thread_ts: str, message_ts: str
) -> None:
    """Delete a stale request, as `remove_request`/`ThreadSession._delete_request` do;
    `message_not_found` counts as done."""
    try:
        await slack.chat_delete(channel=channel_id, ts=message_ts)
    except Exception as exc:
        if describe(exc) != "message_not_found":
            logger.warning(
                "could not remove a stale request in %s/%s: %s",
                channel_id,
                thread_ts,
                describe(exc),
            )


async def _repair_status(slack: AsyncWebClient, channel_id: str, thread_ts: str, name: str) -> None:
    """Set ❌ on a root left ⏳ or ✋; `already_reacted`/`no_reaction` count as done, as
    `StatusReaction` treats them."""
    if name not in (Status.WORKING.value, Status.WAITING.value):
        return
    try:
        await slack.reactions_remove(channel=channel_id, timestamp=thread_ts, name=name)
    except Exception as exc:
        if describe(exc) != "no_reaction":
            logger.warning(
                "could not clear a crashed root's reaction in %s/%s: %s",
                channel_id,
                thread_ts,
                describe(exc),
            )
    try:
        await slack.reactions_add(channel=channel_id, timestamp=thread_ts, name=Status.ERROR.value)
    except Exception as exc:
        if describe(exc) != "already_reacted":
            logger.warning(
                "could not set the stopped reaction in %s/%s: %s",
                channel_id,
                thread_ts,
                describe(exc),
            )
