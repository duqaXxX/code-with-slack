"""The app's Home tab: the owner's index of sessions. One row per thread that holds a session,
across every channel, ordered by the session's last message, each with a button that opens the
thread: a channel lists its threads by when they started, and Slack's Threads view by unread
replies (Help Center, "Use threads to organize discussions", read 2026-10-01), so neither finds
the thread worked in last.

A row is built from what the daemon already keeps and what Claude Code already knows: the thread
and its root's reaction from `state.json`, the title, branch and last message from the session's
transcript, as `!resume` shows them. Nothing is stored for the page.

`views.publish` (docs.slack.dev/reference/methods/views.publish, read 2026-10-01) takes no scope
and may be called at any time, with no event from the owner ("Home tab updates can happen when a
user isn't interacting with Slack or the app", docs.slack.dev/surfaces/app-home), so the page is
rewritten when the index changes, with no event subscribed. A view holds 100 blocks. A click on a
row's link button opens the thread in Slack and still reaches the app as a `block_actions`
payload that must be acknowledged (button element reference, read 2026-10-01): `slack_app`
acknowledges `HOME_OPEN_ACTION` and does nothing else.
Ages are Slack's own `{ago}` date token (docs.slack.dev/messaging/formatting-message-text, seen
rendered in a Home view in Block Kit Builder on 2026-10-01), so they do not go stale between two
publishes.
"""

import asyncio
import contextlib
import dataclasses
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import SDKSessionInfo
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from code_with_slack import texts
from code_with_slack.render.escape import shown_as_written
from code_with_slack.render.renderer import one_line
from code_with_slack.render.sinks import context_block, describe
from code_with_slack.render.status import Status
from code_with_slack.resume import ID_SHOWN, TITLE_LIMIT, dated
from code_with_slack.state import StateStore

logger = logging.getLogger(__name__)

HOME_BLOCKS = 100
# The header, the divider and the line that says older sessions are not shown.
HOME_ROWS = HOME_BLOCKS - 3
# A turn changes its root's reaction several times in a row (⏳, ✋ at an approval, ⏳ again):
# one publish covers the burst.
DEBOUNCE_SECONDS = 2.0
NOT_ENABLED = "not_enabled"
RATE_LIMITED = "ratelimited"
HOME_OPEN_ACTION = "home_open"
# How long a stop waits for its last publish: the page must never hold the daemon's exit.
CLOSE_SECONDS = 10.0

_WORDS = {
    Status.WORKING.value: texts.HOME_WORKING,
    Status.WAITING.value: texts.HOME_WAITING,
    Status.DONE.value: texts.HOME_ENDED,
    Status.ERROR.value: texts.HOME_ERROR,
}


@dataclass(frozen=True)
class HomeRow:
    channel_id: str
    thread_ts: str
    title: str
    # The root's reaction name (`render.status.Status.value`); None for a thread that ended
    # before the daemon kept its last reaction.
    status: str | None
    last_activity: int  # epoch seconds
    branch: str | None
    permalink: str | None = None


def _date(epoch: int, token: str) -> str:
    """Slack's date markup; the fallback is what a client that cannot render it shows."""
    fallback = datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f"<!date^{epoch}^{{{token}}}|{fallback}>"


def _row_block(row: HomeRow) -> dict[str, Any]:
    icon = f":{row.status}:  " if row.status else ""
    title = shown_as_written(one_line(row.title, TITLE_LIMIT))
    details = [
        f"<#{row.channel_id}>",
        _WORDS.get(row.status) if row.status else None,
        _date(row.last_activity, "ago"),
        f"`{shown_as_written(row.branch)}`" if row.branch else None,
    ]
    block: dict[str, Any] = {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"{icon}*{title}*\n" + " · ".join(d for d in details if d),
        },
    }
    if row.permalink is not None:
        block["accessory"] = {
            "type": "button",
            "action_id": HOME_OPEN_ACTION,
            "text": {"type": "plain_text", "text": texts.HOME_OPEN},
            "url": row.permalink,
        }
    return block


def home_view(rows: list[HomeRow], now: int, *, total: int) -> dict[str, Any]:
    """The Home tab's view: `rows` in the order given, under a line that says when the page was
    written; `total` is how many sessions there are, so a longer list says it is cut."""
    blocks: list[dict[str, Any]] = [
        context_block(texts.HOME_HEADER.format(time=_date(now, "time"))),
        {"type": "divider"},
    ]
    if not rows:
        blocks.append(context_block(texts.HOME_EMPTY))
    blocks += [_row_block(row) for row in rows]
    if total > len(rows):
        blocks.append(context_block(texts.HOME_MORE.format(rows=len(rows))))
    return {"type": "home", "blocks": blocks}


