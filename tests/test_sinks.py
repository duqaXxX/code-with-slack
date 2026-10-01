import asyncio
import itertools
import json
import time
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from slack_sdk.errors import SlackApiError

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


async def test_the_end_after_the_switch_posts_a_closing_message(slack: FakeSlack) -> None:
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
    # the body message itself carries no footer: it lives in the closing message
    assert all(b["type"] != "divider" for b in slack.calls_to("chat.update")[-1]["blocks"])


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


async def test_a_reply_that_is_no_longer_latest_drops_its_footer(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Done.")
    await sink.finish([])
    await sink.close_out("footer")
    await sink.set_latest(False)
    await settled()
    update = slack.calls_to("chat.update")[-1]
    assert [b["type"] for b in update["blocks"]] == ["markdown"]


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
    # the message that pushed stays, with nothing to show
    assert slack.calls_to("chat.update")[-1]["blocks"] == [
        sinks.context_block(sinks.ZERO_WIDTH_SPACE)
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
    # it (dropping the footer, say) must leave that body as it is: no plain-text fallback.
    sink = reply(slack)
    await sink.text("line of text\n" * 400)
    await sink.task(tool("t1", "Bash"))
    await sink.finish([])
    assert await sink.close_out("footer")
    slack.responses["chat.update"] = rejected("invalid_blocks")
    await sink.set_latest(False)
    await settled()
    updates = slack.calls_to("chat.update")
    assert updates and all(u["blocks"] != [] for u in updates)
    tried = len(updates)
    await settled()
    assert len(slack.calls_to("chat.update")) == tried  # the change is dropped, not retried
    slack.responses["chat.update"] = {"ok": True}
    await sink.set_latest(True)  # a later change is tried again
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


async def test_a_diff_preview_follows_its_card_as_a_blocks_chunk(slack: FakeSlack) -> None:
    sink = reply(slack)
    view = Preview("Update(a.txt)", "Added 1 line", "+\U0001f7e9 1 x", "diff")
    await sink.task(tool("e", "Edit", preview=view))
    await settled()
    [start] = slack.calls_to("chat.startStream")
    card, blocks = start["chunks"]
    assert card == {
        "type": "task_update",
        "id": "e",
        "title": "Update(a.txt)",
        "status": "complete",
        "output": "Added 1 line",
    }
    assert blocks["type"] == "blocks"
    [container] = blocks["blocks"]
    assert container["type"] == "container" and container["is_collapsible"] is True


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
    cards = slack.message_cards()[0]
    assert [
        (c["title"], c["status"]) for c in cards if c["title"].startswith(("Write(", "Upd"))
    ] == [
        ("Write(new.txt)", "complete"),
        ("Update(notes.txt)", "complete"),
        ("Write(notes.txt)", "complete"),
    ]
    assert cards[0]["output"] == "Wrote 15 lines to new.txt"
    chunks = [c for _, a in slack.calls if "chunks" in a for c in a["chunks"]]
    diffs = [
        b for c in chunks if c["type"] == "blocks" for b in c["blocks"] if b["type"] == "container"
    ]
    assert [sinks.block_text(d) for d in diffs] == [
        "    1 alpha\n-\U0001f7e5 2 beta\n+\U0001f7e9 2 gamma\n    3 delta",
        "-\U0001f7e5 1 alpha\n-\U0001f7e5 2 gamma\n-\U0001f7e5 3 delta\n"
        "+\U0001f7e9 1 one\n+\U0001f7e9 2 two",
    ]
    code = [
        b["text"]
        for c in chunks
        if c["type"] == "blocks"
        for b in c["blocks"]
        if b["type"] == "markdown"
    ]
    assert code[0].splitlines()[1:3] == [" 1 1", " 2 2"] and "… +5 lines" in code[0]


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
    assert [b["type"] for b in blocks] == [
        "task_card",
        "container",
        "task_card",
        "markdown",
        "markdown",
    ]
    assert blocks[3]["text"] == "```\n1 hi\n```"


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


def test_a_task_card_block_reads_back_as_the_card_it_was() -> None:
    update = tool("t1", "Bash", "error", output="exit 1", details="ignored while it ended")
    block = sinks.card_block(update)
    assert block["type"] == "task_card" and block["task_id"] == "t1"
    assert block["status"] == "error" and block["title"] == "Bash: t1"
    assert block["output"]["type"] == "rich_text" and "details" not in block


def title_of(container: dict[str, Any]) -> str:
    """A container's rich title as it reads, with the call's name in code style checked."""
    [section] = container["rich_text_title"]["elements"]
    icon, name = section["elements"]
    assert name["style"] == {"code": True}
    assert container["title"]["text"] == icon["text"] + name["text"]  # the plain fallback
    return str(icon["text"] + name["text"])


def test_a_diff_shows_collapsed_and_full_width() -> None:
    [block] = sinks.diff_containers("✓", "Update(a.txt)", "Added 1 line", "+\U0001f7e9 1 x")
    assert block["is_collapsible"] is True and block["default_collapsed"] is True
    assert block["width"] == "full"
    [child] = block["child_blocks"]
    [pre] = child["elements"]
    assert pre["type"] == "rich_text_preformatted" and pre["language"] == "diff"


def test_a_diff_past_a_message_continues_in_the_next() -> None:
    body = "\n".join(f"+\U0001f7e9 {i} {'x' * 90}" for i in range(1, 400))
    blocks = sinks.diff_containers("✓", "Update(a.txt)", "Added 399 lines", body)
    assert len(blocks) > 1
    assert all(len(sinks.block_text(b)) <= sinks.MESSAGE_LIMIT for b in blocks)
    assert "\n".join(sinks.block_text(b) for b in blocks) == body  # nothing lost at the cuts


def test_a_long_title_is_cut_to_slacks_limit() -> None:
    [block] = sinks.diff_containers("✓", "Update(" + "a" * 200 + ")", "Added 1 line", "+x")
    assert len(block["title"]["text"]) == 150
    assert title_of(block) == block["title"]["text"]


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


async def test_a_fold_that_cannot_be_written_is_tried_once_more(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 0.02)
    sink = reply(slack)
    await sink.task(tool("a", "Bash"))
    await settled()
    await sink.finish([])
    slack.responses["chat.update"] = [rejected("ratelimited"), {"ok": True}]
    assert await sink.close_out("footer") is False
    assert await sink.wait_landed() is True
    assert slack.calls_to("chat.update")[-1]["blocks"][0] == context("✓ Ran 1 shell command")
    assert slack.pushes() == 1 and slack.posted_ts == []  # no second stop, no closing message


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
