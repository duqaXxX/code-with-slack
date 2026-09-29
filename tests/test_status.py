import asyncio
import logging
from collections.abc import Iterator

import pytest
from slack_sdk.errors import SlackApiError

from code_with_slack.render.status import Status, StatusReaction
from tests.fakes import CHANNEL, THREAD, FakeSlack


@pytest.fixture(autouse=True)
def _reset_missing_scope() -> Iterator[None]:
    """`_missing_scope` is process-wide by design (D10): reset around each test so one test's
    `missing_scope` never leaks into the next."""
    StatusReaction._missing_scope = False
    yield
    StatusReaction._missing_scope = False


def reaction(slack: FakeSlack) -> StatusReaction:
    return StatusReaction(slack, channel=CHANNEL, root_ts=THREAD)


def rejected(error: str) -> SlackApiError:
    return SlackApiError(error, {"ok": False, "error": error})


async def test_a_sequence_of_changes_adds_the_new_reaction_before_removing_the_previous_one(
    slack: FakeSlack,
) -> None:
    sr = reaction(slack)
    await sr.show(Status.WORKING)  # the fresh instance's own cleanup: not this test's concern
    slack.calls.clear()
    await sr.show(Status.WAITING)
    await sr.show(Status.WORKING)
    await sr.show(Status.DONE)
    assert slack.calls == [
        ("reactions.add", {"channel": CHANNEL, "name": "raised_hand", "timestamp": THREAD}),
        (
            "reactions.remove",
            {"channel": CHANNEL, "name": "hourglass_flowing_sand", "timestamp": THREAD},
        ),
        (
            "reactions.add",
            {"channel": CHANNEL, "name": "hourglass_flowing_sand", "timestamp": THREAD},
        ),
        ("reactions.remove", {"channel": CHANNEL, "name": "raised_hand", "timestamp": THREAD}),
        ("reactions.add", {"channel": CHANNEL, "name": "white_check_mark", "timestamp": THREAD}),
        (
            "reactions.remove",
            {"channel": CHANNEL, "name": "hourglass_flowing_sand", "timestamp": THREAD},
        ),
    ]


async def test_showing_the_same_state_again_makes_no_call(slack: FakeSlack) -> None:
    sr = reaction(slack)
    await sr.show(Status.WORKING)
    before = len(slack.calls)
    await sr.show(Status.WORKING)
    assert len(slack.calls) == before


async def test_already_reacted_on_add_counts_as_done(
    slack: FakeSlack, caplog: pytest.LogCaptureFixture
) -> None:
    slack.responses["reactions.add"] = rejected("already_reacted")
    sr = reaction(slack)
    with caplog.at_level(logging.WARNING):
        await sr.show(Status.WORKING)  # never raises
    assert "already_reacted" not in caplog.text


async def test_no_reaction_on_remove_counts_as_done(
    slack: FakeSlack, caplog: pytest.LogCaptureFixture
) -> None:
    slack.responses["reactions.remove"] = rejected("no_reaction")
    sr = reaction(slack)
    await sr.show(Status.WORKING)
    with caplog.at_level(logging.WARNING):
        await sr.show(Status.WAITING)  # the remove of the previous reaction never raises
    assert "no_reaction" not in caplog.text


async def test_another_error_is_logged_with_the_ids_and_swallowed(
    slack: FakeSlack, caplog: pytest.LogCaptureFixture
) -> None:
    slack.responses["reactions.add"] = rejected("channel_not_found")
    sr = reaction(slack)
    with caplog.at_level(logging.WARNING):
        await sr.show(Status.WORKING)  # never raises
    assert CHANNEL in caplog.text
    assert THREAD in caplog.text
    assert "channel_not_found" in caplog.text


async def test_concurrent_show_calls_end_on_the_last_state(slack: FakeSlack) -> None:
    sr = reaction(slack)
    await sr.show(Status.DONE)  # past the fresh instance's own cleanup, not this test's concern
    slack.calls.clear()
    slack.delay = 0.02  # forces the two calls to genuinely overlap
    first = asyncio.create_task(sr.show(Status.WORKING))
    second = asyncio.create_task(sr.show(Status.WAITING))
    await asyncio.gather(first, second)
    added = [args["name"] for method, args in slack.calls if method == "reactions.add"]
    removed = [args["name"] for method, args in slack.calls if method == "reactions.remove"]
    assert added == ["hourglass_flowing_sand", "raised_hand"]
    # The first call's own removal of the primed DONE, then the second's removal of WORKING.
    assert removed == ["white_check_mark", "hourglass_flowing_sand"]


async def test_a_fresh_instance_strips_a_leftover_reaction_from_a_previous_session(
    slack: FakeSlack,
) -> None:
    # An earlier session on the same root left ❌ standing (a restart); this one's own first
    # `show` must remove it too, not just the one name it happens to know about, so the root
    # never carries two.
    sr = reaction(slack)
    await sr.show(Status.WORKING)
    removed = [args["name"] for method, args in slack.calls if method == "reactions.remove"]
    assert sorted(removed) == sorted(["raised_hand", "white_check_mark", "x"])


async def test_a_fresh_instance_s_second_show_removes_only_its_own_previous(
    slack: FakeSlack,
) -> None:
    sr = reaction(slack)
    await sr.show(Status.WORKING)
    slack.calls.clear()
    await sr.show(Status.WAITING)
    removed = [args["name"] for method, args in slack.calls if method == "reactions.remove"]
    assert removed == ["hourglass_flowing_sand"]


async def test_a_failed_add_leaves_current_unchanged_so_the_next_show_retries(
    slack: FakeSlack,
) -> None:
    slack.responses["reactions.add"] = rejected("channel_not_found")
    sr = reaction(slack)
    await sr.show(Status.WORKING)
    assert sr.current is None
    assert slack.calls_to("reactions.remove") == []  # no previous reaction to strip: add lost
    slack.responses.pop("reactions.add")
    await sr.show(Status.WORKING)  # retried, not skipped as "no change"
    assert sr.current is Status.WORKING


async def test_current_reflects_the_last_state_shown_successfully(slack: FakeSlack) -> None:
    sr = reaction(slack)
    assert sr.current is None
    await sr.show(Status.WORKING)
    assert sr.current is Status.WORKING


async def test_missing_scope_is_logged_once_and_stops_further_reactions(
    slack: FakeSlack, caplog: pytest.LogCaptureFixture
) -> None:
    slack.responses["reactions.add"] = rejected("missing_scope")
    first = reaction(slack)
    second = reaction(slack)
    with caplog.at_level(logging.WARNING):
        await first.show(Status.WORKING)
        await second.show(Status.WAITING)
    assert caplog.text.count("missing_scope") == 1
    assert first.current is None
    assert second.current is None
    before = len(slack.calls)
    slack.responses.pop("reactions.add")
    await second.show(Status.WAITING)
    assert second.current is None  # the process gave up on reactions for this run
    assert len(slack.calls) == before  # no further Slack call, even once add would pass again
