import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SDKSessionInfo

from code_with_slack import home as home_module
from code_with_slack import texts
from code_with_slack.home import HOME_BLOCKS, HOME_OPEN_ACTION, Home, HomeRow, home_view
from code_with_slack.render.status import Status
from code_with_slack.state import StateStore
from tests.fakes import FakeSlack
from tests.test_repair import slack_error
from tests.test_resume import message, write_transcript

OWNER = "U000ALICE"
CHANNEL = "C000CHAN"
OTHER_CHANNEL = "C000OTHR"
NOW = 1_790_000_000
LINK = "https://example.slack.com/archives/C000CHAN/p1780000000000001"


def row(title: str = "Refactor the feed parser", **fields: Any) -> HomeRow:
    defaults: dict[str, Any] = {
        "channel_id": CHANNEL,
        "thread_ts": "1789990000.000100",
        "status": Status.WORKING.value,
        "last_activity": NOW - 120,
        "branch": "main",
        "permalink": LINK,
    }
    return HomeRow(title=title, **{**defaults, **fields})


def sections(view: dict[str, Any]) -> list[dict[str, Any]]:
    return [b for b in view["blocks"] if b["type"] == "section"]


def test_a_row_shows_status_title_channel_age_and_branch_with_a_link_button() -> None:
    view = home_view([row()], NOW, total=1)
    assert view["type"] == "home"
    # The view shape and the 100 block cap: docs.slack.dev/surfaces/app-home, read 2026-10-01.
    header, divider, section = view["blocks"]
    assert header["elements"][0]["text"] == texts.HOME_HEADER.format(
        time=f"<!date^{NOW}^{{time}}|2026-09-21 14:13 UTC>"
    )
    assert divider == {"type": "divider"}
    # Slack's own relative date (formatting-message-text, read 2026-10-01): it stays right while
    # the page sits unpublished.
    assert section["text"] == {
        "type": "mrkdwn",
        "text": (
            ":hourglass_flowing_sand:  *Refactor the feed parser*\n"
            f"<#{CHANNEL}> · working · <!date^{NOW - 120}^{{ago}}|2026-09-21 14:11 UTC> · `main`"
        ),
    }
    assert section["accessory"] == {
        "type": "button",
        "action_id": HOME_OPEN_ACTION,
        "text": {"type": "plain_text", "text": texts.HOME_OPEN},
        "url": LINK,
    }


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
    (section,) = sections(home_view([row(status=status.value)], NOW, total=1))
    assert section["text"]["text"].startswith(f":{status.value}:  *")
    assert f"<#{CHANNEL}> · {word} · " in section["text"]["text"]


def test_a_row_with_no_reaction_no_branch_and_no_link_shows_what_is_left() -> None:
    plain = row(status=None, branch=None, permalink=None)
    (section,) = sections(home_view([plain], NOW, total=1))
    assert section["text"]["text"] == (
        f"*Refactor the feed parser*\n<#{CHANNEL}> · "
        f"<!date^{NOW - 120}^{{ago}}|2026-09-21 14:11 UTC>"
    )
    assert "accessory" not in section


def test_a_title_and_a_branch_are_shown_as_written() -> None:
    # Model-written and git-written text: unescaped, `<!channel>` would read as a mention.
    hostile = row("ping <!channel> & `more`\nsecond line", branch="fix/<!here>")
    (section,) = sections(home_view([hostile], NOW, total=1))
    text = section["text"]["text"]
    assert "<!channel>" not in text and "<!here>" not in text
    assert "&lt;!channel&gt; &amp;" in text and "fix/&lt;!here&gt;" in text
    assert text.count("\n") == 1  # the title stays on its own single line


def test_no_session_says_so() -> None:
    view = home_view([], NOW, total=0)
    assert [b["type"] for b in view["blocks"]] == ["context", "divider", "context"]
    assert view["blocks"][-1]["elements"][0]["text"] == texts.HOME_EMPTY


def test_rows_past_the_cap_are_named_and_the_view_stays_within_slacks_blocks() -> None:
    shown = [row(f"s{i}", thread_ts=f"17899{i:05d}.000100") for i in range(HOME_BLOCKS - 3)]
    view = home_view(shown, NOW, total=len(shown) + 40)
    assert len(view["blocks"]) == HOME_BLOCKS
    assert view["blocks"][-1]["elements"][0]["text"] == texts.HOME_MORE.format(rows=len(shown))


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
            info(OLD, "Fix the footer", git_branch="main"),
            info("68da9311-0000-4000-8000-00000000000c", "A terminal session, in no thread"),
        ],
        tmp_path / "other": [info(NEW, "Add retry to the uploader", git_branch="retry")],
    }


def make_home(
    slack: FakeSlack, state: StateStore, sessions: dict[Path, list[SDKSessionInfo]]
) -> Home:
    return Home(
        slack,
        owner_user_id=OWNER,
        state=state,
        sessions_of=lambda directory: sessions[directory],
        debounce=0.01,
        clock=lambda: NOW,
    )


def published(slack: FakeSlack) -> list[dict[str, Any]]:
    return [args["view"] for args in slack.calls_to("views.publish")]


