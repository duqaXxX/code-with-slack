import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SDKSessionInfo

from code_with_slack import home as home_module
from code_with_slack import texts
from code_with_slack.home import (
    ALL,
    CHANNEL_ACTION,
    DATE_ACTION,
    FILTERS_BLOCK,
    HOME_BLOCKS,
    HOME_OPEN_ACTION,
    LAST_7,
    LAST_30,
    NEW_THREAD_ACTION,
    PER_CHANNEL,
    SEARCH_ACTION,
    SEARCH_BLOCK,
    SHOW_ALL_ACTION,
    STATUS_ACTION,
    TODAY,
    YESTERDAY,
    Home,
    HomeFilter,
    HomeRow,
    home_view,
    read_filter,
)
from code_with_slack.render.status import Status
from code_with_slack.state import StateStore
from tests.fakes import FakeSlack, slack_payload
from tests.test_repair import slack_error
from tests.test_resume import message, write_transcript
from tests.test_sessions import until

OWNER = "U000ALICE"
TEAM = "T000TEAM"
CHANNEL = "C000CHAN"
OTHER_CHANNEL = "C000OTHR"
EMPTY_CHANNEL = "C000NONE"
CHANNELS = {CHANNEL: "cc-articles", OTHER_CHANNEL: "cc-shop", EMPTY_CHANNEL: "cc-tools"}
NOW = datetime(2026, 9, 21, 14, 13, tzinfo=UTC)
EPOCH = int(NOW.timestamp())
LINK = "https://example.slack.com/archives/C000CHAN/p1780000000000001"


def row(title: str = "Refactor the feed parser", **fields: Any) -> HomeRow:
    defaults: dict[str, Any] = {
        "channel_id": CHANNEL,
        "thread_ts": "1789990000.000100",
        "status": Status.WORKING.value,
        "last_activity": EPOCH - 120,
        "permalink": LINK,
    }
    return HomeRow(title=title, **{**defaults, **fields})


def view(rows: list[HomeRow], chosen: HomeFilter | None = None, **kwargs: Any) -> dict[str, Any]:
    return home_view(
        rows,
        kwargs.pop("channels", CHANNELS),
        team_id=TEAM,
        chosen=chosen or HomeFilter(),
        now=NOW,
        **kwargs,
    )


def cards(page: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        b
        for b in page["blocks"]
        if b["type"] == "section" and b["accessory"]["action_id"] == HOME_OPEN_ACTION
    ]


def titles(page: dict[str, Any]) -> list[str]:
    return [c["text"]["text"] for c in cards(page)]


def headers(page: dict[str, Any]) -> list[str]:
    return [
        b["text"]["text"]
        for b in page["blocks"]
        if b["type"] == "section" and b["accessory"]["action_id"] == NEW_THREAD_ACTION
    ]


def notes(page: dict[str, Any]) -> list[str]:
    return [b["elements"][0]["text"] for b in page["blocks"] if b["type"] == "context"]


def test_a_channel_is_a_header_with_a_new_thread_link_and_a_card_per_session() -> None:
    page = view([row()], channels={CHANNEL: "cc-articles"})
    assert page["type"] == "home"
    # The view shape and the 100 block cap: docs.slack.dev/surfaces/app-home, read 2026-10-01.
    controls, search, written, divider, header, card, details = page["blocks"]
    assert (controls["type"], search["type"]) == ("actions", "input")
    assert written["elements"][0]["text"] == texts.HOME_HEADER.format(
        time=f"<!date^{EPOCH}^{{time}}|2026-09-21 14:13 UTC>"
    )
    assert divider == {"type": "divider"}
    # The documented deep link to a channel (docs.slack.dev/interactivity/deep-linking).
    assert header == {
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*<#{CHANNEL}>*"},
        "accessory": {
            "type": "button",
            "action_id": NEW_THREAD_ACTION,
            "text": {"type": "plain_text", "text": texts.HOME_NEW_THREAD},
            "url": f"slack://channel?team={TEAM}&id={CHANNEL}",
        },
    }
    assert card == {
        "type": "section",
        "text": {"type": "mrkdwn", "text": ":hourglass_flowing_sand:  *Refactor the feed parser*"},
        "accessory": {
            "type": "button",
            "action_id": HOME_OPEN_ACTION,
            "text": {"type": "plain_text", "text": texts.HOME_OPEN},
            "url": LINK,
        },
    }
    # Slack's own relative date (formatting-message-text, read 2026-10-01): it stays right while
    # the page sits unpublished.
    assert details["elements"][0]["text"] == (
        f"working · <!date^{EPOCH - 120}^{{ago}}|2026-09-21 14:11 UTC>"
    )


