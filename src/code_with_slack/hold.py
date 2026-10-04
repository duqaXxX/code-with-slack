"""Questions that hold a message before it is sent. The one question today is the session setup
(`setup.py`): model, effort and bypass, then Start.

Kept in memory only, like `approvals.Approvals`: a hold never outlives the process (`!stop`, a
top-level `!stop` of its channel, and a drain all cancel one), so nothing here needs to survive a
restart. A thread has at most one open at a time.
"""

import asyncio
import secrets
from dataclasses import dataclass, field
from typing import Any

from code_with_slack.setup import Choice


@dataclass
class Pending:
    channel_id: str
    thread_ts: str
    # What the owner chose, or None when the question was cancelled (`!stop`, a drain).
    future: asyncio.Future[Choice | None]
    message_ts: str | None = field(default=None)
    # The model list the message was built from: a click carries only the id and the controls'
    # state, which is read against it.
    models: list[dict[str, Any]] = field(default_factory=list)
    # Set by `Holds.cancel` while an answer is being applied (the setup's Start settling): the
    # asker sends nothing once it is done.
    cancelled: bool = False


class Holds:
    """Questions waiting for the owner, by an id that only the posted buttons carry (as
    `approvals.Approvals` keys its own requests)."""

    def __init__(self) -> None:
        self._pending: dict[str, Pending] = {}

    def open(
        self, channel_id: str, thread_ts: str, models: list[dict[str, Any]]
    ) -> tuple[str, Pending]:
        hold_id = secrets.token_urlsafe(16)
        pending = Pending(
            channel_id, thread_ts, asyncio.get_running_loop().create_future(), models=models
        )
        self._pending[hold_id] = pending
        return hold_id, pending

    def get(self, hold_id: str) -> Pending | None:
        return self._pending.get(hold_id)

    def resolve(
        self, hold_id: str, channel_id: str, thread_ts: str, value: Choice | None
    ) -> Pending | None:
        """Resolve once with `value` (None cancels), and only from the channel and thread the
        request was posted in. A choice keeps the entry until the asker `discard`s it, so a stop
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
        """Cancel the hold open in this thread, if any: the same outcome for every way to cancel
        (`!stop`, a top-level `!stop` of its channel, a drain). One already answered but
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
