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
    assert "… `Bash: pytest`" in slack.message_texts()[0]
    assert shown[-1]["elements"][0]["text"] == texts.WRITING
    await sink.finish([TaskUpdate("t1", "Bash: pytest", "complete")], "footer")
    final = last_blocks(slack)
    assert "✓ `Bash: pytest`" in slack.message_texts()[0]
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


async def test_running_counts_join_the_footer_of_a_finished_reply(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Started it.")
    await sink.finish([], "footer")
    await sink.set_running("⏳ 1 shell")
    shown = last_blocks(slack)  # written at once: no rewrite is scheduled after the end
    assert [b["type"] for b in shown] == ["markdown", "divider", "context"]
    assert shown[-1] == sinks.context_block("footer · ⏳ 1 shell")
    await sink.set_running("")
    assert last_blocks(slack)[-1] == sinks.context_block("footer")
    assert len(slack.calls_to("chat.postMessage")) == 1


async def test_running_counts_stand_alone_when_a_reply_has_no_footer(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("The command finished.")
    await sink.finish([], None)
    assert [b["type"] for b in last_blocks(slack)] == ["markdown"]
    await sink.set_running("⏳ 1 agent")
    assert last_blocks(slack)[1:] == [{"type": "divider"}, sinks.context_block("⏳ 1 agent")]


async def test_running_counts_follow_the_status_while_writing(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Working.")
    await sink.set_running("⏳ 2 agents")
    await asyncio.sleep(0.05)
    assert last_blocks(slack)[-1] == sinks.context_block(f"{texts.WRITING} · ⏳ 2 agents")


async def test_unchanged_running_counts_write_nothing(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.finish([], "footer")
    before = len(writes(slack))
    await sink.set_running("")
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


def tool(id: str, name: str, status: str = "complete", **fields: Any) -> TaskUpdate:
    return TaskUpdate(id, f"{name}: {id}", status, name=name, **fields)  # type: ignore[arg-type]


async def test_finished_tool_lines_collapse_into_one(slack: FakeSlack) -> None:
    sink = reply(slack)
    for update in [tool("a", "Bash"), tool("b", "Read"), tool("c", "Read"), tool("d", "Grep")]:
        await sink.task(update)
    await sink.text("Done.")
    await sink.finish([], None)
    assert slack.message_texts() == ["✓ Bash · Read \u00d72 · Grep\n\nDone."]


async def test_running_failed_and_task_lines_stay_whole(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.task(tool("a", "Bash"))
    await sink.task(tool("b", "Bash", "in_progress"))
    await sink.task(tool("c", "Edit", "error", output="old_string not found"))
    await sink.task(tool("d", "Agent", task=True))
    await sink.task(tool("e", "Read"))
    await sink.finish([], None)
    assert slack.message_texts() == [
        "✓ Bash\n… `Bash: b`\n✗ `Edit: c` · old_string not found\n✓ `Agent: d`\n✓ Read"
    ]


async def test_lines_fold_only_once_the_reply_is_finished(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.task(tool("a", "Read"))
    await sink.task(tool("b", "Read"))
    await asyncio.sleep(0.05)
    assert slack.message_texts() == ["✓ `Read: a`\n✓ `Read: b`"]  # nothing moves while it works
    await sink.finish([], None)
    assert slack.message_texts() == ["✓ Read \u00d72"]


async def test_a_reply_that_shrinks_on_folding_removes_its_extra_message(slack: FakeSlack) -> None:
    slack.responses["chat.postMessage"] = [{"ok": True, "ts": "1.1"}, {"ok": True, "ts": "2.2"}]
    sink = reply(slack)
    for i in range(300):  # two messages while whole, one line once folded
        await sink.task(TaskUpdate(f"t{i}", f"Read: {'x' * 40}{i}", "complete", name="Read"))
    await asyncio.sleep(0.05)
    assert len(slack.calls_to("chat.postMessage")) == 2
    await sink.finish([], "footer")
    assert [a["ts"] for a in slack.calls_to("chat.delete")] == ["2.2"]
    assert slack.message_texts()[0] == "✓ Read \u00d7300"


async def test_tool_lines_are_secondary_text_and_claude_s_words_are_not(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Looking.")
    await sink.task(tool("a", "Read"))
    await sink.text("Found it.")
    await sink.finish([], None)
    blocks = last_blocks(slack)
    assert [b["type"] for b in blocks] == ["markdown", "context", "markdown"]
    assert blocks[1]["elements"][0]["text"] == "✓ Read"


async def test_tool_lines_escape_what_slack_mrkdwn_reads_as_markup(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.task(TaskUpdate("t", "Bash: a < b && c > d", "in_progress", name="Bash"))
    await sink.finish([], None)
    assert last_blocks(slack)[0]["elements"][0]["text"] == "… `Bash: a &lt; b &amp;&amp; c &gt; d`"


async def test_a_reply_that_is_no_longer_the_latest_drops_its_footer(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Done.")
    await sink.finish([], "footer")
    await sink.set_running("⏳ 1 shell")
    await sink.set_latest(False)
    assert [b["type"] for b in last_blocks(slack)] == ["markdown"]