@pytest.mark.parametrize(
    ("status", "word"),
    [
        (Status.WORKING, texts.HOME_WORKING),
        (Status.WAITING, texts.HOME_WAITING),
        (Status.DONE, texts.HOME_ENDED),
        (Status.ERROR, texts.HOME_ERROR),
    ],
)
def test_each_reaction_has_its_word(status: Status, word: str) -> None:
    page = view([row(status=status.value)])
    assert titles(page) == [f":{status.value}:  *Refactor the feed parser*"]
    assert any(note.startswith(f"{word} · <!date^") for note in notes(page))


def test_a_card_with_no_reaction_shows_the_title_and_the_age_alone() -> None:
    page = view([row(status=None)], channels={CHANNEL: "cc-articles"})
    assert titles(page) == ["*Refactor the feed parser*"]
    assert notes(page)[-1] == f"<!date^{EPOCH - 120}^{{ago}}|2026-09-21 14:11 UTC>"


def test_a_title_is_shown_as_written_on_one_line() -> None:
    # Model-written text: unescaped, `<!channel>` would read as a mention.
    (title,) = titles(view([row("ping <!channel> & `more`\nsecond line")]))
    assert "<!channel>" not in title and "&lt;!channel&gt; &amp;" in title
    assert "\n" not in title


def test_channels_with_sessions_come_first_by_their_newest_then_the_empty_ones() -> None:
    rows = [
        row("newest, in the shop", channel_id=OTHER_CHANNEL, last_activity=EPOCH - 60),
        row("older, in articles", last_activity=EPOCH - 600),
    ]
    page = view(rows)
    assert headers(page) == [f"*<#{OTHER_CHANNEL}>*", f"*<#{CHANNEL}>*", f"*<#{EMPTY_CHANNEL}>*"]
    assert notes(page)[-1] == texts.HOME_NO_SESSIONS


def many(count: int, **fields: Any) -> list[HomeRow]:
    return [
        row(f"s{i}", thread_ts=f"17899{i:05d}.000100", last_activity=EPOCH - 60 * i, **fields)
        for i in range(count)
    ]


def test_a_channel_shows_its_newest_five_and_a_button_for_all_of_them() -> None:
    page = view(many(8), channels={CHANNEL: "cc-articles"})
    assert titles(page) == [f":hourglass_flowing_sand:  *s{i}*" for i in range(PER_CHANNEL)]
    assert page["blocks"][-1] == {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "action_id": SHOW_ALL_ACTION,
                "text": {"type": "plain_text", "text": texts.HOME_SHOW_ALL.format(count=8)},
                "value": CHANNEL,
            }
        ],
    }


def test_choosing_a_channel_shows_all_of_it_and_nothing_else() -> None:
    rows = [*many(8), row("in the shop", channel_id=OTHER_CHANNEL)]
    page = view(rows, HomeFilter(channel=CHANNEL))
    assert headers(page) == [f"*<#{CHANNEL}>*"]
    assert len(cards(page)) == 8
    assert all(b["type"] != "actions" or b.get("block_id") for b in page["blocks"])  # no Show all


def test_a_chosen_channel_with_no_session_says_so() -> None:
    page = view([row()], HomeFilter(channel=EMPTY_CHANNEL))
    assert headers(page) == [f"*<#{EMPTY_CHANNEL}>*"]
    assert notes(page)[-1] == texts.HOME_NO_SESSIONS


def test_a_status_filter_keeps_the_sessions_in_that_status_whatever_their_number() -> None:
    rows = [*many(7, status=Status.WAITING.value), row("done", status=Status.DONE.value)]
    page = view(rows, HomeFilter(status=Status.WAITING.value))
    assert len(cards(page)) == 7  # no five-per-channel cut under a filter
    assert headers(page) == [f"*<#{CHANNEL}>*"]  # a channel with no match is not listed


