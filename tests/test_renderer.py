import dataclasses
import re
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, Message, ResultMessage, UserMessage
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk.types import (
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ToolUseBlock,
)

from awaydesk import texts
from awaydesk.render.renderer import (
    BACKGROUND,
    STOPPED,
    TaskUpdate,
    TurnRenderer,
    ended_line,
    task_title,
)
from tests.fakes import sdk_messages, split_turns

ALLOWED = {"pending", "in_progress", "complete", "error"}


class RecordingSink:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.tasks: list[TaskUpdate] = []
        self.finished: list[TaskUpdate] | None = None
        self.notices: list[str] = []
        self.endings: list[str] = []
        self.closed_out: bool | str | None = False

    async def text(self, markdown: str, *, notice: bool = False, ending: bool = False) -> None:
        self.texts.append(markdown)
        if notice:
            self.notices.append(markdown)
        if ending:
            self.endings.append(markdown)

    async def task(self, update: TaskUpdate) -> None:
        self.tasks.append(update)

    async def finish(self, closing: list[TaskUpdate]) -> None:
        self.finished = closing

    async def close_out(self, footer: str | None) -> bool:
        self.closed_out = footer
        return True

    async def wait_landed(self) -> bool:
        return True

    async def settle(self) -> bool:
        return True


async def render(
    messages: list[Message], footer: str | None = "footer"
) -> tuple[RecordingSink, TurnRenderer]:
    sink = RecordingSink()
    renderer = TurnRenderer(sink)
    for message in messages:
        await renderer.feed(message)
    await renderer.close(footer)
    await renderer.close_out()
    return sink, renderer


def top_level_tool_ids(messages: list[Message]) -> list[str]:
    return [
        b.id
        for m in messages
        if isinstance(m, AssistantMessage) and m.parent_tool_use_id is None
        for b in m.content
        if isinstance(b, ToolUseBlock)
    ]


async def test_tools_turn_streams_text_and_one_line_per_tool() -> None:
    messages = sdk_messages("tools")
    sink, renderer = await render(messages)
    assert "".join(sink.texts).strip()
    ids = top_level_tool_ids(messages)
    assert ids and {t.id for t in sink.tasks} == set(ids)
    finals = {t.id: t for t in sink.tasks}
    assert all(finals[i].status == "complete" for i in ids)
    assert sink.finished == [] and sink.closed_out == "footer"
    assert renderer.result is not None


async def test_a_failed_tool_is_an_error_line() -> None:
    sink, _ = await render(sdk_messages("tool-error"))
    assert "error" in {t.status for t in sink.tasks}


async def test_a_long_command_in_the_foreground_stays_a_call_line() -> None:
    # foreground.jsonl (CLI 2.1.286): task_started with is_backgrounded false, then the task's
    # end, then the call's result.
    sink, renderer = await render(sdk_messages("foreground"))
    assert not any(t.task or t.details == BACKGROUND for t in sink.tasks)
    assert sink.tasks[-1].status == "complete"
    assert renderer.running_tasks == []


async def test_a_call_whose_task_outlives_its_result_becomes_a_task_line() -> None:
    # background.jsonl: the call's result arrives while its command still runs.
    messages = sdk_messages("background")
    sink, _ = await render(messages)
    (call,) = top_level_tool_ids(messages)
    history = [t for t in sink.tasks if t.id == call]
    assert not history[0].task  # a call's line until its result
    assert any(t.task and t.details == BACKGROUND for t in history)
    assert history[-1].task  # a task's line stays one after the task ends


async def test_subagent_calls_nest_in_the_parent_line() -> None:
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


def error_reply(messages: list[Message]) -> AssistantMessage:
    """The reply Claude Code wrote itself after the API refused the turn."""
    (reply,) = [m for m in messages if isinstance(m, AssistantMessage) and m.error is not None]
    return reply


