"""Questions that hold a message before it is sent. D8: before a message wakes an idle session
while a live session of another thread (any channel) is busy in the same resolved folder, the
daemon asks `Another session is working in this folder: <link>. Send anyway?`, with two buttons,
`Send anyway` and `Don't send`, which the code calls Continue and Cancel.
The session setup (`setup.py`) is the other one: model, effort and bypass, then Start.

Kept in memory only, like `approvals.Approvals`: a hold never outlives the process (`!stop`, a
top-level `!stop` of its channel, and a drain all cancel one exactly as Cancel does), so nothing
here needs to survive a restart. A thread has at most one open at a time, whichever kind.
"""

import asyncio
import secrets
from dataclasses import dataclass, field
from typing import Any

from awaydesk import texts

HOLD_CONTINUE = "hold_continue"
HOLD_CANCEL = "hold_cancel"


@dataclass
class Pending:
    channel_id: str
    thread_ts: str
    # What the owner chose (D8: True for Continue; the setup: its `Choice`), or None when the
    # question was cancelled (Cancel, `!stop`, a drain).
    future: asyncio.Future[Any]
    message_ts: str | None = field(default=None)
    # What the asker needs back with a click that carries only the id (the setup's model list).
    context: Any = None
    # Set by `Holds.cancel` while an answer is being applied (the setup's Start settling): the
    # asker sends nothing once it is done.
    cancelled: bool = False


class Holds:
    """Questions waiting for the owner, by an id that only the posted buttons carry (as
    `approvals.Approvals` keys its own requests)."""

    def __init__(self) -> None:
        self._pending: dict[str, Pending] = {}

    def open(self, channel_id: str, thread_ts: str, context: Any = None) -> tuple[str, Pending]:
        hold_id = secrets.token_urlsafe(16)
        pending = Pending(
            channel_id, thread_ts, asyncio.get_running_loop().create_future(), context=context
        )
        self._pending[hold_id] = pending
        return hold_id, pending

    def get(self, hold_id: str) -> Pending | None:
        return self._pending.get(hold_id)

    def resolve(self, hold_id: str, channel_id: str, thread_ts: str, value: Any) -> Pending | None:
        """Resolve once with `value` (None cancels), and only from the channel and thread the
        request was posted in. A value keeps the entry until the asker `discard`s it, so a stop
        that arrives while the answer is applied still finds it (`cancel`)."""
        pending = self._pending.get(hold_id)
        if (
            pending is None
            or pending.channel_id != channel_id
            or pending.thread_ts != thread_ts
            or pending.future.done()
        ):
            return None
        pending.future.set_result(value)
        if value is None:
            del self._pending[hold_id]
        return pending

    def posted(self, hold_id: str, message_ts: str) -> bool:
        """Record the message that shows a hold, so it can be removed once decided; False when
        the hold was decided before its message was known (a click or `!stop` while posting)."""
        pending = self._pending.get(hold_id)
        if pending is None:
            return False
        pending.message_ts = message_ts
        return not pending.future.done()

    def cancel(self, channel_id: str, thread_ts: str) -> Pending | None:
        """Cancel the hold open in this thread, if any: the same outcome as the owner clicking
        Cancel (`!stop`, a top-level `!stop` of its channel, a drain). One already answered but
        still being applied is flagged `cancelled` instead: its asker sends nothing."""
        for hold_id, pending in list(self._pending.items()):
            if pending.channel_id != channel_id or pending.thread_ts != thread_ts:
                continue
            if not pending.future.done():
                pending.future.set_result(None)
                del self._pending[hold_id]
                return pending
            if not pending.cancelled:
                pending.cancelled = True
                return pending
        return None

    def at_message(self, channel_id: str, thread_ts: str, message_ts: str) -> str | None:
        """The id of the open hold shown by this message, for a control that carries no id of its
        own (a select): None when it is decided, gone, or shown elsewhere."""
        for hold_id, pending in self._pending.items():
            if (
                pending.channel_id == channel_id
                and pending.thread_ts == thread_ts
                and pending.message_ts == message_ts
                and not pending.future.done()
            ):
                return hold_id
        return None

    def discard(self, hold_id: str) -> None:
        self._pending.pop(hold_id, None)


def hold_blocks(hold_id: str, link: str) -> list[dict[str, Any]]:
    """The question in the thread: a `mrkdwn` section (`link` in mrkdwn's own `<url|label>` form,
    same as approvals and the resume picker use for their own buttons) plus Continue and Cancel.
    Only Continue is `primary`: one button of a set, and never `danger` for an answer that
    destroys nothing (button element reference, docs.slack.dev, read 2026-10-07)."""
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": texts.HOLD_QUESTION.format(link=link)},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": HOLD_CONTINUE,
                    "value": hold_id,
                    "style": "primary",
                    "text": {"type": "plain_text", "text": texts.HOLD_CONTINUE_BUTTON},
                },
                {
                    "type": "button",
                    "action_id": HOLD_CANCEL,
                    "value": hold_id,
                    "text": {"type": "plain_text", "text": texts.HOLD_CANCEL_BUTTON},
                },
            ],
        },
    ]