def test_the_search_matches_a_part_of_the_title_whatever_the_case() -> None:
    rows = [row("Fix the Footer on long replies"), row("Bump the SDK pin")]
    assert titles(view(rows, HomeFilter(search="footer"))) == [
        ":hourglass_flowing_sand:  *Fix the Footer on long replies*"
    ]


@pytest.mark.parametrize(
    ("date", "expected"),
    [
        (TODAY, ["an hour ago"]),
        (YESTERDAY, ["yesterday evening"]),  # that day alone
        (LAST_7, ["an hour ago", "yesterday evening", "five days ago"]),
        (LAST_30, ["an hour ago", "yesterday evening", "five days ago", "three weeks ago"]),
        (None, ["an hour ago", "yesterday evening", "five days ago", "three weeks ago", "old"]),
    ],
)
def test_the_date_filter_reads_the_sessions_last_message(
    date: str | None, expected: list[str]
) -> None:
    ages = {
        "an hour ago": timedelta(hours=1),
        "yesterday evening": timedelta(hours=18),  # 20:13 the day before
        "five days ago": timedelta(days=5),
        "three weeks ago": timedelta(days=21),
        "old": timedelta(days=45),
    }
    rows = [
        row(title, last_activity=int((NOW - age).timestamp()), status=None)
        for title, age in ages.items()
    ]
    assert titles(view(rows, HomeFilter(date=date))) == [f"*{title}*" for title in expected]


def test_filters_add_up_and_no_match_says_so() -> None:
    rows = [row("Fix the footer", status=Status.DONE.value), row("Fix the header")]
    both = HomeFilter(status=Status.DONE.value, search="fix")
    assert titles(view(rows, both)) == [":white_check_mark:  *Fix the footer*"]
    nothing = view(rows, HomeFilter(status=Status.ERROR.value, search="fix"))
    assert cards(nothing) == [] and notes(nothing)[-1] == texts.HOME_NO_MATCH


def test_no_bound_channel_says_how_to_bind_one() -> None:
    page = view([], channels={})
    assert notes(page)[-1] == texts.HOME_EMPTY


def test_the_page_stops_within_slacks_blocks_and_says_how_many_it_shows() -> None:
    page = view(many(60), HomeFilter(channel=CHANNEL))
    assert len(page["blocks"]) <= HOME_BLOCKS
    shown = len(cards(page))
    assert 40 < shown < 60
    assert notes(page)[-1] == texts.HOME_MORE.format(rows=shown)


def test_many_channels_stop_within_slacks_blocks_too() -> None:
    channels = {f"C{i:08d}": f"project-{i}" for i in range(12)}
    rows = [
        row(f"s{c}-{i}", channel_id=c, thread_ts=f"1789{i}.{n}", last_activity=EPOCH - n * 60 - i)
        for n, c in enumerate(channels)
        for i in range(6)
    ]
    page = view(rows, channels=channels)
    assert len(page["blocks"]) <= HOME_BLOCKS
    assert notes(page)[-1] == texts.HOME_MORE.format(rows=len(cards(page)))


def control(page: dict[str, Any], action_id: str) -> dict[str, Any]:
    (controls,) = [b for b in page["blocks"] if b.get("block_id") == FILTERS_BLOCK]
    return next(e for e in controls["elements"] if e["action_id"] == action_id)