async def test_an_api_error_shows_the_text_claude_code_wrote() -> None:
    # Recorded on SDK 0.2.163 (CLI 2.1.286) against a local endpoint answering 529 (issue #157).
    messages = sdk_messages("server-error")
    reply = error_reply(messages)
    assert reply.error == "server_error"
    (block,) = reply.content
    assert isinstance(block, TextBlock) and block.text.startswith("API Error: 529")
    sink, renderer = await render(messages)
    # Once: the result repeats the same sentence, and the category is not shown beside it.
    assert "".join(sink.texts) == block.text
    assert sink.notices == sink.texts
    assert renderer.error == "server_error" and not renderer.auth_failed


# One recording for each status a local endpoint answered with, and the error type the API
# reference gives it (issue #162, SDK 0.2.163, CLI 2.1.286). 401, 402 and 403 were recorded with
# a made-up API key, so their sentence is the one of API-key auth.
RECORDED_API_ERRORS = {
    400: "unknown",
    402: "unknown",
    404: "model_not_found",
    413: "invalid_request",
    429: "rate_limit",
    500: "server_error",
    504: "server_error",
}


@pytest.mark.parametrize("status", RECORDED_API_ERRORS)
async def test_every_recorded_api_error_shows_claude_code_s_sentence_once(status: int) -> None:
    messages = sdk_messages(f"api-error-{status}")
    reply = error_reply(messages)
    # The category is read from the recording, whatever the SDK's `AssistantMessageError` lists:
    # `model_not_found` is not in it, and the text is shown the same way.
    assert reply.error == RECORDED_API_ERRORS[status]
    (block,) = reply.content
    assert isinstance(block, TextBlock) and block.text
    sink, renderer = await render(messages)
    assert "".join(sink.texts) == block.text  # once, and never read: the words are Claude Code's
    assert sink.notices == sink.texts
    assert renderer.error == reply.error and not renderer.auth_failed
    assert renderer.result is not None and renderer.result.api_error_status == status


@pytest.mark.parametrize("status", [401, 403])
async def test_a_401_and_a_403_both_arrive_as_a_failed_login(status: int) -> None:
    # A 403 is a missing permission in the API reference, and Claude Code still sends it as
    # `authentication_failed` with `Failed to authenticate.`: the category is the documented
    # field, so it gets the note a 401 gets. The status is on the result, which comes after.
    messages = sdk_messages(f"api-error-{status}")
    assert error_reply(messages).error == "authentication_failed"
    sink, renderer = await render(messages)
    assert renderer.auth_failed and "".join(sink.texts) == texts.AUTH_FAILED
    assert renderer.result is not None and renderer.result.api_error_status == status


async def test_a_subagent_s_api_error_is_left_out_of_the_reply_s_text() -> None:
    # subagent-api-error.jsonl (issue #161, CLI 2.1.286): the subagent's requests all got a 529
    # while the main conversation's succeeded. Claude Code forwards the subagent's own error
    # message, with `error` and `parent_tool_use_id` both set, after the main turn's result.
    messages = sdk_messages("subagent-api-error")
    reply = error_reply(messages)
    assert reply.error == "server_error" and reply.parent_tool_use_id is not None
    (block,) = reply.content
    assert isinstance(block, TextBlock) and block.text.startswith("API Error: 529")
    sink, renderer = await render(messages)
    # Like every other text of a subagent: the failure belongs to its own card, not to the reply.
    assert block.text not in "".join(sink.texts)
    assert renderer.error is None and not renderer.auth_failed


async def test_a_subagent_s_api_error_shows_under_its_card_when_no_task_frame_says_so() -> None:
    # The recorded stream with the task's own end taken out, as for a subagent whose task frames
    # never reach this reply (one started by another subagent): the words are still on the card.
    messages = [
        m
        for m in sdk_messages("subagent-api-error")
        if not isinstance(m, TaskNotificationMessage | TaskUpdatedMessage)
    ]
    reply = error_reply(messages)
    sink, _ = await render(messages)
    card = [u for u in sink.tasks if u.id == reply.parent_tool_use_id][-1]
    assert card.details is not None and card.details.splitlines()[-1].startswith("API Error: 529")
    assert card.calls == 0  # a note under the card, not one more call of the subagent
    assert "API Error" not in "".join(sink.texts)


