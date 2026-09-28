"""D8: two busy sessions in one folder. Before a message wakes an idle session while a live
session of another thread (any channel) is busy in the same resolved folder, the daemon asks
`Another session is working in this folder: <link>. Send anyway?`, with Continue and Cancel.

Kept in memory only, like `approvals.Approvals`: a hold never outlives the process (`!stop`, a
top-level `!stop` of its channel, and a drain all cancel one exactly as Cancel does), so nothing
here needs to survive a restart.
"""

import asyncio
import secrets
from dataclasses import dataclass, field
from typing import Any

from code_with_slack import texts

HOLD_CONTINUE = "hold_continue"
HOLD_CANCEL = "hold_cancel"


@dataclass
class Pending:
    channel_id: str
    thread_ts: str
    future: asyncio.Future[bool]  # True: Continue was chosen; False: cancelled
    message_ts: str | None = field(default=None)


class Holds:
    """Questions waiting for the owner, by an id that only the posted buttons carry (as
    `approvals.Approvals` keys its own requests)."""

    def __init__(self) -> None:
        self._pending: dict[str, Pending] = {}

    def open(self, channel_id: str, thread_ts: str) -> tuple[str, Pending]:
        hold_id = secrets.token_urlsafe(16)
        pending = Pending(channel_id, thread_ts, asyncio.get_running_loop().create_future())
        self._pending[hold_id] = pending
        return hold_id, pending

    def get(self, hold_id: str) -> Pending | None:
        return self._pending.get(hold_id)

    def resolve(
        self, hold_id: str, channel_id: str, thread_ts: str, *, continue_: bool
    ) -> Pending | None:
        """Resolve once, and only from the channel and thread the request was posted in."""
        pending = self._pending.get(hold_id)
        if (
            pending is None
            or pending.channel_id != channel_id
            or pending.thread_ts != thread_ts
            or pending.future.done()
        ):
            return None
        pending.future.set_result(continue_)
        del self._pending[hold_id]
        return pending

    def posted(self, hold_id: str, message_ts: str) -> bool:
        """Record the message that shows a hold, so it can be removed once decided; False when
        the hold was decided before its message was known (`!stop` while posting)."""
        pending = self._pending.get(hold_id)
        if pending is None:
            return False
        pending.message_ts = message_ts
        return True

    def cancel(self, channel_id: str, thread_ts: str) -> Pending | None:
        """Cancel the hold open in this thread, if any: the same outcome as the owner clicking
        Cancel (`!stop`, a top-level `!stop` of its channel, a drain)."""
        for hold_id, pending in list(self._pending.items()):
            if (
                pending.channel_id == channel_id
                and pending.thread_ts == thread_ts
                and not pending.future.done()
            ):
                pending.future.set_result(False)
                del self._pending[hold_id]
                return pending
        return None

    def discard(self, hold_id: str) -> None:
        self._pending.pop(hold_id, None)


def hold_blocks(hold_id: str, link: str) -> list[dict[str, Any]]:
    """The question in the thread: a `mrkdwn` section (`link` in mrkdwn's own `<url|label>` form,
    same as approvals and the resume picker use for their own buttons) plus Continue and Cancel."""
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
