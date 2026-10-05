"""The app's Home tab: the owner's index of sessions. Two lines per thread that holds a session,
grouped by channel, each group and each session ordered by the thread's last reply, with a link
that opens the thread: a channel lists its threads by when they started, and Slack's
Threads view by unread replies (Help Center, "Use threads to organize discussions", read
2026-10-01), so neither finds the thread worked in last.

A card is built from what the daemon already keeps, what Claude Code knows and what Slack shows:
the thread and its root's reaction from `state.json`, the title from the session's transcript,
as `!resume` shows it, and from Slack the thread's number of replies and the time of its last
one, the same the channel shows under the root. Nothing is stored for the page: the filters the
owner chooses live in memory and start again at their defaults with the daemon.

`conversations.replies` with the root's `ts` and `limit=1` returns the root alone, carrying
`reply_count`, `latest_reply` and `reactions` (measured 2026-10-01 on a free workspace with
`groups:history`, slack-sdk 3.44.1; a deleted root answers `thread_not_found`). The root's own
reaction fills in the status of a thread that ended before the daemon kept it.

`views.publish` (docs.slack.dev/reference/methods/views.publish, read 2026-10-01) takes no scope
and may be called at any time, with no event from the owner ("Home tab updates can happen when a
user isn't interacting with Slack or the app", docs.slack.dev/surfaces/app-home), so the page is
rewritten when the index changes, with no event subscribed. A view holds 100 blocks. Ages are
Slack's own `{ago}` date token (docs.slack.dev/messaging/formatting-message-text, seen rendered
in a Home view on 2026-10-01), so they do not go stale between two publishes.

What reaches the app from the page is a `block_actions` payload per use of a control
(`slack_app` owns the listeners): a filter, which carries the state of every control in
`view.state.values`, and the **New thread** link button, which Slack follows itself and still
reports (button element reference, read 2026-10-01). A session's **Open** is a plain link in its
line of details, which reports nothing: the permalink of the thread's last reply, which Slack
opens the thread on (seen in the Mac app and on iOS, 2026-10-05), or of its root while it has
none. **New thread** is the documented deep link to the channel
(docs.slack.dev/interactivity/deep-linking; it opened the channel in the desktop app on
2026-10-01): the owner's own top-level message there starts the session, so the thread is one
the owner started and its replies notify.
"""

import asyncio
import contextlib
import dataclasses
import hashlib
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from claude_agent_sdk import SDKSessionInfo
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from code_with_slack import texts
from code_with_slack.render.escape import mrkdwn_escape, shown_as_written
from code_with_slack.render.renderer import one_line
from code_with_slack.render.sinks import context_block, describe
from code_with_slack.render.status import Status
from code_with_slack.resume import ID_SHOWN, TITLE_LIMIT
from code_with_slack.state import StateStore

logger = logging.getLogger(__name__)

HOME_BLOCKS = 100
# What a channel shows while no filter is chosen; a filter shows every session it matches.
PER_CHANNEL = 5
# A turn changes its root's reaction several times in a row (⏳, ✋ at an approval, ⏳ again):
# one publish covers the burst.
DEBOUNCE_SECONDS = 2.0
# How long a stop waits for its last publish: the page must never hold the daemon's exit.
CLOSE_SECONDS = 10.0
# How many threads Slack is asked about at once on the first publish of a run.
THREADS_AT_ONCE = 8
NOT_ENABLED = "not_enabled"
# What Slack answers about a channel or a message it no longer has: the only refusals the page
# takes as final. Any other failure (a rate limit, a server error, the network) is no answer.
GONE = frozenset({"channel_not_found", "message_not_found", "thread_not_found"})
# How long after a page that lacked an answer the next one is tried.
RETRY_SECONDS = 60.0
# A select menu holds 100 options (select menu reference): "All channels" and 99 channels.
CHANNEL_OPTIONS = 99

