import logging
from pathlib import Path
from typing import Any

import pytest

from code_with_slack import cleanup
from code_with_slack.cleanup import clean, forget_gone_channels
from code_with_slack.state import StateStore
from tests.fakes import FakeSlack, slack_payload
from tests.test_repair import slack_error

CHANNEL = "C000CHAN"
GONE_CHANNEL = "C000GONE"
OTHER_GONE = "C000GON2"
THREAD = "1789000000.000100"
GONE_THREAD = "1789000500.000100"
SESSION = "68da9311-0000-4000-8000-00000000000a"
NOBODY: frozenset[tuple[str, str]] = frozenset()


def nothing_live() -> frozenset[tuple[str, str]]:
    return NOBODY


@pytest.fixture
def state(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "state.json")
    store.bind(CHANNEL, tmp_path / "project")
    store.bind(GONE_CHANNEL, tmp_path / "old")
    store.open_thread(CHANNEL, THREAD, session_id=SESSION)
    store.open_thread(GONE_CHANNEL, GONE_THREAD, session_id=SESSION)
    return store


def channels_in_slack(slack: FakeSlack, answers: dict[str, Any]) -> None:
    """What `conversations.info` answers for each channel id: the recorded channel when the id
    is not named, else the exception given."""

    def answer(args: dict[str, Any]) -> Any:
        return answers.get(str(args["channel"]), slack_payload("api-conversations-info"))

    slack.responses["conversations.info"] = answer