def test_the_controls_start_on_all_and_show_what_is_chosen() -> None:
    page = view([row()])
    for action_id in (CHANNEL_ACTION, STATUS_ACTION, DATE_ACTION):
        assert control(page, action_id)["initial_option"]["value"] == ALL
    assert [o["text"]["text"] for o in control(page, CHANNEL_ACTION)["options"]] == [
        texts.HOME_ALL_CHANNELS,
        "cc-articles",
        "cc-shop",
        "cc-tools",
    ]
    assert [o["text"]["text"] for o in control(page, STATUS_ACTION)["options"]] == [
        texts.HOME_ALL_STATUSES,
        "Waiting for you",
        "Working",
        "Ended",
        "Error",
    ]
    assert [o["value"] for o in control(page, DATE_ACTION)["options"]] == [
        ALL,
        TODAY,
        YESTERDAY,
        LAST_7,
        LAST_30,
    ]
    (search,) = [b for b in page["blocks"] if b.get("block_id") == SEARCH_BLOCK]
    assert search["dispatch_action"] is True and "initial_value" not in search["element"]

    chosen = HomeFilter(channel=OTHER_CHANNEL, status=Status.ERROR.value, date=LAST_7, search="x")
    page = view([row()], chosen)
    assert control(page, CHANNEL_ACTION)["initial_option"]["value"] == OTHER_CHANNEL
    assert control(page, STATUS_ACTION)["initial_option"]["value"] == Status.ERROR.value
    assert control(page, DATE_ACTION)["initial_option"]["value"] == LAST_7
    (search,) = [b for b in page["blocks"] if b.get("block_id") == SEARCH_BLOCK]
    assert search["element"]["initial_value"] == "x"


def option(value: str) -> dict[str, Any]:
    # A static_select's state, as tests/fixtures/slack/001-block_actions.json records its action.
    return {"type": "static_select", "selected_option": {"value": value}}


def test_a_filter_is_read_from_the_state_of_every_control() -> None:
    values = {
        FILTERS_BLOCK: {
            CHANNEL_ACTION: option(CHANNEL),
            STATUS_ACTION: option(Status.WAITING.value),
            DATE_ACTION: option(YESTERDAY),
        },
        SEARCH_BLOCK: {SEARCH_ACTION: {"type": "plain_text_input", "value": "  Fix\nthe footer "}},
    }
    assert read_filter(values, HomeFilter()) == HomeFilter(
        channel=CHANNEL, status=Status.WAITING.value, date=YESTERDAY, search="Fix the footer"
    )


def test_all_clears_a_filter_and_an_emptied_search_clears_it() -> None:
    current = HomeFilter(channel=CHANNEL, status=Status.DONE.value, date=TODAY, search="x")
    values = {
        FILTERS_BLOCK: {a: option(ALL) for a in (CHANNEL_ACTION, STATUS_ACTION, DATE_ACTION)},
        SEARCH_BLOCK: {SEARCH_ACTION: {"type": "plain_text_input", "value": None}},
    }
    assert read_filter(values, current) == HomeFilter()


def test_a_value_the_page_never_offered_and_a_missing_control_keep_what_was_chosen() -> None:
    current = HomeFilter(status=Status.DONE.value, date=TODAY, search="x")
    values = {FILTERS_BLOCK: {STATUS_ACTION: option("tada"), DATE_ACTION: option("365")}}
    assert read_filter(values, current) == current
    assert read_filter({}, current) == current


# --- the publisher ---

OLD, NEW = "68da9311-0000-4000-8000-00000000000a", "68da9311-0000-4000-8000-00000000000b"
OLD_THREAD, NEW_THREAD, EMPTY_THREAD = "1789000000.000100", "1789000500.000100", "1789000900.0001"


def info(sid: str, summary: str, **fields: Any) -> SDKSessionInfo:
    """Session metadata as `list_sessions` returns it (claude-agent-sdk 0.2.163 SDKSessionInfo)."""
    return SDKSessionInfo(session_id=sid, summary=summary, last_modified=1, **fields)


@pytest.fixture
def state(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "state.json")
    store.bind(CHANNEL, tmp_path / "project")
    store.bind(OTHER_CHANNEL, tmp_path / "other")
    store.open_thread(CHANNEL, OLD_THREAD, session_id=OLD)
    store.open_thread(OTHER_CHANNEL, NEW_THREAD, session_id=NEW)
    store.open_thread(CHANNEL, EMPTY_THREAD)  # its setup is still open: no session yet
    store.set_status_pending(CHANNEL, OLD_THREAD, None, Status.DONE.value)
    store.set_status_pending(OTHER_CHANNEL, NEW_THREAD, Status.WAITING.value)
    return store


def listing(tmp_path: Path) -> dict[Path, list[SDKSessionInfo]]:
    return {
        tmp_path / "project": [
            info(OLD, "Fix the footer"),
            info("68da9311-0000-4000-8000-00000000000c", "A terminal session, in no thread"),
        ],
        tmp_path / "other": [info(NEW, "Add retry to the uploader")],
    }


