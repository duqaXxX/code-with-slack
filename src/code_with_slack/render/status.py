"""One reaction on a session's root message (Decision D10): working, waiting for the owner,
everything ended, error, or stopped/restarted. Adding or removing a reaction on the owner's own
message notifies nobody and only shows in the channel list on iOS (measured 2026-09-28, Slack
free plan). Arguments and error codes read from docs.slack.dev/reference/methods/reactions.add
and .../reactions.remove, 2026-09-28.
"""

import asyncio
import enum
import logging

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

logger = logging.getLogger(__name__)


class Status(enum.Enum):
    WORKING = "hourglass_flowing_sand"
    WAITING = "raised_hand"
    DONE = "white_check_mark"
    ERROR = "x"


def _describe(exc: Exception) -> str:
    """Slack's error code, or the exception type: never the request or its content."""
    if isinstance(exc, SlackApiError):
        return str(exc.response.get("error"))
    return type(exc).__name__


class StatusReaction:
    """Keeps one reaction on `root_ts` in sync with the session's state. Showing the same state
    again makes no call. A change adds the new reaction before removing the previous one, so the
    root is never bare between them. Calls are serialized per instance (an asyncio.Lock): two
    quick changes end on the last one. `already_reacted` on add and `no_reaction` on remove count
    as done; any other failure is logged (channel, ts and the error code only) and swallowed,
    since a reaction must never break a turn."""

    def __init__(self, slack: AsyncWebClient, *, channel: str, root_ts: str) -> None:
        self._slack = slack
        self._channel = channel
        self._root_ts = root_ts
        self._lock = asyncio.Lock()
        self._current: Status | None = None

    async def show(self, state: Status) -> None:
        async with self._lock:
            if state is self._current:
                return
            previous = self._current
            await self._add(state)
            if previous is not None:
                await self._remove(previous)
            self._current = state

    async def _add(self, state: Status) -> None:
        try:
            await self._slack.reactions_add(
                channel=self._channel, timestamp=self._root_ts, name=state.value
            )
        except Exception as exc:
            code = _describe(exc)
            if code != "already_reacted":
                logger.warning(
                    "reactions.add failed on %s/%s: %s", self._channel, self._root_ts, code
                )

    async def _remove(self, state: Status) -> None:
        try:
            await self._slack.reactions_remove(
                channel=self._channel, timestamp=self._root_ts, name=state.value
            )
        except Exception as exc:
            code = _describe(exc)
            if code != "no_reaction":
                logger.warning(
                    "reactions.remove failed on %s/%s: %s", self._channel, self._root_ts, code
                )