async def test_an_api_error_with_no_words_of_its_own_lets_the_result_speak() -> None:
    # No such message was recorded: the recorded one, with its blocks taken out. The result is
    # the recorded one, and carries the sentence.
    messages = sdk_messages("server-error")
    reply = error_reply(messages)
    bare = [dataclasses.replace(m, content=[]) if m is reply else m for m in messages]
    sink, renderer = await render(bare)
    assert renderer.result is not None and renderer.result.result
    assert "".join(sink.texts) == renderer.result.result


async def test_an_api_error_with_no_words_anywhere_shows_its_category() -> None:
    messages = sdk_messages("server-error")
    reply = error_reply(messages)
    silent = [
        dataclasses.replace(m, content=[])
        if m is reply
        else dataclasses.replace(m, result=None)
        if isinstance(m, ResultMessage)
        else m
        for m in messages
    ]
    sink, _ = await render(silent)
    assert "".join(sink.texts) == texts.ERROR_REPLY.format(error="server_error")


async def test_an_interrupted_turn_closes_every_open_line_without_error() -> None:
    sink, _ = await render(sdk_messages("interrupt"))
    assert sink.finished is not None
    assert all(t.status == "complete" for t in sink.finished)


async def test_background_notification_in_a_later_turn_gets_a_line() -> None:
    turns = split_turns(sdk_messages("background"))
    assert len(turns) >= 2, "the recording holds the injected notification turn"
    sink, _ = await render(turns[1])
    assert sink.tasks and sink.tasks[-1].status in {"complete", "error"}


async def test_a_task_still_running_when_its_turn_ends_stays_open_until_it_ends() -> None:
    first, later = split_turns(sdk_messages("background"))[:2]
    sink, renderer = await render(first)
    assert sink.finished is not None
    (running,) = [t for t in sink.finished if t.status == "in_progress"]
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


@pytest.mark.parametrize(
    "name",
    [
        "tools",
        "tool-error",
        "subagent",
        "interrupt",
        "background",
        "foreground",
        "subagent-foreground",
    ],
)
async def test_statuses_are_only_the_ones_slack_accepts(name: str) -> None:
    sink, _ = await render(sdk_messages(name))
    closing = sink.finished if sink.finished is not None else []
    assert {t.status for t in sink.tasks + closing} <= ALLOWED


def test_the_renderer_never_branches_on_a_tool_name() -> None:
    source = (Path(__file__).parents[1] / "src/awaydesk/render/renderer.py").read_text()
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
    assert sink.notices == sink.texts and sink.endings == []  # a note, not how the reply ended


async def test_feed_ending_marks_the_line_on_how_the_reply_ended() -> None:
    sink = RecordingSink()
    renderer = TurnRenderer(sink)
    await renderer.feed_ending("Claude Code reported an error: `ProcessError`")
    assert sink.endings == sink.notices == ["\n\nClaude Code reported an error: `ProcessError`"]


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


@pytest.mark.parametrize(
    ("summary", "status", "duration_ms", "expected"),
    [
        ('Agent "Scan" finished', "completed", 10_400, '✓ Agent "Scan" finished · 10s'),
        ('Agent "Scan" finished', "completed", 239_000, '✓ Agent "Scan" finished · 3m 59s'),
        (
            'Background command "Wait" completed (exit code 0)',
            "completed",
            None,
            '✓ Background command "Wait" completed (exit code 0)',
        ),
        ('Background command "Wait" failed', "failed", None, '✗ Background command "Wait" failed'),
    ],
)
def test_ended_line_is_claude_code_s_own_summary(
    summary: str, status: str, duration_ms: int | None, expected: str
) -> None:
    assert ended_line(summary, status, duration_ms) == expected


