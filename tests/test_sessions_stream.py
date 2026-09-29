"""A session's replies as native Slack streams: what starts one, what ends it, what cuts a turn
short, and what a failed end leaves behind. The sink's own behaviour is tested in test_sinks.py;
here the session drives it."""

import asyncio
from collections.abc import Callable
from typing import Any

import aiohttp
import pytest
from claude_agent_sdk.types import TaskNotificationMessage

from code_with_slack import sessions, texts
from code_with_slack.approvals import Approve
from code_with_slack.render import sinks
from code_with_slack.render.status import Status
from tests.fakes import CHANNEL, THREAD, CanUseToolCall, FakeClock, sdk_messages, split_turns
from tests.test_sessions import Harness, is_report, split_background, until

WRITES = ("chat.postMessage", "chat.startStream", "chat.appendStream", "chat.stopStream")


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sinks, "DEBOUNCE_SECONDS", 0.01)


def with_ask(ask: CanUseToolCall) -> list[Any]:
    """The recorded `tools` turn, asking for permission right after its Bash call starts: Claude
    has already written by then."""
    messages = sdk_messages("tools")
    return [*messages[:21], ask, *messages[21:]]


def writes(h: Harness) -> list[str]:
    return [m for m, _ in h.slack.calls if m in (*WRITES, "chat.update")]


def open_streams(h: Harness) -> list[str]:
    return [ts for ts, m in h.slack.messages.items() if m.streaming]


