import asyncio
import itertools
import json
import logging
import time
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from claude_agent_sdk import AssistantMessage, ToolUseBlock, UserMessage
from slack_sdk.errors import SlackApiError

from code_with_slack import texts
from code_with_slack.render import sinks
from code_with_slack.render.previews import Preview
from code_with_slack.render.renderer import STOPPED, TaskUpdate, TurnRenderer
from code_with_slack.render.sinks import ReplySink, UpdateLimiter
from tests.fakes import (
    BOT,
    CHANNEL,
    OWNER,
    TEAM,
    THREAD,
    FakeClock,
    FakeSlack,
    ResetAfterApply,
    SlowAfterApply,
    sdk_messages,
)

WRITE_METHODS = (
    "chat.postMessage",
    "chat.startStream",
    "chat.appendStream",
    "chat.stopStream",
    "chat.update",
)


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sinks, "DEBOUNCE_SECONDS", 0.01)


def reply(
    slack: FakeSlack,
    *,
    limiter: Any = None,
    clock: FakeClock | None = None,
    on_open_reply: Any = None,
) -> ReplySink:
    return ReplySink(
        slack,
        channel=CHANNEL,
        thread_ts=THREAD,
        team_id=TEAM,
        user_id=OWNER,
        bot_user_id=BOT,
        limiter=limiter or UpdateLimiter(),
        clock=clock or FakeClock(),
        on_open_reply=on_open_reply,
    )


def methods(slack: FakeSlack) -> list[str]:
    """The Slack writes a reply made, in order."""
    return [m for m, _ in slack.calls if m in WRITE_METHODS]


async def settled() -> None:
    """Long enough for a debounced write, with the debounce shrunk to 10 ms."""
    await asyncio.sleep(0.05)
    # A loop stalled past both (a full garbage collection) finds the debounce and this sleep due
    # together, and would hand back before the write got anywhere: let what the debounce woke
    # run to its next wait, as `FakeClock.advance` does.
    for _ in range(20):
        await asyncio.sleep(0)


def tool(id: str, name: str, status: str = "complete", **fields: Any) -> TaskUpdate:
    return TaskUpdate(id, f"{name}: {id}", status, name=name, **fields)  # type: ignore[arg-type]


def rejected(error: str) -> SlackApiError:
    """Slack's answer to a refused write: `ok` false and an error code."""
    return SlackApiError(error, {"ok": False, "error": error})


# The stream: what starts it, what grows it, what ends it.


