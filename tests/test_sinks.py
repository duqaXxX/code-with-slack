import asyncio
from typing import Any

import pytest

from code_with_slack.render import sinks
from code_with_slack.render.renderer import TaskUpdate
from code_with_slack.render.sinks import ReplySink, StreamingSwitch
from tests.fakes import CHANNEL, OWNER, TEAM, FakeSlack


def reply(slack: FakeSlack, switch: StreamingSwitch) -> ReplySink:
    return ReplySink(
        slack, switch, channel=CHANNEL, thread_ts="111.222", team_id=TEAM, user_id=OWNER
    )


def chunks_sent(slack: FakeSlack) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for method in ("chat.startStream", "chat.appendStream", "chat.stopStream"):
        for args in slack.calls_to(method):
            out += list(args.get("chunks") or [])
    return out


async def test_a_reply_streams_in_the_thread_in_timeline_mode(slack: FakeSlack) -> None:
    sink = reply(slack, StreamingSwitch())
    await sink.text("Hello " * 100)
    await sink.task(TaskUpdate("t1", "Bash: ls", "in_progress"))
    await sink.task(TaskUpdate("t1", "Bash: ls", "complete", output="3 files"))
    await sink.finish([], "main · ctx 6%")
    start = slack.calls_to("chat.startStream")[0]
    assert start["thread_ts"] == "111.222" and start["task_display_mode"] == "timeline"
    assert start["recipient_team_id"] == TEAM and start["recipient_user_id"] == OWNER
    stop = slack.calls_to("chat.stopStream")[0]
    assert stop["blocks"][0]["type"] == "context"
    statuses = [c["status"] for c in chunks_sent(slack) if c["type"] == "task_update"]
    assert statuses == ["in_progress", "complete"]


async def test_closing_cards_travel_with_stop(slack: FakeSlack) -> None:
    sink = reply(slack, StreamingSwitch())
    await sink.task(TaskUpdate("t1", "Bash: sleep", "in_progress"))
    await sink.finish([TaskUpdate("t1", "Bash: sleep", "complete", output="Stopped")], None)
    stop_chunks = slack.calls_to("chat.stopStream")[0]["chunks"]
    assert [(c["id"], c["status"]) for c in stop_chunks if c["type"] == "task_update"] == [
        ("t1", "complete")
    ]


async def test_long_text_is_split_under_the_append_limit(slack: FakeSlack) -> None:
    body = "x" * 30_000
    sink = reply(slack, StreamingSwitch())
    await sink.text(body)
    await sink.finish([], None)
    pieces = [c["text"] for c in chunks_sent(slack) if c["type"] == "markdown_text"]
    assert all(len(p) <= sinks.APPEND_LIMIT for p in pieces)
    assert "".join(pieces) == body


async def test_refused_streaming_falls_back_for_good(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sinks, "DEBOUNCE_SECONDS", 0.01)
    slack.responses["chat.startStream"] = {"ok": False, "error": "not_allowed"}
    switch = StreamingSwitch()
    sink = reply(slack, switch)
    await sink.text("a" * 300)
    await sink.task(TaskUpdate("t1", "Bash: ls", "in_progress"))
    await sink.finish([TaskUpdate("t1", "Bash: ls", "complete")], "footer")
    assert switch.enabled is False
    posted = slack.calls_to("chat.postMessage")
    assert posted and posted[0]["thread_ts"] == "111.222"
    final = (slack.calls_to("chat.update") or posted)[-1]
    assert final["blocks"][-1]["type"] == "context"
    slack.calls.clear()
    await reply(slack, switch).text("next reply")
    assert not slack.calls_to("chat.startStream")


async def test_a_mid_stream_failure_moves_only_this_reply(slack: FakeSlack) -> None:
    slack.responses["chat.appendStream"] = [{"ok": False, "error": "msg_too_long"}, {"ok": True}]
    switch = StreamingSwitch()
    sink = reply(slack, switch)
    await sink.text("a" * 300)
    await sink.text("b" * 300)
    await sink.finish([], None)
    assert switch.enabled is True
    assert slack.calls_to("chat.postMessage")


async def test_updates_are_debounced(slack: FakeSlack, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sinks, "DEBOUNCE_SECONDS", 0.05)
    sink = sinks.UpdateSink(slack, channel=CHANNEL, thread_ts="111.222")
    for _ in range(20):
        await sink.text("word ")
    await asyncio.sleep(0.2)
    assert len(slack.calls_to("chat.postMessage")) + len(slack.calls_to("chat.update")) <= 2
