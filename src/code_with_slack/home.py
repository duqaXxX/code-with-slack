"""The app's Home tab: the owner's index of sessions. One card per thread that holds a session,
side by side in a carousel per channel, each channel and each card ordered by the session's last
message, with a button that opens the thread: a channel lists its threads by when they started,
and Slack's Threads view by unread replies (Help Center, "Use threads to organize discussions",
read 2026-10-01), so neither finds the thread worked in last.

A card is built from what the daemon already keeps and what Claude Code already knows: the thread
and its root's reaction from `state.json`, the title and last message from the session's
transcript, as `!resume` shows them. Nothing is stored for the page: the filters the owner
chooses live in memory and start again at their defaults with the daemon.

`views.publish` (docs.slack.dev/reference/methods/views.publish, read 2026-10-01) takes no scope
and may be called at any time, with no event from the owner ("Home tab updates can happen when a
user isn't interacting with Slack or the app", docs.slack.dev/surfaces/app-home), so the page is
rewritten when the index changes, with no event subscribed. A view holds 100 blocks, and a
carousel, one block, holds 10 cards (docs.slack.dev/reference/block-kit/blocks/carousel-block
and card-block, read 2026-10-01: both work in a Home tab). Ages are Slack's own `{ago}` date
token (docs.slack.dev/messaging/formatting-message-text, seen rendered in a Home view on
2026-10-01), so they do not go stale between two publishes.

What reaches the app from the page is a `block_actions` payload per use of a control
(`slack_app` owns the listeners): a filter, which carries the state of every control in
`view.state.values`, and a link button, which Slack follows itself and still reports (button
element reference, read 2026-10-01). **New thread** is the documented deep link to the channel
(docs.slack.dev/interactivity/deep-linking; it opened the channel in the desktop app on
2026-10-01): the owner's own top-level message there starts the session, so the thread is one
the owner started and its replies notify.
"""

import asyncio
import contextlib
import dataclasses
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
# What a channel shows while no filter is chosen; a filter shows every session it matches.
PER_CHANNEL = 5
# A carousel holds 10 cards and a card's title 150 characters (the two blocks' references).
CAROUSEL_CARDS = 10
CARD_TITLE = 150
# A turn changes its root's reaction several times in a row (⏳, ✋ at an approval, ⏳ again):
# one publish covers the burst.
DEBOUNCE_SECONDS = 2.0
# How long a stop waits for its last publish: the page must never hold the daemon's exit.
CLOSE_SECONDS = 10.0
# How many permalinks are asked for at once on the first publish of a run.
PERMALINKS_AT_ONCE = 8
NOT_ENABLED = "not_enabled"
RATE_LIMITED = "ratelimited"

FILTERS_BLOCK = "home_filters"
SEARCH_BLOCK = "home_search"
CHANNEL_ACTION = "home_channel"
STATUS_ACTION = "home_status"
DATE_ACTION = "home_date"
SEARCH_ACTION = "home_search_text"
SHOW_ALL_ACTION = "home_show_all"
HOME_OPEN_ACTION = "home_open"
NEW_THREAD_ACTION = "home_new_thread"
FILTER_ACTIONS = (CHANNEL_ACTION, STATUS_ACTION, DATE_ACTION, SEARCH_ACTION)
LINK_ACTIONS = (HOME_OPEN_ACTION, NEW_THREAD_ACTION)
ALL = "all"
SEARCH_LIMIT = 80
# A select option's text holds 75 characters (option object reference).
OPTION_TEXT = 75

