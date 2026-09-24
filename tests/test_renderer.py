import dataclasses
import re
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, Message, ResultMessage
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk.types import TaskNotificationMessage, TaskUpdatedMessage, ToolUseBlock

from code_with_slack import texts
from code_with_slack.render.renderer import (
    BACKGROUND,
    STOPPED,
    TaskUpdate,
    TurnRenderer,
    task_title,
)
from tests.fakes import sdk_messages, split_turns

ALLOWED = {"pending", "in_progress", "complete", "error"}


class RecordingSink:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.tasks: list[TaskUpdate] = []
        self.finished: tuple[list[TaskUpdate], str | None] | None = None

    async def text(self, markdown: str) -> None:
        self.texts.append(markdown)

    async def task(self, update: TaskUpdate) -> None:
        self.tasks.append(update)

    async def finish(self, closing: list[TaskUpdate], footer: str | None) -> None:
        self.finished = (closing, footer)


async def render(
    messages: list[Message], footer: str | None = "footer"
) -> tuple[RecordingSink, TurnRenderer]:
    sink = RecordingSink()
    renderer = TurnRenderer(sink)
    for message in messages:
        await renderer.feed(message)
    await renderer.close(footer)
    return sink, renderer


def top_level_tool_ids(messages: list[Message]) -> list[str]:
    return [
        b.id
        for m in messages
        if isinstance(m, AssistantMessage) and m.parent_tool_use_id is None
        for b in m.content
        if isinstance(b, ToolUseBlock)
    ]


async def test_tools_turn_streams_text_and_one_card_per_tool() -> None:
    messages = sdk_messages("tools")
    sink, renderer = await render(messages)
    assert "".join(sink.texts).strip()
    ids = top_level_tool_ids(messages)
    assert ids and {t.id for t in sink.tasks} == set(ids)
    finals = {t.id: t for t in sink.tasks}
    assert all(finals[i].status == "complete" for i in ids)
    assert sink.finished == ([], "footer")
    assert renderer.result is not None


async def test_a_failed_tool_is_an_error_card() -> None:
    sink, _ = await render(sdk_messages("tool-error"))
    assert "error" in {t.status for t in sink.tasks}


async def test_subagent_calls_nest_in_the_parent_card() -> None:
    messages = sdk_messages("subagent")
    sink, _ = await render(messages)
    parents = top_level_tool_ids(messages)
    assert {t.id for t in sink.tasks} == set(parents)
    assert any(t.details for t in sink.tasks)


async def test_a_local_command_shows_its_result_text() -> None:
    messages = sdk_messages("usage")
    sink, renderer = await render(messages)
    assert renderer.result is not None and renderer.result.result
    assert "".join(sink.texts) == renderer.result.result


async def test_logged_out_cli_gets_the_login_instructions() -> None:
    sink, renderer = await render(sdk_messages("auth-failed"))
    assert renderer.auth_failed
    assert texts.AUTH_FAILED in "".join(sink.texts)


async def test_an_interrupted_turn_closes_every_open_card_without_error() -> None:
    sink, _ = await render(sdk_messages("interrupt"))
    assert sink.finished is not None
    closing, _ = sink.finished
    assert all(t.status == "complete" for t in closing)


async def test_background_notification_in_a_later_turn_gets_a_card() -> None:
    turns = split_turns(sdk_messages("background"))
    assert len(turns) >= 2, "the recording holds the injected notification turn"
    sink, _ = await render(turns[1])
    assert sink.tasks and sink.tasks[-1].status in {"complete", "error"}


async def test_a_task_still_running_when_its_turn_ends_stays_open_until_it_ends() -> None:
    first, later = split_turns(sdk_messages("background"))[:2]
    sink, renderer = await render(first)
    assert sink.finished is not None
    (running,) = [t for t in sink.finished[0] if t.status == "in_progress"]
    assert running.details == BACKGROUND
    assert renderer.running_tasks
    for message in later:
        if isinstance(message, TaskNotificationMessage | TaskUpdatedMessage):
            await renderer.feed(message)
    assert not renderer.running_tasks
    assert sink.tasks[-1].id == running.id
    assert sink.tasks[-1].status == "complete"


async def test_stopping_the_running_tasks_closes_their_lines() -> None:
    first = split_turns(sdk_messages("background"))[0]
    sink, renderer = await render(first)
    await renderer.stop_running()
    assert not renderer.running_tasks
    assert sink.tasks[-1].status == "complete" and sink.tasks[-1].output == STOPPED


