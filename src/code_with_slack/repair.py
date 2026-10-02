"""Repair what a crashed daemon left open (issue #19): a reply whose stream never stopped or
whose cards still run, an approval, question or D8 hold request still carrying buttons, and the
⏳/✋ reaction a turn mid-flight left on its root. Runs once on start, right after `auth.test` and
before opening the Socket Mode connection (the web client works without it) and before
`state.prune` (a pruned thread's leftovers must still be repaired); a graceful stop clears these
same fields itself, so a second start finds nothing to do.

A reply's message is stopped first (`chat.stopStream`; Slack ends a stream itself 5 minutes after
it started, and answers `message_not_in_streaming_state` to a stop then), which is the one
notification the message owes; the edit that follows says it stopped and never notifies, and
nothing is posted.

`conversations.replies` (docs.slack.dev/reference/methods/conversations.replies, read 2026-09-28,
confirmed against a real `conversations.replies` response recorded from a real workspace on
2026-09-28, scrubbed and kept as `tests/fixtures/slack/api-conversations-replies-by-ts.json`, and
of a stopped stream on 2026-09-29, `api-conversations-replies-stream.json`): `ts` set to the
reply's own ts, with `limit=1`, returns only that one message (an `oldest`/`latest`/`inclusive`
range built around the same ts instead returned the thread's root too).
"""

import logging
from collections.abc import Callable
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from code_with_slack import texts
from code_with_slack.render.sinks import (
    NOT_STREAMING,
    UpdateLimiter,
    context_block,
    delete_request,
    describe,
    is_still_running,
)
from code_with_slack.render.status import Status, StatusReaction
from code_with_slack.state import StateStore, ThreadState

logger = logging.getLogger(__name__)

_STOPPED_BLOCK = context_block(texts.STOPPED_BEFORE_ANSWER)
# What a card left running becomes: it never ended, and will not.
_RUNNING_CARD = ("pending", "in_progress")
# Slack's own cap: the read-back count is Slack's (streamed markdown reads back as several
# blocks), not the sink's, which keeps a margin under it for what it writes itself.
SLACK_BLOCKS = 50


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
        ended = await _repair_status(slack, channel_id, thread_ts, thread.status)
        _safe(state.set_status_pending, channel_id, thread_ts, None, ended)


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
    """Stop the reply's message, then rewrite it: its blocks as Slack keeps them, without a line
    of what still runs (`is_still_running`), every card left running closed as an error (a
    stopped message would show it so anyway, until it is updated: measured 2026-09-28), plus
    the line that says it stopped. Kept within Slack's 50 blocks: at
    the cap the line goes into the last context block (the footer), or is left out if there is
    none; a block that holds content is never replaced. A failed read or a message already gone
    is logged and left alone: never replaced with a shorter form that would lose its content."""
    try:
        await slack.chat_stopStream(channel=channel_id, ts=message_ts)
    except Exception as exc:
        if describe(exc) != NOT_STREAMING:  # the stream is over already: nothing to stop
            logger.warning(
                "could not stop a crashed reply's stream in %s/%s: %s",
                channel_id,
                thread_ts,
                describe(exc),
            )
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
    blocks = [
        {**block, "status": "error"}
        if block.get("type") == "task_card" and block.get("status") in _RUNNING_CARD
        else block
        for block in message.get("blocks") or []
        # Nothing still runs: the tasks went with the process.
        if not is_still_running(block)
    ]
    if len(blocks) < SLACK_BLOCKS:
        blocks.append(_STOPPED_BLOCK)
    else:
        _say_stopped_in_the_footer(blocks)
    try:
        await limiter.acquire()
        await slack.chat_update(
            channel=channel_id, ts=message_ts, text=texts.STOPPED_BEFORE_ANSWER, blocks=blocks
        )
    except Exception as exc:
        logger.warning(
            "could not rewrite a crashed reply in %s/%s: %s", channel_id, thread_ts, describe(exc)
        )


def _say_stopped_in_the_footer(blocks: list[dict[str, Any]]) -> None:
    """Add the stopped line to the text of the last context block, in place; nothing when the
    message has none: the edit's own `text` says it."""
    last = next((b for b in reversed(blocks) if b.get("type") == "context"), None)
    if last is None or not last.get("elements"):
        return
    first = last["elements"][0]
    if first.get("type") not in ("mrkdwn", "plain_text"):
        return
    joined = f"{first.get('text', '')} · {texts.STOPPED_BEFORE_ANSWER}".lstrip(" ·")
    last["elements"] = [{**first, "text": joined}, *last["elements"][1:]]


async def _repair_status(
    slack: AsyncWebClient, channel_id: str, thread_ts: str, name: str
) -> str | None:
    """Set ❌ on a root left ⏳ or ✋, through the same `StatusReaction` a live session uses (issue
    #19 fix round item 9): a fresh instance's first `show` also strips every other stray
    reaction from the root on its own (D10), which is strictly more thorough than removing just
    the one name state.json recorded. Returns the reaction asked for, the thread's ended one
    from now on; None for a name that is neither."""
    if name not in (Status.WORKING.value, Status.WAITING.value):
        return None
    await StatusReaction(slack, channel=channel_id, root_ts=thread_ts).show(Status.ERROR)
    return Status.ERROR.value