_WORDS = {
    Status.WAITING.value: texts.HOME_WAITING,
    Status.WORKING.value: texts.HOME_WORKING,
    Status.DONE.value: texts.HOME_ENDED,
    Status.ERROR.value: texts.HOME_ERROR,
}
LAST_48, TODAY, YESTERDAY, LAST_7, LAST_30 = "48h", "today", "yesterday", "7", "30"
# How far back each rolling period reaches.
_REACH = {LAST_48: timedelta(hours=48), LAST_7: timedelta(days=7), LAST_30: timedelta(days=30)}
_DATES = {
    LAST_48: texts.HOME_LAST_48,
    TODAY: texts.HOME_TODAY,
    YESTERDAY: texts.HOME_YESTERDAY,
    LAST_7: texts.HOME_LAST_7,
    LAST_30: texts.HOME_LAST_30,
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
    permalink: str


@dataclass(frozen=True)
class HomeFilter:
    """What the owner chose in the page's controls. The page starts on the last 48 hours
    (the maintainer, 2026-10-01); `date` None is any time, and the other fields unset match all."""

    channel: str | None = None
    status: str | None = None
    date: str | None = LAST_48
    search: str = ""

    @property
    def narrowed(self) -> bool:
        """Whether a channel, a status or a search is chosen: the page then shows every session
        that matches. The period alone keeps the page's shape, each channel cut to its newest."""
        return bool(self.channel or self.status or self.search)

    def matches(self, row: HomeRow, now: datetime) -> bool:
        """Whether `row` passes every chosen filter. The date is the session's last message, on
        `now`'s calendar: `yesterday` is that day alone, the others reach back from now."""
        if self.channel and row.channel_id != self.channel:
            return False
        if self.status and row.status != self.status:
            return False
        if self.search and self.search.casefold() not in row.title.casefold():
            return False
        when = datetime.fromtimestamp(row.last_activity, now.tzinfo)
        if self.date in (TODAY, YESTERDAY):
            return (now.date() - when.date()).days == (0 if self.date == TODAY else 1)
        if self.date in _REACH:
            return now - when <= _REACH[self.date]
        return True


def read_filter(values: dict[str, Any], current: HomeFilter) -> HomeFilter:
    """The filter a use of a control leaves chosen, from the `view.state.values` its payload
    carries: every control's state rides on it, so nothing is tracked per control. Untrusted
    like every click: a status or a date that is not one of the page's own keeps the current
    one, and the channel is checked by `Home.choose`, which knows the bound ones."""
    selects = values.get(FILTERS_BLOCK)
    selects = selects if isinstance(selects, dict) else {}

    def picked(action_id: str, now_chosen: str | None, known: Any) -> str | None:
        control = selects.get(action_id)
        if not isinstance(control, dict):
            return now_chosen
        value = (control.get("selected_option") or {}).get("value")
        if value == ALL:
            return None
        if not isinstance(value, str) or (known is not None and value not in known):
            return now_chosen
        return value

    search = current.search
    box = (values.get(SEARCH_BLOCK) or {}).get(SEARCH_ACTION)
    if isinstance(box, dict):
        typed = box.get("value")
        search = one_line(typed, SEARCH_LIMIT) if isinstance(typed, str) else ""
    return HomeFilter(
        channel=picked(CHANNEL_ACTION, current.channel, None),
        status=picked(STATUS_ACTION, current.status, _WORDS),
        date=picked(DATE_ACTION, current.date, _DATES),
        search=search,
    )


def _date(epoch: int, token: str) -> str:
    """Slack's date markup; the fallback is what a client that cannot render it shows."""
    fallback = datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f"<!date^{epoch}^{{{token}}}|{fallback}>"


def _link_button(text: str, url: str, action_id: str) -> dict[str, Any]:
    return {
        "type": "button",
        "action_id": action_id,
        "text": {"type": "plain_text", "text": text},
        "url": url,
    }


def _select(action_id: str, options: list[tuple[str, str]], chosen: str | None) -> dict[str, Any]:
    """A menu that starts on `chosen`, or on its `ALL` option when nothing is chosen."""
    built = [
        {"text": {"type": "plain_text", "text": text}, "value": value} for text, value in options
    ]
    initial = next(option for option in built if option["value"] == (chosen or ALL))
    return {
        "type": "static_select",
        "action_id": action_id,
        "options": built,
        "initial_option": initial,
    }


def _controls(channels: dict[str, str], chosen: HomeFilter) -> list[dict[str, Any]]:
    search: dict[str, Any] = {
        "type": "plain_text_input",
        "action_id": SEARCH_ACTION,
        "placeholder": {"type": "plain_text", "text": texts.HOME_SEARCH_HINT},
        "dispatch_action_config": {"trigger_actions_on": ["on_enter_pressed"]},
    }
    if chosen.search:
        search["initial_value"] = chosen.search
    return [
        {
            "type": "actions",
            "block_id": FILTERS_BLOCK,
            "elements": [
                _select(
                    CHANNEL_ACTION,
                    [
                        (texts.HOME_ALL_CHANNELS, ALL),
                        *((one_line(name, OPTION_TEXT), cid) for cid, name in channels.items()),
                    ],
                    chosen.channel,
                ),
                _select(
                    STATUS_ACTION,
                    [
                        (texts.HOME_ALL_STATUSES, ALL),
                        *((word.capitalize(), name) for name, word in _WORDS.items()),
                    ],
                    chosen.status,
                ),
                _select(
                    DATE_ACTION,
                    [*((text, key) for key, text in _DATES.items()), (texts.HOME_ANY_TIME, ALL)],
                    chosen.date,
                ),
            ],
        },
        {
            "type": "input",
            "block_id": SEARCH_BLOCK,
            "dispatch_action": True,
            "label": {"type": "plain_text", "text": texts.HOME_SEARCH_LABEL},
            "element": search,
        },
    ]


def _channel_header(team_id: str, channel_id: str) -> dict[str, Any]:
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*<#{channel_id}>*"},
        "accessory": _link_button(
            texts.HOME_NEW_THREAD,
            f"slack://channel?team={team_id}&id={channel_id}",
            NEW_THREAD_ACTION,
        ),
    }