FILTERS_BLOCK = "home_filters"
SEARCH_BLOCK = "home_search"
CHANNEL_ACTION = "home_channel"
STATUS_ACTION = "home_status"
DATE_ACTION = "home_date"
SEARCH_ACTION = "home_search_text"
SHOW_ALL_ACTION = "home_show_all"
NEW_THREAD_ACTION = "home_new_thread"
EDIT_ACTION = "home_edit"
DELETE_ACTION = "home_delete"
# What the Edit button carries: the mode a click asks for, so a click sent twice asks the same.
EDIT_ON, EDIT_OFF = "edit", "done"
# A confirmation dialog's text holds 300 characters (confirmation dialog object reference).
CONFIRM_TEXT = 300
FILTER_ACTIONS = (CHANNEL_ACTION, STATUS_ACTION, DATE_ACTION, SEARCH_ACTION)
# A blank row between two sessions: a context line that holds a zero-width space, since Slack
# sets the distance between blocks itself and takes no empty text.
SPACER = "\u200b"
# A session is a title, a line of details and the blank row above it.
CARD_BLOCKS = 3
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
# A session in one of these states is not offered for deleting: its thread is in use.
_IN_USE = frozenset({Status.WAITING.value, Status.WORKING.value})
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
    replies: int
    # The thread's last reply, or its root while it has none: epoch seconds.
    last_activity: int
    permalink: str
    # Its thread is being deleted, or waits for the delete before it: the row says so.
    deleting: bool = False


@dataclass(frozen=True)
class ThreadFacts:
    """What Slack shows of a thread's root message."""

    replies: int
    latest_reply: int | None  # epoch seconds
    # The last reply's own `ts`, as Slack wrote it: what a permalink to that message is asked by.
    latest_ts: str | None
    # The root's status reaction (`render.status.Status.value`), when it carries one.
    reaction: str | None


def thread_facts(root: dict[str, Any]) -> ThreadFacts:
    """Read a root message as `conversations.replies` returns it. A root nobody replied to has
    no `reply_count` and no `latest_reply`."""
    latest = root.get("latest_reply")
    names = [str(r.get("name")) for r in root.get("reactions") or [] if isinstance(r, dict)]
    return ThreadFacts(
        replies=int(root.get("reply_count") or 0),
        latest_reply=int(float(latest)) if latest else None,
        latest_ts=str(latest) if latest else None,
        reaction=next((name for name in names if name in _WORDS), None),
    )


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
        """Whether `row` passes every chosen filter. The date is the thread's last reply:
        `today` and `yesterday` are calendar days of the machine's own time zone, each instant
        read with the offset in force at that instant (a day a clock change makes 23 or 25
        hours long stays one day); the others reach back from now."""
        if self.channel and row.channel_id != self.channel:
            return False
        if self.status and row.status != self.status:
            return False
        if self.search and self.search.casefold() not in row.title.casefold():
            return False
        if self.date in (TODAY, YESTERDAY):
            today, day = date.fromtimestamp(now.timestamp()), date.fromtimestamp(row.last_activity)
            return (today - day).days == (0 if self.date == TODAY else 1)
        if self.date in _REACH:
            return now.timestamp() - row.last_activity <= _REACH[self.date].total_seconds()
        return True


def read_filter(values: dict[str, Any], current: HomeFilter) -> HomeFilter:
    """The filter a use of a control leaves chosen, from the `view.state.values` its payload
    carries: every control's state rides on it, so nothing is tracked per control. Untrusted
    like every click, and never raising on a shape Slack does not send: a status or a date that
    is not one of the page's own keeps the current one, and the channel is checked by
    `Home.publish`, which knows the page's own."""
    # Found by action id, whatever the block: the blocks' ids change with the choice.
    controls: dict[str, Any] = {}
    for block in values.values() if isinstance(values, dict) else ():
        if isinstance(block, dict):
            controls.update(block)

    def picked(action_id: str, now_chosen: str | None, known: Any) -> str | None:
        control = controls.get(action_id)
        if not isinstance(control, dict):
            return now_chosen
        option = control.get("selected_option")
        value = option.get("value") if isinstance(option, dict) else None
        if value == ALL:
            return None
        if not isinstance(value, str) or (known is not None and value not in known):
            return now_chosen
        return value

    search = current.search
    box = controls.get(SEARCH_ACTION)
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
    by_value = {option["value"]: option for option in built}
    # A choice the menu does not hold (a channel Slack did not answer about) reads as "all".
    initial = by_value.get(chosen or ALL, by_value[ALL])
    return {
        "type": "static_select",
        "action_id": action_id,
        "options": built,
        "initial_option": initial,
    }


