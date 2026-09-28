import asyncio
import dataclasses
import itertools
import time
from dataclasses import replace
from typing import Any

import aiohttp
import pytest
from claude_agent_sdk.types import TaskNotificationMessage, TaskStartedMessage
from slack_sdk.errors import SlackApiError

from code_with_slack import texts
from code_with_slack.render import sinks
from code_with_slack.render.escape import mrkdwn_escape
from code_with_slack.render.renderer import STOPPED, TaskUpdate, TurnRenderer
from code_with_slack.render.sinks import ReplySink, UpdateLimiter
from tests.fakes import CHANNEL, THREAD, FakeSlack, sdk_messages


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sinks, "DEBOUNCE_SECONDS", 0.01)


def writes(slack: FakeSlack) -> list[dict[str, Any]]:
    return [a for m, a in slack.calls if m in ("chat.postMessage", "chat.update")]


def last_blocks(slack: FakeSlack) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = writes(slack)[-1]["blocks"]
    return blocks


def reply(slack: FakeSlack, *, limiter: UpdateLimiter | None = None) -> ReplySink:
    return ReplySink(slack, channel=CHANNEL, thread_ts=THREAD, limiter=limiter or UpdateLimiter())


async def test_a_reply_s_body_is_one_message_and_the_closing_message_follows(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.text("Looking at the files.")
    await sink.task(TaskUpdate("t1", "Bash: ls", "in_progress"))
    await asyncio.sleep(0.05)
    await sink.task(TaskUpdate("t1", "Bash: ls", "complete"))
    await sink.finish([])
    await sink.close_out("main · ctx 6%")
    posts = slack.calls_to("chat.postMessage")
    # the body, then the closing message that carries the footer; both post in the thread.
    assert len(posts) == 2 and all(p.get("thread_ts") == THREAD for p in posts)
    assert all(a["ts"] == slack.posted_ts[0] for a in slack.calls_to("chat.update"))
    assert last_blocks(slack)[-2] == {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "main · ctx 6%"}],
    }