def make_home(
    slack: FakeSlack, state: StateStore, sessions: dict[Path, list[SDKSessionInfo]], **kwargs: Any
) -> Home:
    return Home(
        slack,
        owner_user_id=OWNER,
        team_id=TEAM,
        state=state,
        sessions_of=lambda directory: sessions[directory],
        debounce=kwargs.pop("debounce", 0.01),
        clock=lambda: EPOCH,
        **kwargs,
    )


def published(slack: FakeSlack) -> list[dict[str, Any]]:
    return [args["view"] for args in slack.calls_to("views.publish")]


async def test_publish_lists_every_held_session_by_channel_newest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slack: FakeSlack, state: StateStore
) -> None:
    config = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    for directory in ("project", "other"):
        (tmp_path / directory).mkdir()
    write_transcript(config, tmp_path / "project", OLD, [
        message("assistant", "2026-09-20T10:00:00.000Z", OLD, "done"),
    ])  # fmt: skip
    write_transcript(config, tmp_path / "other", NEW, [
        message("assistant", "2026-09-21T10:00:00.000Z", NEW, "which one?"),
    ])  # fmt: skip

    await make_home(slack, state, listing(tmp_path)).publish()

    (call,) = slack.calls_to("views.publish")
    assert call["user_id"] == OWNER  # the owner's Home, whoever opens the app
    page = call["view"]
    assert headers(page) == [f"*<#{OTHER_CHANNEL}>*", f"*<#{CHANNEL}>*"]
    assert titles(page) == [
        ":raised_hand:  *Add retry to the uploader*",
        ":white_check_mark:  *Fix the footer*",
    ]
    # Dated by the last message of each transcript, not by the file.
    assert any(n.startswith("waiting for you · <!date^1789984800^{ago}|") for n in notes(page))
    assert all(c["accessory"]["url"] == LINK for c in cards(page))
    # The channel menu names the channels as Slack does (conversations.info).
    name = slack_payload("api-conversations-info")["channel"]["name"]
    assert [o["text"]["text"] for o in control(page, CHANNEL_ACTION)["options"]][1:] == [name, name]


async def test_a_cross_over_a_kept_status_shows_the_cross(
    tmp_path: Path, slack: FakeSlack, state: StateStore
) -> None:
    # An answer that never reached Slack: the root shows ❌ while crash repair still holds ⏳.
    state.set_status_pending(CHANNEL, OLD_THREAD, Status.WORKING.value, Status.ERROR.value)
    await make_home(slack, state, listing(tmp_path)).publish()
    assert ":x:  *Fix the footer*" in titles(published(slack)[0])


async def test_a_session_claude_code_does_not_list_yet_shows_its_id_and_its_roots_time(
    tmp_path: Path, slack: FakeSlack, state: StateStore
) -> None:
    await make_home(slack, state, {tmp_path / "project": [], tmp_path / "other": []}).publish()
    page = published(slack)[0]
    assert titles(page)[0] == f":raised_hand:  *{texts.HOME_UNTITLED.format(id=NEW[:8])}*"
    assert any(f"<!date^{int(float(NEW_THREAD))}^{{ago}}|" in n for n in notes(page))
    assert any(f"<!date^{int(float(OLD_THREAD))}^{{ago}}|" in n for n in notes(page))


async def test_a_folder_that_cannot_be_listed_does_not_stop_the_page(
    tmp_path: Path, slack: FakeSlack, state: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    def sessions_of(directory: Path) -> list[SDKSessionInfo]:
        raise OSError("gone")

    home = Home(
        slack,
        owner_user_id=OWNER,
        team_id=TEAM,
        state=state,
        sessions_of=sessions_of,
        clock=lambda: EPOCH,
    )
    with caplog.at_level(logging.WARNING):
        await home.publish()
    assert len(cards(published(slack)[0])) == 2
    assert "OSError" in caplog.text


async def test_dates_that_cannot_be_read_keep_the_titles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slack: FakeSlack, state: StateStore
) -> None:
    def broken(directory: Path, sessions: list[SDKSessionInfo]) -> list[SDKSessionInfo]:
        raise OSError("unreadable")

    monkeypatch.setattr(home_module, "dated", broken)
    await make_home(slack, state, listing(tmp_path)).publish()
    assert sorted(titles(published(slack)[0])) == [
        ":raised_hand:  *Add retry to the uploader*",
        ":white_check_mark:  *Fix the footer*",
    ]