async def test_publish_lists_every_held_session_newest_activity_first(
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
    texts_shown = [s["text"]["text"] for s in sections(call["view"])]
    assert [t.splitlines()[0] for t in texts_shown] == [
        ":raised_hand:  *Add retry to the uploader*",
        ":white_check_mark:  *Fix the footer*",
    ]
    # Dated by the last message of each transcript, not by the file.
    assert "<!date^1789984800^{ago}|" in texts_shown[0]
    assert f"<#{OTHER_CHANNEL}> · waiting for you · " in texts_shown[0]
    assert texts_shown[0].endswith("· `retry`")
    assert all(s["accessory"]["url"] == LINK for s in sections(call["view"]))


async def test_a_cross_over_a_kept_status_shows_the_cross(
    tmp_path: Path, slack: FakeSlack, state: StateStore
) -> None:
    # An answer that never reached Slack: the root shows ❌ while crash repair still holds ⏳.
    state.set_status_pending(CHANNEL, OLD_THREAD, Status.WORKING.value, Status.ERROR.value)
    await make_home(slack, state, listing(tmp_path)).publish()
    lines = [s["text"]["text"].splitlines()[0] for s in sections(published(slack)[0])]
    assert ":x:  *Fix the footer*" in lines


async def test_a_session_claude_code_does_not_list_yet_shows_its_id_and_its_roots_time(
    tmp_path: Path, slack: FakeSlack, state: StateStore
) -> None:
    await make_home(slack, state, {tmp_path / "project": [], tmp_path / "other": []}).publish()
    first, second = (s["text"]["text"] for s in sections(published(slack)[0]))
    assert first.startswith(f":raised_hand:  *{texts.HOME_UNTITLED.format(id=NEW[:8])}*")
    assert f"<!date^{int(float(NEW_THREAD))}^{{ago}}|" in first
    assert f"<!date^{int(float(OLD_THREAD))}^{{ago}}|" in second


async def test_a_folder_that_cannot_be_listed_does_not_stop_the_page(
    tmp_path: Path, slack: FakeSlack, state: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    def sessions_of(directory: Path) -> list[SDKSessionInfo]:
        raise OSError("gone")

    home = Home(slack, owner_user_id=OWNER, state=state, sessions_of=sessions_of, clock=lambda: NOW)
    with caplog.at_level(logging.WARNING):
        await home.publish()
    assert len(sections(published(slack)[0])) == 2
    assert "OSError" in caplog.text


async def test_a_permalink_is_asked_once_per_thread_and_a_refused_one_leaves_no_button(
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
    assert all("accessory" not in s for s in sections(published(slack)[0]))
    # Slack's refusal stands for the run (a deleted root stays deleted): not asked again.
    await refused.publish()
    assert len(slack.calls_to("chat.getPermalink")) == 2


async def test_a_permalink_that_failed_without_an_answer_is_asked_again(
    tmp_path: Path, slack: FakeSlack, state: StateStore
) -> None:
    slack.responses["chat.getPermalink"] = OSError("network down")
    home = make_home(slack, state, listing(tmp_path))
    await home.publish()
    assert all("accessory" not in s for s in sections(published(slack)[0]))
    slack.responses["chat.getPermalink"] = {"ok": True, "permalink": LINK}
    await home.publish()
    assert all(s["accessory"]["url"] == LINK for s in sections(published(slack)[1]))


async def test_the_button_names_the_action_the_app_acknowledges() -> None:
    (section,) = sections(home_view([row()], NOW, total=1))
    assert section["accessory"]["action_id"] == HOME_OPEN_ACTION


async def test_dates_that_cannot_be_read_keep_the_titles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slack: FakeSlack, state: StateStore
) -> None:
    def broken(directory: Path, sessions: list[SDKSessionInfo]) -> list[SDKSessionInfo]:
        raise OSError("unreadable")

    monkeypatch.setattr(home_module, "dated", broken)
    await make_home(slack, state, listing(tmp_path)).publish()
    lines = [s["text"]["text"].splitlines()[0] for s in sections(published(slack)[0])]
    assert sorted(lines) == [
        ":raised_hand:  *Add retry to the uploader*",
        ":white_check_mark:  *Fix the footer*",
    ]


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


async def test_a_disabled_home_tab_is_logged_once_and_never_asked_again(
    tmp_path: Path, slack: FakeSlack, state: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    # views.publish reference, read 2026-10-01: `not_enabled`, "Error returned if a home view is
    # published but the Home tab isn't enabled for the app."
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
    await asyncio.sleep(0.6)
    last = [s["text"]["text"] for s in sections(published(slack)[-1])]
    assert any(t.startswith(":hourglass_flowing_sand:  *Fix the footer*") for t in last)


async def test_close_publishes_what_a_pending_request_still_owed(
    tmp_path: Path, slack: FakeSlack, state: StateStore
) -> None:
    home = Home(
        slack,
        owner_user_id=OWNER,
        state=state,
        sessions_of=lambda directory: listing(tmp_path)[directory],
        debounce=60,
        clock=lambda: NOW,
    )
    home.request()
    await home.close()
    assert len(published(slack)) == 1
    home.request()  # after the close: nothing is scheduled any more
    await asyncio.sleep(0.02)
    assert len(published(slack)) == 1
