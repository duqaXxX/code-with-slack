import asyncio
from typing import Any

import aiohttp
import pytest

from code_with_slack.render import sinks
from code_with_slack.render.renderer import TaskUpdate
from code_with_slack.render.sinks import ReplySink
from tests.fakes import CHANNEL, FakeSlack


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sinks, "DEBOUNCE_SECONDS", 0.01)


def writes(slack: FakeSlack) -> list[dict[str, Any]]:
    return [a for m, a in slack.calls if m in ("chat.postMessage", "chat.update")]


def bodies(slack: FakeSlack) -> dict[str, str]:
    """The last markdown body written to each message, by message ts."""
    posted_ts = slack.responses["chat.postMessage"]["ts"]
    out: dict[str, str] = {}
    for method, args in slack.calls:
        if method in ("chat.postMessage", "chat.update"):
            ts = args.get("ts") or posted_ts
            out[ts] = next(b["text"] for b in args["blocks"] if b["type"] == "markdown")
    return out


def last_blocks(slack: FakeSlack) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = writes(slack)[-1]["blocks"]
    return blocks


def reply(slack: FakeSlack) -> ReplySink:
    return ReplySink(slack, channel=CHANNEL)


async def test_a_reply_is_one_message_in_the_main_window(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Looking at the files.")
    await sink.task(TaskUpdate("t1", "Bash: ls", "in_progress"))
    await asyncio.sleep(0.05)
    await sink.task(TaskUpdate("t1", "Bash: ls", "complete"))
    await sink.finish([], "main · ctx 6%")
    posts = slack.calls_to("chat.postMessage")
    assert len(posts) == 1 and posts[0].get("thread_ts") is None
    assert all(
        a["ts"] == slack.responses["chat.postMessage"]["ts"] for a in slack.calls_to("chat.update")
    )
    assert last_blocks(slack)[-1] == {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "main · ctx 6%"}],
    }


async def test_tool_lines_sit_where_they_happen(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("First I list the files.")
    await sink.task(TaskUpdate("t1", "Bash: ls", "in_progress"))
    await sink.task(TaskUpdate("t1", "Bash: ls", "complete"))
    await sink.text("Then I read the README.")
    await sink.finish([], None)
    body = next(iter(bodies(slack).values()))
    assert body == "First I list the files.\n✓ `Bash: ls`\nThen I read the README."


async def test_a_running_tool_and_the_writing_line_show_until_the_end(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Running the tests.")
    await sink.task(TaskUpdate("t1", "Bash: pytest", "in_progress"))
    await asyncio.sleep(0.05)
    shown = last_blocks(slack)
    assert "… `Bash: pytest`" in shown[0]["text"]
    assert shown[-1]["elements"][0]["text"] == sinks.WRITING
    await sink.finish([TaskUpdate("t1", "Bash: pytest", "complete")], "footer")
    final = last_blocks(slack)
    assert "✓ `Bash: pytest`" in final[0]["text"]
    assert all(sinks.WRITING not in str(b) for b in final)


async def test_a_failed_tool_shows_its_output(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.task(TaskUpdate("t1", "Bash: ls missing", "error", output="Exit code 1"))
    await sink.finish([], None)
    assert next(iter(bodies(slack).values())) == "✗ `Bash: ls missing` · Exit code 1"


async def test_a_long_reply_continues_in_a_new_message(slack: FakeSlack) -> None:
    slack.responses["chat.postMessage"] = [
        {"ok": True, "ts": "1.1"},
        {"ok": True, "ts": "2.2"},
        {"ok": True, "ts": "3.3"},
    ]
    text = "\n".join(f"line {i:05d} " + "x" * 90 for i in range(300))
    sink = reply(slack)
    await sink.text(text)
    await sink.finish([], "footer")
    posts = slack.calls_to("chat.postMessage")
    assert len(posts) >= 3
    chunks = [next(b["text"] for b in p["blocks"] if b["type"] == "markdown") for p in posts]
    assert all(len(c) <= sinks.MESSAGE_LIMIT for c in chunks)
    assert "\n".join(chunks) == text
    assert last_blocks(slack)[-1]["elements"][0]["text"] == "footer"


async def test_updates_are_debounced(slack: FakeSlack, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sinks, "DEBOUNCE_SECONDS", 0.05)
    sink = reply(slack)
    for _ in range(20):
        await sink.text("word ")
    await asyncio.sleep(0.2)
    assert len(writes(slack)) <= 2


async def test_a_slack_failure_never_raises(slack: FakeSlack) -> None:
    for method in ("chat.postMessage", "chat.update"):
        slack.responses[method] = aiohttp.ClientConnectionError("network down")
    sink = reply(slack)
    await sink.text("hello")
    await sink.task(TaskUpdate("t1", "Bash: ls", "in_progress"))
    await asyncio.sleep(0.05)
    await sink.finish([], "footer")
