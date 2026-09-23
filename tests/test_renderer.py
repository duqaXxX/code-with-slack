import dataclasses
import re
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, Message
from claude_agent_sdk.types import ToolUseBlock

from code_with_slack import texts
from code_with_slack.render.renderer import TaskUpdate, TurnRenderer, task_title
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