async def test_tool_and_task_lines_carry_their_name_and_kind() -> None:
    first = split_turns(sdk_messages("background"))[0]
    sink, _ = await render(first)
    last = {t.id: t for t in sink.tasks + (sink.finished or [])}
    assert all(t.name for t in last.values())
    assert any(t.task for t in last.values())


async def test_the_daemon_s_own_lines_are_notices_and_claude_s_words_are_not() -> None:
    sink = RecordingSink()
    renderer = TurnRenderer(sink)
    await renderer.feed_notice("The previous session could not be resumed.")
    await renderer.feed_error("The process exited.")
    for message in sdk_messages("tools"):
        await renderer.feed(message)
    assert sink.notices == [
        "The previous session could not be resumed.\n\n",
        "\n\nThe process exited.",
    ]
    assert len(sink.texts) > len(sink.notices)  # Claude's own text followed, unmarked


async def test_a_turn_that_says_nothing_gets_a_notice_not_words() -> None:
    sink, _ = await render([silent_result()])
    assert sink.notices == [texts.NO_OUTPUT]


def block_events(*pieces: str, parent: str | None = None, thinking: bool = False) -> list[Message]:
    """The stream events of one content block, in the shape the recorded streams have (see
    `tests/fixtures/sdk/goal.jsonl`): a start, one delta per piece, a stop."""
    kind, delta = ("thinking", "thinking_delta") if thinking else ("text", "text_delta")
    wire: list[dict[str, Any]] = [
        {"type": "content_block_start", "index": 1, "content_block": {"type": kind, kind: ""}}
    ]
    wire += [
        {"type": "content_block_delta", "index": 1, "delta": {"type": delta, kind: piece}}
        for piece in pieces
    ]
    wire.append({"type": "content_block_stop", "index": 1})
    return stream_events(wire, parent)


def stream_events(wire: list[dict[str, Any]], parent: str | None = None) -> list[Message]:
    out = [
        parse_message(
            {
                "type": "stream_event",
                "event": event,
                "session_id": "00000000-0000-0000-0000-000000000001",
                "parent_tool_use_id": parent,
                "uuid": "00000000-0000-0000-0000-000000000002",
            }
        )
        for event in wire
    ]
    return [m for m in out if m is not None]


def message_start(message_id: str | None) -> list[Message]:
    """The event that opens a streamed message, with the keys the renderer reads of the recorded
    one (`goal.jsonl`); `None` leaves the id out, which no recording shows."""
    message: dict[str, Any] = {"type": "message", "role": "assistant", "content": []}
    if message_id is not None:
        message["id"] = message_id
    return stream_events([{"type": "message_start", "message": message}])


def message_stop() -> list[Message]:
    return stream_events([{"type": "message_stop"}])


def unannounced(
    text: str,
    *,
    message_id: str | None = "00000000-0000-0000-0000-00000000000a",
    parent: str | None = None,
) -> list[Message]:
    """A command's own output, in the shape of the `Goal set:` message of `goal.jsonl`: a whole
    assistant message with a synthetic model and no stream event before it."""
    message: dict[str, Any] = {
        "model": "<synthetic>",
        "role": "assistant",
        "type": "message",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
    }
    if message_id is not None:
        message["id"] = message_id
    parsed = parse_message(
        {
            "type": "assistant",
            "message": message,
            "parent_tool_use_id": parent,
            "session_id": "00000000-0000-0000-0000-000000000001",
            "uuid": "00000000-0000-0000-0000-000000000003",
        }
    )
    assert parsed is not None
    return [parsed]


async def test_the_inner_turns_of_a_goal_do_not_run_together() -> None:
    sink, _ = await render(sdk_messages("goal"))
    written = "".join(sink.texts)
    assert written.endswith("tick\n\ntick\n\ntick")
    assert "ticktick" not in written