async def test_a_channel_slack_no_longer_has_is_left_out_with_its_threads_and_asked_once(
    tmp_path: Path, slack: FakeSlack, state: StateStore
) -> None:
    # The first bound channel is there, the second is gone.
    slack.responses["conversations.info"] = [
        slack_payload("api-conversations-info"),
        slack_error("channel_not_found"),
    ]
    home = make_home(slack, state, listing(tmp_path))
    await home.publish()
    page = published(slack)[0]
    assert headers(page) == [f"*<#{CHANNEL}>*"]
    assert titles(page) == [":white_check_mark:  *Fix the footer*"]
    assert len(control(page, CHANNEL_ACTION)["options"]) == 2  # All channels, and the one left
    # No permalink is asked for a thread of a channel that is gone.
    assert [a["channel"] for a in slack.calls_to("chat.getPermalink")] == [CHANNEL]
    await home.publish()
    assert len(slack.calls_to("conversations.info")) == 2  # each asked once per run


async def test_a_thread_slack_no_longer_has_is_left_out_and_asked_once(
    tmp_path: Path, slack: FakeSlack, state: StateStore
) -> None:
    home = make_home(slack, state, listing(tmp_path))
    await home.publish()
    await home.publish()
    assert len(slack.calls_to("chat.getPermalink")) == 2  # one per thread, not per publish

    slack.calls.clear()
    slack.responses["chat.getPermalink"] = slack_error("message_not_found")
    refused = make_home(slack, state, listing(tmp_path))
    await refused.publish()
    page = published(slack)[0]
    assert cards(page) == []  # a thread that cannot be opened is not listed
    assert headers(page) == [f"*<#{CHANNEL}>*", f"*<#{OTHER_CHANNEL}>*"]
    # Slack's refusal stands for the run (a deleted root stays deleted): not asked again.
    await refused.publish()
    assert len(slack.calls_to("chat.getPermalink")) == 2


async def test_a_permalink_that_failed_without_an_answer_is_asked_again(
    tmp_path: Path, slack: FakeSlack, state: StateStore
) -> None:
    slack.responses["chat.getPermalink"] = OSError("network down")
    home = make_home(slack, state, listing(tmp_path))
    await home.publish()
    assert cards(published(slack)[0]) == []
    slack.responses["chat.getPermalink"] = {"ok": True, "permalink": LINK}
    await home.publish()
    assert len(cards(published(slack)[1])) == 2


async def test_choose_publishes_at_once_with_the_filter(
    tmp_path: Path, slack: FakeSlack, state: StateStore
) -> None:
    home = make_home(slack, state, listing(tmp_path), debounce=60)
    await home.choose(HomeFilter(status=Status.WAITING.value))
    page = published(slack)[0]
    assert titles(page) == [":raised_hand:  *Add retry to the uploader*"]
    assert control(page, STATUS_ACTION)["initial_option"]["value"] == Status.WAITING.value
    # The filter stays for the pages a later change of the sessions publishes.
    await home.publish()
    assert titles(published(slack)[1]) == [":raised_hand:  *Add retry to the uploader*"]


async def test_a_channel_that_is_not_bound_is_no_filter(
    tmp_path: Path, slack: FakeSlack, state: StateStore
) -> None:
    home = make_home(slack, state, listing(tmp_path))
    await home.choose(HomeFilter(channel="C000NOPE", search="footer"))
    assert home.chosen == HomeFilter(search="footer")