class Home:
    """Publishes the owner's Home tab. `request` asks for a publish soon and returns at once (a
    state write calls it); `publish` builds the page and sends it, and never raises: the page
    must never break a turn. Always published to the configured owner, whoever opens the app.
    `not_enabled` (the Home tab is off in the Slack app's settings) is logged once and ends the
    publishing for this run; any other failure is logged by its code and the next change tries
    again."""

    def __init__(
        self,
        slack: AsyncWebClient,
        *,
        owner_user_id: str,
        state: StateStore,
        sessions_of: Callable[[Path], list[SDKSessionInfo]],
        debounce: float = DEBOUNCE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._slack = slack
        self._owner = owner_user_id
        self._state = state
        self._sessions_of = sessions_of
        self._debounce = debounce
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._dirty = False
        self._off = False
        # A thread's permalink never changes: asked once per run. None is Slack's refusal (a
        # deleted root, most likely), which stands for the run too.
        self._links: dict[tuple[str, str], str | None] = {}
        # Per session id, its file's time when its last message was read, and that message's
        # time: a transcript is read again only once its file has changed.
        self._stamps: dict[str, tuple[int, int]] = {}

    def request(self) -> None:
        if self._off:
            return
        self._dirty = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._publish_when_settled())

    async def _publish_when_settled(self) -> None:
        while self._dirty and not self._off:
            await asyncio.sleep(self._debounce)
            self._dirty = False
            await self.publish()

    async def close(self) -> None:
        """Publish what a pending request still owed, then stop: the page a stop leaves behind
        shows the sessions as the stop left them. Gives up after `CLOSE_SECONDS`."""
        task, self._task = self._task, None
        # Waiting out its debounce or cut in the middle of a publish: owed either way.
        owed = self._dirty or (task is not None and not task.done())
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if owed:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(CLOSE_SECONDS):
                    await self.publish()
        self._off = True

    async def publish(self) -> None:
        if self._off:
            return
        try:
            rows, total = await self._rows()
            view = home_view(rows, int(self._clock()), total=total)
            await self._slack.views_publish(user_id=self._owner, view=view)
        except Exception as exc:
            code = describe(exc)
            if code == NOT_ENABLED:
                self._off = True
                logger.warning(
                    "the Home tab is not enabled in the Slack app (docs/setup.md): the session "
                    "index is not published this run"
                )
            else:
                logger.warning("could not publish the session index: %s", code)

    async def _rows(self) -> tuple[list[HomeRow], int]:
        held = [(c, ts, t) for c, ts, t in self._state.threads() if t.session_id is not None]
        wanted: dict[Path, set[str]] = {}
        for _, _, thread in held:
            wanted.setdefault(thread.directory, set()).add(str(thread.session_id))
        infos = await asyncio.to_thread(self._infos, wanted)
        rows = []
        for channel_id, thread_ts, thread in held:
            info = infos.get(str(thread.session_id))
            rows.append(
                HomeRow(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    title=(info.summary if info is not None else "")
                    or texts.HOME_UNTITLED.format(id=str(thread.session_id)[:ID_SHOWN]),
                    # ❌ over a kept ⏳ or ✋ (an answer that never reached Slack) shows ❌.
                    status=thread.ended or thread.status,
                    # A session Claude Code does not list yet: its root message's time.
                    last_activity=info.last_modified // 1000
                    if info is not None
                    else int(float(thread_ts)),
                    branch=info.git_branch if info is not None else None,
                )
            )
        rows.sort(key=lambda row: row.last_activity, reverse=True)
        shown = [
            dataclasses.replace(row, permalink=await self._permalink(row.channel_id, row.thread_ts))
            for row in rows[:HOME_ROWS]
        ]
        return shown, len(rows)

    def _infos(self, wanted: dict[Path, set[str]]) -> dict[str, SDKSessionInfo]:
        """The sessions the threads hold, by id, each dated by its last message. Blocking file
        reads: runs off the event loop. A folder that cannot be listed is logged and skipped;
        one whose transcripts cannot be dated keeps its titles, dated by their files."""
        found: dict[str, SDKSessionInfo] = {}
        for directory, session_ids in wanted.items():
            try:
                listed = [s for s in self._sessions_of(directory) if s.session_id in session_ids]
            except Exception as exc:
                logger.warning(
                    "could not list a folder's sessions for the session index: %s", describe(exc)
                )
                continue
            changed = [
                s for s in listed if self._stamps.get(s.session_id, (None, 0))[0] != s.last_modified
            ]
            try:
                for before, after in zip(changed, dated(directory, changed), strict=True):
                    self._stamps[before.session_id] = (before.last_modified, after.last_modified)
            except Exception as exc:
                logger.warning(
                    "could not date a folder's sessions for the session index: %s", describe(exc)
                )
            for session in listed:
                stamp = self._stamps.get(session.session_id)
                found[session.session_id] = dataclasses.replace(
                    session, last_modified=stamp[1] if stamp else session.last_modified
                )
        return found

    async def _permalink(self, channel_id: str, thread_ts: str) -> str | None:
        key = (channel_id, thread_ts)
        if key not in self._links:
            try:
                answer = await self._slack.chat_getPermalink(
                    channel=channel_id, message_ts=thread_ts
                )
            except Exception as exc:
                code = describe(exc)
                logger.warning(
                    "could not get a permalink for %s/%s: %s", channel_id, thread_ts, code
                )
                if isinstance(exc, SlackApiError) and code != RATE_LIMITED:
                    self._links[key] = None
                return None
            self._links[key] = str(answer["permalink"])
        return self._links[key]