@pytest.mark.parametrize("name", ["FutureTool", "TodoWrite", "mcp__srv__do_thing"])
async def test_any_tool_name_renders_the_same_way(name: str) -> None:
    renamed: list[Message] = []
    for m in sdk_messages("tools"):
        if isinstance(m, AssistantMessage):
            m = dataclasses.replace(
                m,
                content=[
                    dataclasses.replace(b, name=name) if isinstance(b, ToolUseBlock) else b
                    for b in m.content
                ],
            )
        renamed.append(m)
    sink, _ = await render(renamed)
    assert sink.tasks and all(t.title.startswith(name) for t in sink.tasks)


@pytest.mark.parametrize("name", ["tools", "tool-error", "subagent", "interrupt", "background"])
async def test_statuses_are_only_the_ones_slack_accepts(name: str) -> None:
    sink, _ = await render(sdk_messages(name))
    closing = sink.finished[0] if sink.finished else []
    assert {t.status for t in sink.tasks + closing} <= ALLOWED


def test_the_renderer_never_branches_on_a_tool_name() -> None:
    source = (Path(__file__).parents[1] / "src/code_with_slack/render/renderer.py").read_text()
    assert not re.search(
        r"[\"'](Bash|Read|Write|Edit|Agent|Task|TodoWrite|AskUserQuestion|WebSearch)[\"']", source
    )


def test_task_title_uses_the_first_string_argument() -> None:
    assert task_title("Bash", {"timeout": 5, "command": "ls   -la\n/x"}) == "Bash: ls -la /x"
    assert task_title("Thing", {"n": 1}) == "Thing"
    assert len(task_title("Read", {"file_path": "x" * 500})) == 80


async def test_feed_error_appends_to_the_reply() -> None:
    sink = RecordingSink()
    renderer = TurnRenderer(sink)
    await renderer.feed_error("Claude Code reported an error: `ProcessError`")
    assert sink.texts == ["\n\nClaude Code reported an error: `ProcessError`"]


async def test_feed_notice_opens_the_reply() -> None:
    sink = RecordingSink()
    renderer = TurnRenderer(sink)
    await renderer.feed_notice("The previous session could not be resumed.")
    assert sink.texts == ["The previous session could not be resumed.\n\n"]


# The compact_boundary payload of a /compact turn, as the bundled CLI 2.1.280 sent it
# (recorded 2026-09-24 by actions/scripts/2026-09-24-compact-stream-probe.py; ids synthetic).
COMPACT_BOUNDARY = {
    "type": "system",
    "subtype": "compact_boundary",
    "session_id": "00000000-0000-0000-0000-000000000001",
    "uuid": "00000000-0000-0000-0000-000000000002",
    "compact_metadata": {
        "trigger": "manual",
        "pre_tokens": 15022,
        "post_tokens": 2035,
        "cumulative_dropped_tokens": 12987,
        "duration_ms": 15136,
    },
    "logical_parent_uuid": "00000000-0000-0000-0000-000000000003",
}


def silent_result() -> ResultMessage:
    """A recorded result with no text, as /compact ends (`result` was '')."""
    result = next(m for m in sdk_messages("tools") if isinstance(m, ResultMessage))
    return dataclasses.replace(result, result="")


async def test_a_compaction_says_how_many_tokens_it_saved() -> None:
    sink, _ = await render([parse_message(COMPACT_BOUNDARY), silent_result()])
    assert "".join(sink.texts).strip() == texts.COMPACTED.format(before="15.0k", after="2.0k")


async def test_a_turn_with_no_text_and_no_tool_says_it_is_done() -> None:
    sink, _ = await render([silent_result()])
    assert "".join(sink.texts) == texts.NO_OUTPUT


async def test_a_turn_with_only_tool_lines_gets_no_filler() -> None:
    sink, _ = await render([m for m in sdk_messages("tools") if not isinstance(m, ResultMessage)])
    assert texts.NO_OUTPUT not in "".join(sink.texts)


async def test_a_silent_turn_that_was_stopped_says_so() -> None:
    stopped = dataclasses.replace(silent_result(), terminal_reason="aborted_streaming")
    sink, _ = await render([stopped])
    assert "".join(sink.texts) == texts.STOPPED


async def test_a_notice_does_not_hide_a_local_command_s_result() -> None:
    sink = RecordingSink()
    renderer = TurnRenderer(sink)
    await renderer.feed_notice("The previous session could not be resumed.")
    for message in sdk_messages("usage"):
        await renderer.feed(message)
    assert renderer.result is not None and renderer.result.result
    assert renderer.result.result in "".join(sink.texts)