async def test_a_filter_chosen_while_a_page_is_on_its_way_is_the_page_that_stays(
    tmp_path: Path, slack: FakeSlack, state: StateStore
) -> None:
    # The first page is held inside its views.publish; a filter is chosen meanwhile. Published
    # side by side, the filtered page would land first and the held one would replace it.
    entered, release = asyncio.Event(), asyncio.Event()
    original = slack.views_publish
    calls = 0

    async def held(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return await original(**kwargs)

    slack.views_publish = held  # type: ignore[method-assign]
    home = make_home(slack, state, listing(tmp_path))
    slow = asyncio.create_task(home.publish())
    await entered.wait()
    choosing = asyncio.create_task(home.choose(HomeFilter(status=Status.DONE.value)))
    await asyncio.sleep(0.02)
    release.set()
    await asyncio.gather(slow, choosing)
    assert titles(published(slack)[-1]) == [":white_check_mark:  *Fix the footer*"]


async def test_a_disabled_home_tab_is_logged_once_and_never_asked_again(
    tmp_path: Path, slack: FakeSlack, state: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    # views.publish reference, read 2026-10-01: `not_enabled`, "Error returned if a home view is
    # published but the Home tab isn't enabled for the app." Measured live the same day.
    slack.responses["views.publish"] = slack_error("not_enabled")
    home = make_home(slack, state, listing(tmp_path))
    with caplog.at_level(logging.WARNING):
        await home.publish()
        await home.publish()
        home.request()
        await asyncio.sleep(0.05)
    assert len(slack.calls_to("views.publish")) == 1
    assert caplog.text.count("Home tab") == 1 and "docs/setup.md" in caplog.text


async def test_a_failed_publish_is_swallowed_and_the_next_one_tries_again(
    tmp_path: Path, slack: FakeSlack, state: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    slack.responses["views.publish"] = [slack_error("internal_error"), {"ok": True}]
    home = make_home(slack, state, listing(tmp_path))
    with caplog.at_level(logging.WARNING):
        await home.publish()  # never raises: the page must not break a turn
    assert "internal_error" in caplog.text
    await home.publish()
    assert len(slack.calls_to("views.publish")) == 2


async def test_a_burst_of_requests_publishes_once_and_a_later_one_publishes_again(
    tmp_path: Path, slack: FakeSlack, state: StateStore
) -> None:
    home = make_home(slack, state, listing(tmp_path))
    for _ in range(5):
        home.request()
    await asyncio.sleep(0.1)
    assert len(published(slack)) == 1
    home.request()
    await asyncio.sleep(0.1)
    assert len(published(slack)) == 2


async def test_a_change_during_a_publish_is_not_lost(
    tmp_path: Path, slack: FakeSlack, state: StateStore
) -> None:
    slack.delay = 0.05  # each Slack call takes a while: the request below lands mid-publish
    home = make_home(slack, state, listing(tmp_path))
    home.request()
    await asyncio.sleep(0.04)
    state.set_status_pending(CHANNEL, OLD_THREAD, Status.WORKING.value)
    home.request()

    def last_titles() -> list[str]:
        pages = published(slack)
        return titles(pages[-1]) if pages else []

    await until(lambda: ":hourglass_flowing_sand:  *Fix the footer*" in last_titles(), 3)


async def test_close_publishes_what_a_pending_request_still_owed(
    tmp_path: Path, slack: FakeSlack, state: StateStore
) -> None:
    home = make_home(slack, state, listing(tmp_path), debounce=60)
    home.request()
    await home.close()
    assert len(published(slack)) == 1
    home.request()  # after the close: nothing is scheduled any more
    await asyncio.sleep(0.02)
    assert len(published(slack)) == 1


async def test_close_cut_in_the_middle_of_a_publish_still_publishes(
    tmp_path: Path, slack: FakeSlack, state: StateStore
) -> None:
    slack.delay = 0.05
    home = make_home(slack, state, listing(tmp_path))
    home.request()
    await asyncio.sleep(0.03)  # past the debounce, inside the first Slack call
    slack.delay = 0.0
    await home.close()
    assert len(published(slack)) == 1


async def test_close_gives_up_on_a_slack_that_does_not_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slack: FakeSlack, state: StateStore
) -> None:
    monkeypatch.setattr(home_module, "CLOSE_SECONDS", 0.05)
    slack.delay = 5.0
    home = make_home(slack, state, listing(tmp_path))
    home.request()
    await asyncio.wait_for(home.close(), 1)  # returns: a stop never hangs on the page
    assert published(slack) == []
