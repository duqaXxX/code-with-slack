import asyncio
from typing import Any

import aiohttp
import pytest

from code_with_slack import texts
from code_with_slack.render import sinks
from code_with_slack.render.renderer import STOPPED, TaskUpdate
from code_with_slack.render.sinks import ReplySink
from tests.fakes import CHANNEL, FakeSlack


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sinks, "DEBOUNCE_SECONDS", 0.01)


def writes(slack: FakeSlack) -> list[dict[str, Any]]:
    return [a for m, a in slack.calls if m in ("chat.postMessage", "chat.update")]


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
    assert all(a["ts"] == slack.posted_ts[0] for a in slack.calls_to("chat.update"))
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
    (body,) = slack.message_texts()
    assert body == "First I list the files.\n\n✓ `Bash: ls`\n\nThen I read the README."


async def test_text_that_already_breaks_a_paragraph_gets_no_extra_blank_line(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.text("Checking.\n\n")
    await sink.task(TaskUpdate("t1", "Bash: ls", "complete"))
    await sink.task(TaskUpdate("t2", "Read: README.md", "complete"))
    await sink.text("\nDone.")
    await sink.finish([], None)
    (body,) = slack.message_texts()
    assert body == "Checking.\n\n✓ `Bash: ls`\n✓ `Read: README.md`\n\nDone."


async def test_a_running_tool_and_the_writing_line_show_until_the_end(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Running the tests.")
    await sink.task(TaskUpdate("t1", "Bash: pytest", "in_progress"))
    await asyncio.sleep(0.05)
    shown = last_blocks(slack)
    assert "… `Bash: pytest`" in shown[0]["text"]
    assert shown[-1]["elements"][0]["text"] == texts.WRITING
    await sink.finish([TaskUpdate("t1", "Bash: pytest", "complete")], "footer")
    final = last_blocks(slack)
    assert "✓ `Bash: pytest`" in final[0]["text"]
    assert all(texts.WRITING not in str(b) for b in final)


async def test_a_failed_tool_shows_its_output(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.task(TaskUpdate("t1", "Bash: ls missing", "error", output="Exit code 1"))
    await sink.finish([], None)
    assert slack.message_texts() == ["✗ `Bash: ls missing` · Exit code 1"]


async def test_a_stopped_tool_says_so(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.task(TaskUpdate("t1", "Bash: sleep 20", "complete", output=STOPPED))
    await sink.finish([], None)
    assert slack.message_texts() == ["✓ `Bash: sleep 20` · Stopped"]


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


async def test_a_divider_separates_the_reply_from_its_footer(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Done.")
    await sink.finish([], "main · ctx 6%")
    assert [b["type"] for b in last_blocks(slack)] == ["markdown", "divider", "context"]


async def test_opening_shows_the_status_before_any_content(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.open(texts.WAITING)
    assert last_blocks(slack) == [sinks.context_block(texts.WAITING)]
    await sink.announce(texts.WRITING)
    assert last_blocks(slack) == [sinks.context_block(texts.WRITING)]
    await sink.text("Hello.")
    await sink.finish([], None)
    assert len(slack.calls_to("chat.postMessage")) == 1


async def test_the_running_list_sits_at_the_end_of_a_finished_reply(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Started it.")
    await sink.finish([], "footer")
    await sink.set_running(["… `Bash: sleep 60`"])
    shown = last_blocks(slack)  # written at once: no rewrite is scheduled after the end
    assert [b["type"] for b in shown] == ["markdown", "context", "divider", "context"]
    assert shown[1] == sinks.context_block(texts.RUNNING.format(count=1) + "\n… `Bash: sleep 60`")
    await sink.set_running([])
    assert [b["type"] for b in last_blocks(slack)] == ["markdown", "divider", "context"]
    assert len(slack.calls_to("chat.postMessage")) == 1


async def test_the_running_list_sits_above_the_status_while_writing(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Working.")
    await sink.set_running(["… `Agent: review`", "… `Bash: sleep 60`"])
    await asyncio.sleep(0.05)
    shown = last_blocks(slack)
    assert [b["type"] for b in shown] == ["markdown", "context", "context"]
    assert shown[1]["elements"][0]["text"].startswith(texts.RUNNING.format(count=2))
    assert shown[2] == sinks.context_block(texts.WRITING)


async def test_an_unchanged_running_list_writes_nothing(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.finish([], "footer")
    before = len(writes(slack))
    await sink.set_running([])
    assert len(writes(slack)) == before


async def test_closing_several_lines_writes_the_reply_once(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Working.")
    await asyncio.sleep(0.05)
    before = len(writes(slack))
    await sink.finish(
        [TaskUpdate(f"t{i}", f"Bash: step {i}", "complete") for i in range(5)], "footer"
    )
    assert len(writes(slack)) == before + 1


async def test_text_after_the_end_keeps_the_footer(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Done.")
    await sink.finish([], "footer")
    await sink.text("\n\nA late error.")
    await asyncio.sleep(0.05)
    shown = last_blocks(slack)
    assert "A late error." in shown[0]["text"]
    assert shown[-1] == sinks.context_block("footer")