async def test_a_turn_is_one_stream_that_stops_with_the_footer(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({"turns": [sdk_messages("tools")]})
    turn = await h.session().submit("list the files")
    await asyncio.wait_for(turn.done.wait(), 2)
    assert len(h.slack.stream_ts) == 1 and h.slack.posted_ts == []  # no post, no placeholder
    [start] = h.slack.calls_to("chat.startStream")
    assert start["thread_ts"] == THREAD and start["recipient_user_id"] == "U000ALICE"
    [stop] = h.slack.calls_to("chat.stopStream")
    assert stop["blocks"][0] == {"type": "divider"} and stop["blocks"][-1]["type"] == "context"
    assert h.slack.pushes() == 1
    assert open_streams(h) == []


async def test_submitting_writes_nothing_until_claude_has_something_to_show(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({})  # a turn that never answers
    session = h.session()
    await session.submit("hello")
    await until(lambda: bool(h.clients) and h.clients[-1].queries == ["hello"])
    await asyncio.sleep(0.05)
    assert writes(h) == []  # nothing says Claude is writing, or waiting for the previous reply


async def test_a_queued_message_has_no_reply_until_its_turn_starts(
    harness_for: Callable[..., Harness],
) -> None:
    ask = CanUseToolCall("Bash", {"command": "ls"})
    h = harness_for({"turns": [[ask, *sdk_messages("tools")], sdk_messages("tools")]})
    session = h.session()
    first = await session.submit("first")
    second = await session.submit("second")
    await until(lambda: bool(h.approvals._pending))
    await asyncio.sleep(0.05)
    assert h.slack.stream_ts == []  # the approval is a post; neither reply has started
    approval_id = next(iter(h.approvals._pending))
    assert h.approvals.resolve(approval_id, CHANNEL, THREAD, Approve()) is not None
    await asyncio.wait_for(second.done.wait(), 2)
    assert first.done.is_set()
    assert len(h.slack.stream_ts) == 2
    assert open_streams(h) == []
    assert h.slack.pushes() == 3  # the approval request, and each reply once


async def test_a_tool_first_turn_and_its_cards_read_as_the_recorded_turn(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({"turns": [sdk_messages("tools")]})
    turn = await h.session().submit("list the files")
    await asyncio.wait_for(turn.done.wait(), 2)
    [cards] = h.slack.message_cards()
    assert cards and all(c["status"] == "complete" for c in cards)
    assert all(c["title"].split(":")[0] for c in cards)


async def test_a_reply_waits_for_its_background_task_before_it_stops(
    harness_for: Callable[..., Harness],
) -> None:
    first, notice, injected = split_background()
    h = harness_for({"turns": [first]})
    await asyncio.wait_for((await h.session().submit("start it")).done.wait(), 2)
    await asyncio.sleep(0.05)
    assert len(h.slack.stream_ts) == 1 and open_streams(h) == h.slack.stream_ts  # still open
    assert h.slack.calls_to("chat.stopStream") == [] and h.slack.pushes() == 0
    h.clients[0].inject(notice + injected)
    await until(lambda: bool(h.slack.calls_to("chat.stopStream")))
    summary = next(m.summary for m in notice if isinstance(m, TaskNotificationMessage))
    assert f"✓ {summary}" in h.slack.stream_texts()[0]  # the report joins the same stream
    assert len(h.slack.stream_ts) == 1 and h.slack.pushes() == 1
    assert {"type": "divider"} in h.slack.message_blocks()[0]


async def test_a_reply_that_outlives_the_stream_ends_with_a_closing_message(
    harness_for: Callable[..., Harness],
) -> None:
    ask = CanUseToolCall("Bash", {"command": "ls"})
    h = harness_for({"turns": [with_ask(ask)]})
    clock = FakeClock()
    h.deps.stream_clock = clock
    turn = await h.session().submit("list the files")
    await until(lambda: bool(h.approvals._pending))
    await asyncio.sleep(0.1)
    await clock.advance(sinks.STREAM_SECONDS + 1)
    assert len(h.slack.calls_to("chat.stopStream")) == 1  # the 280 s stop pushes (accepted)
    approval_id = next(iter(h.approvals._pending))
    assert h.approvals.resolve(approval_id, CHANNEL, THREAD, Approve()) is not None
    await asyncio.wait_for(turn.done.wait(), 2)
    closing = h.slack.calls_to("chat.postMessage")[-1]
    assert closing["blocks"][-1]["type"] == "context"  # the footer
    [body] = h.slack.stream_texts()
    first_words = body.split("\n\n")[0]
    assert closing["text"] == sinks.banner_text(first_words)[: sinks.BANNER_LIMIT]
    assert h.slack.pushes() == 3  # the approval request, the 280 s stop, the closing message


# `!stop` ends like any other end, and a restart with queued messages says so once.


async def test_stop_ends_the_stream_with_the_footer_and_shows_the_checkmark(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for(
        {
            "turns": [
                [CanUseToolCall("Bash", {"command": "rm -rf build"}), *sdk_messages("interrupt")]
            ]
        }
    )
    session = h.session()
    turn = await session.submit("clean")
    await until(lambda: bool(h.approvals._pending))
    assert await session.stop() is True
    await asyncio.wait_for(turn.done.wait(), 2)
    assert len(h.slack.stream_ts) == 1 and open_streams(h) == []
    [stop] = h.slack.calls_to("chat.stopStream")
    assert stop["blocks"][-1]["type"] == "context"  # a normal end: the footer, one push
    assert h.slack.pushes() == 2  # the approval request (deleted by the stop), and the reply
    await until(lambda: h.reactions()[-1] == Status.DONE.value)


async def test_a_restart_with_queued_messages_ends_the_running_reply_with_one_note(
    harness_for: Callable[..., Harness],
) -> None:
    ask = CanUseToolCall("Bash", {"command": "ls"})
    h = harness_for({"turns": [with_ask(ask)]})
    session = h.session()
    first = await session.submit("first question")
    await session.submit("second question")
    await session.submit("third question")
    await until(lambda: bool(h.approvals._pending) and bool(session.busy))
    await session.drop_queued(error=True)
    approval_id = next(iter(h.approvals._pending))
    assert h.approvals.resolve(approval_id, CHANNEL, THREAD, Approve()) is not None
    await asyncio.wait_for(first.done.wait(), 2)
    [reply] = h.slack.stream_texts()
    note = "2 messages were not sent because code-with-slack restarted: send them again."
    assert note in reply
    assert '"second question"' in reply and '"third question"' in reply
    assert len(h.slack.stream_ts) == 1  # the dropped messages get no reply of their own
    assert h.slack.pushes() == 2  # the approval request, and this one end
    assert Status.ERROR.value in h.reactions()  # D10: a dropped message reacts ❌


async def test_dropped_messages_with_nothing_running_get_one_message(
    harness_for: Callable[..., Harness],
) -> None:
    gate = asyncio.Event()
    h = harness_for({"connect_gate": gate})
    session = h.session()
    await session.submit("first")  # taken by the worker, waiting for the client to connect
    await session.submit("second")
    await session.submit("third")
    await asyncio.sleep(0.05)
    await session.drop_queued(error=True)
    [post] = h.slack.calls_to("chat.postMessage")
    assert "2 messages were not sent because code-with-slack restarted" in post["text"]
    assert h.slack.stream_ts == []
    gate.set()


async def test_one_dropped_message_is_named_in_the_singular(
    harness_for: Callable[..., Harness],
) -> None:
    gate = asyncio.Event()
    h = harness_for({"connect_gate": gate})
    session = h.session()
    await session.submit("first")
    await session.submit("only the second")
    await asyncio.sleep(0.05)
    await session.drop_queued(error=True)
    [post] = h.slack.calls_to("chat.postMessage")
    assert (
        "1 message was not sent because code-with-slack restarted: send it again." in post["text"]
    )
    gate.set()


async def test_closing_a_session_ends_its_open_reply_and_leaves_no_stream_open(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({"turns": [sdk_messages("tools")[:21]]})  # starts, never ends
    session = h.session()
    await session.submit("hello")
    await until(lambda: bool(h.slack.stream_ts))
    await session.close()
    assert open_streams(h) == []
    assert texts.ENDED.format(reason=texts.ENDED_SHUTDOWN) in h.slack.stream_texts()[0]
    assert h.slack.pushes() == 1


async def test_an_error_that_cuts_a_turn_ends_the_stream_with_the_cross(
    harness_for: Callable[..., Harness],
) -> None:
    from tests.fakes import EndOfStream

    h = harness_for({"turns": [[*sdk_messages("tools")[:21], EndOfStream()]]})
    turn = await h.session().submit("hello")
    await asyncio.wait_for(turn.done.wait(), 2)
    assert open_streams(h) == []
    await until(lambda: h.reactions()[-1] == Status.ERROR.value)
    assert "Claude Code reported an error" in h.slack.stream_texts()[0]
    [cards] = h.slack.message_cards()
    assert [c["status"] for c in cards] == ["complete"]  # the call that was running is closed
    assert h.slack.pushes() == 1


async def test_a_turn_that_fails_before_claude_answers_is_a_reply_of_its_own(
    harness_for: Callable[..., Harness], tmp_path: Any
) -> None:
    gone = tmp_path / "gone"
    gone.mkdir()
    h = harness_for({})
    h.state.bind(CHANNEL, gone)
    gone.rmdir()
    failed = await h.session().submit("hello")
    await asyncio.wait_for(failed.done.wait(), 2)
    assert h.slack.stream_texts() == [texts.DIRECTORY_MISSING.format(directory=gone)]
    assert h.slack.pushes() == 1 and open_streams(h) == []


# A final write that fails: no checkmark, the cross, and the persisted status kept for repair.


def lose_stops(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 0.05)
    h.slack.responses["chat.stopStream"] = aiohttp.ClientConnectionError("network down")


async def test_a_reply_whose_end_failed_shows_no_checkmark_then_the_cross(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness_for({"turns": [sdk_messages("tools")]})
    lose_stops(h, monkeypatch)
    turn = await h.session().submit("list the files")
    await asyncio.wait_for(turn.done.wait(), 2)
    await asyncio.sleep(0.02)  # the stop failed; its retry is still waiting
    assert Status.DONE.value not in h.reactions()
    assert h.state.thread(CHANNEL, THREAD).status == Status.WORKING.value
    await until(lambda: h.reactions()[-1] == Status.ERROR.value)
    assert Status.DONE.value not in h.reactions()
    # kept, so a crash right now is still repaired
    assert h.state.thread(CHANNEL, THREAD).status == Status.WORKING.value
    assert h.state.thread(CHANNEL, THREAD).open_replies == (h.slack.stream_ts[0],)


async def test_a_retry_that_lands_shows_the_checkmark_and_clears_the_status(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 0.05)
    h = harness_for({"turns": [sdk_messages("tools")]})
    h.slack.responses["chat.stopStream"] = [aiohttp.ClientConnectionError("down"), {"ok": True}]
    turn = await h.session().submit("list the files")
    await asyncio.wait_for(turn.done.wait(), 2)
    await asyncio.sleep(0.01)
    assert Status.DONE.value not in h.reactions()
    await until(lambda: h.reactions()[-1] == Status.DONE.value)
    assert Status.ERROR.value not in h.reactions()
    assert h.state.thread(CHANNEL, THREAD).status is None
    assert h.state.thread(CHANNEL, THREAD).open_replies == ()


async def test_a_close_that_cannot_end_a_reply_shows_the_cross_and_keeps_what_repair_needs(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness_for({"turns": [sdk_messages("tools")]})
    lose_stops(h, monkeypatch)
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 60.0)  # the close falls in the window
    session = h.session()
    await asyncio.wait_for((await session.submit("list")).done.wait(), 2)
    assert Status.DONE.value not in h.reactions()
    await session.close()
    assert h.reactions()[-1] == Status.ERROR.value
    stored = h.state.thread(CHANNEL, THREAD)
    assert stored.status == Status.WORKING.value  # repair covers it
    assert stored.open_replies == (h.slack.stream_ts[0],)  # the stream Slack still holds open


async def test_shutdown_ends_every_reply_with_a_retry_pending(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 60.0)
    h = harness_for({"turns": [sdk_messages("tools"), sdk_messages("tools")]})
    h.slack.responses["chat.stopStream"] = aiohttp.ClientConnectionError("down")
    session = h.session()
    await asyncio.wait_for((await session.submit("a")).done.wait(), 2)
    await asyncio.wait_for((await session.submit("b")).done.wait(), 2)
    assert len(open_streams(h)) == 2  # both stops failed, their retries pending
    h.slack.responses["chat.stopStream"] = {"ok": True}
    await session.close()
    assert open_streams(h) == []  # each ended once at the close
    assert h.state.thread(CHANNEL, THREAD).status is None


async def test_the_status_is_persisted_until_the_end_lands(
    harness_for: Callable[..., Harness],
) -> None:
    # A crash before the stop lands must still be repaired: the persisted status is cleared only
    # after the end has landed.
    h = harness_for({"turns": [sdk_messages("tools")]})
    seen: list[str | None] = []
    original = h.slack.api_call

    async def spy(api_method: str, **kwargs: Any) -> Any:
        if api_method == "chat.stopStream":
            seen.append(h.state.thread(CHANNEL, THREAD).status)
        return await original(api_method, **kwargs)

    h.slack.api_call = spy  # type: ignore[method-assign]
    turn = await h.session().submit("list the files")
    await asyncio.wait_for(turn.done.wait(), 2)
    assert seen == [Status.WORKING.value]


async def test_the_open_reply_is_the_stream_until_it_ends(
    harness_for: Callable[..., Harness],
) -> None:
    ask = CanUseToolCall("Bash", {"command": "ls"})
    h = harness_for({"turns": [with_ask(ask)]})
    turn = await h.session().submit("list the files")
    await until(lambda: bool(h.approvals._pending) and bool(h.slack.stream_ts))
    assert h.state.thread(CHANNEL, THREAD).open_replies == (h.slack.stream_ts[0],)
    approval_id = next(iter(h.approvals._pending))
    assert h.approvals.resolve(approval_id, CHANNEL, THREAD, Approve()) is not None
    await asyncio.wait_for(turn.done.wait(), 2)
    assert h.state.thread(CHANNEL, THREAD).open_replies == ()


async def test_a_crossed_owner_query_is_answered_in_the_reply_it_landed_in(
    harness_for: Callable[..., Harness],
) -> None:
    # `_settle`: the start guessed a background report, the result says the owner asked. The
    # answer is in the misrouted reply; the owner turn's own reply is never written.
    h = harness_for({"turns": []})
    session = h.session()
    owner = await session.submit("what happened")
    await until(lambda: bool(h.clients) and h.clients[-1].queries == ["what happened"])
    session._expect_injected_turn()
    h.clients[0].inject(split_turns(sdk_messages("tools"))[0])
    await asyncio.wait_for(owner.done.wait(), 2)
    assert len(h.slack.stream_ts) == 1 and h.slack.posted_ts == []
    assert open_streams(h) == []


def test_the_report_helper_still_reads_a_report() -> None:
    assert is_report(texts.BACKGROUND_NOTICE)
    assert sessions.asked("hello") == "hello"
