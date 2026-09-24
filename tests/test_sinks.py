import asyncio
from typing import Any

import aiohttp
import pytest
from slack_sdk.errors import SlackApiError

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
    assert last_blocks(slack)[-2] == {
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
    assert last_blocks(slack)[-2]["elements"][0]["text"] == "footer"


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
    blocks = last_blocks(slack)
    assert [b["type"] for b in blocks] == ["markdown", "context", "divider", "context", "context"]
    assert (
        blocks[1] == sinks.SPACER_ABOVE and blocks[-1] == sinks.SPACER_BELOW
    )  # an empty line before and after the footer


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
    assert [b["type"] for b in shown] == ["markdown", "context", "divider", "context", "context"]
    assert shown[-2] == sinks.context_block("footer · ⏳ 1 shell")
    await sink.set_running("")
    assert last_blocks(slack)[-2] == sinks.context_block("footer")
    assert len(slack.calls_to("chat.postMessage")) == 1


async def test_running_counts_stand_alone_when_a_reply_has_no_footer(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("The command finished.")
    await sink.finish([], None)
    assert [b["type"] for b in last_blocks(slack)] == ["markdown"]
    await sink.set_running("⏳ 1 agent")
    assert last_blocks(slack)[1:] == [
        sinks.SPACER_ABOVE,
        {"type": "divider"},
        sinks.context_block("⏳ 1 agent"),
        sinks.SPACER_BELOW,
    ]


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
    assert shown[-2] == sinks.context_block("footer")


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


async def test_a_tool_block_stays_under_slack_s_limit_once_escaped(slack: FakeSlack) -> None:
    sink = reply(slack)
    for i in range(60):
        await sink.task(
            TaskUpdate(f"t{i}", f"Bash: {i} 2>&1 && a <b> & c" * 2, "error", name="Bash")
        )
    await sink.finish([], None)
    contexts = [b for b in last_blocks(slack) if b["type"] == "context"]
    assert contexts and all(len(b["elements"][0]["text"]) <= 3000 for b in contexts)


async def test_an_empty_finished_reply_that_is_no_longer_latest_goes(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.open(texts.WRITING)
    await sink.finish([], "footer")
    await sink.set_latest(False)
    assert [a["ts"] for a in slack.calls_to("chat.delete")] == [slack.posted_ts[0]]


async def test_every_block_id_in_a_message_is_unique(slack: FakeSlack) -> None:
    # Slack refuses a message whose blocks repeat a block_id (invalid_blocks, seen live).
    sink = reply(slack)
    await sink.text("Text.")
    await sink.task(tool("a", "Read"))
    await sink.text("More text.")
    await sink.task(tool("b", "Bash", "error", output="boom"))
    await sink.finish([], "footer")
    for _, args in slack.calls:
        ids = [b["block_id"] for b in args.get("blocks") or [] if "block_id" in b]
        assert len(ids) == len(set(ids)), ids


def rejected(error: str) -> SlackApiError:
    """Slack's answer to a refused write: the envelope of the chat.update reference (read
    2026-09-25), `ok` false and an error code."""
    return SlackApiError(error, {"ok": False, "error": error})


async def test_a_refused_final_write_is_retried_as_plain_text(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.open(texts.WRITING)
    await sink.text("All **done**.")
    await sink.task(TaskUpdate("t1", "Bash: ls", "complete"))
    slack.responses["chat.update"] = [rejected("invalid_blocks"), {"ok": True}]
    await sink.finish([], "main · ctx 6%")
    final = writes(slack)[-1]
    # No blocks: Slack then renders the text and drops the old ones, "Claude is writing…" too.
    assert final["blocks"] == []
    assert final["text"] == "All **done**.\n\nmain · ctx 6%"


async def test_a_refused_draft_write_is_not_retried(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.open(texts.WRITING)
    slack.responses["chat.update"] = rejected("invalid_blocks")
    await sink.text("partial")
    await asyncio.sleep(0.05)
    assert all(w.get("blocks") != [] for w in writes(slack))


async def test_a_network_failure_on_the_final_write_is_not_retried(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.open(texts.WRITING)
    await sink.text("Done.")
    slack.responses["chat.update"] = aiohttp.ClientConnectionError("network down")
    await sink.finish([], "footer")
    assert all(w.get("blocks") != [] for w in writes(slack))


async def test_a_plain_retry_still_finishes_the_reply_s_later_messages(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.task(TaskUpdate("t1", "Read: notes.md", "complete", name="Read"))
    await sink.text("x" * 15_000)  # the status line ends up on a later message
    await asyncio.sleep(0.05)
    assert len(slack.posted_ts) > 1
    # Folding changes the first message only; Slack refuses that write once.
    slack.responses["chat.update"] = [rejected("invalid_blocks"), {"ok": True}]
    await sink.finish([], "main · ctx 6%")
    last_write = {w.get("ts") or w.get("channel"): w for w in slack.calls_to("chat.update")}
    final = last_write[slack.posted_ts[-1]]
    assert texts.WRITING not in str(final)
    assert "main · ctx 6%" in str(final)


async def test_a_rate_limited_final_write_is_not_turned_into_plain_text(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.open(texts.WRITING)
    await sink.text("Done.")
    slack.responses["chat.update"] = rejected("ratelimited")
    await sink.finish([], "footer")
    assert all(w.get("blocks") != [] for w in writes(slack))