async def test_a_goal_s_reply_opens_with_the_command_s_own_line() -> None:
    """`Goal set: …` is `/goal`'s own output: an assistant message no stream event announced
    (recorded: `goal.jsonl`), followed by the first inner turn's text."""
    sink, _ = await render(sdk_messages("goal"))
    written = "".join(sink.texts)
    assert written.startswith("Goal set: Reply with the single word tick")
    assert "Never use a tool.\n\nAcknowledged." in written
    assert written.count("Goal set:") == 1


async def test_text_the_stream_already_wrote_is_not_written_again() -> None:
    """Every streamed message also arrives whole: only its deltas are written."""
    sink, _ = await render(sdk_messages("goal"))
    assert "".join(sink.texts).count("Acknowledged.") == 1


async def test_unannounced_text_after_streamed_text_is_a_paragraph_apart() -> None:
    streamed = message_start("msg_1") + block_events("one") + message_stop()
    sink, _ = await render(streamed + unannounced("Goal cleared: x"))
    assert sink.texts == ["one", "\n\nGoal cleared: x"]


async def test_a_subagent_s_unannounced_text_stays_out_of_the_reply() -> None:
    sink, _ = await render(unannounced("inner words", parent="toolu_1"))
    assert "inner words" not in "".join(sink.texts)


async def test_a_message_with_no_id_is_not_taken_for_unannounced() -> None:
    sink, _ = await render(unannounced("words", message_id=None))
    assert "words" not in "".join(sink.texts)


async def test_nothing_unannounced_is_written_while_a_streamed_message_is_unfinished() -> None:
    """A stream that stops halfway may be followed by the same text sent whole under another
    id: writing it would repeat what the deltas already wrote."""
    halfway = message_start("msg_1") + block_events("Here is the pla")
    sink, _ = await render(halfway + unannounced("Here is the plan."))
    assert sink.texts == ["Here is the pla"]


async def test_a_stream_that_names_no_message_turns_the_rule_off() -> None:
    """Without ids every streamed message would look unannounced and be written twice."""
    streamed = message_start(None) + block_events("one") + message_stop()
    sink, _ = await render(streamed + unannounced("one", message_id="msg_1"))
    assert sink.texts == ["one"]


async def test_two_text_blocks_with_nothing_between_are_a_paragraph_apart() -> None:
    sink, _ = await render(block_events("one") + block_events("two"))
    assert sink.texts == ["one", "\n\ntwo"]


async def test_deltas_of_one_block_stay_joined() -> None:
    sink, _ = await render(block_events("on", "e ", "two"))
    assert sink.texts == ["on", "e ", "two"]


async def test_a_reply_s_first_text_has_no_leading_break() -> None:
    sink, _ = await render(block_events("one"))
    assert sink.texts == ["one"]


async def test_a_tool_card_between_two_texts_adds_no_break() -> None:
    recorded = sdk_messages("foreground")
    plain, _ = await render(recorded)
    sink, _ = await render(block_events("before") + recorded)
    assert sink.texts == ["before", *plain.texts]


async def test_an_empty_block_adds_no_break() -> None:
    sink, _ = await render(block_events("one") + block_events() + block_events("two"))
    assert sink.texts == ["one", "\n\ntwo"]
    sink, _ = await render(block_events("one") + block_events())
    assert sink.texts == ["one"]


async def test_a_subagent_s_text_neither_adds_nor_takes_a_break() -> None:
    nested = block_events("inner", parent="toolu_000")
    sink, _ = await render(block_events("one") + nested + block_events("two"))
    assert sink.texts == ["one", "\n\ntwo"]
    sink, _ = await render(block_events("one") + nested)
    assert sink.texts == ["one"]


async def test_a_thinking_block_between_two_texts_adds_one_break() -> None:
    sink, _ = await render(
        block_events("one") + block_events("hmm", thinking=True) + block_events("two")
    )
    assert sink.texts == ["one", "\n\ntwo"]


