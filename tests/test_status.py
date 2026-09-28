import asyncio
import logging

import pytest
from slack_sdk.errors import SlackApiError

from code_with_slack.render.status import Status, StatusReaction
from tests.fakes import CHANNEL, THREAD, FakeSlack


def reaction(slack: FakeSlack) -> StatusReaction:
    return StatusReaction(slack, channel=CHANNEL, root_ts=THREAD)


def rejected(error: str) -> SlackApiError:
    return SlackApiError(error, {"ok": False, "error": error})


async def test_a_sequence_of_changes_adds_the_new_reaction_before_removing_the_previous_one(
    slack: FakeSlack,
) -> None:
    sr = reaction(slack)
    await sr.show(Status.WORKING)
    await sr.show(Status.WAITING)
    await sr.show(Status.WORKING)
    await sr.show(Status.DONE)
    assert slack.calls == [
        (
            "reactions.add",
            {"channel": CHANNEL, "name": "hourglass_flowing_sand", "timestamp": THREAD},
        ),
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
    slack.delay = 0.02  # forces the two calls to genuinely overlap
    sr = reaction(slack)
    first = asyncio.create_task(sr.show(Status.WORKING))
    second = asyncio.create_task(sr.show(Status.WAITING))
    await asyncio.gather(first, second)
    added = [args["name"] for method, args in slack.calls if method == "reactions.add"]
    removed = [args["name"] for method, args in slack.calls if method == "reactions.remove"]
    assert added == ["hourglass_flowing_sand", "raised_hand"]
    assert removed == ["hourglass_flowing_sand"]
