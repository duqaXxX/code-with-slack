import asyncio
import logging
from collections.abc import Iterator

import pytest
from slack_sdk.errors import SlackApiError

from code_with_slack import texts
from code_with_slack.render.status import Status, StatusReaction, ThreadStatus
from tests.fakes import CHANNEL, THREAD, FakeSlack


@pytest.fixture(autouse=True)
def _reset_missing_scope() -> Iterator[None]:
    """`_missing_scope` is process-wide by design (D10): reset around each test so one test's
    `missing_scope` never leaks into the next."""
    StatusReaction._missing_scope = False
    ThreadStatus._refused = False
    yield
    StatusReaction._missing_scope = False
    ThreadStatus._refused = False


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


async def test_settle_finishes_a_change_whose_caller_was_cancelled_between_its_two_calls(
    slack: FakeSlack,
) -> None:
    # Issue #104: the new reaction is on the root, the previous one still is, and the caller
    # (a session's reader, cancelled by a close) is gone.
    sr = reaction(slack)
    await sr.show(Status.WORKING)
    slack.calls.clear()
    slack.delay = 0.05
    change = asyncio.create_task(sr.show(Status.DONE))
    while not slack.calls:  # noqa: ASYNC110 (the fake exposes no event to wait on)
        await asyncio.sleep(0.005)
    change.cancel()
    with pytest.raises(asyncio.CancelledError):
        await change
    assert [(method, args["name"]) for method, args in slack.calls] == [
        ("reactions.add", "white_check_mark")
    ]
    slack.delay = 0.0
    await sr.settle()
    assert (
        "reactions.remove",
        {"channel": CHANNEL, "name": "hourglass_flowing_sand", "timestamp": THREAD},
    ) in slack.calls
    assert sr.current is Status.DONE


async def test_a_change_after_a_cancelled_one_leaves_neither_of_its_two_reactions(
    slack: FakeSlack,
) -> None:
    # The cancelled change left ⏳ and ✅ on the root, and the next state is neither: a close
    # that cuts a new prompt short shows ❌, which must stand alone.
    sr = reaction(slack)
    await sr.show(Status.WORKING)
    slack.delay = 0.05
    change = asyncio.create_task(sr.show(Status.DONE))
    while slack.calls[-1][0] != "reactions.add":  # noqa: ASYNC110
        await asyncio.sleep(0.005)
    change.cancel()
    with pytest.raises(asyncio.CancelledError):
        await change
    slack.delay = 0.0
    slack.calls.clear()
    await sr.show(Status.ERROR)
    removed = {args["name"] for method, args in slack.calls if method == "reactions.remove"}
    assert {"hourglass_flowing_sand", "white_check_mark"} <= removed
    assert "x" not in removed


async def test_settle_makes_no_call_when_the_last_change_landed(slack: FakeSlack) -> None:
    sr = reaction(slack)
    await sr.settle()  # nothing was ever asked for
    await sr.show(Status.WORKING)
    await sr.show(Status.DONE)
    before = len(slack.calls)
    await sr.settle()
    assert len(slack.calls) == before


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


# The thread's status line (issue #83).

SHOWN = {
    "channel_id": CHANNEL,
    "thread_ts": THREAD,
    "status": texts.THREAD_WORKING_STATUS,
    "loading_messages": [texts.THREAD_WORKING],
}
CLEARED = {"channel_id": CHANNEL, "thread_ts": THREAD, "status": ""}


def thread_status(slack: FakeSlack, **timing: float) -> ThreadStatus:
    return ThreadStatus(slack, channel=CHANNEL, thread_ts=THREAD, **timing)


def statuses(slack: FakeSlack) -> list[dict[str, object]]:
    return slack.calls_to("assistant.threads.setStatus")


async def beat() -> None:
    """Long enough for the status's own task to make a call that is due."""
    await asyncio.sleep(0.02)


async def test_the_thread_status_is_set_with_a_loading_message_then_cleared(
    slack: FakeSlack,
) -> None:
    status = thread_status(slack)
    status.show(texts.THREAD_WORKING)
    await beat()
    assert statuses(slack) == [SHOWN]
    status.show("")
    await beat()
    assert statuses(slack) == [SHOWN, CLEARED]


async def test_the_same_thread_status_again_makes_no_call(slack: FakeSlack) -> None:
    status = thread_status(slack)
    status.show("")  # never shown: nothing to clear
    await beat()
    assert statuses(slack) == []
    status.show(texts.THREAD_WORKING)
    status.show(texts.THREAD_WORKING)
    await beat()
    assert statuses(slack) == [SHOWN]


async def test_a_thread_status_asked_for_and_dropped_at_once_makes_no_call(
    slack: FakeSlack,
) -> None:
    status = thread_status(slack)
    status.show(texts.THREAD_WORKING)
    status.show("")
    await beat()
    assert statuses(slack) == []


async def test_the_thread_status_is_set_again_before_slack_removes_it(slack: FakeSlack) -> None:
    status = thread_status(slack, refresh=0.03)
    status.show(texts.THREAD_WORKING)
    await asyncio.sleep(0.1)
    assert len(statuses(slack)) >= 3 and all(call == SHOWN for call in statuses(slack))
    await status.close()
    count = len(statuses(slack))
    assert statuses(slack)[-1] == CLEARED
    await asyncio.sleep(0.1)
    assert len(statuses(slack)) == count  # closed: nothing is left running


