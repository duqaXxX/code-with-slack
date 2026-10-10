"""One reaction on a session's root message (Decision D10): working, waiting for the owner,
everything ended, error, or stopped/restarted. Adding or removing a reaction on the owner's own
message notifies nobody and only shows in the channel list on iOS (measured 2026-09-28, Slack
free plan). Arguments and error codes read from docs.slack.dev/reference/methods/reactions.add
and .../reactions.remove, 2026-09-28.

And Slack's own status line under a thread's last message (`ThreadStatus`), for as long as a
prompt is on its way or a turn runs. Arguments, the two minute timeout and the rate limit (600
calls a minute for the app) read from
docs.slack.dev/reference/methods/assistant.threads.setStatus, 2026-10-02.
"""

import asyncio
import contextlib
import enum
import logging

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from awaydesk import texts

logger = logging.getLogger(__name__)

# Slack removes a status two minutes after it was set (the method's reference): set again well
# before that.
THREAD_STATUS_REFRESH_SECONDS = 60.0
# The reference says a status is cleared "when the app sends a reply". Whether a stream append
# or an edit counts was not measured, so the status is set again this long after a write: at
# most one call every two seconds per thread while its replies are written (the method allows
# 600 a minute for the app), whichever way Slack treats them.
THREAD_STATUS_AFTER_WRITE_SECONDS = 2.0
# Answers that say the app's token cannot call the method at all (its reference's error list).
THREAD_STATUS_REFUSED = ("missing_scope", "not_allowed_token_type")


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
    per instance (an asyncio.Lock): two quick changes end on the last one. A change is two calls,
    so a caller cancelled between them leaves both reactions on the root: the instance then
    reads as a fresh one, whose next change strips the other names, and `settle` makes that
    change, to the state asked for last, for whoever cancelled it. `already_reacted` on
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
        # What `show` or `clear` was asked for last, set before any call: it differs from
        # `_current` while a change is on its way, and after one that never finished.
        self._wanted: Status | None = None

    @property
    def current(self) -> Status | None:
        """The reaction this instance last showed successfully; `None` before that, and after a
        change that was cancelled on its way."""
        return self._current

    async def show(self, state: Status) -> None:
        if StatusReaction._missing_scope:
            return
        self._wanted = state
        async with self._lock:
            if state is self._current:
                return
            previous = self._current
            try:
                if not await self._add(state):
                    return  # `_current` stays as it was: the next `show` retries the add
                if previous is not None:
                    await self._remove(previous)
                else:
                    # A fresh instance: an earlier session's reaction may still sit on this
                    # root (an idle close's DONE, a restart's ERROR). Strip every other name so
                    # the root carries exactly this one (D10).
                    for other in Status:
                        if other is not state:
                            await self._remove(other)
            except asyncio.CancelledError:
                # Issue #104: the root may carry the new name, the previous one or both. Read
                # as a fresh instance from here, so the next change strips every other name.
                self._current = None
                raise
            self._current = state

    async def clear(self) -> None:
        """Remove the current reaction, leaving the root bare: for a caller with nothing to
        revert to (D8's Cancel on a session that had shown no reaction yet)."""
        self._wanted = None
        async with self._lock:
            if self._current is None:
                return
            await self._remove(self._current)
            self._current = None

    async def settle(self) -> None:
        """Bring the root to the state asked for last, making no call when it already shows it.
        For the caller that cancelled a task in the middle of a change (a session's close, issue
        #104), which is the only one left to finish it."""
        if self._wanted is None:
            await self.clear()
        else:
            await self.show(self._wanted)

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