async def test_the_first_content_starts_the_stream_and_nothing_comes_before_it(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    assert slack.calls == []  # no placeholder: a reply that says nothing writes nothing
    await sink.text("Looking at the files.")
    await settled()
    [start] = slack.calls_to("chat.startStream")
    assert methods(slack) == ["chat.startStream"]
    assert start["channel"] == CHANNEL and start["thread_ts"] == THREAD
    assert (start["recipient_team_id"], start["recipient_user_id"]) == (TEAM, OWNER)
    assert start["task_display_mode"] == "timeline"
    assert start["chunks"] == [{"type": "markdown_text", "text": "Looking at the files."}]


async def test_a_turn_that_opens_with_a_tool_starts_with_its_card(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.task(tool("t1", "Bash", "in_progress"))
    await settled()
    [start] = slack.calls_to("chat.startStream")
    assert start["chunks"] == [
        {"type": "task_update", "id": "fold:t1", "title": "Bash: t1", "status": "in_progress"}
    ]


async def test_text_grows_the_stream_and_a_card_updates_in_place(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Let me look.\n\n")
    await settled()
    await sink.task(tool("t1", "Bash", "in_progress"))
    await settled()
    await sink.task(tool("t1", "Bash", "complete"))
    await sink.text("The tree is clean.")
    await settled()
    assert methods(slack) == ["chat.startStream"] + ["chat.appendStream"] * 2
    assert slack.message_texts() == ["Let me look.\n\nThe tree is clean."]
    # one card, which the stream keeps updating where it first appeared
    assert slack.message_cards() == [
        [{"id": "fold:t1", "title": "Bash: t1", "status": "complete"}],
    ]
    chunks = [c for a in slack.calls_to("chat.appendStream") for c in a["chunks"]]
    assert [c["type"] for c in chunks] == ["task_update", "task_update", "markdown_text"]
    assert all(a["ts"] == slack.stream_ts[0] for a in slack.calls_to("chat.appendStream"))


async def test_changes_inside_one_debounce_go_in_one_append(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("A")
    await settled()
    for piece in ("B", "C", "D"):
        await sink.text(piece)
    await sink.task(tool("t1", "Bash", "in_progress"))
    await sink.task(tool("t1", "Bash", "complete"))
    await settled()
    [append] = slack.calls_to("chat.appendStream")
    assert append["chunks"] == [
        {"type": "markdown_text", "text": "BCD"},
        {"type": "task_update", "id": "fold:t1", "title": "Bash: t1", "status": "complete"},
    ]


async def test_the_end_stops_the_stream_with_the_footer_in_one_push(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Done.")
    await settled()
    await sink.finish([])
    assert await sink.close_out("main · ctx 6%") is True
    [stop] = slack.calls_to("chat.stopStream")
    assert stop["ts"] == slack.stream_ts[0]
    assert stop["blocks"] == [
        {"type": "divider"},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "main · ctx 6%"}]},
    ]
    assert methods(slack) == ["chat.startStream", "chat.stopStream"]  # no post, no update
    assert slack.pushes() == 1


async def test_a_reply_that_ends_before_its_first_write_starts_and_stops_its_stream(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.text("Done.")
    await sink.finish([])
    await sink.close_out(None)
    assert methods(slack) == ["chat.startStream", "chat.stopStream"]
    assert "blocks" not in slack.calls_to("chat.stopStream")[0]  # no footer, nothing to add
    assert slack.message_texts() == ["Done."]
    assert slack.pushes() == 1


async def test_a_reply_with_no_content_writes_nothing(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.finish([])
    assert await sink.close_out("footer") is True
    assert slack.calls == []


async def test_no_write_carries_a_placeholder(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Working on it.")
    await sink.task(tool("t1", "Bash", "in_progress"))
    await settled()
    await sink.finish([])
    await sink.close_out("footer")
    written = json.dumps([args for method, args in slack.calls if method in WRITE_METHODS])
    assert "writing" not in written and "Waiting" not in written


async def test_a_stream_pushes_only_when_it_stops(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("one")
    await settled()
    await sink.text(" two")
    await settled()
    assert slack.pushes() == 0  # nothing at the start, nothing while it grows
    await sink.finish([])
    await sink.close_out(None)
    assert slack.pushes() == 1


async def test_updates_are_debounced(slack: FakeSlack, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sinks, "DEBOUNCE_SECONDS", 0.1)
    sink = reply(slack)
    await sink.text("x")
    await asyncio.sleep(0.15)
    for i in range(30):
        await sink.text(str(i))
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.3)
    stamps = len(slack.calls_to("chat.appendStream"))
    assert 1 <= stamps <= 6  # 0.3 s of writing, at most one append per 0.1 s


# The 280 seconds: a stream cannot outlive 5 minutes, and a stopped message takes updates.


async def test_the_stream_stops_at_280_seconds_and_the_message_grows_by_update(
    slack: FakeSlack,
) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("First words.\n\n")
    await sink.task(tool("t1", "Bash", "complete"))
    await settled()
    await clock.advance(sinks.STREAM_SECONDS - 1)
    assert slack.calls_to("chat.stopStream") == []
    await clock.advance(2)
    [stop] = slack.calls_to("chat.stopStream")
    assert "blocks" not in stop and slack.pushes() == 1  # accepted: the stop pushes
    await sink.text("Later words.")
    await settled()
    assert methods(slack)[-1] == "chat.update" and len(slack.stream_ts) == 1
    update = slack.calls_to("chat.update")[-1]
    assert update["ts"] == slack.stream_ts[0]
    assert update["blocks"][0] == {"type": "markdown", "text": "First words."}
    assert (
        update["blocks"][1]["type"] == "task_card" and update["blocks"][1]["task_id"] == "fold:t1"
    )
    assert update["blocks"][2] == {"type": "markdown", "text": "Later words."}
    assert update["text"] == "First words."  # short, the banner of the message
    assert slack.pushes() == 1  # an update never pushes


async def test_a_stream_stopped_early_with_nothing_changed_needs_no_update(
    slack: FakeSlack,
) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("All of it.")
    await sink.task(tool("t1", "Bash", "complete"))
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    await settled()
    assert methods(slack) == ["chat.startStream", "chat.stopStream"]  # the stream shows it all


async def test_a_card_running_at_the_switch_is_updated_not_left_as_an_error(
    slack: FakeSlack,
) -> None:
    # A card left in progress in a stopped stream is stored as an error until updated (M33).
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("Started a server.")
    await sink.task(tool("t1", "Bash", "in_progress", task=True, details="Running in background"))
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    await settled()
    update = slack.calls_to("chat.update")[-1]
    [card] = [b for b in update["blocks"] if b["type"] == "task_card"]
    assert card["status"] == "in_progress"


async def test_an_answer_that_is_text_alone_ends_on_a_footer_only_message(
    slack: FakeSlack,
) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("**Bold** answer, with a [link](https://example.com).\n\nSecond paragraph.")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    await sink.text(" More.")
    await sink.finish([])
    assert await sink.close_out("main · ctx 6%") is True
    [closing] = slack.calls_to("chat.postMessage")
    assert closing["thread_ts"] == THREAD and closing["unfurl_links"] is False
    assert closing["blocks"] == [
        {"type": "divider"},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "main · ctx 6%"}]},
    ]
    # Claude's own words, plain: never a line of the daemon's
    assert closing["text"] == "Bold answer, with a link."
    assert slack.pushes() == 2  # the stop at 280 s, and this one
    # nothing is moved (the first message would keep nothing): the footer lives in the closing
    # message, and the body message carries none
    assert all(b["type"] != "divider" for b in slack.calls_to("chat.update")[-1]["blocks"])


async def long_reply(slack: FakeSlack) -> ReplySink:
    """A reply past the stream's 280 seconds: words, a call, then the answer Claude ends on."""
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("Let me check the build.")
    await sink.task(tool("t1", "Edit"))
    await sink.text("The build passed.\n\n- 214 tests\n- no failures")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    await sink.finish([])
    return sink


ENDING = {"type": "markdown", "text": "The build passed.\n\n- 214 tests\n- no failures"}


def kept(slack: FakeSlack) -> list[str]:
    """The kinds of block the reply's first message shows last."""
    return [b["type"] for b in slack.messages[slack.stream_ts[0]].blocks]


async def test_the_end_after_the_switch_posts_the_ending_with_the_footer(slack: FakeSlack) -> None:
    sink = await long_reply(slack)
    assert await sink.close_out("main · ctx 6%") is True
    # The text Claude wrote after its last call, whole, and the footer, as the message that
    # notifies: its text says how the work ended (the stream's own stop said how it began).
    [ending] = slack.calls_to("chat.postMessage")
    assert ending["thread_ts"] == THREAD and ending["unfurl_links"] is False
    assert ending["blocks"] == [ENDING, {"type": "divider"}, sinks.context_block("main · ctx 6%")]
    assert ending["text"] == "The build passed."
    assert slack.pushes() == 2  # the stop at 280 s, and this one
    # The message it grew in keeps the rest, silently: nothing shows twice, and no footer there.
    assert slack.message_texts() == ["Let me check the build.", ENDING["text"]]
    assert kept(slack) == ["markdown", "context"]  # its words, and the call as a line of counts


async def test_an_ending_takes_what_follows_it(slack: FakeSlack) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.task(tool("t1", "Edit"))
    await sink.text("The edit is done.")
    await sink.text("_2 messages were not sent._", notice=True)
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    await sink.finish([])
    assert await sink.close_out("footer") is True
    [ending] = slack.calls_to("chat.postMessage")
    assert [b["type"] for b in ending["blocks"]] == ["markdown", "markdown", "divider", "context"]
    assert ending["blocks"][0]["text"] == "The edit is done."
    assert ending["text"] == "The edit is done."  # never a line of the daemon's
    assert "markdown" not in kept(slack)  # the call stays, the words moved


async def test_an_ending_that_slack_did_not_take_is_posted_once_by_the_retry(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 0.02)
    sink = await long_reply(slack)
    answer = slack.responses["chat.postMessage"]
    slack.responses["chat.postMessage"] = [rejected("ratelimited"), answer]
    assert await sink.close_out("footer") is False
    # Not posted: the answer stays where it was, never nowhere.
    assert slack.message_texts() == [f"Let me check the build.\n\n{ENDING['text']}"]
    assert await sink.wait_landed() is True
    assert slack.message_texts() == ["Let me check the build.", ENDING["text"]]
    assert len(slack.posted_ts) == 1 and slack.pushes() == 2


async def test_an_ending_left_twice_by_a_failed_edit_has_not_landed_until_the_retry(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 0.02)
    sink = await long_reply(slack)
    slack.responses["chat.update"] = [rejected("ratelimited"), {"ok": True}]
    assert await sink.close_out("footer") is False  # posted, still shown in the first message
    assert slack.message_texts()[1] == ENDING["text"] and "markdown" in kept(slack)[2:]
    assert await sink.wait_landed() is True
    assert slack.message_texts() == ["Let me check the build.", ENDING["text"]]
    assert len(slack.posted_ts) == 1


async def cancelled_inside_the_end(slack: FakeSlack) -> tuple[ReplySink, asyncio.Event]:
    """A reply whose `close_out` caller is cancelled while Slack holds the end's write open
    (released by setting the returned event)."""
    sink = reply(slack)
    await sink.text("Hello.")
    await settled()
    await sink.finish([])
    gate = slack.gate = asyncio.Event()
    slack.gated.clear()
    closing = asyncio.create_task(sink.close_out("footer"))
    await slack.gated.wait()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    return sink, gate


async def test_a_close_out_whose_caller_is_cancelled_still_lands_and_resolves(
    slack: FakeSlack,
) -> None:
    sink, gate = await cancelled_inside_the_end(slack)
    slack.gate = None
    gate.set()
    assert await asyncio.wait_for(sink.wait_landed(), 1.0) is True
    assert await sink.close_out("footer") is True


async def test_a_close_out_cancelled_over_a_write_slack_refused_still_gets_its_retry(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 0.02)
    slack.responses["chat.stopStream"] = [rejected("ratelimited"), {"ok": True}]
    sink, gate = await cancelled_inside_the_end(slack)
    slack.gate = None
    gate.set()
    assert await asyncio.wait_for(sink.wait_landed(), 1.0) is True
    assert len(slack.calls_to("chat.stopStream")) == 2  # the refused write, then the retry


async def test_a_settle_behind_a_refused_end_leaves_no_retry_alive(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 0.02)
    # Slack refuses the end and then the shutdown's own write: that shutdown has lost the reply
    # and nothing may write it afterwards.
    slack.responses["chat.stopStream"] = [rejected("ratelimited"), rejected("ratelimited")]
    sink, gate = await cancelled_inside_the_end(slack)
    settling = asyncio.create_task(sink.settle())  # queues behind the end's write
    await asyncio.sleep(0)
    slack.gate = None
    gate.set()
    assert await asyncio.wait_for(settling, 1.0) is False
    writes = len(slack.calls)
    await asyncio.sleep(0.1)  # well past FINAL_RETRY_SECONDS
    assert len(slack.calls) == writes  # nothing goes out after `settle` returned


async def test_a_settle_behind_an_end_cancelled_itself_does_not_raise(slack: FakeSlack) -> None:
    sink, gate = await cancelled_inside_the_end(slack)
    settling = asyncio.create_task(sink.settle())  # waits for the end in flight
    await asyncio.sleep(0)
    assert sink._ending is not None
    sink._ending.cancel()  # a loop's teardown: `settle` itself was not cancelled
    slack.gate = None
    gate.set()
    await asyncio.wait_for(settling, 1.0)  # no CancelledError reaches the caller of `settle`
    assert await asyncio.wait_for(sink.wait_landed(), 1.0) is False


async def test_an_end_that_raises_still_resolves_landed(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = reply(slack)
    await sink.text("Hello.")
    await settled()
    await sink.finish([])

    async def broken() -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(sink, "_flush", broken)
    with pytest.raises(RuntimeError):
        await sink.close_out("footer")
    assert await asyncio.wait_for(sink.wait_landed(), 1.0) is False
    assert await sink.close_out("footer") is False


async def test_an_ending_cut_off_while_it_is_posted_is_posted_before_anything_is_shortened(
    slack: FakeSlack,
) -> None:
    sink = await long_reply(slack)
    slack.delay = 0.05
    closing = asyncio.create_task(sink.close_out("footer"))
    await asyncio.sleep(0.02)  # the ending's post is out
    closing.cancel()
    await asyncio.gather(closing, return_exceptions=True)
    slack.delay = 0.0
    assert slack.posted_ts == []  # cut off before Slack took it
    writes = len(slack.calls)
    assert await sink.settle() is True  # a shutdown's last pass
    order = [m for m, _ in slack.calls[writes:] if m in ("chat.postMessage", "chat.update")]
    assert order[0] == "chat.postMessage"  # posted first: never nowhere
    assert slack.message_texts() == ["Let me check the build.", ENDING["text"]]
    assert len(slack.posted_ts) == 1


async def test_the_ending_follows_the_running_counts_by_edits(slack: FakeSlack) -> None:
    sink = await long_reply(slack)
    await sink.close_out("footer")
    ts = slack.posted_ts[0]
    await sink.set_running("⏳ 1 shell")
    await settled()
    edit = slack.calls_to("chat.update")[-1]
    assert edit["ts"] == ts and edit["blocks"][-1] == sinks.context_block("footer · ⏳ 1 shell")
    await sink.set_running("")
    await settled()
    assert slack.calls_to("chat.update")[-1]["blocks"][-1] == sinks.context_block("footer")
    assert len(slack.posted_ts) == 1 and slack.pushes() == 2  # an edit never pushes


async def test_a_reply_says_its_footer_shows_only_once_its_end_has_landed(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 0.02)
    sink = reply(slack)
    await sink.text("Done.")
    await sink.finish([])
    assert sink.footer_shown is False
    slack.responses["chat.stopStream"] = [rejected("ratelimited"), {"ok": True}]
    assert await sink.close_out("footer") is False
    assert sink.footer_shown is False  # no footer on Slack yet: the status line still counts
    assert await sink.wait_landed() is True
    assert sink.footer_shown is True


async def test_the_closing_message_text_skips_the_daemon_s_own_lines(slack: FakeSlack) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("_Compacted the conversation._\n\n", notice=True)
    await sink.text("The real answer & more.")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    await sink.finish([])
    await sink.close_out(None)
    [closing] = slack.calls_to("chat.postMessage")
    assert closing["text"] == "The real answer &amp; more."
    assert closing["blocks"] == [sinks.context_block(sinks.ZERO_WIDTH_SPACE)]  # no footer to show


async def test_a_stream_slack_closed_first_falls_back_to_updates(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("one")
    await settled()
    slack.expire(slack.stream_ts[0])  # Slack ended it, at 5 minutes, before the daemon's timer
    await sink.text(" two")
    await settled()
    assert slack.calls_to("chat.appendStream")[-1]["chunks"][0]["text"] == " two"  # refused
    update = slack.calls_to("chat.update")[-1]
    assert update["blocks"] == [{"type": "markdown", "text": "one two"}]
    await sink.finish([])
    await sink.close_out("footer")
    assert len(slack.calls_to("chat.postMessage")) == 1  # the closing message, as after 280 s


async def test_a_stop_refused_because_the_stream_expired_still_ends_the_reply(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.text("one")
    await settled()
    slack.expire(slack.stream_ts[0])
    await sink.finish([])
    assert await sink.close_out("footer") is True
    assert len(slack.calls_to("chat.postMessage")) == 1
    assert slack.pushes() == 2  # Slack's own stop, then the closing message


# Limits: a message holds so much, and the reply goes on in the next.


async def test_text_past_the_limit_continues_in_a_new_stream(slack: FakeSlack) -> None:
    sink = reply(slack)
    body = "a line of text\n" * 1_000  # 15,000 characters
    await sink.text(body)
    await settled()
    await sink.finish([])
    await sink.close_out("footer")
    assert len(slack.stream_ts) == 2 and slack.posted_ts == []
    first, second = slack.message_texts()
    assert len(first) <= sinks.MESSAGE_LIMIT and len(second) <= sinks.MESSAGE_LIMIT
    assert first + "\n" + second == body.strip("\n")  # nothing lost at the cut
    stops = slack.calls_to("chat.stopStream")
    assert [("blocks" in s) for s in stops] == [False, True]  # the footer only ends the reply
    assert slack.pushes() == 2  # each extra message pushes (accepted)


async def test_cards_past_the_limit_continue_in_a_new_stream(slack: FakeSlack) -> None:
    sink = reply(slack)
    for i in range(60):
        await sink.task(tool(f"t{i}", "Agent", task=True))  # each has a card of its own
    await settled()
    await sink.finish([])
    await sink.close_out(None)
    assert [len(cards) for cards in slack.message_cards()] == [sinks.BLOCKS_LIMIT, 15]


async def test_a_reply_past_the_limit_after_the_window_continues_in_a_post(
    slack: FakeSlack,
) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("start\n")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    await sink.text("a line of text\n" * 1_000)
    await settled()
    assert len(slack.stream_ts) == 1 and len(slack.posted_ts) == 1
    first, second = slack.message_texts()
    assert len(first) <= sinks.MESSAGE_LIMIT and len(second) <= sinks.MESSAGE_LIMIT
    await sink.finish([])
    await sink.close_out("footer")
    assert len(slack.posted_ts) == 2  # the closing message follows the continuation


async def test_a_message_never_holds_more_blocks_than_slack_allows(slack: FakeSlack) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("go\n")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    for i in range(80):  # text and cards alternating: every one is a block of its own
        await sink.text(f"step {i}")
        await sink.task(tool(f"t{i}", "Read"))
    await sink.finish([])
    await sink.close_out("footer")
    await settled()
    for message in slack.message_blocks():
        assert len(message) <= 50
    for method, args in slack.calls:
        if method in ("chat.update", "chat.postMessage"):
            assert len(args["blocks"]) <= 50


async def test_a_long_reply_keeps_the_text_of_an_update_short(slack: FakeSlack) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("word " * 60 + "\n")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    await sink.text("tail " * 2_000)
    await settled()
    for method, args in slack.calls:
        if method in ("chat.update", "chat.postMessage"):
            assert len(args["text"]) <= sinks.BANNER_LIMIT


# What follows the end: a late change edits the stopped message, silently.


async def test_a_task_that_ends_after_the_reply_edits_the_stopped_message(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.text("Started it.")
    await sink.task(tool("t1", "Bash", "in_progress", task=True, details="Running in background"))
    await settled()
    await sink.finish([])
    await sink.close_out("footer")
    assert slack.pushes() == 1
    await sink.task(tool("t1", "Bash", "complete", task=True))
    await settled()
    update = slack.calls_to("chat.update")[-1]
    assert update["ts"] == slack.stream_ts[0]
    [card] = [b for b in update["blocks"] if b["type"] == "task_card"]
    assert card["status"] == "complete"
    assert update["blocks"][-1]["elements"][0]["text"] == "footer"  # the footer stays
    assert slack.pushes() == 1


async def test_running_counts_join_the_footer_of_a_stopped_stream(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Done.")
    await sink.finish([])
    await sink.close_out("main · ctx 6%")
    await sink.set_running("⏳ 1 shell")
    await settled()
    update = slack.calls_to("chat.update")[-1]
    assert update["blocks"][-1]["elements"][0]["text"] == "main · ctx 6% · ⏳ 1 shell"
    await sink.set_running("")
    await settled()
    assert slack.calls_to("chat.update")[-1]["blocks"][-1]["elements"][0]["text"] == "main · ctx 6%"


async def test_unchanged_running_counts_write_nothing(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Done.")
    await sink.finish([])
    await sink.close_out("footer")
    before = len(slack.calls)
    await sink.set_running("")
    await sink.set_running("")
    await settled()
    assert len(slack.calls) == before


async def test_a_reply_that_is_no_longer_latest_keeps_its_footer_and_drops_the_counts(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.text("Done.")
    await sink.finish([])
    await sink.close_out("footer")
    await sink.set_running("⏳ 1 shell")
    await settled()
    assert slack.calls_to("chat.update")[-1]["blocks"][-1] == sinks.context_block(
        "footer · ⏳ 1 shell"
    )
    await sink.set_latest(False)
    await settled()
    # The footer is the record of how this turn ended; what still runs is said once, at the
    # bottom of the thread, by the latest reply.
    update = slack.calls_to("chat.update")[-1]
    assert [b["type"] for b in update["blocks"]] == ["markdown", "divider", "context"]
    assert update["blocks"][-1] == sinks.context_block("footer")


async def test_the_closing_message_follows_running_counts_and_latest(slack: FakeSlack) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("Done.")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    await sink.finish([])
    await sink.close_out("footer")
    ts = slack.posted_ts[0]
    await sink.set_running("⏳ 1 shell")
    await settled()
    edit = slack.calls_to("chat.update")[-1]
    assert edit["ts"] == ts and edit["blocks"][-1]["elements"][0]["text"] == "footer · ⏳ 1 shell"
    assert edit["text"] == "Done."  # an edit never pushes: the text stays Claude's words
    await sink.set_latest(False)
    await settled()
    # no longer the latest: the counts go, the footer stays
    assert slack.calls_to("chat.update")[-1]["blocks"] == [
        {"type": "divider"},
        sinks.context_block("footer"),
    ]
    assert len(slack.posted_ts) == 1 and slack.calls_to("chat.delete") == []


async def test_a_second_close_out_call_is_a_no_op(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Done.")
    await sink.finish([])
    await sink.close_out("one")
    before = len(slack.calls)
    await sink.close_out("two")
    assert len(slack.calls) == before


# Failures never raise, and the end is tried once more.


async def test_a_slack_failure_never_raises(slack: FakeSlack) -> None:
    slack.responses["chat.startStream"] = aiohttp.ClientConnectionError("network down")
    sink = reply(slack)
    await sink.text("hello")
    await sink.task(tool("t1", "Bash"))
    await settled()
    await sink.finish([])
    assert await sink.close_out("footer") is False
    await sink.settle()


async def test_a_failed_append_is_sent_with_the_next_flush(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("A")
    await settled()
    slack.responses["chat.appendStream"] = [aiohttp.ClientConnectionError("down"), {"ok": True}]
    await sink.text("B")
    await settled()
    await sink.text("C")
    await settled()
    assert slack.message_texts() == ["ABC"]  # B was not lost, nor sent twice


async def test_a_failed_start_is_tried_again_with_everything_since(slack: FakeSlack) -> None:
    slack.responses["chat.startStream"] = [
        aiohttp.ClientConnectionError("down"),
        slack.responses["chat.startStream"],
    ]
    sink = reply(slack)
    await sink.text("one")
    await settled()
    assert slack.stream_ts == []
    await sink.text(" two")
    await settled()
    assert slack.message_texts() == ["one two"]


async def test_a_final_write_lost_to_the_network_is_tried_again_once(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 0.01)
    sink = reply(slack)
    await sink.text("Done.")
    await settled()
    slack.responses["chat.stopStream"] = [aiohttp.ClientConnectionError("down"), {"ok": True}]
    await sink.finish([])
    assert await sink.close_out("footer") is False
    assert await sink.wait_landed() is True
    assert len(slack.calls_to("chat.stopStream")) == 2


async def test_a_final_write_that_fails_twice_is_reported_lost(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 0.01)
    sink = reply(slack)
    await sink.text("Done.")
    await settled()
    slack.responses["chat.stopStream"] = aiohttp.ClientConnectionError("down")
    await sink.finish([])
    assert await sink.close_out("footer") is False
    assert await sink.wait_landed() is False


async def test_settle_writes_what_is_still_debounced_and_retries_the_end(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.text("one")
    assert await sink.settle() is True  # nothing waits a debounce at a shutdown
    assert slack.message_texts() == ["one"]
    slack.responses["chat.stopStream"] = [aiohttp.ClientConnectionError("down"), {"ok": True}]
    await sink.finish([])
    assert await sink.close_out(None) is False
    assert await sink.settle() is True  # the retry still waiting is tried now
    assert await sink.wait_landed() is True


async def test_an_error_is_logged_without_message_content(
    slack: FakeSlack, caplog: pytest.LogCaptureFixture
) -> None:
    sink = reply(slack)
    await sink.text("first")
    await settled()
    slack.responses["chat.appendStream"] = rejected("ratelimited")
    with caplog.at_level("WARNING"):
        await sink.text(" the owner's secret content")
        await settled()
    assert "ratelimited" in caplog.text
    assert "secret content" not in caplog.text


async def test_a_refused_rewrite_never_replaces_a_message_that_shows_its_body(
    slack: FakeSlack,
) -> None:
    # A reply that ended inline already shows its whole body. Slack refusing a later rewrite of
    # it (the counts joining its footer, say) must leave that body as it is: no plain-text
    # fallback.
    sink = reply(slack)
    await sink.text("line of text\n" * 400)
    await sink.task(tool("t1", "Bash"))
    await sink.finish([])
    assert await sink.close_out("footer")
    slack.responses["chat.update"] = rejected("invalid_blocks")
    await sink.set_running("⏳ 1 shell")
    await settled()
    updates = slack.calls_to("chat.update")
    assert updates and all(u["blocks"] != [] for u in updates)
    tried = len(updates)
    await settled()
    assert len(slack.calls_to("chat.update")) == tried  # the change is dropped, not retried
    slack.responses["chat.update"] = {"ok": True}
    await sink.set_running("⏳ 2 shells")  # a later change is tried again
    await settled()
    assert len(slack.calls_to("chat.update")) == tried + 1


async def test_a_refused_continuation_post_is_posted_as_plain_text(slack: FakeSlack) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("start\n")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    slack.responses["chat.postMessage"] = [rejected("invalid_blocks")] * 2 + [
        {"ok": True, "ts": "9.9"}
    ]
    await sink.text("a line of text\n" * 1_000)
    await sink.finish([])
    await sink.close_out(None)
    plain = [p for p in slack.calls_to("chat.postMessage") if "blocks" not in p]
    assert plain and plain[0]["text"].startswith("a line of text")


async def test_a_refused_draft_update_is_not_retried_as_plain_text(slack: FakeSlack) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("partial")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    slack.responses["chat.update"] = rejected("invalid_blocks")
    await sink.text(" more")
    await settled()
    assert all(a.get("blocks") != [] for a in slack.calls_to("chat.update"))


async def test_a_rate_limited_update_is_never_turned_into_plain_text(slack: FakeSlack) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("one")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    await sink.text(" two")
    slack.responses["chat.update"] = rejected("ratelimited")
    await sink.finish([])
    await sink.close_out(None)
    assert all(a.get("blocks") != [] for a in slack.calls_to("chat.update"))


# Crash repair's bookkeeping: the message that a crash would leave unfinished.


async def test_the_open_reply_is_the_stream_until_the_end_lands(slack: FakeSlack) -> None:
    seen: list[tuple[str | None, str | None]] = []
    sink = reply(slack, on_open_reply=lambda old, new: seen.append((old, new)))
    await sink.text("x" * (sinks.MESSAGE_LIMIT + 10))  # two streams
    await settled()
    first, second = slack.stream_ts
    # the first is whole once the second starts (its cards are final): only the last stays open
    assert seen == [(None, first), (None, second), (first, None)]
    await sink.finish([])
    assert seen[-1] == (first, None)  # still open: the reply has not ended
    await sink.close_out("footer")
    assert seen[-1] == (second, None)


async def test_two_sinks_never_step_on_each_other_s_entry(slack: FakeSlack, tmp_path: Path) -> None:
    from code_with_slack.state import StateStore

    store = StateStore(tmp_path / "state.json")
    store.bind(CHANNEL, tmp_path)
    store.open_thread(CHANNEL, THREAD)

    def track(old: str | None, new: str | None) -> None:
        store.replace_open_reply(CHANNEL, THREAD, old, new)

    limiter = UpdateLimiter()
    a = reply(slack, limiter=limiter, on_open_reply=track)
    b = reply(slack, limiter=limiter, on_open_reply=track)
    await a.text("A")
    await a.settle()
    await b.text("B")
    await b.settle()
    assert store.thread(CHANNEL, THREAD).open_replies == tuple(slack.stream_ts)
    await a.finish([])
    await a.close_out(None)
    assert store.thread(CHANNEL, THREAD).open_replies == (slack.stream_ts[1],)


async def test_a_failed_end_keeps_the_reply_tracked_until_the_retry_lands(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 0.01)
    seen: list[tuple[str | None, str | None]] = []
    sink = reply(slack, on_open_reply=lambda old, new: seen.append((old, new)))
    await sink.text("Done.")
    await settled()
    ts = slack.stream_ts[0]
    slack.responses["chat.stopStream"] = [aiohttp.ClientConnectionError("down"), {"ok": True}]
    await sink.finish([])
    await sink.close_out(None)
    assert seen == [(None, ts)]  # the stream is still open: repair must still find it
    await sink.wait_landed()
    assert seen[-1] == (ts, None)


async def test_a_failed_tracking_write_never_orphans_the_stored_ts(
    slack: FakeSlack, tmp_path: Path
) -> None:
    from code_with_slack.state import StateStore

    store = StateStore(tmp_path / "state.json")
    store.bind(CHANNEL, tmp_path)
    store.open_thread(CHANNEL, THREAD)
    failing = False

    def track(old: str | None, new: str | None) -> None:
        if failing:
            raise RuntimeError("disk full")
        store.replace_open_reply(CHANNEL, THREAD, old, new)

    sink = reply(slack, on_open_reply=track)
    await sink.text("x")
    await settled()
    first = slack.stream_ts[0]
    failing = True
    await sink.text("y" * (sinks.MESSAGE_LIMIT + 10))
    await settled()
    assert store.thread(CHANNEL, THREAD).open_replies == (first,)
    failing = False
    await sink.finish([])
    await sink.close_out(None)
    assert store.thread(CHANNEL, THREAD).open_replies == ()


# Cards and previews.


async def test_a_card_says_what_the_tool_line_said(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.task(
        tool("a", "Agent", "in_progress", details="Read: x\nBash: ls", calls=3, task=True)
    )
    await sink.task(tool("b", "Bash", "error", output="exit 1"))
    await sink.task(tool("c", "Bash", "complete", output=STOPPED))
    await sink.task(tool("d", "Bash", "complete", output="fine"))  # a success shows no output
    await settled()
    assert slack.message_cards()[0] == [
        {
            "id": "a",
            "title": "Agent: a · 3 calls",
            "status": "in_progress",
            "details": "Read: x\nBash: ls",
        },
        # a call of a run of calls says why it failed in its title: its card is reused, and
        # Slack appends `output` to what a card already holds
        {"id": "fold:b", "title": "Bash: b · exit 1", "status": "error"},
        {"id": "c", "title": "Bash: c", "status": "complete", "output": STOPPED},
        {"id": "fold:d", "title": "Bash: d", "status": "complete"},
    ]


async def test_an_edit_that_ended_well_is_one_container_with_no_card(slack: FakeSlack) -> None:
    # Issue #136: the call's line is the container's title, its sentence the subtitle.
    sink = reply(slack)
    view = Preview("Update(a.txt)", "Added 1 line", "+\U0001f7e9 1 x", "diff")
    await sink.task(tool("e", "Edit", preview=view))
    await settled()
    [start] = slack.calls_to("chat.startStream")
    [blocks] = start["chunks"]
    assert blocks["type"] == "blocks"
    [container] = blocks["blocks"]
    assert container["type"] == "container" and container["is_collapsible"] is True
    assert container["title"] == {"type": "plain_text", "text": "Update(a.txt)"}
    assert container["subtitle"] == {"type": "plain_text", "text": "Added 1 line"}
    # The call's line in code style, as a tool line shows it; the plain title is the fallback.
    [section] = container["rich_text_title"]["elements"]
    assert section["elements"] == [
        {"type": "text", "text": "Update(a.txt)", "style": {"code": True}}
    ]


async def test_a_new_file_that_was_written_is_one_container_with_no_card(slack: FakeSlack) -> None:
    sink = reply(slack)
    view = Preview("Write(new.txt)", "Wrote 2 lines to new.txt", "1 alpha\n2 beta")
    await sink.task(tool("w", "Write", preview=view))
    await settled()
    [start] = slack.calls_to("chat.startStream")
    [blocks] = start["chunks"]
    [container] = blocks["blocks"]
    assert container["title"]["text"] == "Write(new.txt)"
    assert container["subtitle"]["text"] == "Wrote 2 lines to new.txt"
    assert container["default_collapsed"] is True
    [pre] = container["child_blocks"][0]["elements"]
    assert "language" not in pre  # no highlighting asked for lines that are no diff
    assert sinks.block_text(container) == "1 alpha\n2 beta"


async def test_a_diff_under_a_card_that_already_showed_keeps_the_sentence_as_title(
    slack: FakeSlack,
) -> None:
    # A stream cannot take a card back: a call that showed one keeps it, and its container
    # does not repeat the call's line (issue #110).
    sink = reply(slack)
    view = Preview("Update(a.txt)", "Added 1 line", "+x", "diff")
    await sink.task(tool("e", "Edit", "in_progress"))
    await settled()
    await sink.task(tool("e", "Edit", preview=view))
    await settled()
    [append] = slack.calls_to("chat.appendStream")
    card, blocks = append["chunks"]
    assert card["type"] == "task_update" and card["title"] == "Update(a.txt)"
    [container] = blocks["blocks"]
    assert container["title"]["text"] == "Added 1 line" and "subtitle" not in container
    assert "rich_text_title" not in container


async def test_a_preview_with_no_lines_keeps_its_card(slack: FakeSlack) -> None:
    # An empty new file has nothing to put in a container: the card says the sentence.
    sink = reply(slack)
    await sink.task(
        tool("w", "Write", preview=Preview("Write(e.txt)", "Wrote 0 lines to e.txt", ""))
    )
    await settled()
    [start] = slack.calls_to("chat.startStream")
    [card] = start["chunks"]
    assert card["type"] == "task_update" and card["output"] == "Wrote 0 lines to e.txt"


async def test_a_new_file_preview_follows_its_card_as_a_code_block(slack: FakeSlack) -> None:
    sink = reply(slack)
    view = Preview("Write(new.txt)", "Wrote 2 lines to new.txt", "1 alpha\n2 beta")
    await sink.task(tool("w", "Write", "in_progress"))
    await settled()
    await sink.task(tool("w", "Write", preview=view))
    await settled()
    [append] = slack.calls_to("chat.appendStream")
    assert [c["type"] for c in append["chunks"]] == ["task_update", "blocks"]
    # a blocks chunk holding a markdown block: measured accepted 2026-09-29
    assert append["chunks"][1] == {
        "type": "blocks",
        "blocks": [{"type": "markdown", "text": "```\n1 alpha\n2 beta\n```"}],
    }


async def test_a_preview_is_sent_once_however_often_the_card_changes(slack: FakeSlack) -> None:
    sink = reply(slack)
    view = Preview("Update(a.txt)", "Added 1 line", "+x", "diff")
    await sink.task(tool("e", "Edit", preview=view))
    await settled()
    await sink.task(tool("e", "Edit", preview=view, output="again"))
    await sink.text("after")
    await settled()
    chunks = [c for _, a in slack.calls if "chunks" in a for c in a["chunks"]]
    assert sum(c["type"] == "blocks" for c in chunks) == 1


async def test_a_failed_call_shows_its_error_even_if_it_carries_a_preview(slack: FakeSlack) -> None:
    sink = reply(slack)
    view = Preview("Update(a.txt)", "Added 1 line", "+x", "diff")
    await sink.task(tool("e", "Edit", status="error", output="File not found", preview=view))
    await settled()
    [start] = slack.calls_to("chat.startStream")
    assert start["chunks"] == [
        {
            "type": "task_update",
            "id": "fold:e",
            "title": "Edit: e · File not found",
            "status": "error",
        }
    ]


async def test_an_edit_and_a_write_show_as_the_terminal_shows_them(slack: FakeSlack) -> None:
    # edit-write.jsonl (CLI 2.1.286): Write a new file, Read, a failed Edit, an Edit, a Write over
    # the file. The terminal showed each Edit and Write whole, with its sentence and its lines.
    sink = reply(slack)
    renderer = TurnRenderer(sink, "/home/dev/project")
    for message in sdk_messages("edit-write"):
        await renderer.feed(message)
    await renderer.close(None)
    chunks = [c for _, a in slack.calls if "chunks" in a for c in a["chunks"]]
    # Each one that ended well is a container alone; no card ever named it (issue #136).
    cards = [c for c in chunks if c["type"] == "task_update"]
    assert not [c["title"] for c in cards if c["title"].startswith(("Write", "Update("))]
    assert [(c["title"], c["status"]) for c in cards if c["title"].startswith("Edit")] == [
        (
            "Edit: /home/dev/project/notes.txt · "
            "<tool_use_error>String to replace not found in file.",
            "error",
        )
    ]
    shown = [b for c in chunks if c["type"] == "blocks" for b in c["blocks"]]
    assert [b["type"] for b in shown] == ["container"] * 3
    assert [(b["title"]["text"], b["subtitle"]["text"]) for b in shown] == [
        ("Write(new.txt)", "Wrote 15 lines to new.txt"),
        ("Update(notes.txt)", "Added 1 line, removed 1 line"),
        ("Write(notes.txt)", "Added 2 lines, removed 3 lines"),
    ]
    new_file, *diffs = shown
    assert [sinks.block_text(d) for d in diffs] == [
        "    1 alpha\n-\U0001f7e5 2 beta\n+\U0001f7e9 2 gamma\n    3 delta",
        "-\U0001f7e5 1 alpha\n-\U0001f7e5 2 gamma\n-\U0001f7e5 3 delta\n"
        "+\U0001f7e9 1 one\n+\U0001f7e9 2 two",
    ]
    lines = sinks.block_text(new_file).splitlines()
    assert lines[:2] == [" 1 1", " 2 2"] and lines[-1] == "… +5 lines"


async def test_a_stopped_message_shows_its_previews_as_blocks(slack: FakeSlack) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    diff = Preview("Update(a.txt)", "Added 1 line", "+x", "diff")
    new = Preview("Write(b.txt)", "Wrote 1 line to b.txt", "1 hi")
    await sink.task(tool("e", "Edit", preview=diff))
    await sink.task(tool("w", "Write", preview=new))
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    await sink.text("more")
    await settled()
    blocks = slack.calls_to("chat.update")[-1]["blocks"]
    assert [b["type"] for b in blocks] == ["container", "container", "markdown"]
    assert [b["title"]["text"] for b in blocks[:2]] == ["Update(a.txt)", "Write(b.txt)"]
    assert sinks.block_text(blocks[1]) == "1 hi"


async def test_an_answered_question_shows_in_the_reply_where_it_was_answered(
    slack: FakeSlack,
) -> None:
    # ask-answered.jsonl (CLI 2.1.286). The terminal keeps `User answered Claude's questions:`
    # and a line per answer where the question was asked, before what Claude says next.
    sink = reply(slack)
    renderer = TurnRenderer(sink, "/home/dev/project")
    call: ToolUseBlock | None = None
    for message in sdk_messages("ask-answered"):
        if isinstance(message, AssistantMessage):
            call = next((b for b in message.content if isinstance(b, ToolUseBlock)), call)
        if isinstance(message, UserMessage) and call is not None:
            # The session hands the answers over once the owner gave them, before the result.
            assert isinstance(message.tool_use_result, dict)
            assert renderer.answered(
                call.id, call.input["questions"], message.tool_use_result["answers"]
            )
        await renderer.feed(message)
    await renderer.close(None)
    [card] = slack.message_cards()[0]
    assert (card["title"], card["status"]) == ("User answered Claude's questions:", "complete")
    assert "output" not in card and "details" not in card
    chunks = [c for _, a in slack.calls if "chunks" in a for c in a["chunks"]]
    kinds = [c["type"] for c in chunks]
    [answers] = [c for c in chunks if c["type"] == "blocks"]
    assert answers["blocks"] == [
        sinks.context_block(
            f"{texts.NESTED}· Which color do you prefer? → Blue\n"
            f"{texts.NESTED}· Do you also like green? → Yes, Only in spring"
        )
    ]
    # Claude's next words come after the answers, in the same message.
    assert "markdown_text" in kinds[kinds.index("blocks") + 1 :]


async def test_answers_to_a_call_the_reply_has_no_line_for_are_not_kept(slack: FakeSlack) -> None:
    renderer = TurnRenderer(reply(slack))
    assert not renderer.answered("toolu_unseen", [{"question": "Colour?"}], {"Colour?": "blue"})


async def test_a_stopped_message_shows_the_answers_under_their_card(slack: FakeSlack) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    answered = Preview("User answered Claude's questions:", "", "· Colour? → <b> `x`", plain=True)
    await sink.task(tool("q", "AskUserQuestion", preview=answered))
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    await sink.text("more")
    await settled()
    blocks = slack.calls_to("chat.update")[-1]["blocks"]
    assert [b["type"] for b in blocks] == ["task_card", "context", "markdown"]
    assert "output" not in blocks[0]
    # Shown as written: no markup of the question's or the answer's is read as Slack's.
    assert (
        blocks[1]["elements"][0]["text"] == f"{texts.NESTED}· Colour? → &lt;b&gt; `\u200bx`\u200b"
    )


async def test_answers_longer_than_a_context_block_are_cut(slack: FakeSlack) -> None:
    sink = reply(slack)
    body = "\n".join(f"· {'q' * 900} → {'a' * 300}" for _ in range(4))
    await sink.task(
        tool("q", "AskUserQuestion", preview=Preview("User answered", "", body, plain=True))
    )
    await settled()
    [answers] = [
        c for _, a in slack.calls if "chunks" in a for c in a["chunks"] if c["type"] == "blocks"
    ]
    [block] = answers["blocks"]
    text = block["elements"][0]["text"]
    assert len(text) == sinks.CONTEXT_LIMIT and text.endswith("…")


async def test_answers_longer_than_a_message_still_show_and_the_reply_goes_on(
    slack: FakeSlack,
) -> None:
    # Counted for the 3,000 characters it shows, never for its whole body: a body past
    # MESSAGE_LIMIT would fit no message, and the reply would stop there.
    sink = reply(slack)
    body = "· " + "q" * (sinks.MESSAGE_LIMIT + 4_000) + " → yes"
    await sink.task(
        tool("q", "AskUserQuestion", preview=Preview("User answered", "", body, plain=True))
    )
    await sink.text("And then.")
    await settled()
    chunks = [c for _, a in slack.calls if "chunks" in a for c in a["chunks"]]
    assert [c["type"] for c in chunks] == ["task_update", "blocks", "markdown_text"]
    assert len(slack.created_ts) == 1


async def test_a_subagent_card_counts_its_calls_from_a_recorded_turn(slack: FakeSlack) -> None:
    # subagent-foreground.jsonl (CLI 2.1.286): the agent's task ends before the Agent call's result.
    sink = reply(slack)
    renderer = TurnRenderer(sink)
    for message in sdk_messages("subagent-foreground"):
        await renderer.feed(message)
    await renderer.close(None)
    [card] = [c for c in slack.message_cards()[0] if c["title"].startswith("Agent: ")]
    assert card["title"].endswith(" · 1 call") and card["status"] == "complete"


# The limiter: appends and updates spend its budget, a start, a stop and a post do not.


class CountingLimiter:
    def __init__(self) -> None:
        self.acquired = 0
        self.refunded = 0

    async def acquire(self) -> None:
        self.acquired += 1

    async def refund(self) -> None:
        self.refunded += 1


async def test_appends_and_updates_spend_the_budget_and_starts_stops_and_posts_do_not(
    slack: FakeSlack,
) -> None:
    limiter = CountingLimiter()
    clock = FakeClock()
    sink = reply(slack, limiter=limiter, clock=clock)
    await sink.text("one")
    await settled()  # a start
    await sink.text(" two")
    await settled()  # an append
    assert limiter.acquired == 1
    await clock.advance(sinks.STREAM_SECONDS + 1)  # a stop, then an update
    await sink.text(" three")
    await settled()
    assert limiter.acquired == 2
    await sink.finish([])
    await sink.close_out("footer")  # a post
    assert limiter.acquired == 2 and limiter.refunded == 0


class StuckLimiter:
    """`acquire` blocks until `release`, as a busy shared limiter does while another reply
    spends its only token."""

    def __init__(self) -> None:
        self.acquired = 0
        self.refunded = 0
        self._gate = asyncio.Event()

    async def acquire(self) -> None:
        self.acquired += 1
        await self._gate.wait()

    async def refund(self) -> None:
        self.refunded += 1

    def release(self) -> None:
        self._gate.set()


async def test_a_task_update_never_blocks_the_caller(slack: FakeSlack) -> None:
    # `task` runs in the SDK reader loop: a limiter that makes a write wait must not hold it.
    limiter = StuckLimiter()
    sink = reply(slack, limiter=limiter)
    await sink.text("one")
    await settled()
    await asyncio.wait_for(sink.text(" two"), 0.05)
    await asyncio.wait_for(sink.task(tool("t1", "Bash", "in_progress")), 0.05)
    await settled()
    assert limiter.acquired == 1  # the append is waiting its turn, and the caller is not
    limiter.release()
    await settled()


async def test_a_change_during_a_limiter_wait_is_not_dropped(slack: FakeSlack) -> None:
    limiter = UpdateLimiter(limit=1, window=0.5, burst=1)
    sink = reply(slack, limiter=limiter)
    await sink.text("one")
    await settled()
    await sink.text(" two")  # spends the only token; the next append waits for a refill
    await asyncio.sleep(0.05)
    await sink.text(" three")
    await sink.text(" four")
    await asyncio.sleep(1.3)
    assert slack.message_texts() == ["one two three four"]


async def test_the_debounce_holds_while_streaming_through_a_slow_round_trip(
    slack: FakeSlack,
) -> None:
    slack.delay = 0.05
    sink = reply(slack)
    stamps: list[float] = []
    original = slack.api_call

    async def timed(api_method: str, **kwargs: Any) -> Any:
        if api_method == "chat.appendStream":
            stamps.append(time.monotonic())
        return await original(api_method, **kwargs)

    slack.api_call = timed  # type: ignore[method-assign]
    await sink.text("go")
    await asyncio.sleep(0.2)
    for i in range(20):
        await sink.text(f" {i}")
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.4)
    assert len(stamps) >= 2
    gaps = [b - a for a, b in itertools.pairwise(stamps)]
    assert min(gaps) >= sinks.DEBOUNCE_SECONDS - 0.005


async def test_two_busy_sinks_share_one_limiter_and_each_reaches_its_end(
    slack: FakeSlack,
) -> None:
    limit, window, burst = 9, 0.6, 1
    limiter = UpdateLimiter(limit=limit, window=window, burst=burst)
    a, b = reply(slack, limiter=limiter), reply(slack, limiter=limiter)
    stamps: list[float] = []
    original = slack.api_call

    async def timed(api_method: str, **kwargs: Any) -> Any:
        if api_method == "chat.appendStream":
            stamps.append(time.monotonic())
        return await original(api_method, **kwargs)

    slack.api_call = timed  # type: ignore[method-assign]
    for i in range(6):
        await a.text(f"a{i} ")
        await b.text(f"b{i} ")
        await asyncio.sleep(0.03)
    await a.finish([])
    await a.close_out("footer-a")
    await b.finish([])
    await b.close_out("footer-b")
    await asyncio.sleep(1.5)
    for i, start in enumerate(stamps):
        assert sum(1 for t in stamps[i:] if t < start + window) <= limit + burst
    assert slack.message_texts() == ["a0 a1 a2 a3 a4 a5", "b0 b1 b2 b3 b4 b5"]
    assert slack.pushes() == 2


# Helpers that build the blocks.


def test_a_notice_fits_one_context_element() -> None:
    assert sinks.notice_text("short") == "short"
    cut = sinks.notice_text("x" * (sinks.CONTEXT_LIMIT + 10))
    assert len(cut) == sinks.CONTEXT_LIMIT and cut.endswith("…")


def test_a_banner_is_plain_and_escaped() -> None:
    text = "## Title\n\n- **bold** and `code` with [a link](https://x.example) & <tags>"
    assert sinks.banner_text(text) == "Title\nbold and code with a link &amp; &lt;tags&gt;"


@pytest.mark.parametrize(
    ("text", "plain"),
    [
        ("see [1] and [a [b](u) then [c](v", "see [1] and a [b then [c](v"),
        ("intro\n \n\t\n- item\n  \nnot a marker", "intro\nitem\n  \nnot a marker"),
        ("snake_case a_`_`_b _lead_ trail_ *x* 2*3", "snake_case a___b lead trail x 2*3"),
    ],
)
def test_stripping_markdown_keeps_what_only_looks_like_a_marker(text: str, plain: str) -> None:
    # The texts a pattern reads and puts back. Each result is the one the patterns gave before
    # they were made linear (taken from them, 2026-10-04).
    assert sinks.strip_markdown(text) == plain


@pytest.mark.parametrize(
    ("paragraph", "banner"),
    [
        ("[" * 100_000, "[" * sinks.BANNER_LIMIT),
        ("x" + "\n " * 50_000 + "y", ("x" + "\n " * 150)[: sinks.BANNER_LIMIT]),
        ("a" + "_`" * 50_000 + "b", "a" + "_" * (sinks.BANNER_LIMIT - 1)),
    ],
    ids=["brackets", "blank lines", "underscores"],
)
def test_a_banner_takes_a_time_linear_in_its_paragraph(paragraph: str, banner: str) -> None:
    # About 4 ms of CPU each. Patterns that start again from every `[`, blank line or `_` took
    # 15, 49 and 11 seconds on these, with the event loop held (measured 2026-10-04, Python 3.12).
    started = time.process_time()
    assert sinks.banner_text(paragraph, limit=sinks.BANNER_LIMIT) == banner
    assert time.process_time() - started < 1


def test_a_task_card_block_reads_back_as_the_card_it_was() -> None:
    update = tool("t1", "Bash", "error", output="exit 1", details="ignored while it ended")
    block = sinks.card_block(update)
    assert block["type"] == "task_card" and block["task_id"] == "t1"
    assert block["status"] == "error" and block["title"] == "Bash: t1"
    assert block["output"]["type"] == "rich_text" and "details" not in block


def test_a_diff_shows_collapsed_and_full_width() -> None:
    [block] = sinks.preview_containers("Added 1 line", "+\U0001f7e9 1 x")
    assert block["is_collapsible"] is True and block["default_collapsed"] is True
    assert block["width"] == "full"
    [child] = block["child_blocks"]
    [pre] = child["elements"]
    assert pre["type"] == "rich_text_preformatted" and pre["language"] == "diff"


def test_a_diff_is_titled_with_its_sentence_alone() -> None:
    # The call's line is the card above it (issue #110): the container must not say it again.
    [block] = sinks.preview_containers("Added 1 line", "+x")
    assert block["title"] == {"type": "plain_text", "text": "Added 1 line"}
    assert "rich_text_title" not in block and "subtitle" not in block


def test_a_container_with_no_card_says_the_sentence_under_the_calls_line() -> None:
    [block] = sinks.preview_containers("Update(a.txt)", "+x", subtitle="s" * 200)
    assert block["title"]["text"] == "Update(a.txt)"
    assert block["subtitle"] == {"type": "plain_text", "text": "s" * 150}


def test_a_diff_past_a_message_continues_in_the_next() -> None:
    body = "\n".join(f"+\U0001f7e9 {i} {'x' * 90}" for i in range(1, 400))
    blocks = sinks.preview_containers("Added 399 lines", body)
    assert len(blocks) > 1
    assert all(len(sinks.block_text(b)) <= sinks.MESSAGE_LIMIT for b in blocks)
    assert "\n".join(sinks.block_text(b) for b in blocks) == body  # nothing lost at the cuts


def test_a_long_title_is_cut_to_slacks_limit() -> None:
    [block] = sinks.preview_containers("x" * 200, "+x", as_code=True)
    assert len(block["title"]["text"]) == 150
    [section] = block["rich_text_title"]["elements"]
    assert len(section["elements"][0]["text"]) == 150


@pytest.mark.parametrize("fence", ["```", "````", "``````", "```x```"])
def test_a_fence_inside_a_preview_does_not_close_its_block(fence: str) -> None:
    # Any run of three or more backticks would close the block (a Markdown file's ```` fence).
    [block] = sinks.preview_blocks(f"a\n{fence}\nb")
    assert block["text"].count("```") == 2


async def test_update_limiter_never_exceeds_the_budget_in_any_window() -> None:
    limit, window, burst = 4, 0.2, 1
    limiter = UpdateLimiter(limit=limit, window=window, burst=burst)
    times: list[float] = []
    for _ in range(limit * 3):
        await limiter.acquire()
        times.append(time.monotonic())
    # a token bucket may spend its whole burst at once; past that, the standard bound holds:
    # tokens spent in any span <= burst + rate * span, i.e. at most limit+burst per window.
    for i in range(len(times) - (limit + burst)):
        assert times[i + limit + burst] - times[i] >= window - 0.03


async def test_update_limiter_paces_evenly_after_its_burst(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even pacing, not a sliding window: past the burst, one token every window/limit seconds,
    never every reply racing through the whole budget and then freezing together. An injected
    clock, advanced by exactly what the limiter itself sleeps for, so the gaps are exact and this
    cannot flake on a loaded runner."""
    limit, window, burst = 6, 0.6, 2
    fake_time = [0.0]

    async def fake_sleep(seconds: float) -> None:
        fake_time[0] += seconds

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    limiter = UpdateLimiter(limit=limit, window=window, burst=burst, clock=lambda: fake_time[0])
    gaps: list[float] = []
    last: float | None = None
    for _ in range(limit):
        await limiter.acquire()
        if last is not None:
            gaps.append(fake_time[0] - last)
        last = fake_time[0]

    steady = gaps[burst - 1 :]  # the gaps once the burst is spent
    interval = window / limit
    assert all(g == pytest.approx(interval) for g in steady)


async def test_update_limiter_serves_waiters_in_arrival_order() -> None:
    limiter = UpdateLimiter(limit=1, window=0.1, burst=1)
    order: list[int] = []

    async def take(n: int) -> None:
        await limiter.acquire()
        order.append(n)

    await asyncio.gather(take(1), take(2), take(3))
    assert order == [1, 2, 3]


async def test_update_limiter_releases_its_place_when_a_waiter_is_cancelled() -> None:
    limiter = UpdateLimiter(limit=1, window=0.2, burst=1)
    await limiter.acquire()  # spends the only token
    waiter = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0.05)  # the waiter is now sleeping, holding the limiter's own lock
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    # a cancelled waiter must not keep the lock: the next acquire is still served promptly.
    await asyncio.wait_for(limiter.acquire(), timeout=0.5)


async def test_update_limiter_refund_makes_a_token_available_at_once() -> None:
    limiter = UpdateLimiter(limit=1, window=10.0, burst=1)  # too slow to refill on its own
    await limiter.acquire()  # spends the only token
    await limiter.refund()
    await asyncio.wait_for(limiter.acquire(), timeout=0.05)  # available again, not after `window`


async def test_update_limiter_refund_never_exceeds_burst() -> None:
    limiter = UpdateLimiter(limit=1, window=10.0, burst=2)
    await limiter.refund()
    await limiter.refund()
    await limiter.refund()  # never more than a full burst, whatever was actually spent
    assert limiter._tokens == 2


# An append Slack refuses for its content (`msg_too_long` on `chat.appendStream`, measured
# 2026-10-01, slack-sdk 3.44.1): the same append would be refused again.


async def test_an_append_refused_for_its_content_stops_the_stream_and_goes_on_by_update(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.text("Hello. ")
    await settled()
    slack.responses["chat.appendStream"] = rejected("msg_too_long")
    await sink.text("World.")
    await settled()
    # stopped at once, and written from the model: nothing waits for the next change
    assert slack.messages[slack.stream_ts[0]].streaming is False
    assert slack.stream_texts() == ["Hello. World."]
    await sink.text(" Again.")
    await settled()
    assert len(slack.calls_to("chat.appendStream")) == 1  # the refused append is never repeated
    assert slack.stream_texts() == ["Hello. World. Again."]
    assert slack.posted_ts == []


async def test_a_final_append_refused_for_its_content_still_ends_the_reply(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.text("Hello. ")
    await settled()
    slack.responses["chat.appendStream"] = rejected("msg_too_long")
    await sink.text("Done.")
    await sink.finish([])
    assert await sink.close_out("footer") is True  # landed in this pass: no retry is owed
    assert slack.stream_texts() == ["Hello. Done."]
    # as a reply past STREAM_SECONDS ends: the bare stop, then the closing message
    assert slack.calls_to("chat.stopStream")[0].get("blocks") is None
    [closing] = slack.posted_ts
    assert slack.messages[closing].blocks[-1]["elements"][0]["text"] == "footer"
    assert slack.pushes() == 2


async def test_an_update_refused_after_a_refused_append_is_an_end_that_did_not_land(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The message shows less than the model and no later write can fix it: never a checkmark.
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 0.01)
    sink = reply(slack)
    await sink.text("Hello. ")
    await settled()
    slack.responses["chat.appendStream"] = rejected("msg_too_long")
    slack.responses["chat.update"] = rejected("msg_too_long")
    await sink.text("Done.")
    await sink.finish([])
    assert await sink.close_out("footer") is False
    assert await sink.wait_landed() is False
    assert slack.stream_texts() == ["Hello."]  # what the stream held when it was stopped
    assert len(slack.posted_ts) == 1  # the closing message, posted once: the retry adds none


async def test_a_later_update_that_passes_lands_the_reply_after_a_refused_one(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.text("Hello. ")
    await settled()
    slack.responses["chat.appendStream"] = rejected("msg_too_long")
    slack.responses["chat.update"] = [rejected("msg_too_long"), {"ok": True}]
    await sink.text("World.")
    await settled()
    assert slack.stream_texts() == ["Hello."]  # the update was refused too: the change is dropped
    await sink.text(" Done.")
    await sink.finish([])
    assert await sink.close_out("footer") is True
    assert slack.stream_texts() == ["Hello. World. Done."]


async def test_an_append_refused_for_another_reason_stays_a_failed_write(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Not measured on `chat.appendStream`, and an update of the same content may be refused
    # too, which would drop it: the end is reported lost, so the session shows the cross.
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 0.01)
    sink = reply(slack)
    await sink.text("Hello. ")
    await settled()
    slack.responses["chat.appendStream"] = rejected("invalid_blocks")
    await sink.text("Done.")
    await sink.finish([])
    assert await sink.close_out("footer") is False
    assert await sink.wait_landed() is False
    assert slack.messages[slack.stream_ts[0]].streaming is True
    assert slack.calls_to("chat.stopStream") == [] and slack.calls_to("chat.update") == []


async def test_a_refused_append_logs_the_method_and_the_sizes_without_content(
    slack: FakeSlack, caplog: pytest.LogCaptureFixture
) -> None:
    sink = reply(slack)
    await sink.text("Hello. ")
    await sink.task(tool("t1", "Bash", "in_progress"))
    await settled()
    slack.responses["chat.appendStream"] = rejected("msg_too_long")
    await sink.text("World.")
    await settled()
    [line] = [r.getMessage() for r in caplog.records if "chat.appendStream refused" in r.message]
    assert "msg_too_long" in line
    # what the message would hold: both texts, three elements (text, card, text), one card,
    # whose title was sent before and is not in the refused append
    assert "text 13, elements 3, cards 1" in line
    assert f"card text sent {len('Bash: t1')} and 0 more" in line
    assert "Bash" not in caplog.text and "Hello" not in caplog.text


# A call whose outcome is unknown: Slack may have applied it. Stream calls are not idempotent.


async def test_an_append_of_unknown_outcome_is_never_sent_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slack = ResetAfterApply()
    sink = reply(slack)
    await sink.text("Hello. ")
    await settled()
    slack.reset_next = "chat.appendStream"
    await sink.text("World. ")
    await settled()
    await sink.text("Again.")
    await settled()
    # the message went on by update, from the model: each word once
    assert len(slack.calls_to("chat.appendStream")) == 1
    assert slack.stream_texts()[0].count("World.") == 1
    assert slack.stream_texts()[0] == "Hello. World. Again."
    assert slack.messages[slack.stream_ts[0]].streaming is False


async def test_a_stop_of_unknown_outcome_that_carried_the_footer_ends_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 0.05)
    slack = ResetAfterApply()
    sink = reply(slack)
    await sink.text("Answer.")
    await settled()
    slack.reset_next = "chat.stopStream"
    await sink.finish([])
    assert await sink.close_out("footer") is False
    assert await sink.wait_landed() is True
    await asyncio.sleep(0.1)
    assert slack.pushes() == 1 and slack.posted_ts == []  # no closing message, footer once
    assert slack.messages[slack.stream_ts[0]].blocks[-1]["elements"][0]["text"] == "footer"


async def test_a_start_of_unknown_outcome_says_so_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    slack = ResetAfterApply()
    slack.reset_next = "chat.startStream"
    sink = reply(slack)
    with caplog.at_level("WARNING"):
        await sink.text("one")
        await settled()
    assert "outcome unknown" in caplog.text


def test_the_connection_retry_skips_the_calls_that_create_a_message() -> None:
    import asyncio as aio

    from slack_sdk.http_retry.request import HttpRequest
    from slack_sdk.http_retry.state import RetryState

    handler = sinks.ConnectionRetryUnlessCreating()
    error = aiohttp.ClientOSError(104, "Connection reset by peer")

    async def can(method: str) -> bool:
        request = HttpRequest(
            method="POST",
            url=f"https://slack.com/api/{method}",
            headers={},
            body_params={},
            data={},
        )
        return await handler.can_retry_async(state=RetryState(), request=request, error=error)

    assert aio.run(can("chat.update")) is True and aio.run(can("reactions.add")) is True
    for creating in (
        "chat.startStream",
        "chat.appendStream",
        "chat.stopStream",
        "chat.postMessage",
    ):
        assert aio.run(can(creating)) is False


async def test_settle_during_the_retry_does_not_lose_the_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 0.01)
    slack = SlowAfterApply()
    sink = reply(slack)
    await sink.text("Answer.")
    await settled()
    slack.responses["chat.stopStream"] = [rejected("ratelimited"), {"ok": True}]
    await sink.finish([])
    assert await sink.close_out("footer") is False
    # the retry's stop is applied and its answer still on the way when the shutdown settles
    slack.slow_method, slack.slow_for = "chat.stopStream", 0.1
    await asyncio.sleep(0.03)
    assert await sink.settle() is True
    assert slack.pushes() == 1 and slack.posted_ts == []


async def test_a_message_stays_tracked_while_its_cards_run_after_a_roll_over(
    slack: FakeSlack,
) -> None:
    seen: list[tuple[str | None, str | None]] = []
    sink = reply(slack, on_open_reply=lambda old, new: seen.append((old, new)))
    await sink.task(tool("t0", "Bash", "in_progress", task=True, details="Running in background"))
    for i in range(1, 60):
        await sink.task(tool(f"t{i}", "Agent", task=True))
    await settled()
    first, second = slack.stream_ts
    assert (None, second) in seen and (first, None) not in seen  # a card still runs in the first
    await sink.task(tool("t0", "Bash", "complete", task=True))
    await settled()
    assert (first, None) in seen  # final now: nothing left for a repair to close


async def test_a_reply_whose_body_landed_is_not_tracked_when_only_the_closing_post_fails(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 60.0)
    clock = FakeClock()
    seen: list[tuple[str | None, str | None]] = []
    sink = reply(slack, clock=clock, on_open_reply=lambda old, new: seen.append((old, new)))
    await sink.text("A complete answer.")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    slack.responses["chat.postMessage"] = aiohttp.ClientConnectionError("down")
    await sink.finish([])
    assert await sink.close_out("footer") is False
    # the answer is whole: a repair must not say it stopped before it
    assert seen[-1] == (slack.stream_ts[0], None)
    await sink.settle()


async def test_a_footerless_stop_of_unknown_outcome_does_not_stand_for_the_footer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The 280 s stop lands and its answer is lost. The stream is over, without the footer: the
    # end must post the closing message, not count the earlier stop as its own.
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 0.05)
    slack = ResetAfterApply()
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("Answer.")
    await settled()
    slack.reset_next = "chat.stopStream"
    await clock.advance(sinks.STREAM_SECONDS + 1)
    await settled()
    await sink.finish([])
    assert await sink.close_out("footer") is True
    [closing] = slack.calls_to("chat.postMessage")
    assert closing["blocks"][-1]["elements"][0]["text"] == "footer"
    assert slack.pushes() == 2  # the 280 s stop, and the end


# Adopting what a create of unknown outcome made, before writing again.


async def test_a_start_of_unknown_outcome_is_adopted_not_started_again() -> None:
    slack = ResetAfterApply()
    seen: list[tuple[str | None, str | None]] = []
    slack.reset_next = "chat.startStream"
    sink = reply(slack, on_open_reply=lambda old, new: seen.append((old, new)))
    await sink.text("one")
    await settled()
    await sink.text(" two")
    await settled()
    assert len(slack.calls_to("chat.startStream")) == 1  # never started again
    assert slack.stream_texts() == ["one two"]  # the adopted stream took the rest
    assert seen == [(None, slack.stream_ts[0])]  # and a repair can find it
    await sink.finish([])
    await sink.close_out("footer")
    assert slack.pushes() == 1


async def test_a_start_that_never_landed_is_started_again() -> None:
    slack = FakeSlack()
    slack.responses["chat.startStream"] = [
        aiohttp.ClientConnectionError("down"),
        slack.responses["chat.startStream"],
    ]
    sink = reply(slack)
    await sink.text("one")
    await settled()
    await sink.text(" two")
    await settled()
    assert slack.stream_texts() == ["one two"] and len(slack.calls_to("chat.startStream")) == 2


async def test_a_continuation_post_of_unknown_outcome_is_adopted() -> None:
    slack = ResetAfterApply()
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("start\n")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    slack.reset_next = "chat.postMessage"
    await sink.text("a line of text\n" * 1_000)
    await settled()
    await sink.finish([])
    await sink.close_out("footer")
    posts = slack.calls_to("chat.postMessage")
    assert len(posts) == 2  # the continuation once, then the closing message
    assert len(slack.posted_ts) == 2


async def test_a_closing_post_of_unknown_outcome_is_adopted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 0.02)
    slack = ResetAfterApply()
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("A complete answer.")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    slack.reset_next = "chat.postMessage"
    await sink.finish([])
    assert await sink.close_out("footer") is True  # read back, found: nothing to retry
    assert len(slack.posted_ts) == 1 and slack.pushes() == 2  # one closing message, not two


# A stop that failed on the connection: landed, or expired at Slack's 5 minutes?


async def test_a_stop_of_unknown_outcome_past_the_streams_life_is_taken_as_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 60.0)
    slack = ResetAfterApply()
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("Answer.")
    await settled()
    slack.reset_next = "chat.stopStream"
    await sink.finish([])
    assert await sink.close_out("footer") is False  # the stop landed, its answer was lost
    clock.now += sinks.STREAM_LIFE + 10  # what the retry finds is Slack's own end, or ours
    assert await sink.settle() is True
    # past the stream's life it cannot be told from an expiry: the footer gets its own message
    assert len(slack.posted_ts) == 1


# What a message holds: a preview that arrives late never passes the limit.


async def test_a_late_preview_that_does_not_fit_its_message_is_cut_with_a_pointer(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.task(tool("t0", "Write", "in_progress"))
    for i in range(1, 60):
        await sink.task(tool(f"t{i}", "Agent", task=True))
    await settled()
    first = slack.stream_ts[0]
    body = "\n".join(f"{i:>4} {'x' * 90}" for i in range(100))  # about 9,000 characters
    view = Preview("Write(big.txt)", "Wrote 100 lines to big.txt", body)
    await sink.task(tool("t0", "Write", preview=view))
    await settled()
    updates = [u for u in slack.calls_to("chat.update") if u["ts"] == first]
    assert updates
    for update in slack.calls_to("chat.update"):
        assert len(update["blocks"]) <= 50
        assert sum(len(sinks.block_text(b)) for b in update["blocks"]) <= 12_000
    blocks = updates[-1]["blocks"]
    assert next(b for b in blocks if b["type"] == "task_card")["status"] == "complete"
    assert sinks.context_block(sinks.PREVIEW_CUT) in blocks


# The sink's own cost: a message whose span did not change is not rendered again.


async def test_a_message_whose_span_did_not_change_is_not_rendered_again(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = reply(slack)
    view = Preview("Write(a.txt)", "Wrote 1 line to a.txt", "1 hi")
    await sink.task(tool("w", "Write", preview=view))
    for i in range(60):
        await sink.task(tool(f"t{i}", "Read"))
    await settled()
    await sink.text("warm")  # the first pass over a message that just froze
    await settled()
    calls: list[int] = []
    real = sinks.piece_blocks
    monkeypatch.setattr(sinks, "piece_blocks", lambda t, i: calls.append(i) or real(t, i))
    for i in range(5):
        await sink.text(f"more {i}")
        await settled()
    assert calls == []  # the first message, frozen with its preview, was left alone


def test_a_banner_is_cut_before_it_is_escaped() -> None:
    sink_text = "&" * 400
    assert len(sinks.banner_text(sink_text, limit=sinks.BANNER_LIMIT)) <= sinks.BANNER_LIMIT
    assert sinks.banner_text(sink_text, limit=10) == "&amp;" * 2  # never half an entity


async def test_many_late_previews_in_a_full_message_get_one_note_and_stay_in_the_limit(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    for w in range(10):
        await sink.task(tool(f"w{w}", "Write", "in_progress"))
    for i in range(60):
        await sink.task(tool(f"t{i}", "Agent", task=True))
    await settled()
    first = slack.stream_ts[0]
    body = "\n".join(f"{i:>4} {'x' * 90}" for i in range(100))
    for w in range(10):
        view = Preview("Write(big.txt)", "Wrote 100 lines to big.txt", body)
        await sink.task(tool(f"w{w}", "Write", preview=view))
    await settled()
    updates = [u for u in slack.calls_to("chat.update") if u["ts"] == first]
    assert updates
    for update in updates:
        assert len(update["blocks"]) <= 50
    notes = [b for b in updates[-1]["blocks"] if b == sinks.context_block(sinks.PREVIEW_CUT)]
    assert len(notes) == 1  # one note for the message, however many previews it left out


async def test_a_stream_is_adopted_from_slack_s_converted_read_back() -> None:
    # Slack reads markdown back converted (`**b**` as `*b*`, a heading without its `## `, a link
    # as `<url|label>`, the blank lines kept): the words are compared, not the markup.
    slack = ResetAfterApply()
    slack.reset_next = "chat.startStream"
    sink = reply(slack)
    await sink.text("## Title\n\n**bold** and [a link](https://example.com) follow.")
    await settled()
    await sink.text(" More.")
    await settled()
    assert len(slack.calls_to("chat.startStream")) == 1


async def test_a_start_that_holds_only_a_container_is_adopted_by_its_title() -> None:
    # A reply that opens on an Edit that ended well starts its stream with a `blocks` chunk
    # alone (issue #136): the container's title is what the read-back is compared with.
    slack = ResetAfterApply()
    slack.reset_next = "chat.startStream"
    sink = reply(slack)
    view = Preview("Update(a.txt)", "Added 1 line", "+x", "diff")
    await sink.task(tool("e", "Edit", preview=view))
    await settled()
    await sink.text("Done.")
    await settled()
    assert len(slack.calls_to("chat.startStream")) == 1  # never started again
    assert slack.stream_texts() == ["Done."]  # the adopted stream took the rest


async def test_a_start_with_nothing_to_compare_is_not_adopted() -> None:
    slack = ResetAfterApply()
    slack.reset_next = "chat.startStream"
    sink = reply(slack)
    await sink.text("…")
    await settled()
    await sink.text(" and then more words")
    await settled()
    assert len(slack.calls_to("chat.startStream")) == 2  # tried again, not guessed at


# A run of calls: two cards while the reply is written, a line of counts once its body ended.


def context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


async def run_of_calls(sink: ReplySink) -> None:
    """Text, then three calls one after the other (the last fails), then text."""
    await sink.text("Let me look.\n\n")
    for id, name, status, fields in (
        ("a", "Bash", "complete", {}),
        ("b", "Read", "complete", {}),
        ("c", "Bash", "error", {"output": "Exit code 1"}),
    ):
        await sink.task(tool(id, name, "in_progress"))
        await settled()
        await sink.task(tool(id, name, status, **fields))
        await settled()
    await sink.text("One test fails.")
    await settled()


async def test_a_run_of_calls_streams_as_the_counts_and_the_call_shown(slack: FakeSlack) -> None:
    sink = reply(slack)
    await run_of_calls(sink)
    assert slack.message_cards() == [
        [
            {"id": "fold:a", "title": "Ran 1 shell command · Read 1 file", "status": "complete"},
            {"id": "now:b", "title": "Bash: c · Exit code 1", "status": "error"},
        ]
    ]
    # No chunk carries text Slack would append to what a reused card holds.
    chunks = [c for _, a in slack.calls if "chunks" in a for c in a["chunks"]]
    assert not any("details" in c or "output" in c for c in chunks)
    assert slack.calls_to("chat.update") == []


async def test_the_end_folds_the_run_into_a_line_with_a_silent_update(slack: FakeSlack) -> None:
    sink = reply(slack)
    await run_of_calls(sink)
    await sink.finish([])
    assert await sink.close_out("main · ctx 6%") is True
    assert methods(slack)[-2:] == ["chat.stopStream", "chat.update"]
    [update] = slack.calls_to("chat.update")
    assert update["ts"] == slack.stream_ts[0]
    assert update["blocks"] == [
        {"type": "markdown", "text": "Let me look."},
        context("✓ Ran 1 shell command · Read 1 file · ✗ Ran 1 shell command"),
        {"type": "markdown", "text": "One test fails."},
        {"type": "divider"},
        context("main · ctx 6%"),
    ]
    assert slack.pushes() == 1  # the stop pushed; the update that folds never does


async def test_a_reply_with_no_run_of_calls_gets_no_update_at_its_end(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Working.\n\n")
    await sink.task(tool("t", "Agent", "in_progress", task=True))
    await sink.task(tool("t", "Agent", "complete", task=True, calls=2))
    await settled()
    await sink.finish([])
    assert await sink.close_out("footer") is True
    assert slack.calls_to("chat.update") == []


async def test_a_fold_that_cannot_be_written_does_not_undo_the_end(slack: FakeSlack) -> None:
    seen: list[tuple[str | None, str | None]] = []
    sink = reply(slack, on_open_reply=lambda old, new: seen.append((old, new)))
    await sink.task(tool("a", "Bash"))
    await settled()
    await sink.finish([])
    slack.responses["chat.update"] = [rejected("ratelimited"), {"ok": True}]
    # The stop carried the footer and pushed: the reply has ended, whatever the fold does.
    assert await sink.close_out("footer") is True
    assert await sink.wait_landed() is True
    assert (slack.stream_ts[0], None) in seen  # nothing is left for a crash repair to close
    await settled()  # the fold Slack refused is tried again with the next write
    updates = slack.calls_to("chat.update")
    assert len(updates) == 2 and updates[-1]["blocks"][0] == context("✓ Ran 1 shell command")
    assert slack.pushes() == 1 and slack.posted_ts == []  # no second stop, no closing message


async def test_a_fold_that_keeps_failing_is_given_up_and_the_cards_stay(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.task(tool("a", "Bash"))
    await settled()
    await sink.finish([])
    slack.responses["chat.update"] = rejected("ratelimited")
    assert await sink.close_out("footer") is True
    await settled()
    await settled()
    assert len(slack.calls_to("chat.update")) == 2  # once at the end, once more, then no loop
    assert await sink.settle() is True


async def test_a_stream_slack_closed_first_is_folded_before_its_closing_message(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.task(tool("a", "Bash"))
    await settled()
    await sink.finish([])
    slack.expire(slack.stream_ts[0])
    assert await sink.close_out("footer") is True
    [update] = slack.calls_to("chat.update")
    assert update["blocks"] == [context("✓ Ran 1 shell command")]
    assert len(slack.posted_ts) == 1  # the footer's own message, as for any stream Slack closed


async def test_past_the_window_the_run_keeps_its_two_cards_until_the_body_ends(
    slack: FakeSlack,
) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.task(tool("a", "Bash"))
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    await sink.task(tool("b", "Read", "in_progress"))
    await settled()
    blocks = slack.calls_to("chat.update")[-1]["blocks"]
    assert [(b["type"], b["title"], b["status"]) for b in blocks] == [
        ("task_card", "Ran 1 shell command", "complete"),
        ("task_card", "Read: b", "in_progress"),
    ]
    await sink.finish([tool("b", "Read")])
    assert slack.calls_to("chat.update")[-1]["blocks"] == [
        context("✓ Ran 1 shell command · Read 1 file")
    ]


async def test_a_task_that_outlives_the_turn_keeps_its_card_beside_the_folded_line(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.task(tool("a", "Read"))
    await sink.task(tool("t", "Bash", "in_progress", task=True, details="Running in background"))
    await settled()
    await sink.finish([])
    assert await sink.close_out(None) is True
    blocks = slack.calls_to("chat.update")[-1]["blocks"]
    assert blocks[0] == context("✓ Read 1 file")
    assert blocks[1]["type"] == "task_card" and blocks[1]["status"] == "in_progress"


async def test_a_run_in_a_message_the_reply_has_left_is_folded_at_the_end_too(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.task(tool("a", "Bash"))
    for i in range(sinks.BLOCKS_LIMIT + 5):
        await sink.task(tool(f"t{i}", "Agent", task=True))
    await settled()
    assert len(slack.stream_ts) == 2
    await sink.text("More.")  # a later pass finds the first message as its stream left it
    await settled()
    await sink.finish([])
    assert await sink.close_out(None) is True
    first = slack.message_blocks()[0]
    assert first[0] == context("✓ Ran 1 shell command")
    assert all(block.get("task_id") != "fold:a" for block in first)


async def test_every_pass_that_wrote_tells_the_thread_status(slack: FakeSlack) -> None:
    passes: list[int] = []
    sink = ReplySink(
        slack,
        channel=CHANNEL,
        thread_ts=THREAD,
        team_id=TEAM,
        user_id=OWNER,
        bot_user_id=BOT,
        limiter=UpdateLimiter(),
        clock=FakeClock(),
        on_write=lambda: passes.append(len(slack.calls)),
    )
    await sink.text("Hello.")
    await settled()
    assert passes == [1]  # after the stream's start
    assert await sink.settle()  # a pass with nothing to write: the status was not cleared
    assert passes == [1]
    await sink.finish([])
    await sink.close_out("footer")
    assert len(passes) >= 2 and passes[-1] == len(slack.calls)


# A reply that has ended never opens a message below its footer or its closing message.


def late_preview(lines: int = 40) -> Preview:
    body = "\n".join(f"{i:>4} {'x' * 90}" for i in range(lines))
    return Preview("Write(big.txt)", f"Wrote {lines} lines to big.txt", body)


def preview_of(lines: int) -> Preview:
    body = "\n".join(f"{i:>4} {'x' * 90}" for i in range(lines))
    return Preview("Write(big.txt)", f"Wrote {lines} lines to big.txt", body)


def marker(line: int) -> str:
    """The text of one line of `preview_of`: what tells that a preview is on the page."""
    return f"{line:>4} {'x' * 90}"


def shows(blocks: list[dict[str, Any]], needle: str) -> bool:
    return any(needle in sinks.block_text(b) for b in blocks)


def cards_of(blocks: list[dict[str, Any]]) -> dict[str, str]:
    return {b["task_id"]: b["status"] for b in blocks if b["type"] == "task_card"}


CUT_NOTE = sinks.context_block(sinks.PREVIEW_CUT)


async def test_a_late_preview_never_opens_a_message_after_the_footer(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("w" * (sinks.MESSAGE_LIMIT - 200))
    await sink.task(tool("t1", "Write", "in_progress", task=True))
    await settled()
    await sink.finish([])
    await sink.close_out("footer")
    posted = len(slack.calls_to("chat.postMessage"))
    await sink.task(tool("t1", "Write", preview=late_preview()))
    await settled()
    assert len(slack.calls_to("chat.postMessage")) == posted
    update = slack.calls_to("chat.update")[-1]
    assert update["ts"] == slack.stream_ts[0]
    blocks = update["blocks"]
    assert blocks[-1] == sinks.context_block("footer")  # the footer stays last
    assert shows(blocks, "w" * (sinks.MESSAGE_LIMIT - 200))  # what was shown stays
    assert cards_of(blocks) == {"t1": "complete"}  # the card took the update in place
    assert CUT_NOTE in blocks and not shows(blocks, marker(0))  # the preview did not fit


async def test_a_late_preview_never_opens_a_message_after_the_closing_message(
    slack: FakeSlack,
) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("w" * (sinks.MESSAGE_LIMIT - 200))
    await sink.task(tool("t1", "Write", "in_progress", task=True))
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    await sink.finish([])
    await sink.close_out("footer")
    [closing] = slack.calls_to("chat.postMessage")  # an answer of text alone: a footer message
    await sink.task(tool("t1", "Write", preview=late_preview()))
    await settled()
    assert slack.calls_to("chat.postMessage") == [closing]
    update = slack.calls_to("chat.update")[-1]
    assert update["ts"] == slack.stream_ts[0]
    blocks = update["blocks"]
    assert shows(blocks, "w" * (sinks.MESSAGE_LIMIT - 200))
    assert cards_of(blocks) == {"t1": "complete"}
    assert CUT_NOTE in blocks and not shows(blocks, marker(0))


async def test_a_late_preview_never_opens_a_message_after_the_ending(slack: FakeSlack) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.task(tool("t1", "Read"))
    await sink.text("w" * (sinks.MESSAGE_LIMIT - 200))
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    await sink.finish([])
    await sink.close_out("footer")
    [ending] = slack.calls_to("chat.postMessage")
    updates = len(slack.calls_to("chat.update"))
    await sink.task(tool("late", "Write", "in_progress"))
    await sink.task(tool("late", "Write", preview=late_preview()))
    await settled()
    assert slack.calls_to("chat.postMessage") == [ending]
    # A late card with its preview does not fit the ending: left out whole, no note, and the
    # ending message already shows what it should.
    assert len(slack.calls_to("chat.update")) == updates
    assert ending["blocks"][-1] == sinks.context_block("footer")
    assert shows(ending["blocks"], "w" * (sinks.MESSAGE_LIMIT - 200))


async def test_a_late_card_never_opens_a_message_past_the_blocks_limit(slack: FakeSlack) -> None:
    sink = reply(slack)
    for i in range(sinks.BLOCKS_LIMIT):
        await sink.task(tool(f"t{i}", "Agent", "in_progress", task=True))
    await settled()
    await sink.finish([])
    await sink.close_out("footer")
    posted = len(slack.calls_to("chat.postMessage"))
    await sink.task(tool("late", "Agent", "in_progress", task=True))
    await sink.set_running("1 agent")
    await settled()
    assert len(slack.calls_to("chat.postMessage")) == posted
    blocks = slack.calls_to("chat.update")[-1]["blocks"]
    assert sorted(cards_of(blocks)) == sorted(f"t{i}" for i in range(sinks.BLOCKS_LIMIT))
    assert blocks[-1] == sinks.context_block("footer · 1 agent")


# What the last message showed when the end was written stays; late content takes the room left.


async def test_late_words_never_remove_a_preview_that_was_shown(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("w" * 6000)
    await sink.task(tool("t1", "Write", preview=preview_of(42)))  # about 4,000 characters
    await settled()
    await sink.finish([])
    await sink.close_out("footer")
    await sink.text("late " * 1_200)  # 6,000 characters, with room for 5,000: none shows
    await settled()
    assert len(slack.created_ts) == 1
    blocks = slack.calls_to("chat.update")[-1]["blocks"]
    assert shows(blocks, marker(0)) and shows(blocks, marker(41))
    assert shows(blocks, "w" * 6000)
    assert CUT_NOTE not in blocks  # late words get no note
    assert not shows(blocks, "late")


async def test_late_cards_never_remove_a_preview_that_was_shown(slack: FakeSlack) -> None:
    sink = reply(slack)
    for i in range(44):  # a preview each, with no card: 44 blocks
        await sink.task(tool(f"w{i}", "Write", preview=preview_of(1)))
    await settled()
    await sink.finish([])
    await sink.close_out("footer")
    for i in range(3):
        await sink.task(tool(f"late{i}", "Agent", "in_progress", task=True))
    await settled()
    assert len(slack.created_ts) == 1
    blocks = slack.calls_to("chat.update")[-1]["blocks"]
    assert len([b for b in blocks if marker(0) in sinks.block_text(b)]) == 44
    assert CUT_NOTE not in blocks
    assert cards_of(blocks) == {}  # none of the late


async def test_a_late_preview_never_removes_the_one_a_later_call_showed(
    slack: FakeSlack,
) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("w" * 9_000)  # b's container adds no characters to an update: the words do
    await sink.task(tool("a", "Write", "in_progress", task=True))
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    await sink.task(tool("b", "Write", preview=preview_of(95)))  # about 9,000 characters
    await settled()
    await sink.finish([])
    await sink.close_out("footer")
    await sink.task(tool("a", "Write", preview=preview_of(32)))  # about 3,000: no room left
    await settled()
    assert len(slack.stream_ts) == 1 and len(slack.posted_ts) == 1  # and the closing message
    blocks = [u for u in slack.calls_to("chat.update") if u["ts"] == slack.stream_ts[0]][-1][
        "blocks"
    ]
    assert shows(blocks, marker(94))  # b's preview, whole
    assert cards_of(blocks) == {"a": "complete"}  # b never had a card
    assert CUT_NOTE in blocks
    counted = [b for b in blocks if b["type"] != "container"]
    assert sum(len(sinks.block_text(b)) for b in counted) <= sinks.MESSAGE_LIMIT + 100


async def test_a_failed_closing_post_is_not_a_written_end(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 30)  # the late update comes first
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("w" * (sinks.MESSAGE_LIMIT - 200))
    await sink.task(tool("t1", "Write", "in_progress", task=True))
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    await sink.finish([])
    taken = slack.responses["chat.postMessage"]
    slack.responses["chat.postMessage"] = rejected("ratelimited")
    assert await sink.close_out("footer") is False  # no closing message exists
    slack.responses["chat.postMessage"] = taken
    await sink.task(tool("t1", "Write", preview=late_preview()))
    await settled()
    posts = slack.calls_to("chat.postMessage")
    # The preview goes on in a message of its own, and the closing message comes last.
    assert shows(posts[-2]["blocks"], marker(0))
    assert posts[-1]["blocks"][-1] == sinks.context_block("footer")
    assert not any(CUT_NOTE in b["blocks"] for b in slack.calls_to("chat.update"))
    await sink.settle()


async def test_a_late_card_that_is_dropped_leaves_the_sink_caught_up(slack: FakeSlack) -> None:
    sink = reply(slack)
    for i in range(sinks.BLOCKS_LIMIT):
        await sink.task(tool(f"t{i}", "Agent", "in_progress", task=True))
    await settled()
    await sink.finish([])
    assert await sink.close_out("footer") is True
    await sink.task(tool("late", "Agent", "in_progress", task=True))
    assert await sink.settle() is True  # nothing failed: no retry, no error
    calls = len(slack.calls)
    assert await sink.settle() is True
    await settled()
    assert len(slack.calls) == calls  # caught up: the same pass writes nothing more


async def test_late_text_past_the_limit_is_cut_and_never_opens_a_message(
    slack: FakeSlack,
) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("w" * (sinks.MESSAGE_LIMIT - 200))
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    await sink.finish([])
    await sink.close_out("footer")
    posted = len(slack.calls_to("chat.postMessage"))
    await sink.text("\n\n" + "late words " * 100)
    await settled()
    assert len(slack.calls_to("chat.postMessage")) == posted
    for update in slack.calls_to("chat.update"):
        assert sum(len(sinks.block_text(b)) for b in update["blocks"]) <= 12_000  # Slack's cap


async def test_the_running_counts_after_the_end_edit_the_message_that_carries_the_footer(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.text("w" * (sinks.MESSAGE_LIMIT - 200))
    await sink.task(tool("t1", "Write", "in_progress", task=True))
    await settled()
    await sink.finish([])
    await sink.close_out("footer")
    posted = len(slack.calls_to("chat.postMessage"))
    await sink.set_running("1 agent")
    await settled()
    assert slack.calls_to("chat.update")[-1]["blocks"][-1] == sinks.context_block(
        "footer · 1 agent"
    )
    await sink.set_latest(False)
    await settled()
    assert slack.calls_to("chat.update")[-1]["blocks"][-1] == sinks.context_block("footer")
    assert len(slack.calls_to("chat.postMessage")) == posted


async def test_a_reply_still_open_splits_into_a_new_message_as_before(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("w" * (sinks.MESSAGE_LIMIT - 200))
    await sink.task(tool("t1", "Write", "in_progress", task=True))
    await settled()
    await sink.task(tool("t1", "Write", preview=late_preview()))
    await settled()
    assert len(slack.created_ts) == 2  # the preview went on in a message of its own


async def test_a_close_whose_first_write_failed_still_opens_the_messages_it_needs(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 0.02)
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("Start.\n")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)  # from here the message goes on by update
    lines = sinks.MESSAGE_LIMIT * 5 // 2 // 100
    await sink.text("x" * 99 + "\n")
    await sink.text(("x" * 99 + "\n") * (lines - 1))
    slack.responses["chat.update"] = rejected("ratelimited")
    await sink.finish([])
    assert await sink.close_out("footer") is False
    del slack.responses["chat.update"]  # Slack takes the retry
    assert await sink.wait_landed() is True
    shown = "".join(slack.message_texts())
    assert shown.count("x") == 99 * lines  # nothing cut
    assert len(slack.message_texts()) >= 3


async def test_late_content_that_fits_is_shown_as_the_open_path_computes_it(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.text("Start.")
    await sink.task(tool("t1", "Write", "in_progress", task=True))
    await settled()
    await sink.finish([])
    await sink.close_out("footer")
    await sink.task(tool("t1", "Write", preview=preview_of(3)))
    await sink.text("And a late word.")
    await sink.task(tool("t2", "Read", task=True))
    await settled()
    assert len(slack.created_ts) == 1
    blocks = slack.calls_to("chat.update")[-1]["blocks"]
    last = sink._messages[-1]
    assert blocks == sink._render(last, None, None)[0] + sink._closing_blocks()
    assert shows(blocks, marker(2)) and shows(blocks, "And a late word.")
    assert cards_of(blocks) == {"t1": "complete", "t2": "complete"}
    assert CUT_NOTE not in blocks


async def test_words_that_reach_the_model_while_the_end_is_written_open_no_message(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.text("Hello.")
    await settled()
    await sink.finish([])
    slack.delay = 0.02  # the stream's stop takes a while

    async def late() -> None:
        await asyncio.sleep(0.005)
        await sink.text("\n\n" + "z" * 20_000)  # past one message, and held without being shown

    arriving = asyncio.create_task(late())
    await sink.close_out("footer")
    await arriving
    slack.delay = 0
    await settled()
    assert await sink.settle() is True
    assert len(slack.created_ts) == 1
    assert slack.calls_to("chat.postMessage") == []
    # Only what fits the message is held: a later edit stays inside the limit Slack enforces,
    # so the footer still changes.
    await sink.set_running("1 agent")
    await settled()
    edit = slack.calls_to("chat.update")[-1]["blocks"]
    assert sum(len(b["text"]) for b in edit if b["type"] == "markdown") <= sinks.MESSAGE_LIMIT
    assert shows(edit, "footer · 1 agent")


# A refused edit or post logs its method and the sizes it sent, never its content.


async def test_a_refused_update_logs_the_method_and_the_sizes_without_content(
    slack: FakeSlack, caplog: pytest.LogCaptureFixture
) -> None:
    sink = reply(slack)
    await sink.text("Hello.")
    await sink.task(tool("t1", "Agent", task=True))
    await sink.finish([])
    assert await sink.close_out("footer")
    slack.responses["chat.update"] = rejected("msg_too_long")
    await sink.set_running("1 agent")
    await settled()
    [line] = [r.getMessage() for r in caplog.records if "chat.update failed" in r.message]
    assert "msg_too_long" in line
    # the message sent: its words, the card, and the footer under a divider
    assert "text 22, elements 4, cards 1" in line
    assert "Agent" not in caplog.text and "Hello" not in caplog.text


async def test_a_refused_post_logs_the_method_and_the_sizes_without_content(
    slack: FakeSlack, caplog: pytest.LogCaptureFixture
) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("start\n")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    slack.responses["chat.postMessage"] = rejected("msg_too_long")
    await sink.text("secret line\n" * 1_500)  # past one message: the rest is posted
    await settled()
    [line] = [r.getMessage() for r in caplog.records if "chat.postMessage failed" in r.message]
    assert "msg_too_long" in line
    [attempt] = slack.calls_to("chat.postMessage")
    sent = sum(len(b["text"]) for b in attempt["blocks"])
    assert 0 < sent < sinks.MESSAGE_LIMIT
    assert f"text {sent}, elements 1, cards 0" in line
    assert "secret" not in caplog.text and "start" not in caplog.text


# What a message written by `chat.update` holds: a collapsed container's text does not count
# toward MESSAGE_LIMIT there (measured 2026-10-06, slack-sdk 3.44.1: `chat.update` took 50
# containers of 10,000 characters and refused nothing; a stream and a post count the text).


def large_diff(i: int, chars: int = 9_000) -> Preview:
    """A diff of about `chars` characters: the size of the largest Edit previews, which a
    message of MESSAGE_LIMIT cannot hold two of."""
    body = "\n".join(f"+{n:>4} {'x' * 90}" for n in range(chars // 96))
    return Preview(f"Update(f{i}.txt)", "Added lines", body, "diff")


def containers_of(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b for b in blocks if b["type"] == "container"]


async def test_a_stopped_message_holds_large_diffs_a_stream_would_continue(
    slack: FakeSlack,
) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.task(tool("e0", "Edit", preview=large_diff(0)))
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    for i in range(1, 4):
        await sink.task(tool(f"e{i}", "Edit", preview=large_diff(i)))
    await settled()
    assert slack.calls_to("chat.postMessage") == [] and len(slack.stream_ts) == 1
    update = slack.calls_to("chat.update")[-1]
    assert update["ts"] == slack.stream_ts[0]
    assert [b["title"]["text"] for b in containers_of(update["blocks"])] == [
        f"Update(f{i}.txt)" for i in range(4)
    ]


async def test_a_stream_still_continues_in_a_new_message_past_the_limit_with_diffs(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)  # no stop: the diffs go to a stream, which counts their text
    for i in range(4):
        await sink.task(tool(f"e{i}", "Edit", preview=large_diff(i)))
    await settled()
    assert len(slack.stream_ts) >= 2
    for chunks in slack.calls_to("chat.startStream") + slack.calls_to("chat.appendStream"):
        shown = sum(
            len(sinks.block_text(b))
            for c in chunks.get("chunks", [])
            if c["type"] == "blocks"
            for b in c["blocks"]
        )
        assert shown <= sinks.MESSAGE_LIMIT


async def test_a_fixed_span_no_longer_cuts_a_late_diff_for_its_size(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.task(tool("t0", "Edit", "in_progress"))
    await sink.text("w" * (sinks.MESSAGE_LIMIT - 200))
    await settled()
    await sink.text("v" * 1_000)  # does not fit: the reply goes on in a second stream
    await settled()
    assert len(slack.stream_ts) == 2
    await sink.task(tool("t0", "Edit", preview=large_diff(0)))
    await settled()
    blocks = [u for u in slack.calls_to("chat.update") if u["ts"] == slack.stream_ts[0]][-1][
        "blocks"
    ]
    assert len(containers_of(blocks)) == 1 and CUT_NOTE not in blocks


async def test_a_fixed_span_still_cuts_a_late_diff_for_the_blocks_limit(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.task(tool("t0", "Edit", "in_progress"))
    for i in range(1, 60):
        await sink.task(tool(f"t{i}", "Agent", task=True))
    await settled()
    first = slack.stream_ts[0]
    await sink.task(tool("t0", "Edit", preview=large_diff(0)))
    await settled()
    blocks = [u for u in slack.calls_to("chat.update") if u["ts"] == first][-1]["blocks"]
    assert len(blocks) <= 50
    assert CUT_NOTE in blocks and containers_of(blocks) == []


async def test_a_preview_in_markdown_still_counts_toward_the_limit_on_an_update(
    slack: FakeSlack,
) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.task(tool("a", "Write", "in_progress"))
    await sink.text("w" * 6_000)
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    await sink.task(tool("a", "Write", preview=preview_of(95)))  # about 9,000 characters
    await settled()
    assert len(slack.calls_to("chat.postMessage")) == 1  # it did not fit the first message
    for update in slack.calls_to("chat.update"):
        assert sum(len(sinks.block_text(b)) for b in update["blocks"]) <= 12_000  # Slack's cap


async def continuation_with_diffs(slack: FakeSlack, clock: FakeClock) -> ReplySink:
    """A stopped first message of 45 blocks, and a continuation that gets three large diffs."""
    sink = reply(slack, clock=clock)
    await sink.text("start\n")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    for i in range(50):
        await sink.task(tool(f"c{i}", "Agent", task=True))
    for i in range(3):
        await sink.task(tool(f"e{i}", "Edit", preview=large_diff(i)))
    return sink


async def test_a_continuation_post_grows_by_update_to_what_an_update_holds(
    slack: FakeSlack,
) -> None:
    clock = FakeClock()
    await continuation_with_diffs(slack, clock)
    await settled()
    [post] = slack.calls_to("chat.postMessage")  # the post counts a container: one fits
    assert len(containers_of(post["blocks"])) == 1
    ts = slack.posted_ts[0]
    update = [u for u in slack.calls_to("chat.update") if u["ts"] == ts][-1]
    assert len(containers_of(update["blocks"])) == 3  # the other two reached it by update


async def test_a_refused_growth_of_a_continuation_goes_on_in_a_new_message(
    slack: FakeSlack,
) -> None:
    clock = FakeClock()
    refused: list[str] = []

    def refuse_the_first_growth(args: dict[str, Any]) -> Any:
        if args["ts"] in slack.posted_ts and not refused:
            refused.append(args["ts"])
            return rejected("invalid_blocks")
        return {"ok": True}

    slack.responses["chat.update"] = refuse_the_first_growth
    await continuation_with_diffs(slack, clock)
    await settled()
    first, second = slack.calls_to("chat.postMessage")  # what the update was refused went on
    assert [len(containers_of(p["blocks"])) for p in (first, second)] == [1, 1]
    update = [u for u in slack.calls_to("chat.update") if u["ts"] == slack.posted_ts[1]][-1]
    assert len(containers_of(update["blocks"])) == 2  # and it took the last by update
    # the refused update is the only one of the posted message: it is not rewritten with what
    # its post already holds
    assert len([u for u in slack.calls_to("chat.update") if u["ts"] == slack.posted_ts[0]]) == 1


def refusing_containers(slack: FakeSlack, code: str = "msg_too_long") -> list[dict[str, Any]]:
    """Slack refuses every `chat.update` that carries more than one container; the refused
    updates are listed."""
    refused: list[dict[str, Any]] = []

    def answer(args: dict[str, Any]) -> Any:
        if len(containers_of(args["blocks"])) > 1:
            refused.append(args)
            return rejected(code)
        return {"ok": True}

    slack.responses["chat.update"] = answer
    return refused


async def test_a_refused_update_with_containers_counts_them_from_then_on(
    slack: FakeSlack,
) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.task(tool("e0", "Edit", preview=large_diff(0)))
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    refused = refusing_containers(slack)
    for i in range(1, 3):
        await sink.task(tool(f"e{i}", "Edit", preview=large_diff(i)))
    await settled()
    await sink.finish([])
    assert await sink.close_out("footer") is True  # the reply's outcome is not an error
    # what fits by the post's counting stays, the rest went on in new messages, each shown whole
    assert refused and len(slack.posted_ts) >= 2
    titles = [
        b["title"]["text"] for blocks in slack.message_blocks() for b in containers_of(blocks)
    ]
    assert titles == [f"Update(f{i}.txt)" for i in range(3)]
    assert any(
        b == sinks.context_block("footer") for blocks in slack.message_blocks() for b in blocks
    )
    await sink.task(tool("e3", "Edit", preview=large_diff(3)))  # a later diff, in the last message
    await settled()
    tried = len(refused)  # that message was refused once too, and counts from then on
    await sink.set_running("1 agent")  # a later change to it: no refused blocks again
    await settled()
    assert len(refused) == tried


async def test_a_refused_update_of_a_fixed_span_cuts_the_late_diff_with_the_note(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.task(tool("t0", "Edit", "in_progress"))
    await sink.text("w" * (sinks.MESSAGE_LIMIT - 200))
    await settled()
    await sink.text("v" * 1_000)
    await settled()
    first = slack.stream_ts[0]
    slack.responses["chat.update"] = lambda args: (
        rejected("invalid_blocks") if containers_of(args["blocks"]) else {"ok": True}
    )
    await sink.task(tool("t0", "Edit", preview=large_diff(0)))
    await settled()
    blocks = [u for u in slack.calls_to("chat.update") if u["ts"] == first][-1]["blocks"]
    assert containers_of(blocks) == [] and CUT_NOTE in blocks
    assert list(cards_of(blocks).values()) == ["complete"]  # card and words updated
    assert shows(blocks, "w" * (sinks.MESSAGE_LIMIT - 200))


async def test_a_refused_update_with_no_container_is_dropped_as_before(
    slack: FakeSlack,
) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("partial")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    slack.responses["chat.update"] = rejected("invalid_blocks")
    await sink.text(" more")
    await settled()
    assert len(slack.calls_to("chat.update")) == 1  # no second try with other counting
    slack.responses["chat.update"] = {"ok": True}
    await sink.text(" again")
    await settled()
    assert len(slack.calls_to("chat.update")) == 2  # the next change is tried


async def test_a_request_slack_refuses_to_delete_is_not_logged_as_removed(
    slack: FakeSlack, caplog: pytest.LogCaptureFixture
) -> None:
    # Issue #71 reads the removal's timing from the log: a delete that failed must not be in it.
    slack.responses["chat.delete"] = {"ok": False, "error": "cant_delete_message"}
    with caplog.at_level(logging.INFO, logger="code_with_slack"):
        await sinks.delete_request(slack, channel=CHANNEL, ts="1790000000.000100")
    assert "could not remove a request" in caplog.text
    assert "removed a request" not in caplog.text
    slack.responses["chat.delete"] = {"ok": False, "error": "message_not_found"}
    with caplog.at_level(logging.INFO, logger="code_with_slack"):
        await sinks.delete_request(slack, channel=CHANNEL, ts="1790000000.000100")
    assert "removed a request" in caplog.text  # already gone counts as done


# A stream's card: Slack adds the details and the output of every chunk to what the card holds
# (measured 2026-10-01 and 2026-10-08), so a stream is sent only what the card lacks.


def card_chunks(slack: FakeSlack, card_id: str) -> list[dict[str, Any]]:
    """Every chunk a reply's streams were sent for one card, in order."""
    return [
        chunk
        for method in ("chat.startStream", "chat.appendStream")
        for call in slack.calls_to(method)
        for chunk in call["chunks"]
        if chunk.get("id") == card_id
    ]


@pytest.mark.parametrize(
    ("held", "wanted", "more"),
    [
        ("", "a\nb", "a\nb"),
        ("a\nb", "a\nb", ""),
        ("a\nb", "a\nb\nc", "\nc"),
        ("a\nb\nc", "b\nc\nd\ne", "\nd\ne"),
        ("a\nb", "c", "\nc"),
        ("a\na", "a\na\na", "\na"),
        ("x\na", "a\na", "\na"),
    ],
)
def test_a_card_is_sent_the_lines_it_lacks(held: str, wanted: str, more: str) -> None:
    assert sinks.lacking(held, wanted) == more


async def test_a_subagent_card_is_sent_each_of_its_lines_once(slack: FakeSlack) -> None:
    sink = reply(slack, limiter=UpdateLimiter(burst=20))  # room for a write per line
    lines = [f"Bash: step {n}" for n in range(1, 13)]
    for n in range(1, 13):
        window = "\n".join(lines[max(0, n - 10) : n])  # the last ten, as the renderer keeps them
        await sink.task(tool("a1", "Agent", "in_progress", details=window, task=True, calls=n))
        await settled()
    # However the writes were batched, the chunks carry each line once, in order.
    sent = "".join(chunk.get("details", "") for chunk in card_chunks(slack, "a1"))
    assert sent == "\n".join(lines)
    [[card]] = slack.message_cards()
    assert card["details"] == "\n".join(lines) and card["title"].endswith(" · 12 calls")


async def test_a_card_whose_title_alone_changes_is_sent_no_text_again(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.task(tool("a1", "Agent", "in_progress", details="Read: notes.md", task=True))
    await settled()
    await sink.task(
        tool("a1", "Agent", "in_progress", details="Read: notes.md", task=True, calls=1)
    )
    await settled()
    await sink.task(tool("b1", "Agent", "error", output="exit 2", task=True))
    await settled()
    await sink.task(
        TaskUpdate("b1", "Agent: again", "error", name="Agent", output="exit 2", task=True)
    )
    await settled()
    first, second = card_chunks(slack, "a1")
    assert first["details"] == "Read: notes.md" and "details" not in second
    first, second = card_chunks(slack, "b1")
    assert first["output"] == "exit 2" and "output" not in second
    assert [c.get("details", c.get("output")) for c in slack.message_cards()[0]] == [
        "Read: notes.md",
        "exit 2",
    ]


async def test_a_card_whose_one_line_changes_gains_a_line(slack: FakeSlack) -> None:
    sink = reply(slack)
    for words in ("Reading the tests", "Running the tests"):
        await sink.task(tool("k1", "task", "in_progress", details=words, task=True))
        await settled()
    [[card]] = slack.message_cards()
    assert card["details"] == "Reading the tests\nRunning the tests"


async def test_cards_whose_text_fills_the_message_continue_in_a_new_stream(
    slack: FakeSlack,
) -> None:
    # Measured 2026-10-01: a stream took 4 error cards with 2,900 characters of output and
    # refused the fifth. Three of them are counted as a full message.
    sink = reply(slack)
    for i in range(6):
        await sink.task(
            tool(f"t{i}", "Agent", "error", output=f"{i}" * sinks.CARD_TEXT_LIMIT, task=True)
        )
        await settled()
    await sink.finish([])
    await sink.close_out(None)
    assert len(slack.stream_ts) == 2
    assert [len(cards) for cards in slack.message_cards()] == [3, 3]


async def test_text_after_cards_that_fill_the_message_continues_in_a_new_stream(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    for i in range(3):
        await sink.task(
            tool(f"t{i}", "Agent", "error", output=f"{i}" * sinks.CARD_TEXT_LIMIT, task=True)
        )
    await settled()
    await sink.text("a line of text\n" * 200)  # 3,000 characters: the cards left room for less
    await settled()
    first, second = slack.message_texts()
    assert first and second and len(first) + len(second) >= 2_990
    assert len(first) < 1_500


async def test_a_running_card_in_a_full_message_keeps_its_title_and_gains_no_line(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.task(tool("a1", "Agent", "in_progress", details="Read: notes.md", task=True))
    await sink.text("a line of text\n" * 700)  # 10,500 characters
    await settled()
    await sink.task(
        tool(
            "a1", "Agent", "in_progress", details="Read: notes.md\n" + "x" * 400, task=True, calls=2
        )
    )
    await settled()
    last = card_chunks(slack, "a1")[-1]
    assert last["title"].endswith(" · 2 calls") and "details" not in last
    assert slack.message_cards()[0][0]["details"] == "Read: notes.md"
    assert len(slack.stream_ts) == 1


async def test_a_card_that_ends_is_sent_no_text_for_the_details_it_no_longer_says(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.task(tool("a1", "Agent", "in_progress", details="Read: notes.md", task=True))
    await settled()
    await sink.task(tool("a1", "Agent", "complete", task=True))
    await settled()
    _, second = card_chunks(slack, "a1")
    assert second == {"type": "task_update", "id": "a1", "title": "Agent: a1", "status": "complete"}
    assert slack.message_cards()[0][0]["details"] == "Read: notes.md"  # Slack keeps them


async def test_a_card_that_fails_in_a_full_message_still_says_why(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.task(tool("a1", "Agent", "in_progress", task=True))
    await sink.text("a line of text\n" * 720)  # 10,800 characters
    await settled()
    await sink.task(tool("a1", "Agent", "error", output="x" * 400, task=True))
    await settled()
    assert card_chunks(slack, "a1")[-1]["output"] == "x" * 400
    assert len(slack.stream_ts) == 1


# The blocks Slack makes of markdown: a header per heading, a table per table, a divider per
# rule, rich text for each run between them. A post or an update whose message passes 50 of them
# is refused; a stream is not, and the update after its stop is (measured 2026-10-08).


def sections(count: int, start: int = 1) -> str:
    """`count` headings with a paragraph each: two blocks a section, as Slack stored them."""
    return "\n\n".join(f"## Heading {n}\n\nparagraph {n}" for n in range(start, start + count))


def lines_of(*texts: str) -> list[str]:
    """The lines with words of some texts, in order: what a reply says, however it was cut."""
    return [line for text in texts for line in text.split("\n") if line.strip()]


def too_many(error: str = "invalid_blocks") -> SlackApiError:
    """Slack's refusal of a message with more than 50 blocks, as recorded on 2026-10-08."""
    notes = ["[ERROR] no more than 50 items allowed [json-pointer:/blocks]"]
    return SlackApiError(
        error, {"ok": False, "error": error, "response_metadata": {"messages": notes}}
    )


@pytest.mark.parametrize(
    ("text", "stored"),
    [
        # each shape was posted alone on 2026-10-08 and its stored blocks counted
        ("one\n\ntwo", 1),
        ("## Title\n\ntext", 2),
        ("before\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nafter", 3),
        ("| a | b |\n|---|---|\n| 1 | 2 |\n\nmid\n\n| c | d |\n|---|---|\n| 3 | 4 |", 3),
        ("- one\n- two\n\ntext", 1),
        ("text\n\n```\ncode\n```\n\nmore", 1),
        ("## A\n\na\n\n## B\n\nb\n\n## C\n\nc", 6),
        ("before\n\n> quoted\n\nafter", 1),
        ("before\n\n---\n\nafter", 3),
        ("before\n\n![alt](https://example.com/a.png)\n\nafter", 1),
        ("- [ ] one\n- [x] two\n\nafter", 1),
        ("```\n# not a heading\n```\n\nafter", 1),
        ("# one\n\n## two\n\n### three\n\ntext", 4),
        ("## one\n## two\n\ntext", 3),
        ("**Title**\n\ntext\n\n**Title 2**\n\ntext", 1),
        (sections(25), 50),
    ],
)
def test_markdown_counts_the_blocks_slack_stored_for_it(text: str, stored: int) -> None:
    assert sinks.markdown_blocks(text) == stored


def test_markdown_is_cut_at_the_line_that_starts_one_block_too_many() -> None:
    text = sections(3)
    assert sinks.markdown_cut(text, 6) is None
    cut = sinks.markdown_cut(text, 4)
    assert cut is not None and text[cut:] == "## Heading 3\n\nparagraph 3"


async def test_a_streamed_text_with_many_headings_continues_in_a_new_stream(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    text = sections(60)  # 120 blocks, 1,600 characters: far below the size limit
    for i in range(0, len(text), 400):
        await sink.text(text[i : i + 400])
        await settled()
    await sink.finish([])
    await sink.close_out(None)
    shown = slack.message_texts()
    assert len(slack.stream_ts) == 3
    assert all(sinks.markdown_blocks(words) <= sinks.BLOCKS_LIMIT for words in shown)
    assert lines_of(*shown) == lines_of(text)  # nothing left out


async def test_an_updated_message_with_many_headings_continues_in_a_post(
    slack: FakeSlack,
) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock, limiter=UpdateLimiter(burst=50))
    await sink.text("start\n\n")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    text = sections(60)
    for i in range(0, len(text), 400):
        await sink.text(text[i : i + 400])
        await settled()
    await sink.finish([])
    await sink.close_out(None)
    writes = slack.calls_to("chat.update") + slack.calls_to("chat.postMessage")
    assert max(sinks.blocks_count(w["blocks"]) for w in writes if w.get("blocks")) <= 50
    assert lines_of(*slack.message_texts()) == lines_of("start", text)


async def test_cards_and_headings_share_the_room_of_a_message(slack: FakeSlack) -> None:
    sink = reply(slack)
    for i in range(30):
        await sink.task(tool(f"t{i}", "Agent", task=True))
    await settled()
    await sink.text(sections(20))  # 40 blocks: 15 fit beside the 30 cards
    await settled()
    await sink.finish([])
    await sink.close_out(None)
    first, second = slack.message_texts()
    assert sinks.markdown_blocks(first) == 14  # the fifteenth is a heading: it goes with its text
    assert lines_of(first, second) == lines_of(sections(20))


async def test_an_update_refused_for_too_many_blocks_goes_on_in_a_new_message(
    slack: FakeSlack, caplog: pytest.LogCaptureFixture
) -> None:
    # Slack counts more than the daemon does: here every list item is a block of its own.
    def answer(args: dict[str, Any]) -> Any:
        items = sum(b["text"].count("\n- ") for b in args["blocks"] if b["type"] == "markdown")
        return too_many() if items > 20 else {"ok": True}

    slack.responses["chat.update"] = answer
    clock = FakeClock()
    sink = reply(slack, clock=clock, limiter=UpdateLimiter(burst=50))
    await sink.text("start\n")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    items = "".join(f"\n- item {n}\n\n## Heading {n}\n" for n in range(1, 41))
    with caplog.at_level(logging.WARNING, logger="code_with_slack.render.sinks"):
        await sink.text(items)
        await settled()
        await sink.finish([])
        assert await sink.close_out(None)
    assert lines_of(*slack.message_texts()) == lines_of("start", items)  # nothing was dropped
    assert len(slack.posted_ts) >= 2
    assert "chat.update failed (invalid_blocks at /blocks, over 50 blocks once translated)" in (
        caplog.text
    )
    assert "item" not in caplog.text


async def test_an_update_refused_for_another_reason_is_not_split(slack: FakeSlack) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("start\n")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    slack.responses["chat.update"] = lambda args: rejected("invalid_blocks")
    await sink.text(sections(5))
    await settled()
    assert slack.posted_ts == []  # the change is dropped, as before


async def test_a_post_refused_for_too_many_blocks_is_posted_with_less(slack: FakeSlack) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("start\n")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    slack.responses["chat.postMessage"] = [too_many(), slack.responses["chat.postMessage"]]
    text = "a line of text\n" * 1_000  # past one message: the rest is posted
    await sink.text(text)
    await settled()
    assert len(slack.calls_to("chat.postMessage")) == 2  # refused, then posted
    assert lines_of(*slack.message_texts()) == lines_of("start", text)


@pytest.mark.parametrize(
    ("text", "blocks"),
    [
        # Not measured on Slack: read as CommonMark and GFM define them.
        ("Title\n=====\n\ntext", 3),
        ("````md\n```\n# inside\n```\n````\n\n## after\n\ntext", 3),
        ("```x``` then words\n\n## after\n\ntext", 3),
        ("~~~\n# inside\n~~~\n\n## after", 2),
        ("before\r\n\r\n---\r\n\r\nafter", 3),
        ("before\n\n- - -\n\nafter", 3),
        ("```\n# the fence is still open while the text streams", 1),
    ],
)
def test_markdown_shapes_that_were_not_measured_are_read_as_commonmark(
    text: str, blocks: int
) -> None:
    assert sinks.markdown_blocks(text) == blocks


async def test_a_fold_refused_for_too_many_blocks_keeps_the_text_the_stream_showed(
    slack: FakeSlack,
) -> None:
    # Slack counts more than the daemon does: here every list item is a block of its own.
    def answer(args: dict[str, Any]) -> Any:
        items = sum(b["text"].count("- item") for b in args["blocks"] if b["type"] == "markdown")
        return too_many() if items > 10 else {"ok": True}

    slack.responses["chat.update"] = answer
    sink = reply(slack)
    await sink.task(tool("t1", "Bash"))
    text = "".join(f"- item {n}\n\n## Heading {n}\n\n" for n in range(1, 21))
    await sink.text(text)
    await settled()
    await sink.finish([])
    assert await sink.close_out(None) is True  # nothing is missing: the reply ended well
    await settled()
    assert lines_of(*slack.message_texts()) == lines_of(text)  # the fold is dropped, not the text


async def test_an_update_refused_down_to_one_block_repeats_nothing(slack: FakeSlack) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock, limiter=UpdateLimiter(burst=50))
    await sink.text("start\n")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    slack.responses["chat.update"] = lambda args: too_many()
    await sink.text(sections(5))
    await settled()
    assert slack.posted_ts == []  # no rest posted while the message still shows what it showed
    slack.responses["chat.update"] = {"ok": True}
    await sink.text("\n\nthe end")
    await settled()
    assert lines_of(*slack.message_texts()) == lines_of("start", sections(5), "the end")


async def test_a_sent_line_that_turns_into_a_table_is_not_cut_in_two(slack: FakeSlack) -> None:
    sink = reply(slack, limiter=UpdateLimiter(burst=50))
    await sink.text(sections(22) + "\n\nintro\n\na | b")  # 45 blocks, the last one a run of text
    await settled()
    await sink.text("\n---|---\n1 | 2\n\n## Next\n\nlast")
    await settled()
    await sink.finish([])
    await sink.close_out(None)
    first, second = slack.message_texts()
    assert first.endswith("a | b\n---|---\n1 | 2") and second.startswith("## Next")


def test_a_heading_is_not_left_as_the_last_block_before_a_cut() -> None:
    text = sections(3)
    third = text.index("## Heading 3")
    assert sinks.markdown_cut(text, 5) == third  # the fifth block is the third heading
    assert sinks.markdown_cut(text, 4) == third
    # a text that ends on the heading that fills the room: whatever follows would be cut off it
    assert sinks.markdown_cut(text[: third + len("## Heading 3")], 5) == third
    assert sinks.markdown_cut("## Only\n\ntext", 1) == len("## Only\n\n")  # never an empty message
    # a stream cannot take back a heading it was sent
    assert sinks.markdown_cut(text, 5, floor=third + 3) == text.index("paragraph 3")


async def test_a_streamed_heading_goes_to_the_next_message_with_its_text(slack: FakeSlack) -> None:
    # Seen on 2026-10-08: 40 sections, the first message ended on the heading of the 23rd.
    sink = reply(slack, limiter=UpdateLimiter(burst=200))
    text = sections(40)
    for i in range(0, len(text), 7):  # in small pieces, as a stream brings it
        await sink.text(text[i : i + 7])
        await settled()
    await sink.finish([])
    await sink.close_out(None)
    first, second = slack.message_texts()
    assert first.endswith("paragraph 22") and second.startswith("## Heading 23")
    assert lines_of(first, second) == lines_of(text)


# A dropped update leaves the message short of what Claude wrote: the reply's end does not land,
# which the session shows as a failed turn, until an update of that message passes.


async def test_a_dropped_update_of_a_message_written_by_edit_is_an_end_that_did_not_land(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 0.01)
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("partial")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)  # from here the message grows by update
    slack.responses["chat.update"] = rejected("invalid_blocks")
    await sink.text(" and more")
    await sink.finish([])
    assert await sink.close_out("footer") is False
    assert await sink.wait_landed() is False
    assert slack.stream_texts() == ["partial"]


async def test_a_dropped_update_followed_by_one_that_passes_lands_the_reply(
    slack: FakeSlack,
) -> None:
    clock = FakeClock()
    sink = reply(slack, clock=clock)
    await sink.text("partial")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    slack.responses["chat.update"] = [rejected("invalid_blocks"), {"ok": True}]
    await sink.text(" and more")
    await settled()
    assert slack.stream_texts() == ["partial"]  # the change was dropped
    await sink.text(", then the end")
    await sink.finish([])
    assert await sink.close_out("footer") is True
    assert slack.stream_texts()[0].startswith("partial and more, then the end")


async def test_a_dropped_update_of_a_continuation_is_an_end_that_did_not_land(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 0.01)
    clock = FakeClock()
    sink = reply(slack, clock=clock, limiter=UpdateLimiter(burst=50))
    await sink.text("start\n")
    await settled()
    await clock.advance(sinks.STREAM_SECONDS + 1)
    await sink.text("a line of text\n" * 1_000)  # past one message: the rest is a post
    await settled()
    posted = slack.posted_ts[0]
    slack.responses["chat.update"] = lambda args: (
        rejected("invalid_blocks") if args["ts"] == posted else {"ok": True}
    )
    await sink.text("the last line")
    await sink.finish([])
    assert await sink.close_out("footer") is False