async def test_a_channel_slack_no_longer_has_is_forgotten_with_its_threads(
    slack: FakeSlack, state: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    # conversations.info reference, read 2026-10-01: `channel_not_found`, also what a private
    # channel the bot is not in answers (measured the same day on deleted channels).
    channels_in_slack(slack, {GONE_CHANNEL: slack_error("channel_not_found")})
    with caplog.at_level(logging.INFO):
        assert await forget_gone_channels(slack, state, nothing_live) == [GONE_CHANNEL]
    assert state.channels() == [CHANNEL]
    assert state.thread(GONE_CHANNEL, GONE_THREAD) is None
    assert state.thread(CHANNEL, THREAD) is not None
    assert GONE_CHANNEL in caplog.text and "1 thread" in caplog.text


@pytest.mark.parametrize(
    "failure",
    [
        slack_error("internal_error"),
        slack_error("ratelimited"),
        slack_error("missing_scope"),
        OSError("network down"),
    ],
)
async def test_anything_but_not_found_forgets_nothing(
    slack: FakeSlack, state: StateStore, failure: Exception
) -> None:
    # Not an answer about the channel: it cannot be told gone.
    channels_in_slack(slack, {GONE_CHANNEL: failure})
    assert await forget_gone_channels(slack, state, nothing_live) == []
    assert state.channels() == [CHANNEL, GONE_CHANNEL]


async def test_no_channel_slack_can_see_forgets_nothing_and_says_so(
    slack: FakeSlack, state: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    # Every bound channel not found: the token of another workspace, or the app removed from
    # them all. Wiping every binding on that would be the wrong repair.
    slack.responses["conversations.info"] = slack_error("channel_not_found")
    with caplog.at_level(logging.WARNING):
        assert await forget_gone_channels(slack, state, nothing_live) == []
    assert state.channels() == [CHANNEL, GONE_CHANNEL]
    assert "none of the 2 bound channels" in caplog.text


@pytest.mark.parametrize("failure", [OSError("network down"), slack_error("ratelimited")])
async def test_a_pass_in_which_slack_found_no_channel_forgets_nothing(
    slack: FakeSlack, state: StateStore, failure: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    # The token of another workspace, and one call that failed for a reason of its own: a
    # channel Slack did not answer about is not a channel it found.
    state.bind(OTHER_GONE, Path("/srv/elsewhere"))
    gone = slack_error("channel_not_found")
    channels_in_slack(slack, {CHANNEL: failure, GONE_CHANNEL: gone, OTHER_GONE: gone})
    with caplog.at_level(logging.WARNING):
        assert await forget_gone_channels(slack, state, nothing_live) == []
    assert state.channels() == [CHANNEL, GONE_CHANNEL, OTHER_GONE]
    assert "none of the 3 bound channels" in caplog.text


async def test_a_channel_with_a_live_session_is_left_for_the_next_pass(
    slack: FakeSlack, state: StateStore
) -> None:
    channels_in_slack(slack, {GONE_CHANNEL: slack_error("channel_not_found")})
    live = frozenset({(GONE_CHANNEL, GONE_THREAD)})
    assert await forget_gone_channels(slack, state, lambda: live) == []
    assert state.channels() == [CHANNEL, GONE_CHANNEL]


async def test_several_gone_channels_go_in_one_pass(slack: FakeSlack, state: StateStore) -> None:
    state.bind(OTHER_GONE, Path("/srv/elsewhere"))
    gone = slack_error("channel_not_found")
    channels_in_slack(slack, {GONE_CHANNEL: gone, OTHER_GONE: gone})
    assert await forget_gone_channels(slack, state, nothing_live) == [GONE_CHANNEL, OTHER_GONE]
    assert state.channels() == [CHANNEL]


async def test_clean_forgets_gone_channels_then_prunes_what_is_left(
    slack: FakeSlack, state: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    channels_in_slack(slack, {GONE_CHANNEL: slack_error("channel_not_found")})
    asked: list[Path] = []

    def alive(directory: Path) -> set[str]:
        asked.append(directory)
        return set()  # the session is gone from the folder

    with caplog.at_level(logging.INFO):
        await clean(slack, state, alive=alive, live=nothing_live)
    assert state.channels() == [CHANNEL]
    assert state.thread(CHANNEL, THREAD) is None  # pruned: its session no longer exists
    assert asked == [state.channel(CHANNEL).directory]  # the forgotten channel's folder is not read
    assert "pruned 1 stale thread" in caplog.text


async def test_clean_keeps_a_thread_with_a_live_session(
    slack: FakeSlack, state: StateStore
) -> None:
    live = frozenset({(CHANNEL, THREAD)})
    await clean(slack, state, alive=lambda _directory: set(), live=lambda: live)
    assert state.thread(CHANNEL, THREAD) is not None


async def test_a_thread_opened_while_the_folders_are_read_is_kept(
    slack: FakeSlack, state: StateStore, tmp_path: Path
) -> None:
    # The sessions are listed off the event loop: what is opened meanwhile, in a folder the
    # listing did not cover, cannot be told gone.
    late = "1789000900.000100"

    def alive(directory: Path) -> set[str]:
        state.bind("C000LATE", tmp_path / "late")
        state.open_thread("C000LATE", late, session_id="a-session-no-listing-saw")
        return {SESSION}

    await clean(slack, state, alive=alive, live=nothing_live)
    assert state.thread("C000LATE", late) is not None


async def test_clean_goes_on_to_prune_when_the_channel_check_itself_fails(
    slack: FakeSlack,
    state: StateStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def broken(*args: object) -> list[str]:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cleanup, "forget_gone_channels", broken)
    with caplog.at_level(logging.WARNING):
        await clean(slack, state, alive=lambda _directory: set(), live=nothing_live)
    assert "could not check the bound channels" in caplog.text
    assert state.thread(CHANNEL, THREAD) is None  # the prune still ran


async def test_clean_survives_a_folder_that_cannot_be_read_and_a_slack_that_fails(
    slack: FakeSlack, state: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    slack.responses["conversations.info"] = OSError("network down")

    def broken(directory: Path) -> set[str]:
        raise PermissionError("transcripts unreadable")

    with caplog.at_level(logging.WARNING):
        await clean(slack, state, alive=broken, live=nothing_live)  # never raises
    assert state.channels() == [CHANNEL, GONE_CHANNEL]
    assert state.thread(CHANNEL, THREAD) is not None
    assert "could not prune stale threads" in caplog.text