async def test_finish_alone_posts_no_closing_message(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Done.")
    await sink.finish([])
    # the body writes once; the closing message (D1) is close_out's job, not finish's, since a
    # task that outlived the turn can still keep this reply open.
    assert len(slack.calls_to("chat.postMessage")) == 1
    await sink.close_out("footer")
    assert len(slack.calls_to("chat.postMessage")) == 2


async def test_a_second_close_out_call_is_a_no_op(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Done.")
    await sink.finish([])
    await sink.close_out("footer", reply_to="the question?")
    posts = len(slack.calls_to("chat.postMessage"))
    await sink.close_out("a later footer", reply_to="a later question")
    # the closing already posted: a later call changes nothing, whatever it is called with.
    assert len(slack.calls_to("chat.postMessage")) == posts
    assert slack.calls_to("chat.postMessage")[-1]["text"] == "Reply to: the question?"


async def test_a_silent_close_that_would_overflow_the_block_limit_stays_in_one_message(
    slack: FakeSlack,
) -> None:
    # D1: a silent close's footer joins the body's own last message; BLOCKS_LIMIT
    # (45) leaves exactly the margin under Slack's own 50-block cap for that, so a message
    # already at BLOCKS_LIMIT still fits the closing blocks without a message of its own.
    sink = reply(slack)
    await sink.open(texts.WRITING)
    sink._blocks = lambda: [  # type: ignore[method-assign]
        {"type": "markdown", "text": f"x{i}"} for i in range(sinks.BLOCKS_LIMIT)
    ]
    await sink.finish([])
    posts = len(slack.calls_to("chat.postMessage"))
    await sink.set_running("⏳ 1 shell")
    await sink.close_out("footer", "question", silent=True)
    assert len(slack.calls_to("chat.postMessage")) == posts  # no message of its own
    last = slack.calls_to("chat.update")[-1]["blocks"]
    assert len(last) <= 50
    assert {"type": "divider"} in last
    # a later non-silent close_out is a no-op.
    await sink.close_out("footer", "question")
    assert len(slack.calls_to("chat.postMessage")) == posts
    # a running-count change afterward still keeps the footer in the same, last message.
    await sink.set_running("")
    await asyncio.sleep(0.8)
    assert {"type": "divider"} in slack.calls_to("chat.update")[-1]["blocks"]


async def test_tool_lines_sit_where_they_happen(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("First I list the files.")
    await sink.task(TaskUpdate("t1", "Bash: ls", "in_progress"))
    await sink.task(TaskUpdate("t1", "Bash: ls", "complete"))
    await sink.text("Then I read the README.")
    await sink.finish([])
    await sink.close_out(None)
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
    await sink.finish([])
    await sink.close_out(None)
    (body,) = slack.message_texts()
    assert body == "Checking.\n\n✓ `Bash: ls`\n✓ `Read: README.md`\n\nDone."


async def test_a_running_tool_and_the_writing_line_show_until_the_end(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Running the tests.")
    await sink.task(TaskUpdate("t1", "Bash: pytest", "in_progress"))
    await asyncio.sleep(0.05)
    shown = last_blocks(slack)
    assert slack.message_texts()[0].endswith("\n⏳ `Bash: pytest`")  # still running
    assert shown[-1]["elements"][0]["text"] == texts.WRITING
    await sink.finish([TaskUpdate("t1", "Bash: pytest", "complete")])
    await sink.close_out("footer")
    final = last_blocks(slack)
    assert "✓ `Bash: pytest`" in slack.message_texts()[0]
    assert all(texts.WRITING not in str(b) for b in final)


async def test_a_failed_tool_shows_its_output(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.task(TaskUpdate("t1", "Bash: ls missing", "error", output="Exit code 1"))
    await sink.finish([])
    await sink.close_out(None)
    assert slack.message_texts() == ["✗ `Bash: ls missing` · Exit code 1"]


async def test_a_stopped_tool_says_so(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.task(TaskUpdate("t1", "Bash: sleep 20", "complete", output=STOPPED))
    await sink.finish([])
    await sink.close_out(None)
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
    await sink.finish([])
    await sink.close_out("footer")
    posts = slack.calls_to("chat.postMessage")
    # the body's messages carry markdown; the closing message that follows them does not.
    body_posts = [p for p in posts if any(b["type"] == "markdown" for b in p["blocks"])]
    assert len(body_posts) >= 3
    chunks = [next(b["text"] for b in p["blocks"] if b["type"] == "markdown") for p in body_posts]
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
    await sink.finish([])
    await sink.close_out("footer")


async def test_a_divider_separates_the_closing_message_from_the_footer(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Done.")
    await sink.finish([])
    await sink.close_out("main · ctx 6%")
    body, closing = slack.message_blocks()
    assert [b["type"] for b in body] == ["markdown"]  # the footer never sits in the body
    assert [b["type"] for b in closing] == ["context", "divider", "context", "context"]
    assert (
        closing[0] == sinks.SPACER_ABOVE and closing[-1] == sinks.SPACER_BELOW
    )  # an empty line before and after the footer
    assert closing[2] == sinks.context_block("main · ctx 6%")


async def test_opening_shows_the_status_before_any_content(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.open(texts.WAITING)
    assert last_blocks(slack) == [sinks.context_block(texts.WAITING)]
    await sink.announce(texts.WRITING)
    assert last_blocks(slack) == [sinks.context_block(texts.WRITING)]
    await sink.text("Hello.")
    await sink.finish([])
    await sink.close_out(None)
    assert len(slack.calls_to("chat.postMessage")) == 1


async def test_running_counts_join_the_footer_of_a_finished_reply(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Started it.")
    await sink.finish([])
    await sink.close_out("footer")
    await sink.set_running("⏳ 1 shell")
    shown = last_blocks(slack)  # written at once: no rewrite is scheduled after the end
    assert [b["type"] for b in shown] == ["context", "divider", "context", "context"]
    assert shown[-2] == sinks.context_block("footer · ⏳ 1 shell")
    await sink.set_running("")
    assert last_blocks(slack)[-2] == sinks.context_block("footer")
    # the body posted once, the closing message once: running counts only ever update it.
    assert len(slack.calls_to("chat.postMessage")) == 2


async def test_running_counts_stand_alone_when_a_reply_has_no_footer(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("The command finished.")
    await sink.finish([])
    await sink.close_out(None)
    assert [b["type"] for b in last_blocks(slack)] == ["markdown"]
    # nothing to show yet: no closing message exists until something (here, a running count) does.
    assert len(slack.calls_to("chat.postMessage")) == 1
    await sink.set_running("⏳ 1 agent")
    assert last_blocks(slack) == [
        sinks.SPACER_ABOVE,
        {"type": "divider"},
        sinks.context_block("⏳ 1 agent"),
        sinks.SPACER_BELOW,
    ]
    assert len(slack.calls_to("chat.postMessage")) == 2  # the closing message now exists


async def test_running_counts_follow_the_status_while_writing(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Working.")
    await sink.set_running("⏳ 2 agents")
    await asyncio.sleep(0.05)
    assert last_blocks(slack)[-1] == sinks.context_block(f"{texts.WRITING} · ⏳ 2 agents")


async def test_unchanged_running_counts_write_nothing(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.finish([])
    await sink.close_out("footer")
    before = len(writes(slack))
    await sink.set_running("")
    assert len(writes(slack)) == before


async def test_closing_several_lines_writes_the_body_once(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Working.")
    await asyncio.sleep(0.05)
    before = len(writes(slack))
    await sink.finish([TaskUpdate(f"t{i}", f"Bash: step {i}", "complete") for i in range(5)])
    await sink.close_out("footer")
    # one write for the body's final form, whatever the number of lines closing it, and one more
    # to post the closing message: never one write per line.
    assert len(writes(slack)) == before + 2


async def test_text_after_the_end_appends_to_the_body_and_leaves_the_closing_message(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.text("Done.")
    await sink.finish([])
    await sink.close_out("footer")
    posts_before = len(slack.calls_to("chat.postMessage"))  # the body, then the closing message
    await sink.text("\n\nA late error.")
    await asyncio.sleep(0.05)
    body, closing = slack.message_blocks()
    assert "A late error." in body[0]["text"]
    assert closing[2] == sinks.context_block("footer")
    assert len(slack.calls_to("chat.postMessage")) == posts_before  # the closing never re-rings


def tool(id: str, name: str, status: str = "complete", **fields: Any) -> TaskUpdate:
    return TaskUpdate(id, f"{name}: {id}", status, name=name, **fields)  # type: ignore[arg-type]


async def test_finished_tool_lines_collapse_into_one(slack: FakeSlack) -> None:
    sink = reply(slack)
    for update in [tool("a", "Bash"), tool("b", "Read"), tool("c", "Read"), tool("d", "Grep")]:
        await sink.task(update)
    await sink.text("Done.")
    await sink.finish([])
    await sink.close_out(None)
    assert slack.message_texts() == ["✓ Ran 1 shell command · Read 2 files · Grep\n\nDone."]


async def test_failed_calls_are_counted_and_running_task_and_stopped_lines_stay_whole(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.task(tool("a", "Bash"))
    await sink.task(tool("b", "Bash", "in_progress"))
    await sink.task(tool("c", "Edit", "error", output="old_string not found"))
    await sink.task(tool("d", "Agent", task=True))
    await sink.task(tool("e", "Read"))
    await sink.task(tool("f", "Bash", "error", output="Exit code 1"))
    await sink.task(tool("g", "Grep", output=STOPPED))
    await sink.finish([])
    await sink.close_out(None)
    assert slack.message_texts() == [
        "✓ Ran 1 shell command · Read 1 file · ✗ Edit · Ran 1 shell command\n"
        "⏳ `Bash: b`\n✓ `Agent: d`\n✓ `Grep: g` · Stopped"
    ]


async def test_a_recorded_turn_counts_its_failed_calls_in_one_line(slack: FakeSlack) -> None:
    # tool-error.jsonl: a Read of a missing file, then a Bash command that exits 1 (CLI 2.1.283).
    sink = reply(slack)
    renderer = TurnRenderer(sink)
    for message in sdk_messages("tool-error"):
        await renderer.feed(message)
    await renderer.close(None)
    tools = [b for b in last_blocks(slack) if str(b.get("block_id", "")).startswith("tools-")]
    assert [sinks.block_text(b) for b in tools] == ["✗ Read 1 file · Ran 1 shell command"]


async def test_a_long_command_in_the_foreground_folds_once_it_ends(slack: FakeSlack) -> None:
    sink = reply(slack)
    renderer = TurnRenderer(sink)
    for message in sdk_messages("foreground"):
        await renderer.feed(message)
    await renderer.close(None)
    tools = [b for b in last_blocks(slack) if str(b.get("block_id", "")).startswith("tools-")]
    assert [sinks.block_text(b) for b in tools] == ["✓ Ran 1 shell command"]


async def tool_texts_of(slack: FakeSlack, name: str) -> list[str]:
    """The tool blocks of a recorded turn, rendered through a reply and closed."""
    renderer = TurnRenderer(reply(slack))
    for message in sdk_messages(name):
        await renderer.feed(message)
    await renderer.close(None)
    blocks = last_blocks(slack)
    return [sinks.block_text(b) for b in blocks if str(b.get("block_id", "")).startswith("tools-")]


async def test_a_subagent_in_the_foreground_keeps_its_line_once_it_ends(slack: FakeSlack) -> None:
    # subagent-foreground.jsonl (CLI 2.1.283): the agent's task ends before the Agent call's result.
    (text,) = await tool_texts_of(slack, "subagent-foreground")
    assert text.startswith("✓ `Agent: ") and text.endswith("` · 1 call")  # it ran one ls


async def test_a_skill_in_a_forked_context_shows_its_calls_like_a_subagent(
    slack: FakeSlack,
) -> None:
    # skill-fork.jsonl (CLI 2.1.283): a `context: fork` skill's calls carry the Skill call's id.
    (text,) = await tool_texts_of(slack, "skill-fork")
    assert text == "✓ `Skill: list-files` · 2 calls"


async def test_an_agent_inside_a_command_shows_on_the_command_s_line(slack: FakeSlack) -> None:
    # skill-fork-command.jsonl, with an agent the skill starts inside it: a task whose
    # tool_use_id names a call the stream never carried (seen live with `!code-review`).
    messages = sdk_messages("skill-fork-command")
    started = next(m for m in messages if isinstance(m, TaskStartedMessage))
    ended = next(m for m in messages if isinstance(m, TaskNotificationMessage))
    inner = {"task_id": "inner", "tool_use_id": "toolu_inner"}
    renderer = TurnRenderer(reply(slack))
    await renderer.feed(started)
    await renderer.feed(dataclasses.replace(started, **inner, description="Run the tests"))
    await asyncio.sleep(0.05)
    assert slack.message_texts() == [f"⏳ `/list-files` · 1 call\n{sinks.NESTED}Run the tests"]
    await renderer.feed(dataclasses.replace(ended, **inner))
    for message in messages[messages.index(started) + 1 :]:
        await renderer.feed(message)
    await renderer.close(None)
    assert slack.message_texts()[0].startswith("✓ `/list-files` · 1 call\n\n")  # then the text


async def test_a_running_subagent_counts_its_calls_before_its_latest(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.task(
        tool("a", "Agent", "in_progress", details="Read: x\nBash: ls", calls=3, task=True)
    )
    await asyncio.sleep(0.05)
    assert slack.message_texts() == [f"⏳ `Agent: a` · 3 calls\n{sinks.NESTED}Bash: ls"]


async def test_a_stopped_command_keeps_its_line_and_is_not_a_failure(slack: FakeSlack) -> None:
    # interrupt.jsonl: the task ends as stopped, then the result reports the rejection.
    (text,) = await tool_texts_of(slack, "interrupt")
    assert text.startswith("✓ `Bash: ") and text.endswith("· Stopped")


async def test_lines_fold_while_the_turn_runs(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.task(tool("a", "Read"))
    await sink.task(tool("b", "Bash", "error", output="Exit code 1"))
    await sink.task(tool("c", "Read", "in_progress"))
    await asyncio.sleep(0.05)
    assert slack.message_texts() == ["✓ Read 1 file · ✗ Ran 1 shell command\n⏳ `Read: c`"]
    await sink.task(tool("c", "Read"))  # it ended, and it is still the last call: still shown
    await asyncio.sleep(0.05)
    assert slack.message_texts() == ["✓ Read 1 file · ✗ Ran 1 shell command\n`Read: c`"]
    await sink.task(tool("d", "Bash", "in_progress"))  # a new last call: the previous one folds
    await asyncio.sleep(0.05)
    assert slack.message_texts() == ["✓ Read 2 files · ✗ Ran 1 shell command\n⏳ `Bash: d`"]


async def test_the_last_call_keeps_its_icon_and_output_only_when_it_failed(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.task(tool("a", "Read"))
    await sink.task(tool("b", "Bash", "error", output="Exit code 1"))
    await asyncio.sleep(0.05)
    assert slack.message_texts() == ["✓ Read 1 file\n✗ `Bash: b` · Exit code 1"]


async def test_the_last_call_folds_once_claude_writes_or_the_reply_ends(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.task(tool("a", "Read"))
    await sink.task(tool("b", "Read"))
    await asyncio.sleep(0.05)
    assert slack.message_texts() == ["✓ Read 1 file\n`Read: b`"]
    await sink.text("Found it.")  # the run is closed: nothing in it is the last call any more
    await asyncio.sleep(0.05)
    assert slack.message_texts() == ["✓ Read 2 files\n\nFound it."]
    await sink.task(tool("c", "Bash"))
    await sink.finish([])
    await sink.close_out(None)
    assert slack.message_texts() == ["✓ Read 2 files\n\nFound it.\n\n✓ Ran 1 shell command"]


async def test_a_reply_that_shrinks_as_calls_end_removes_its_extra_message(
    slack: FakeSlack,
) -> None:
    slack.responses["chat.postMessage"] = [{"ok": True, "ts": "1.1"}, {"ok": True, "ts": "2.2"}]
    sink = reply(slack)
    running = [
        TaskUpdate(f"t{i}", f"Read: {'x' * 40}{i}", "in_progress", name="Read") for i in range(300)
    ]
    for update in running:  # two messages while they run, one line once they end
        await sink.task(update)
    await asyncio.sleep(0.05)
    ended = [replace(u, status="complete") for u in running]
    assert len(slack.calls_to("chat.postMessage")) == 2
    await sink.finish(ended)
    await sink.close_out("footer")
    assert [a["ts"] for a in slack.calls_to("chat.delete")] == ["2.2"]
    assert slack.message_texts()[0] == "✓ Read 300 files"


async def test_tool_lines_are_secondary_text_and_claude_s_words_are_not(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Looking.")
    await sink.task(tool("a", "Read"))
    await sink.text("Found it.")
    await sink.finish([])
    await sink.close_out(None)
    blocks = last_blocks(slack)
    assert [b["type"] for b in blocks] == ["markdown", "context", "markdown"]
    assert blocks[1]["elements"][0]["text"] == "✓ Read 1 file"


async def test_tool_lines_escape_what_slack_mrkdwn_reads_as_markup(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.task(TaskUpdate("t", "Bash: a < b && c > d", "in_progress", name="Bash"))
    await sink.finish([])
    await sink.close_out(None)
    assert last_blocks(slack)[0]["elements"][0]["text"] == "⏳ `Bash: a &lt; b &amp;&amp; c &gt; d`"


async def test_a_reply_that_is_no_longer_latest_removes_its_closing_message(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.text("Done.")
    await sink.finish([])
    await sink.close_out("footer")
    await sink.set_running("⏳ 1 shell")
    closing_ts = slack.posted_ts[-1]
    await sink.set_latest(False)
    # the body never carried the footer; the closing message, with nothing left to show, goes.
    assert [b["type"] for b in slack.message_blocks()[0]] == ["markdown"]
    assert [a["ts"] for a in slack.calls_to("chat.delete")] == [closing_ts]


async def test_a_tool_block_stays_under_slack_s_limit_once_escaped(slack: FakeSlack) -> None:
    sink = reply(slack)
    for i in range(60):
        await sink.task(
            TaskUpdate(f"t{i}", f"Bash: {i} 2>&1 && a <b> & c" * 2, "error", name="Bash")
        )
    await sink.finish([])
    await sink.close_out(None)
    contexts = [b for b in last_blocks(slack) if b["type"] == "context"]
    assert contexts and all(len(b["elements"][0]["text"]) <= 3000 for b in contexts)


async def test_an_empty_finished_reply_that_is_no_longer_latest_goes(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.open(texts.WRITING)
    await sink.finish([])
    await sink.close_out("footer")
    # the body carries no text or tool line: it goes at once. The footer still shows in a closing
    # message, since the reply is (still) the channel's latest.
    assert [a["ts"] for a in slack.calls_to("chat.delete")] == [slack.posted_ts[0]]
    await sink.set_latest(False)
    # no longer latest: the closing message has nothing left to show either.
    assert [a["ts"] for a in slack.calls_to("chat.delete")] == list(slack.posted_ts)


async def test_every_block_id_in_a_message_is_unique(slack: FakeSlack) -> None:
    # Slack refuses a message whose blocks repeat a block_id (invalid_blocks, seen live).
    sink = reply(slack)
    await sink.text("Text.")
    await sink.task(tool("a", "Read"))
    await sink.text("More text.")
    await sink.task(tool("b", "Bash", "error", output="boom"))
    await sink.finish([])
    await sink.close_out("footer")
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
    await sink.finish([])
    await sink.close_out("main · ctx 6%")
    final = slack.calls_to("chat.update")[-1]  # the body's plain-text fallback, not the closing
    # No blocks: Slack then renders the text and drops the old ones, "Claude is writing…" too.
    # The footer lives in the closing message now, so it is not repeated here.
    assert final["blocks"] == []
    assert final["text"] == "All **done**."


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
    await sink.finish([])
    await sink.close_out("footer")
    assert all(w.get("blocks") != [] for w in writes(slack))


async def test_a_plain_retry_still_finishes_the_reply_s_later_messages(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.task(TaskUpdate("t1", "Read: notes.md", "complete", name="Read"))
    await sink.text("x" * 15_000)  # the status line ends up on a later message
    await asyncio.sleep(0.05)
    assert len(slack.posted_ts) > 1
    continuation_ts = slack.posted_ts[-1]  # the body's second message, before the closing exists
    # Folding changes the first message only; Slack refuses that write once.
    slack.responses["chat.update"] = [rejected("invalid_blocks"), {"ok": True}]
    await sink.finish([])
    await sink.close_out("main · ctx 6%")
    last_write = {w.get("ts") or w.get("channel"): w for w in slack.calls_to("chat.update")}
    final = last_write[continuation_ts]
    assert texts.WRITING not in str(final)
    # the footer now lives in the closing message, posted after the body.
    closing = slack.message_blocks()[-1]
    assert sinks.block_text(closing[2]) == "main · ctx 6%"


async def test_a_rate_limited_final_write_is_not_turned_into_plain_text(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.open(texts.WRITING)
    await sink.text("Done.")
    slack.responses["chat.update"] = rejected("ratelimited")
    await sink.finish([])
    await sink.close_out("footer")
    assert all(w.get("blocks") != [] for w in writes(slack))


async def test_a_final_write_lost_to_the_network_is_tried_again(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 0.01)
    sink = reply(slack)
    await sink.open(texts.WRITING)
    await sink.text("Done.")
    slack.responses["chat.update"] = [aiohttp.ClientConnectionError("network down"), {"ok": True}]
    await sink.finish([])
    await sink.close_out("main · ctx 6%")  # posts its own closing message right away
    await asyncio.sleep(0.05)
    # the retry lands on the body, the only write that failed; the closing message is untouched.
    body = slack.calls_to("chat.update")[-1]
    assert texts.WRITING not in str(body) and "Done." in str(body)
    assert sinks.context_block("main · ctx 6%") in slack.message_blocks()[-1]


async def test_an_extra_message_that_cannot_be_removed_is_tried_again(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 0.01)
    slack.responses["chat.postMessage"] = [{"ok": True, "ts": "1.1"}, {"ok": True, "ts": "2.2"}]
    slack.responses["chat.delete"] = [aiohttp.ClientConnectionError("network down"), {"ok": True}]
    sink = reply(slack)
    running = [
        TaskUpdate(f"t{i}", f"Read: {'x' * 40}{i}", "in_progress", name="Read") for i in range(300)
    ]
    for update in running:  # two messages while they run, one line once they end
        await sink.task(update)
    await asyncio.sleep(0.05)
    ended = [replace(u, status="complete") for u in running]
    await sink.finish(ended)
    await sink.close_out("footer")
    await asyncio.sleep(0.05)
    assert [a["ts"] for a in slack.calls_to("chat.delete")] == ["2.2", "2.2"]


async def test_ending_a_reply_during_a_write_loses_no_message(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = asyncio.Event()
    original = slack.api_call

    async def slow(api_method: str, **kwargs: Any) -> Any:
        answer = await original(api_method, **kwargs)
        if api_method == "chat.postMessage" and len(slack.posted_ts) == 2:
            await gate.wait()  # Slack has the message; its answer is still on the way
        return answer

    monkeypatch.setattr(slack, "api_call", slow)
    sink = reply(slack)
    await sink.open(texts.WRITING)
    await sink.text("x" * 15_000)  # the draft write posts a continuation message
    await until_posted(slack, 2)

    async def end() -> None:
        await sink.finish([])
        await sink.close_out("footer")

    ending = asyncio.create_task(end())
    await asyncio.sleep(0.02)
    gate.set()
    await ending
    # the two body messages, and the closing message close_out adds after them: none duplicated.
    assert len(slack.posted_ts) == 3
    assert all(texts.WRITING not in text for text in slack.message_texts())


async def until_posted(slack: FakeSlack, count: int) -> None:
    async with asyncio.timeout(2):
        while len(slack.posted_ts) < count:  # noqa: ASYNC110
            await asyncio.sleep(0.005)


async def test_a_draft_rewrite_that_lands_after_the_end_changes_nothing(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.open(texts.WRITING)
    await sink.text("Done.")
    await sink.finish([])
    await sink.close_out("main · ctx 6%")
    before = len(writes(slack))
    await sink._flush(final=False)  # a debounced rewrite that was already running
    assert len(writes(slack)) == before


async def test_an_edit_and_a_write_show_as_the_terminal_shows_them(slack: FakeSlack) -> None:
    # edit-write.jsonl (CLI 2.1.283): Write a new file, Read, a failed Edit, an Edit, a Write over
    # the file. The terminal showed each Edit and Write whole, with its sentence and its lines.
    sink = reply(slack)
    renderer = TurnRenderer(sink, "/home/dev/project")
    for message in sdk_messages("edit-write"):
        await renderer.feed(message)
    await renderer.close(None)
    body = [
        b
        for b in last_blocks(slack)
        if b["type"] in ("markdown", "container") or "tools-" in b.get("block_id", "")
    ]
    lines = [sinks.block_text(b) for b in body if b["type"] == "context"]
    assert lines[0] == f"✓ `Write(new.txt)`\n{sinks.NESTED}Wrote 15 lines to new.txt"
    assert lines[1].startswith("✓ Read 1 file · ✗ Edit")  # the failed Edit folds, as before
    assert len(lines) == 2  # each diff's call line heads its container instead
    code = [sinks.block_text(b) for b in body if b["type"] == "markdown"]
    assert code[0].splitlines()[1:3] == [" 1 1", " 2 2"] and "… +5 lines" in code[0]
    diffs = [b for b in body if b["type"] == "container"]
    assert [(title_of(d), d["subtitle"]["text"]) for d in diffs] == [
        ("✓ Update(notes.txt)", "Added 1 line, removed 1 line"),
        ("✓ Write(notes.txt)", "Added 2 lines, removed 3 lines"),
    ]
    assert [sinks.block_text(d) for d in diffs] == [
        "    1 alpha\n-\U0001f7e5 2 beta\n+\U0001f7e9 2 gamma\n    3 delta",
        "-\U0001f7e5 1 alpha\n-\U0001f7e5 2 gamma\n-\U0001f7e5 3 delta\n"
        "+\U0001f7e9 1 one\n+\U0001f7e9 2 two",
    ]


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


async def test_a_call_with_a_preview_splits_the_fold_around_it(slack: FakeSlack) -> None:
    from code_with_slack.render.previews import Preview

    sink = reply(slack)
    edit = tool("b", "Edit", preview=Preview("Update(a.txt)", "Added 1 line", "1 +x"))
    for update in [tool("a", "Bash"), edit, tool("c", "Bash"), tool("d", "Bash")]:
        await sink.task(update)
    await sink.finish([])
    await sink.close_out(None)
    shown = [
        sinks.block_text(b) for b in last_blocks(slack) if b["type"] in ("context", "markdown")
    ]
    assert shown[:4] == [
        "✓ Ran 1 shell command",
        f"✓ `Update(a.txt)`\n{sinks.NESTED}Added 1 line",
        "```\n1 +x\n```",
        "✓ Ran 2 shell commands",
    ]


@pytest.mark.parametrize("fence", ["```", "````", "``````", "```x```"])
def test_a_fence_inside_a_preview_does_not_close_its_block(fence: str) -> None:
    # Any run of three or more backticks would close the block (a Markdown file's ```` fence).
    [block] = sinks.preview_blocks(f"a\n{fence}\nb")
    assert block["text"].count("```") == 2


async def test_a_failed_call_shows_its_error_even_if_it_carries_a_preview(slack: FakeSlack) -> None:
    from code_with_slack.render.previews import Preview

    sink = reply(slack)
    view = Preview("Update(a.txt)", "Added 1 line", "+x", "diff")
    await sink.task(tool("e", "Edit", status="error", output="File not found", preview=view))
    await sink.finish([])
    await sink.close_out(None)
    shown = [sinks.block_text(b) for b in last_blocks(slack)]
    assert not any("Added 1 line" in t or "```" in t or "Update(" in t for t in shown)
    assert "✗ Edit" in shown  # folded with the failed calls, as any failed call


async def test_a_message_with_two_results_previews_neither(slack: FakeSlack) -> None:
    # One tool_use_result per message: it cannot be told which of two results it belongs to.
    import dataclasses

    from claude_agent_sdk import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage

    recorded = sdk_messages("edit-write")
    use = next(
        (m, b)
        for m in recorded
        if isinstance(m, AssistantMessage)
        for b in m.content
        if isinstance(b, ToolUseBlock) and b.name == "Edit" and b.input.get("old_string") == "beta"
    )
    result = next(
        m
        for m in recorded
        if isinstance(m, UserMessage)
        and isinstance(m.content, list)
        and any(isinstance(b, ToolResultBlock) and b.tool_use_id == use[1].id for b in m.content)
    )
    twin = dataclasses.replace(use[1], id="twin")
    calls = dataclasses.replace(use[0], content=[use[1], twin])
    [block] = [b for b in result.content if isinstance(b, ToolResultBlock)]
    both = dataclasses.replace(
        result, content=[block, dataclasses.replace(block, tool_use_id="twin")]
    )
    sink = reply(slack)
    renderer = TurnRenderer(sink, "/home/dev/project")
    for message in (calls, both):
        await renderer.feed(message)
    await renderer.close(None)
    shown = "\n".join(sinks.block_text(b) for b in last_blocks(slack))
    assert "Update(" not in shown and "```" not in shown and "Edit \u00d72" in shown


def test_a_notice_fits_one_context_element() -> None:
    assert sinks.notice_text("short") == "short"
    cut = sinks.notice_text("x" * (sinks.CONTEXT_LIMIT + 10))
    assert len(cut) == sinks.CONTEXT_LIMIT and cut.endswith("…")


async def test_close_out_with_reply_to_posts_the_question_as_the_closing_message_s_text(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.text("Done.")
    await sink.finish([])
    await sink.close_out("footer", reply_to="the question?")
    posts = slack.calls_to("chat.postMessage")
    assert len(posts) == 2  # the body, then the closing message
    closing = posts[-1]
    assert closing["text"] == "Reply to: the question?"
    # The blocks show only the footer: notifying rests on posting in the thread, not on a mention.
    assert sinks.context_block("footer") in closing["blocks"]


async def test_close_out_without_reply_to_shows_the_footer_as_the_closing_text(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.text("Done.")
    await sink.finish([])
    await sink.close_out("footer")
    closing = slack.calls_to("chat.postMessage")[-1]
    assert closing["text"] == "footer"


async def test_after_close_out_only_updates_and_deletes_follow_no_new_post(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.text("Done.")
    await sink.finish([])
    await sink.close_out("footer", reply_to="the question?")
    before = len(slack.calls_to("chat.postMessage"))
    await sink.set_running("⏳ 1 shell")
    await sink.set_running("")
    await sink.set_latest(False)
    # the closing message rang once, when it was posted: nothing later posts a new one.
    assert len(slack.calls_to("chat.postMessage")) == before
    assert slack.calls_to("chat.update") or slack.calls_to("chat.delete")


async def test_reply_to_on_a_reply_not_latest_at_close_out_still_posts_and_notifies(
    slack: FakeSlack,
) -> None:
    sink = reply(slack)
    await sink.text("Done.")
    await sink.set_latest(False)  # superseded before it even finishes
    await sink.finish([])
    await sink.close_out("footer", reply_to="the question?")
    # No footer to show: a bare line stands in for it, so the notification still posts.
    closing = slack.calls_to("chat.postMessage")[-1]
    assert closing["text"] == "Reply to: the question?"
    bare = sinks.context_block(sinks.ZERO_WIDTH_SPACE)
    assert slack.message_blocks()[-1] == [bare]
    await sink.set_running("⏳ 1 shell")  # still not latest: no effect
    assert slack.message_blocks()[-1] == [bare]
    await sink.set_latest(True)  # a later change: the footer now has somewhere to show
    assert sinks.context_block("footer · ⏳ 1 shell") in slack.message_blocks()[-1]


async def test_a_failed_closing_post_is_retried_by_the_final_retry(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", 0.01)
    sink = reply(slack)
    await sink.text("Done.")
    slack.responses["chat.postMessage"] = [
        {"ok": True, "ts": "1.1"},  # the body
        aiohttp.ClientConnectionError("network down"),  # the closing message, once
        {"ok": True, "ts": "2.2"},  # the retry
    ]
    await sink.finish([])
    await sink.close_out("footer")
    await asyncio.sleep(0.05)
    assert len(slack.calls_to("chat.postMessage")) == 3  # body, failed attempt, successful retry
    assert slack.posted_ts == ["1.1", "2.2"]


async def test_reply_to_with_special_characters_is_escaped(slack: FakeSlack) -> None:
    sink = reply(slack)
    await sink.text("Done.")
    await sink.finish([])
    await sink.close_out(None, reply_to="<a> & <b>")
    closing = slack.calls_to("chat.postMessage")[-1]
    escaped_prompt = mrkdwn_escape("<a> & <b>")
    assert closing["text"] == texts.REPLY_TO.format(prompt=escaped_prompt)
    # With no footer, a bare line stands in for it: the question shows only in the notification.
    assert slack.message_blocks()[-1] == [sinks.context_block(sinks.ZERO_WIDTH_SPACE)]


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


async def test_a_change_during_a_limiter_wait_is_not_dropped(slack: FakeSlack) -> None:
    """A change that arrives while a write is waiting its turn on the shared limiter is not
    lost: it is not part of the write already in flight, so it must reach the next one."""
    limiter = UpdateLimiter(limit=1, window=0.5, burst=1)
    sink = reply(slack, limiter=limiter)
    await sink.text("one")
    await asyncio.sleep(0.05)  # posted (chat.postMessage, not gated by the limiter)
    await limiter.acquire()  # another busy reply spends the only token
    await sink.text(" two")
    await asyncio.sleep(0.05)  # the debounced flush is now waiting its turn on the limiter
    await sink.text(" three")  # arrives mid-wait; the reply then goes idle
    await asyncio.sleep(0.6)  # past the window: the waiting flush gets its token and writes
    assert "one two three" in slack.message_texts()[-1]


async def test_growth_past_the_message_limit_during_a_limiter_wait_is_not_dropped(
    slack: FakeSlack,
) -> None:
    """A change during the wait can be more than new text on the same message: it can push the
    reply into a second message. The write already in flight only knew about the first."""
    limiter = UpdateLimiter(limit=1, window=0.5, burst=1)
    sink = reply(slack, limiter=limiter)
    await sink.text("one ")
    await asyncio.sleep(0.05)  # body posted
    await limiter.acquire()  # another busy reply spends the only token
    await sink.text("two ")
    await asyncio.sleep(0.05)  # the debounced flush is now waiting its turn on the limiter
    # past MESSAGE_LIMIT: this reply now needs a second message, discovered only once the wait
    # ends and the reply is looked at again, not from the snapshot the wait started with.
    await sink.text(("word " * 20) + "\n\n" + ("x" * 100 + "\n\n") * 120)
    await sink.text("TAIL")
    await asyncio.sleep(1.2)  # past the window: the waiting flush gets its token and catches up
    assert any("TAIL" in t for t in slack.message_texts())


async def test_a_change_during_the_chat_update_round_trip_is_not_dropped(
    slack: FakeSlack,
) -> None:
    """A change can also arrive while a write is in flight (Slack's own round trip), not only
    while it waits on the limiter: that write does not know about it either."""
    limiter = UpdateLimiter(limit=100, window=1, burst=100)  # never waits: isolates the round trip
    sink = reply(slack, limiter=limiter)
    await sink.text("one")
    await asyncio.sleep(0.05)  # posted
    orig_api_call = slack.api_call

    async def slow(api_method: str, **kwargs: Any) -> Any:
        if api_method == "chat.update":
            await asyncio.sleep(0.2)
        return await orig_api_call(api_method, **kwargs)

    slack.api_call = slow  # type: ignore[method-assign]
    await sink.text(" two")
    await asyncio.sleep(0.05)  # the rewrite is now in flight, inside its own chat.update call
    await sink.text(" three")  # arrives during that round trip
    await asyncio.sleep(0.5)
    assert "three" in slack.message_texts()[-1]


async def test_the_debounce_holds_while_streaming_through_a_slow_round_trip(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_later` retries at once, with no further debounce, whenever the version moved during its
    last flush. While text streams continuously and each chat.update's own round trip is slow,
    the version moves during every round trip, so a reply was rewritten after every round trip
    rather than at most once per DEBOUNCE_SECONDS."""
    monkeypatch.setattr(sinks, "DEBOUNCE_SECONDS", 1.0)  # the real pacing, not this file's fixture
    limiter = UpdateLimiter(
        limit=1000, window=1, burst=1000
    )  # isolates the debounce, not the budget
    sink = reply(slack, limiter=limiter)
    await sink.text("start ")
    await asyncio.sleep(0.05)

    orig_api_call = slack.api_call
    stamps: list[float] = []

    async def slow(api_method: str, **kwargs: Any) -> Any:
        if api_method == "chat.update":
            stamps.append(time.monotonic())
            await asyncio.sleep(0.3)
        return await orig_api_call(api_method, **kwargs)

    slack.api_call = slow  # type: ignore[method-assign]

    end = time.monotonic() + 6.0
    i = 0
    while time.monotonic() < end:
        await sink.text(f"w{i} ")
        i += 1
        await asyncio.sleep(0.05)
    await asyncio.sleep(1.5)  # let a trailing retry, still catching up, land too

    gaps = [b - a for a, b in itertools.pairwise(stamps)]
    assert gaps  # the slow round trip left more than one update to compare
    assert min(gaps) >= sinks.DEBOUNCE_SECONDS - 0.05


async def test_two_busy_sinks_share_one_limiter_paced_and_each_reaches_its_final_state(
    slack: FakeSlack,
) -> None:
    # Scaled down from the real budget so the test runs fast; the values are independent of the
    # production constants, not a ratio of them.
    limit, window, burst = 9, 0.6, 1
    limiter = UpdateLimiter(limit=limit, window=window, burst=burst)
    a, b = reply(slack, limiter=limiter), reply(slack, limiter=limiter)

    orig_api_call = slack.api_call
    stamps: list[float] = []

    async def timed(api_method: str, **kwargs: Any) -> Any:
        if api_method == "chat.update":
            stamps.append(time.monotonic())
        return await orig_api_call(api_method, **kwargs)

    slack.api_call = timed  # type: ignore[method-assign]

    for i in range(6):
        await a.text(f"a{i} ")
        await b.text(f"b{i} ")
        await asyncio.sleep(0.03)
    await a.finish([])
    await a.close_out("footer-a")
    await b.finish([])
    await b.close_out("footer-b")
    await asyncio.sleep(2.0)  # let every write still queued on the shared, paced limiter drain

    calls = slack.calls_to("chat.update")
    assert len(calls) == len(stamps)
    # never more than the budget's window+burst allowance within any window, from either sink.
    for i, start in enumerate(stamps):
        within = sum(1 for t in stamps[i:] if t < start + window)
        assert within <= limit + burst
    # each reply's own rewrites are still spaced at least one debounce apart.
    by_ts: dict[str, list[float]] = {}
    for stamp, call in zip(stamps, calls, strict=True):
        by_ts.setdefault(call["ts"], []).append(stamp)
    for reply_stamps in by_ts.values():
        reply_stamps.sort()
        for earlier, later in itertools.pairwise(reply_stamps):
            assert later - earlier >= sinks.DEBOUNCE_SECONDS - 0.005
    # both replies' full final content landed, with no leftover "Claude is writing" status line...
    shown = dict(zip(slack.message_texts(), slack.message_blocks(), strict=True))
    for content in ("a0 a1 a2 a3 a4 a5", "b0 b1 b2 b3 b4 b5"):
        (final_text,) = (t for t in shown if content in t)
        assert texts.WRITING not in str(shown[final_text])
    # ...and each reply's closing message was posted (body + closing, once each).
    assert len(slack.calls_to("chat.postMessage")) == 4


async def test_a_ratelimited_chat_update_is_logged_without_message_content(
    slack: FakeSlack, caplog: pytest.LogCaptureFixture
) -> None:
    sink = reply(slack)
    await sink.text("first")
    await asyncio.sleep(0.05)  # the body's first message is posted
    slack.responses["chat.update"] = SlackApiError(
        "ratelimited", {"ok": False, "error": "ratelimited"}
    )
    with caplog.at_level("WARNING"):
        await sink.text(" the owner's secret content")
        await asyncio.sleep(0.05)
    assert "ratelimited" in caplog.text
    assert "secret content" not in caplog.text