async def test_a_card_updated_where_it_sits_between_two_texts_keeps_the_break() -> None:
    # Recorded: a background command ends after the turn's text, so its card changes in place,
    # above that text, and the report turn's text follows it directly.
    recorded = sdk_messages("background")
    ended = next(i for i, m in enumerate(recorded) if isinstance(m, TaskNotificationMessage))
    before, _ = await render(recorded[:ended])
    sink, _ = await render(recorded)
    assert not any(t.id not in {b.id for b in before.tasks} for t in sink.tasks)  # no new card
    report = sink.texts[len(before.texts)]
    assert before.texts[-1].strip() and report.startswith("\n\n")


def nested_command_turns() -> tuple[list[Message], list[Message], str]:
    """subagent-nested-command.jsonl: the owner's turn, and what the main stream carries after it
    (two commands the background subagent runs, each with a task of its own, then the agent's
    own end and the report turn)."""
    first, later = split_turns(sdk_messages("subagent-nested-command"))[:2]
    (agent_call,) = top_level_tool_ids(first)
    return first, later, agent_call


async def test_the_commands_a_subagent_runs_get_no_line_of_their_own() -> None:
    first, later, agent_call = nested_command_turns()
    sink, renderer = await render(first)
    for message in later:
        await renderer.feed(message)
    # one work, one line: the two commands show on the agent's card, through its call count
    assert {t.id for t in sink.tasks} == {agent_call}
    assert sink.tasks[-1].calls == 2 and sink.tasks[-1].status == "complete"
    assert renderer.running_tasks == []


async def test_a_dropped_task_id_that_starts_again_ends_its_line() -> None:
    # Hand-built order, no recording behind it: the recorded frames of the first command, sent a
    # second time after its call's result. Only an agent's task was recorded starting again
    # under its id (`subagent-nested-background.jsonl`).
    first, later, _ = nested_command_turns()
    _, renderer = await render(first)
    started = next(m for m in later if isinstance(m, TaskStartedMessage))
    ended = next(
        m for m in later if isinstance(m, TaskNotificationMessage) and m.task_id == started.task_id
    )
    result = next(
        i for i, m in enumerate(later) if i > later.index(ended) and isinstance(m, UserMessage)
    )
    for message in later[: result + 1]:
        await renderer.feed(message)
    assert started.task_id not in renderer.running_tasks  # dropped: it ended before the result
    await renderer.feed(started)  # its call is closed now: an ordinary task
    assert started.task_id in renderer.running_tasks
    await renderer.feed(ended)
    assert started.task_id not in renderer.running_tasks


async def test_a_subagent_s_command_is_not_a_task_that_outlives_the_turn() -> None:
    first, later, _ = nested_command_turns()
    _, renderer = await render(first)
    (agent_task,) = renderer.running_tasks
    started = next(m for m in later if isinstance(m, TaskStartedMessage))
    for message in later[: later.index(started) + 1]:
        await renderer.feed(message)
    assert renderer.running_tasks == [agent_task]
    assert renderer.task_title(started.task_id) is None


async def test_every_frame_of_a_nested_task_stays_off_the_card_list() -> None:
    first, later, agent_call = nested_command_turns()
    sink, renderer = await render(first)
    started = next(m for m in later if isinstance(m, TaskStartedMessage))
    for message in later:
        await renderer.feed(message)
        if message is started:
            break
    # frames of a kind the recording does not hold for a nested task, built from one it does
    for status in ("completed", "killed"):
        await renderer.feed(
            dataclasses.replace(
                next(m for m in later if isinstance(m, TaskUpdatedMessage)),
                task_id=started.task_id,
                status=status,
            )
        )
    assert {t.id for t in sink.tasks} == {agent_call}