def _card(row: HomeRow) -> dict[str, Any]:
    icon = f":{row.status}:  " if row.status else ""
    # Escaping can lengthen a title (`&` becomes `&amp;`): cut it until the card takes it, since
    # one title too long would have Slack refuse the whole page.
    limit = TITLE_LIMIT
    title = icon + shown_as_written(one_line(row.title, limit))
    while len(title) > CARD_TITLE:
        limit -= 10
        title = icon + shown_as_written(one_line(row.title, limit))
    details = [_WORDS.get(row.status) if row.status else None, _date(row.last_activity, "ago")]
    return {
        "type": "card",
        "title": {"type": "mrkdwn", "text": title},
        "subtitle": {"type": "mrkdwn", "text": " · ".join(d for d in details if d)},
        "actions": [_link_button(texts.HOME_OPEN, row.permalink, HOME_OPEN_ACTION)],
    }


def _carousels(rows: list[HomeRow]) -> list[dict[str, Any]]:
    """`rows` as cards side by side, a carousel for every `CAROUSEL_CARDS` of them."""
    return [
        {
            "type": "carousel",
            "elements": [_card(row) for row in rows[start : start + CAROUSEL_CARDS]],
        }
        for start in range(0, len(rows), CAROUSEL_CARDS)
    ]


def home_view(
    rows: list[HomeRow],
    channels: dict[str, str],
    *,
    team_id: str,
    chosen: HomeFilter,
    now: datetime,
) -> dict[str, Any]:
    """The Home tab's view. `rows` are newest first and `channels` maps each bound channel Slack
    still has to its name. Only the sessions of the chosen period are shown. With no channel,
    status or search chosen, every channel is a group, the ones with sessions first by their
    newest, each showing its `PER_CHANNEL` newest side by side and a button to see them all
    (which chooses that channel); otherwise only what matches, with no such cut. Never past
    Slack's 100 blocks: a channel that does not fit whole is left out, and the page says so."""
    blocks: list[dict[str, Any]] = [
        *_controls(channels, chosen),
        context_block(texts.HOME_HEADER.format(time=_date(int(now.timestamp()), "time"))),
    ]
    if not channels:
        return {"type": "home", "blocks": [*blocks, context_block(texts.HOME_EMPTY)]}
    groups: dict[str, list[HomeRow]] = {}
    for row in rows:
        if chosen.matches(row, now):
            groups.setdefault(row.channel_id, []).append(row)
    if chosen.channel:
        groups.setdefault(chosen.channel, [])
    elif not chosen.narrowed:
        for channel_id in channels:
            groups.setdefault(channel_id, [])
    if not groups:
        blocks.append(context_block(texts.HOME_NO_MATCH))
    with_sessions = {row.channel_id for row in rows}
    shown = 0
    for channel_id, found in groups.items():
        cards = found if chosen.narrowed else found[:PER_CHANNEL]
        group = [{"type": "divider"}, _channel_header(team_id, channel_id), *_carousels(cards)]
        if not found:
            # A channel with sessions, none of them in the period or under the filters.
            hidden = channel_id in with_sessions
            group.append(context_block(texts.HOME_NO_MATCH if hidden else texts.HOME_NO_SESSIONS))
        elif len(found) > len(cards):
            show_all = {
                "type": "button",
                "action_id": SHOW_ALL_ACTION,
                "text": {
                    "type": "plain_text",
                    "text": texts.HOME_SHOW_ALL.format(count=len(found)),
                },
                "value": channel_id,
            }
            group.append({"type": "actions", "elements": [show_all]})
        # One block is kept for the line that says the page stops short.
        if len(blocks) + len(group) > HOME_BLOCKS - 1:
            blocks.append(context_block(texts.HOME_MORE.format(rows=shown)))
            break
        blocks += group
        shown += len(cards)
    return {"type": "home", "blocks": blocks}