def _controls(channels: dict[str, str], chosen: HomeFilter) -> list[dict[str, Any]]:
    # Slack keeps what a control shows for as long as its block keeps its id, whatever
    # `initial_option` a later page carries (seen 2026-10-01: after a restart the menus still
    # showed the choices of the run before). An id that follows the choice makes the controls
    # show what the page was built with: a restart, Show all and a dropped channel included.
    mark = hashlib.sha256(repr(chosen).encode()).hexdigest()[:8]
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
            "block_id": f"{FILTERS_BLOCK}:{mark}",
            "elements": [
                _select(
                    CHANNEL_ACTION,
                    [
                        (texts.HOME_ALL_CHANNELS, ALL),
                        *(
                            (one_line(name, OPTION_TEXT), cid)
                            for cid, name in list(channels.items())[:CHANNEL_OPTIONS]
                        ),
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
            "block_id": f"{SEARCH_BLOCK}:{mark}",
            "dispatch_action": True,
            "label": {"type": "plain_text", "text": texts.HOME_SEARCH_LABEL},
            "element": search,
        },
    ]


def _plain(text: str) -> dict[str, Any]:
    return {"type": "plain_text", "text": text}


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _deleting_line(count: int) -> str:
    return texts.HOME_DELETING_ONE if count == 1 else texts.HOME_DELETING_MANY.format(count=count)


def _header(time: str, *, editing: bool, can_edit: bool) -> dict[str, Any]:
    """The line above the sessions. It carries the Edit button when threads can be deleted: a
    context block holds no button (context block reference), so the line is a section then."""
    text = texts.HOME_HEADER.format(time=time)
    if not can_edit:
        return context_block(text)
    button: dict[str, Any] = {
        "type": "button",
        "action_id": EDIT_ACTION,
        "text": _plain(texts.HOME_DONE if editing else texts.HOME_EDIT),
        "value": EDIT_OFF if editing else EDIT_ON,
    }
    if editing:
        button["style"] = "primary"
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}, "accessory": button}


def _channel_header(
    team_id: str, channel_id: str, *, editing: bool, deleting: int = 0
) -> dict[str, Any]:
    """A channel's name, with how many of its threads are being deleted beside it: the rows
    that say so can be among those the channel does not show."""
    text = f"*<#{channel_id}>*"
    if deleting == 1:
        text += f"   {texts.HOME_CHANNEL_DELETING_ONE}"
    elif deleting:
        text += f"   {texts.HOME_CHANNEL_DELETING_MANY.format(count=deleting)}"
    header: dict[str, Any] = {"type": "section", "text": {"type": "mrkdwn", "text": text}}
    if not editing:
        header["accessory"] = _link_button(
            texts.HOME_NEW_THREAD,
            f"slack://channel?team={team_id}&id={channel_id}",
            NEW_THREAD_ACTION,
        )
    return header


def _delete_button(row: HomeRow, channel_name: str) -> dict[str, Any]:
    """The Delete of a session's row, with Slack's own confirmation dialog, which names the
    thread. The dialog shows before the click is sent (confirmation dialog object reference)."""
    replies = texts.HOME_REPLY if row.replies == 1 else texts.HOME_REPLIES.format(count=row.replies)
    named = texts.HOME_DELETE_NAMED if row.replies else texts.HOME_DELETE_NAMED_BARE
    room = CONFIRM_TEXT - len(texts.HOME_DELETE_TEXT)
    room -= len(named.format(title="", channel=channel_name, replies=replies))
    title = one_line(row.title, max(room, 1))
    return {
        "type": "button",
        "action_id": DELETE_ACTION,
        "style": "danger",
        "text": _plain(texts.HOME_DELETE),
        "value": f"{row.channel_id}:{row.thread_ts}",
        "confirm": {
            "title": _plain(texts.HOME_DELETE_TITLE),
            "text": _plain(
                named.format(title=title, channel=channel_name, replies=replies)
                + texts.HOME_DELETE_TEXT
            ),
            "confirm": _plain(texts.HOME_DELETE_CONFIRM),
            "deny": _plain(texts.HOME_DELETE_DENY),
            "style": "danger",
        },
    }