class ThreadStatus:
    """Slack's status line under a thread's last message, saying what `show` was last given:
    `Working…` as the sign that a prompt was received and that its turn runs, where a reply
    that has written nothing yet, or nothing for a while, gives none (issue #83), then what
    still runs once the turn has ended (`1 shell still running`, issue #95). A state that
    changes belongs here and not in a reply: a stream cannot change what it was told.
    It notifies nobody (measured 2026-09-28) and shows on desktop and on iOS once it carries
    `loading_messages` (measured 2026-10-02). `show` and `wrote` never wait on Slack: one task
    of this instance's own makes the calls, in order, so two quick changes end on the last one.
    It sets the status at once, again within THREAD_STATUS_AFTER_WRITE_SECONDS of a `wrote`,
    and every THREAD_STATUS_REFRESH_SECONDS; `show("")` clears it, only if it was set, and a
    clearing call that fails is tried once more. A
    failure is logged (channel, ts and the error code only, once per code in a row) and
    swallowed, since a status line must never break a turn. A refusal no retry can change
    (`THREAD_STATUS_REFUSED`: the token cannot call the method) stops every instance from
    calling Slack for the rest of the run, as `StatusReaction` does for `missing_scope`."""

    # Process-wide, once true: no instance calls Slack for a thread status again this run.
    _refused = False

    def __init__(
        self,
        slack: AsyncWebClient,
        *,
        channel: str,
        thread_ts: str,
        refresh: float = THREAD_STATUS_REFRESH_SECONDS,
        after_write: float = THREAD_STATUS_AFTER_WRITE_SECONDS,
    ) -> None:
        self._slack = slack
        self._channel = channel
        self._thread_ts = thread_ts
        self._refresh = refresh
        self._after_write = after_write
        self._text = ""  # what it says; empty: nothing is shown
        self._fallback = texts.THREAD_WORKING_STATUS
        self._shown = False  # whether Slack was last asked to show it
        self._due = 0.0  # when it is next set, by the loop's clock
        self._wake = asyncio.Event()
        self._keeper: asyncio.Task[None] | None = None
        self._failed: str | None = None

    @classmethod
    def refused(cls) -> bool:
        """Whether Slack has said this app's token cannot set a thread status at all: what a
        caller with something the owner must be told reads before it falls back on a message."""
        return cls._refused

    def show(self, text: str, fallback: str = texts.THREAD_WORKING_STATUS) -> None:
        """Say `text` from now on, or with an empty one stop showing the status. The same text
        again is a no-op. `fallback` says the same after the app's name, for a client that
        draws `<app name> <status>` instead of the loading message."""
        if text == self._text:
            return
        self._text, self._fallback = text, fallback
        self._due = asyncio.get_running_loop().time()
        self._kick()

    def wrote(self) -> None:
        """Something of the app's was written in this thread, which may have cleared the
        status: it is set again within THREAD_STATUS_AFTER_WRITE_SECONDS, never later than it
        was already due (writes that keep coming must not put off the refresh)."""
        if self._text:
            self._due = min(self._due, asyncio.get_running_loop().time() + self._after_write)
            self._kick()

    async def close(self) -> None:
        """Stop for good: the status is cleared if it shows, and nothing is left running."""
        self._text = ""
        keeper, self._keeper = self._keeper, None
        if keeper is not None and not keeper.done():
            keeper.cancel()
            await asyncio.gather(keeper, return_exceptions=True)
        if self._shown:
            self._shown = False
            await self._set(False)

    def _kick(self) -> None:
        self._wake.set()
        if self._keeper is None or self._keeper.done():
            self._keeper = asyncio.create_task(self._keep())

    async def _keep(self) -> None:
        retried = False
        while True:
            self._wake.clear()
            if not self._text:
                if self._shown:
                    # Cleared only once the call is back and went through: a `close` that
                    # cancels it meanwhile, or that follows a failed one, still reads the
                    # status as shown and clears it itself.
                    if await self._set(False):
                        self._shown = False
                    elif not retried:
                        # Once more after a moment: a status left standing says a session
                        # works that does not, until Slack removes it by itself.
                        retried = True
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(self._wake.wait(), self._after_write)
                        continue
                if self._wake.is_set():
                    continue  # asked for again while that call was out
                return
            retried = False
            delay = self._due - asyncio.get_running_loop().time()
            if delay <= 0:
                # Before the call, so a `wrote` that arrives while it is out still moves it.
                self._due = asyncio.get_running_loop().time() + self._refresh
                self._shown = True
                await self._set(True)
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), delay)

    async def _set(self, on: bool) -> bool:
        """Tell Slack; whether it went through, or nothing is left to try (`_refused`)."""
        if ThreadStatus._refused:
            return True
        try:
            if on:
                await self._slack.assistant_threads_setStatus(
                    channel_id=self._channel,
                    thread_ts=self._thread_ts,
                    status=self._fallback,
                    loading_messages=[self._text],
                )
            else:
                # An empty status clears it (the method's reference).
                await self._slack.assistant_threads_setStatus(
                    channel_id=self._channel, thread_ts=self._thread_ts, status=""
                )
        except Exception as exc:
            code = _describe(exc)
            if code != self._failed:
                logger.warning(
                    "assistant.threads.setStatus failed on %s/%s: %s",
                    self._channel,
                    self._thread_ts,
                    code,
                )
            self._failed = code
            if code in THREAD_STATUS_REFUSED:
                ThreadStatus._refused = True
            return ThreadStatus._refused
        self._failed = None
        return True