async def test_a_write_sets_the_thread_status_again_soon_after(slack: FakeSlack) -> None:
    status = thread_status(slack, after_write=0.05)
    status.wrote()  # not shown: a write changes nothing
    await beat()
    assert statuses(slack) == []
    status.show(texts.THREAD_WORKING)
    await beat()
    status.wrote()
    await beat()
    assert statuses(slack) == [SHOWN]  # not at once: one call covers a burst of writes
    await asyncio.sleep(0.06)
    assert statuses(slack) == [SHOWN, SHOWN]
    await status.close()


async def test_writes_that_keep_coming_do_not_put_the_thread_status_off(slack: FakeSlack) -> None:
    status = thread_status(slack, after_write=0.05)
    status.show(texts.THREAD_WORKING)
    await beat()
    for _ in range(10):  # a card updated again and again while its turn waits on it
        status.wrote()
        await beat()
    assert len(statuses(slack)) >= 3 and all(call == SHOWN for call in statuses(slack))
    await status.close()


async def test_a_write_right_after_the_thread_status_is_asked_for_does_not_delay_it(
    slack: FakeSlack,
) -> None:
    status = thread_status(slack, after_write=10)
    status.show(texts.THREAD_WORKING)
    status.wrote()
    await beat()
    assert statuses(slack) == [SHOWN]
    await status.close()


async def test_a_close_that_cuts_the_clearing_call_short_clears_the_thread_status_itself(
    slack: FakeSlack,
) -> None:
    status = thread_status(slack)
    status.show(texts.THREAD_WORKING)
    await beat()
    slack.delay = 0.05  # the clearing call is out when the session closes
    status.show("")
    await beat()
    assert statuses(slack) == [SHOWN]  # still on its way
    slack.delay = 0.0
    await status.close()
    assert statuses(slack) == [SHOWN, CLEARED]


async def test_a_thread_status_slack_refuses_is_logged_once_and_never_raises(
    slack: FakeSlack, caplog: pytest.LogCaptureFixture
) -> None:
    slack.responses["assistant.threads.setStatus"] = rejected("ratelimited")
    status = thread_status(slack, refresh=0.02)
    with caplog.at_level(logging.WARNING):
        status.show(texts.THREAD_WORKING)
        await asyncio.sleep(0.1)
        await status.close()
    assert len(statuses(slack)) >= 3
    lines = [r.getMessage() for r in caplog.records if "setStatus" in r.getMessage()]
    assert lines == [f"assistant.threads.setStatus failed on {CHANNEL}/{THREAD}: ratelimited"]


async def test_closing_a_thread_status_that_never_showed_makes_no_call(slack: FakeSlack) -> None:
    status = thread_status(slack)
    await status.close()
    assert statuses(slack) == []


async def test_a_token_that_cannot_set_a_thread_status_stops_every_instance(
    slack: FakeSlack,
) -> None:
    slack.responses["assistant.threads.setStatus"] = rejected("missing_scope")
    first = thread_status(slack, refresh=0.02)
    first.show(texts.THREAD_WORKING)
    await asyncio.sleep(0.1)
    other = ThreadStatus(slack, channel=CHANNEL, thread_ts="1790000000.000002")
    other.show(texts.THREAD_WORKING)
    await beat()
    assert len(statuses(slack)) == 1  # asked once, by the first: no retry can change the answer
    await first.close()
    await other.close()


async def test_a_thread_status_that_changes_its_words_says_the_new_ones_at_once(
    slack: FakeSlack,
) -> None:
    status = thread_status(slack)
    status.show(texts.THREAD_WORKING)
    await beat()
    status.show("2 shells still running")
    await beat()
    status.show("1 shell still running")
    await beat()
    assert [call["loading_messages"] for call in statuses(slack)] == [
        [texts.THREAD_WORKING],
        ["2 shells still running"],
        ["1 shell still running"],
    ]
    await status.close()
    assert statuses(slack)[-1] == CLEARED


async def test_a_clearing_call_that_fails_is_tried_once_more(slack: FakeSlack) -> None:
    status = thread_status(slack, after_write=0.03)
    status.show(texts.THREAD_WORKING)
    await beat()
    slack.responses["assistant.threads.setStatus"] = [rejected("ratelimited"), {"ok": True}]
    status.show("")
    await beat()
    assert statuses(slack) == [SHOWN, CLEARED]  # refused: the status still stands
    await asyncio.sleep(0.06)
    assert statuses(slack) == [SHOWN, CLEARED, CLEARED]
    await status.close()
    assert len(statuses(slack)) == 3  # it went through: nothing is left to clear


async def test_a_status_that_could_not_be_cleared_is_cleared_when_it_closes(
    slack: FakeSlack,
) -> None:
    status = thread_status(slack, after_write=0.01)
    status.show(texts.THREAD_WORKING)
    await beat()
    slack.responses["assistant.threads.setStatus"] = rejected("ratelimited")
    status.show("")
    await asyncio.sleep(0.06)  # the call and its one retry both fail
    assert statuses(slack) == [SHOWN, CLEARED, CLEARED]
    slack.responses["assistant.threads.setStatus"] = {"ok": True}
    await status.close()
    assert statuses(slack) == [SHOWN, CLEARED, CLEARED, CLEARED]


async def test_the_fallback_status_says_the_same_as_the_line(slack: FakeSlack) -> None:
    status = thread_status(slack)
    status.show("1 shell still running", "has 1 shell still running")
    await beat()
    [call] = statuses(slack)
    assert call["status"] == "has 1 shell still running"
    assert call["loading_messages"] == ["1 shell still running"]
    await status.close()