def _card(row: HomeRow, channel_name: str = "", *, editing: bool = False) -> list[dict[str, Any]]:
    icon = f":{row.status}:  " if row.status else ""
    title = shown_as_written(one_line(row.title, TITLE_LIMIT))
    replies = texts.HOME_REPLY if row.replies == 1 else texts.HOME_REPLIES.format(count=row.replies)
    when = texts.HOME_LAST_REPLY if row.replies else texts.HOME_STARTED
    details = [
        texts.HOME_DELETING if row.deleting else _WORDS.get(row.status) if row.status else None,
        replies if row.replies else None,
        when.format(when=_date(row.last_activity, "ago")),
    ]
    # Open is a link in the small line, not a button: a button sits at the far right of the
    # title's row, and a row that carries one cannot be small (the maintainer, 2026-10-01).
    # A reply's permalink carries a query (`?thread_ts=…&cid=…`), and mrkdwn reads `&` as markup.
    details.append(f"<{mrkdwn_escape(row.permalink)}|{texts.HOME_OPEN}>")
    head: dict[str, Any] = {
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"{icon}*{title}*"},
    }
    if editing and not row.deleting and row.status not in _IN_USE:
        head["accessory"] = _delete_button(row, channel_name)
    return [head, context_block(" · ".join(d for d in details if d))]