async def test_a_task_of_a_call_the_reply_never_saw_keeps_its_own_line() -> None:
    # The reply cannot tell the call is nested (a restart dropped what held its root): the task
    # is shown as any task of an unknown call.
    _, later, _ = nested_command_turns()
    started = next(m for m in later if isinstance(m, TaskStartedMessage))
    sink, _ = await render([started])
    assert [t.id for t in sink.tasks] == [f"task-{started.task_id}"]


def nested_background_turns() -> tuple[list[Message], list[Message], str]:
    """subagent-nested-background.jsonl: a background subagent starts `sleep 20` in the
    background (the nested call's result comes while the command runs), reports and ends; the
    command's end arrives after the report turn, and the agent starts again and ends once more."""
    first, *later = split_turns(sdk_messages("subagent-nested-background"))
    (agent_call,) = top_level_tool_ids(first)
    return first, [m for turn in later for m in turn], agent_call


async def test_a_subagent_s_command_that_outlives_its_call_becomes_a_task_line() -> None:
    first, later, agent_call = nested_background_turns()
    sink, renderer = await render(first)
    (agent_task,) = renderer.running_tasks
    started = next(m for m in later if isinstance(m, TaskStartedMessage))
    assert started.tool_use_id != agent_call
    for message in later:
        await renderer.feed(message)
        if message is started:
            # held aside while its call is open: no line, not running
            assert renderer.running_tasks == [agent_task]
            assert renderer.task_title(started.task_id) is None
        if isinstance(message, UserMessage) and renderer.running_tasks != [agent_task]:
            break  # the nested call's result: the task outlives it
    assert renderer.running_tasks == [agent_task, started.task_id]
    line = sink.tasks[-1]
    assert (line.id, line.title, line.status, line.task) == (
        f"task-{started.task_id}",
        started.description,
        "in_progress",
        True,
    )
    assert [m.task_id for m in renderer.take_promoted()] == [started.task_id]
    assert renderer.take_promoted() == []
    assert not renderer.nests(started)


async def test_an_ended_command_of_a_subagent_ends_its_line_as_a_background_task() -> None:
    first, later, agent_call = nested_background_turns()
    sink, renderer = await render(first)
    for message in later:
        await renderer.feed(message)
    last = {t.id: t for t in sink.tasks}
    started = next(m for m in later if isinstance(m, TaskStartedMessage))
    assert set(last) == {agent_call, f"task-{started.task_id}"}
    assert last[f"task-{started.task_id}"].status == "complete"
    assert renderer.running_tasks == []


# An Edit or a Write is shown once it has ended (issue #136): a stream cannot take back the card
# a running call would get, and one that ended well is its preview alone.


async def test_an_edit_or_a_write_reaches_the_sink_only_once_it_has_ended() -> None:
    # edit-write.jsonl (CLI 2.1.286): three calls that ended well and an Edit that failed.
    sink, _ = await render(sdk_messages("edit-write"))
    held = [t for t in sink.tasks if t.name in ("Edit", "Write")]
    assert [(t.name, t.status, t.preview is not None) for t in held] == [
        ("Write", "complete", True),
        ("Edit", "error", False),
        ("Edit", "complete", True),
        ("Write", "complete", True),
    ]
    assert len({t.id for t in held}) == 4  # one state each: none was sent while it ran


async def test_a_write_cut_short_by_a_stop_shows_as_stopped() -> None:
    messages = sdk_messages("edit-write")
    call = next(
        i
        for i, m in enumerate(messages)
        if isinstance(m, AssistantMessage) and any(isinstance(b, ToolUseBlock) for b in m.content)
    )
    stopped = dataclasses.replace(silent_result(), terminal_reason="aborted_tools")
    sink, _ = await render([*messages[: call + 1], stopped])
    assert not sink.tasks  # nothing while it ran
    assert sink.finished is not None
    [closing] = sink.finished
    assert (closing.name, closing.status, closing.output) == ("Write", "complete", STOPPED)