class Home:
    """Publishes the owner's Home tab. `request` asks for a publish soon and returns at once (a
    state write calls it); `choose` sets the filters and publishes now (a control was used);
    `publish` builds the page and sends it, one at a time, and never raises: the page must never
    break a turn. Always published to the configured owner, whoever opens the app.
    `not_enabled` (the Home tab is off in the Slack app's settings) is logged once and ends the
    publishing for this run; any other failure is logged by its code and the next change tries
    again."""

    def __init__(
        self,
        slack: AsyncWebClient,
        *,
        owner_user_id: str,
        team_id: str,
        state: StateStore,
        sessions_of: Callable[[Path], list[SDKSessionInfo]],
        debounce: float = DEBOUNCE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._slack = slack
        self._owner = owner_user_id
        self._team = team_id
        self._state = state
        self._sessions_of = sessions_of
        self._debounce = debounce
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._dirty = False
        self._off = False
        self._chosen = HomeFilter()
        # One publish at a time, each built inside the lock: the one that lands last was built
        # last, so a filter just chosen is never overwritten by an older page.
        self._publishing = asyncio.Lock()
        self._permalinks = asyncio.Semaphore(PERMALINKS_AT_ONCE)
        # A thread's permalink never changes: asked once per run. None is Slack's refusal (a
        # deleted root, most likely), which stands for the run too.
        self._links: dict[tuple[str, str], str | None] = {}
        # A channel's name, asked once per run; None is Slack's refusal (a channel it no longer
        # has, or one the bot left): the channel and its threads are left out of the page.
        self._names: dict[str, str | None] = {}
        # Per session id, its file's time when its last message was read, and that message's
        # time: a transcript is read again only once its file has changed.
        self._stamps: dict[str, tuple[int, int]] = {}

    @property
    def chosen(self) -> HomeFilter:
        return self._chosen

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

    async def choose(self, chosen: HomeFilter) -> None:
        """Set the filters and publish at once. A channel that is not one of the page's own
        (not bound, or gone from Slack) is no filter: `publish` drops it."""
        self._chosen = chosen
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
        async with self._publishing:
            try:
                channels = await self._channels()
                rows = await self._rows(channels)
                # Untrusted when it came from a click, and a chosen channel can go away.
                if self._chosen.channel and self._chosen.channel not in channels:
                    self._chosen = dataclasses.replace(self._chosen, channel=None)
                view = home_view(
                    rows,
                    channels,
                    team_id=self._team,
                    chosen=self._chosen,
                    now=datetime.fromtimestamp(self._clock()).astimezone(),
                )
                await self._slack.views_publish(user_id=self._owner, view=view)
            except Exception as exc:
                code = describe(exc)
                if code == NOT_ENABLED:
                    self._off = True
                    logger.warning(
                        "the Home tab is not enabled in the Slack app (docs/setup.md): the "
                        "session index is not published this run"
                    )
                else:
                    logger.warning("could not publish the session index: %s", code)

    async def _channels(self) -> dict[str, str]:
        """Every bound channel Slack still has, with its name, in the order they were bound."""
        found: dict[str, str] = {}
        for channel_id in self._state.channels():
            if channel_id not in self._names:
                try:
                    info = await self._slack.conversations_info(channel=channel_id)
                except Exception as exc:
                    code = describe(exc)
                    logger.warning("could not read channel %s: %s", channel_id, code)
                    if isinstance(exc, SlackApiError) and code != RATE_LIMITED:
                        self._names[channel_id] = None
                    continue
                self._names[channel_id] = str(info["channel"]["name"])
            name = self._names[channel_id]
            if name is not None:
                found[channel_id] = name
        return found

    async def _rows(self, channels: dict[str, str]) -> list[HomeRow]:
        """One row per thread that holds a session in one of `channels` and that Slack still
        has (a thread with no permalink cannot be opened: it is left out), newest first."""
        held = [
            (channel_id, thread_ts, thread)
            for channel_id, thread_ts, thread in self._state.threads()
            if thread.session_id is not None and channel_id in channels
        ]
        wanted: dict[Path, set[str]] = {}
        for _, _, thread in held:
            wanted.setdefault(thread.directory, set()).add(str(thread.session_id))
        infos = await asyncio.to_thread(self._infos, wanted)
        links = await asyncio.gather(
            *(self._permalink(channel_id, thread_ts) for channel_id, thread_ts, _ in held)
        )
        rows = []
        for (channel_id, thread_ts, thread), link in zip(held, links, strict=True):
            if link is None:
                continue
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
                    permalink=link,
                )
            )
        rows.sort(key=lambda row: row.last_activity, reverse=True)
        return rows

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
                async with self._permalinks:
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