def home_view(
    rows: list[HomeRow],
    channels: dict[str, str],
    *,
    team_id: str,
    chosen: HomeFilter,
    now: datetime,
    can_edit: bool = False,
    editing: bool = False,
    notice: str | None = None,
    deleting: int = 0,
) -> dict[str, Any]:
    """The Home tab's view. `rows` are newest first and `channels` maps each bound channel Slack
    still has to its name. Only the sessions of the chosen period are shown. With no channel,
    status or search chosen, every channel is a group, the ones with sessions first by their
    newest, each showing its `PER_CHANNEL` newest and a button to see them all (which chooses
    that channel); otherwise only what matches, with no such cut. Never past Slack's 100 blocks:
    the page says when it stops short. With `can_edit` the header line carries Edit; in
    `editing` each session that is not in use carries Delete and no channel carries New thread.
    `notice` is the line a delete that did not end left, under the header; `deleting` is how
    many threads are being deleted or wait for it, said in a line of full size under the
    header, since a row that says so can be one of those a channel does not show."""
    editing = editing and can_edit
    blocks: list[dict[str, Any]] = [
        *_controls(channels, chosen),
        _header(_date(int(now.timestamp()), "time"), editing=editing, can_edit=can_edit),
        *([_section(_deleting_line(deleting))] if deleting else []),
        *([context_block(notice)] if notice else []),
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
    # Counted over every session of the channel, shown or not, whatever the filters.
    going = Counter(row.channel_id for row in rows if row.deleting)
    wanted = {c: found if chosen.narrowed else found[:PER_CHANNEL] for c, found in groups.items()}
    shown = 0
    for channel_id, found in groups.items():
        cards = wanted[channel_id]
        # One block is kept for the line that says the page stops short; a group is a divider,
        # a header, `CARD_BLOCKS` a card at most, and one closing block at most.
        room = (HOME_BLOCKS - 1 - len(blocks) - 3) // CARD_BLOCKS
        if room < min(1, len(cards)) or HOME_BLOCKS - 1 - len(blocks) < 3:
            break
        blocks += [
            {"type": "divider"},
            _channel_header(team_id, channel_id, editing=editing, deleting=going[channel_id]),
        ]
        name = channels.get(channel_id, "")
        for index, row in enumerate(cards[:room]):
            blocks += [
                *([context_block(SPACER)] if index else []),
                *_card(row, name, editing=editing),
            ]
        shown += min(len(cards), room)
        if len(cards) > room:
            break
        if not found:
            # A channel with sessions, none of them in the period or under the filters.
            hidden = channel_id in with_sessions
            blocks.append(context_block(texts.HOME_NO_MATCH if hidden else texts.HOME_NO_SESSIONS))
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
            blocks.append({"type": "actions", "elements": [show_all]})
    # Said only when a session is left out: channels with none can fall off the end unsaid.
    if shown < sum(len(cards) for cards in wanted.values()):
        blocks.append(context_block(texts.HOME_MORE.format(rows=shown)))
    return {"type": "home", "blocks": blocks}


def _gone(exc: Exception) -> bool:
    """Whether Slack answered that the channel or the message is not there any more."""
    return isinstance(exc, SlackApiError) and describe(exc) in GONE


class Home:
    """Publishes the owner's Home tab. `request` asks for a publish soon and returns at once (a
    state write calls it, naming the threads it changed); `choose` sets the filters and
    publishes now (a control was used); `publish` builds the page and sends it, one at a time,
    and never raises: the page must never break a turn. Always published to the configured
    owner, whoever opens the app. `not_enabled` (the Home tab is off in the Slack app's
    settings) is logged once and ends the publishing for this run. Any other failure is logged
    by its code, and a page that Slack did not fully answer for is tried again after
    `RETRY_SECONDS`."""

    def __init__(
        self,
        slack: AsyncWebClient,
        *,
        owner_user_id: str,
        team_id: str,
        state: StateStore,
        sessions_of: Callable[[Path], list[SDKSessionInfo]],
        delete: Callable[[str, str], Awaitable[str | None]] | None = None,
        debounce: float = DEBOUNCE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._slack = slack
        self._owner = owner_user_id
        self._team = team_id
        self._state = state
        self._sessions_of = sessions_of
        # Deletes a thread and answers the line to show when it could not
        # (`delete.ThreadDeleter.delete`). None: no Edit button, the page as it always was.
        self._delete = delete
        # Kept in memory like the filters: a restart starts out of edit mode.
        self._editing = False
        # The filters as they were when edit mode was entered: Done brings them back.
        self._before_edit: HomeFilter | None = None
        self._notice: str | None = None
        # The threads a delete was asked for and has not ended: Slack's rate limit makes one
        # take from seconds to minutes, and the page says so from the click on.
        self._deleting: set[tuple[str, str]] = set()
        self._debounce = debounce
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._retry: asyncio.Task[None] | None = None
        self._dirty = False
        self._off = False
        # Whether the page being built lacks something Slack gave no answer about.
        self._incomplete = False
        self._chosen = HomeFilter()
        # One publish at a time, each built inside the lock: the one that lands last was built
        # last, so a filter just chosen is never overwritten by an older page.
        self._publishing = asyncio.Lock()
        self._asking = asyncio.Semaphore(THREADS_AT_ONCE)
        # Each thread's permalink, with the `ts` of the message it opens: the thread's last reply,
        # or its root while it has none. Asked again only once that `ts` changed. A None link:
        # Slack no longer has that message (`GONE`).
        self._links: dict[tuple[str, str], tuple[str, str | None]] = {}
        # A channel's name, asked once per run. None: Slack no longer has the channel, or the
        # bot left it (`GONE`): the channel and its threads are left out of the page.
        self._names: dict[str, str | None] = {}
        # What Slack showed of each thread's root. None: the root is gone (`GONE`).
        self._facts: dict[tuple[str, str], ThreadFacts | None] = {}
        # The threads a state write changed since their root was read: read again at the next
        # publish, which every turn causes at its start and at its end.
        self._stale: set[tuple[str, str]] = set()

    @property
    def chosen(self) -> HomeFilter:
        return self._chosen

    def request(self, changed: Iterable[tuple[str, str]] = ()) -> None:
        """Ask for a publish soon; `changed` are the (channel, thread) a state write touched."""
        if self._off:
            return
        self._stale.update(changed)
        self._dirty = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._publish_when_settled())

    async def _publish_when_settled(self) -> None:
        while self._dirty and not self._off:
            await asyncio.sleep(self._debounce)
            self._dirty = False
            await self.publish()

    async def _request_later(self) -> None:
        await asyncio.sleep(RETRY_SECONDS)
        self.request()

    async def choose(self, chosen: HomeFilter) -> None:
        """Set the filters and publish at once. A channel that is not one of the page's own
        (not bound, or gone from Slack) is no filter: `publish` drops it."""
        self._chosen = chosen
        await self.publish()

    async def edit(self, on: bool) -> None:
        """Enter or leave edit mode and publish at once. Edit mode is a parenthesis: a filter
        chosen inside it (Show all on the channel being cleaned, most often) lasts while it
        does, and leaving it brings back the filters the page had on entering."""
        on = on and self._delete is not None
        if on and not self._editing:
            self._before_edit = self._chosen
        elif not on and self._editing and self._before_edit is not None:
            self._chosen = self._before_edit
            self._before_edit = None
        self._editing = on
        self._notice = None
        await self.publish()

    async def delete(self, channel_id: str, thread_ts: str) -> None:
        """Delete a thread the page lists. Publishes at once with the row marked as being
        deleted, then again when the delete ended: without the row, or with the line that says
        why it is still there. A second click on the same thread while it runs does nothing.
        The page stays in edit mode."""
        key = (channel_id, thread_ts)
        if self._delete is None or not self._editing or key in self._deleting:
            return
        self._deleting.add(key)
        self._notice = None
        await self.publish()
        try:
            self._notice = await self._delete(channel_id, thread_ts)
        finally:
            self._deleting.discard(key)
        # A delete that stopped half way changed the thread: its root is read again.
        self._stale.add((channel_id, thread_ts))
        await self.publish()

    async def close(self) -> None:
        """Publish what a pending request still owed, then stop: the page a stop leaves behind
        shows the sessions as the stop left them. Gives up after `CLOSE_SECONDS`."""
        task, self._task = self._task, None
        retry, self._retry = self._retry, None
        # Waiting out its debounce or cut in the middle of a publish: owed either way.
        owed = self._dirty or (task is not None and not task.done())
        for pending in (task, retry):
            if pending is not None:
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending
        if owed:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(CLOSE_SECONDS):
                    await self.publish()
        self._off = True

    async def publish(self) -> None:
        if self._off:
            return
        async with self._publishing:
            self._incomplete = False
            try:
                await self._publish()
            except Exception as exc:
                code = describe(exc)
                if code == NOT_ENABLED:
                    self._off = True
                    logger.warning(
                        "the Home tab is not enabled in the Slack app (docs/setup.md): the "
                        "session index is not published this run"
                    )
                    return
                logger.warning("could not publish the session index: %s", code)
                self._incomplete = True
            if self._incomplete and (self._retry is None or self._retry.done()):
                self._retry = asyncio.create_task(self._request_later())

    async def _publish(self) -> None:
        bound = self._state.channels()
        channels = await self._channels(bound)
        if bound and not channels and self._incomplete:
            return  # Slack answered about no channel: the page stays as it is until it does
        rows = await self._rows(channels)
        # Untrusted when it came from a click, and a chosen channel can go away. One Slack
        # merely did not answer about is still the owner's choice.
        chosen = self._chosen.channel
        if chosen and (chosen not in bound or self._names.get(chosen, "") is None):
            self._chosen = dataclasses.replace(self._chosen, channel=None)
        view = home_view(
            rows,
            channels,
            team_id=self._team,
            chosen=self._chosen,
            now=datetime.fromtimestamp(self._clock()).astimezone(),
            can_edit=self._delete is not None,
            editing=self._editing,
            notice=self._notice,
            deleting=len(self._deleting),
        )
        await self._slack.views_publish(user_id=self._owner, view=view)

    async def _channels(self, bound: list[str]) -> dict[str, str]:
        """Every bound channel Slack still has, with its name, in the order they were bound."""
        found: dict[str, str] = {}
        # A channel forgotten and bound again later is asked about again.
        for unbound in self._names.keys() - set(bound):
            del self._names[unbound]
        for channel_id in bound:
            if channel_id not in self._names:
                try:
                    info = await self._slack.conversations_info(channel=channel_id)
                    self._names[channel_id] = str(info["channel"]["name"])
                except Exception as exc:
                    logger.warning("could not read channel %s: %s", channel_id, describe(exc))
                    if not _gone(exc):
                        self._incomplete = True
                        continue
                    self._names[channel_id] = None
            name = self._names[channel_id]
            if name is not None:
                found[channel_id] = name
        return found

    async def _rows(self, channels: dict[str, str]) -> list[HomeRow]:
        """One row per thread that holds a session in one of `channels` and that Slack still
        has (a thread whose root is gone cannot be opened: it is left out), last reply first."""
        threads = self._state.threads()
        # What is kept per thread goes with the thread: a pruned one leaves nothing behind.
        existing = {(channel_id, thread_ts) for channel_id, thread_ts, _ in threads}
        self._stale &= existing
        for cache in (self._links, self._facts):
            for key in cache.keys() - existing:
                del cache[key]
        held = [
            (channel_id, thread_ts, thread)
            for channel_id, thread_ts, thread in threads
            if thread.session_id is not None and channel_id in channels
        ]
        titles = await asyncio.to_thread(self._titles, {thread.directory for _, _, thread in held})
        seen = await asyncio.gather(
            *(self._in_slack(channel_id, thread_ts) for channel_id, thread_ts, _ in held)
        )
        rows = []
        for (channel_id, thread_ts, thread), (link, facts) in zip(held, seen, strict=True):
            if link is None or facts is None:
                continue
            rows.append(
                HomeRow(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    title=titles.get(str(thread.session_id))
                    or texts.HOME_UNTITLED.format(id=str(thread.session_id)[:ID_SHOWN]),
                    # ❌ over a kept ⏳ or ✋ (an answer that never reached Slack) shows ❌. A
                    # thread that ended before the daemon kept its reaction shows the root's.
                    status=thread.ended or thread.status or facts.reaction,
                    replies=facts.replies,
                    last_activity=facts.latest_reply or int(float(thread_ts)),
                    permalink=link,
                    deleting=(channel_id, thread_ts) in self._deleting,
                )
            )
        rows.sort(key=lambda row: row.last_activity, reverse=True)
        return rows

    def _titles(self, directories: set[Path]) -> dict[str, str]:
        """The title Claude Code gives each session of `directories`, by session id. Blocking
        file reads: runs off the event loop. A folder that cannot be listed is logged and
        skipped: its sessions show their id."""
        found: dict[str, str] = {}
        for directory in directories:
            try:
                found.update({s.session_id: s.summary for s in self._sessions_of(directory)})
            except Exception as exc:
                logger.warning(
                    "could not list a folder's sessions for the session index: %s", describe(exc)
                )
        return found

    async def _in_slack(
        self, channel_id: str, thread_ts: str
    ) -> tuple[str | None, ThreadFacts | None]:
        """A thread's permalink and what Slack shows of its root; either is None for a thread
        the page leaves out."""
        async with self._asking:
            facts = await self._root(channel_id, thread_ts)
            if facts is None:
                return None, None
            return await self._permalink(channel_id, thread_ts, facts.latest_ts or thread_ts), facts

    async def _root(self, channel_id: str, thread_ts: str) -> ThreadFacts | None:
        key = (channel_id, thread_ts)
        if key in self._facts and key not in self._stale:
            return self._facts[key]
        try:
            answer = await self._slack.conversations_replies(
                channel=channel_id, ts=thread_ts, limit=1
            )
            messages = answer.get("messages") or []
            facts = thread_facts(next(m for m in messages if str(m.get("ts")) == thread_ts))
        except Exception as exc:
            # A root Slack returns in a shape this cannot read counts as no answer too.
            logger.warning("could not read thread %s/%s: %s", channel_id, thread_ts, describe(exc))
            if _gone(exc):
                self._stale.discard(key)
                self._facts[key] = None
                return None
            # No answer: what was read before stands, and the thread is asked about again.
            self._incomplete = True
            return self._facts.get(key)
        self._stale.discard(key)
        self._facts[key] = facts
        return facts

    async def _permalink(self, channel_id: str, thread_ts: str, message_ts: str) -> str | None:
        """The permalink that opens the thread on `message_ts`: Slack scrolls a thread to the
        reply its permalink names (seen in the Mac app and on iOS, 2026-10-05)."""
        key = (channel_id, thread_ts)
        kept = self._links.get(key)
        if kept is not None and kept[0] == message_ts:
            return kept[1]
        link: str | None = None
        try:
            answer = await self._slack.chat_getPermalink(channel=channel_id, message_ts=message_ts)
            link = str(answer["permalink"])
        except Exception as exc:
            logger.warning(
                "could not get a permalink for %s/%s: %s", channel_id, message_ts, describe(exc)
            )
            if not _gone(exc):
                self._incomplete = True
                return None
        self._links[key] = (message_ts, link)
        return link
