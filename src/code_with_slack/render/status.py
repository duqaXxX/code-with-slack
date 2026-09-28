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
    root is never bare between them, and only once the add succeeded (or was already there):
    a failed add leaves `_current` as it was, so the next `show` retries rather than stripping
    the root's own reaction for nothing. A fresh instance (a new session's reaction, next to
    whatever an earlier one already left on the same root) also strips the other three names on
    its own first successful add, so the root never carries more than one. Calls are serialized
    per instance (an asyncio.Lock): two quick changes end on the last one. `already_reacted` on
    add and `no_reaction` on remove count as done. `missing_scope` (the workspace has not
    reinstalled the app for `reactions:write`) is logged once for the whole process, and every
    instance stops calling Slack for reactions from then on; any other failure is logged
    (channel, ts and the error code only) and swallowed, since a reaction must never break a
    turn."""

    # Process-wide, once true: no instance calls Slack for reactions again this run.
    _missing_scope = False

    def __init__(self, slack: AsyncWebClient, *, channel: str, root_ts: str) -> None:
        self._slack = slack
        self._channel = channel
        self._root_ts = root_ts
        self._lock = asyncio.Lock()
        self._current: Status | None = None

    @property
    def current(self) -> Status | None:
        """The reaction this instance last showed successfully; `None` before that."""
        return self._current

    async def show(self, state: Status) -> None:
        if StatusReaction._missing_scope:
            return
        async with self._lock:
            if state is self._current:
                return
            previous = self._current
            if not await self._add(state):
                return  # `_current` stays as it was: the next `show` retries the add
            if previous is not None:
                await self._remove(previous)
            else:
                # A fresh instance: an earlier session's reaction may still sit on this root
                # (an idle close's DONE, a restart's ERROR). Strip every other name so the root
                # carries exactly this one (D10).
                for other in Status:
                    if other is not state:
                        await self._remove(other)
            self._current = state

    async def clear(self) -> None:
        """Remove the current reaction, leaving the root bare: for a caller with nothing to
        revert to (D8's Cancel on a session that had shown no reaction yet)."""
        async with self._lock:
            if self._current is None:
                return
            await self._remove(self._current)
            self._current = None

    async def _add(self, state: Status) -> bool:
        """Whether the root now carries `state`: true on success or `already_reacted`."""
        try:
            await self._slack.reactions_add(
                channel=self._channel, timestamp=self._root_ts, name=state.value
            )
        except Exception as exc:
            code = _describe(exc)
            if code == "already_reacted":
                return True
            if code == "missing_scope":
                self._note_missing_scope()
            else:
                logger.warning(
                    "reactions.add failed on %s/%s: %s", self._channel, self._root_ts, code
                )
            return False
        return True

    async def _remove(self, state: Status) -> None:
        try:
            await self._slack.reactions_remove(
                channel=self._channel, timestamp=self._root_ts, name=state.value
            )
        except Exception as exc:
            code = _describe(exc)
            if code == "no_reaction":
                return
            if code == "missing_scope":
                self._note_missing_scope()
            else:
                logger.warning(
                    "reactions.remove failed on %s/%s: %s", self._channel, self._root_ts, code
                )

    def _note_missing_scope(self) -> None:
        if not StatusReaction._missing_scope:
            logger.warning(
                "reactions.add/remove failed on %s/%s: missing_scope (reactions:write); "
                "the app needs reinstalling with the current scopes; no more reactions "
                "will be attempted this run",
                self._channel,
                self._root_ts,
            )
        StatusReaction._missing_scope = True
