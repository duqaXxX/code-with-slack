"""The fakes are useful only if they carry the recorded shapes: pin what later tasks rely on."""

from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, UserMessage
from claude_agent_sdk.types import ConversationResetMessage, ToolResultBlock, ToolUseBlock

from tests.fakes import FakeSlack, sdk_json, sdk_messages, slack_payload, split_turns


def test_tools_stream_has_partial_text_tool_use_and_result() -> None:
    messages = sdk_messages("tools")
    assert any(isinstance(m, StreamEvent) for m in messages)
    blocks = [b for m in messages if isinstance(m, AssistantMessage) for b in m.content]
    assert any(isinstance(b, ToolUseBlock) for b in blocks)
    results = [
        b
        for m in messages
        if isinstance(m, UserMessage) and isinstance(m.content, list)
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]
    assert results
    assert isinstance(messages[-1], ResultMessage)


def test_clear_emits_a_reset_then_a_new_session_id() -> None:
    first, second = split_turns(sdk_messages("clear"))
    assert any(isinstance(m, ConversationResetMessage) for m in second)
    assert isinstance(first[-1], ResultMessage) and isinstance(second[-1], ResultMessage)
    assert first[-1].session_id != second[-1].session_id


def test_auth_failed_is_flagged_on_the_assistant_message() -> None:
    errors = [m.error for m in sdk_messages("auth-failed") if isinstance(m, AssistantMessage)]
    assert "authentication_failed" in errors


def test_server_info_lists_commands_with_names() -> None:
    commands = sdk_json("server-info")["commands"]
    assert commands and {"name", "description"} <= set(commands[0])


def test_slack_command_payload_has_the_ids_the_guard_reads() -> None:
    body = slack_payload(next_named("command"))
    assert body["user_id"] == "U000ALICE" and body["team_id"] == "T000TEAM"


async def test_fake_slack_returns_recorded_responses() -> None:
    slack = FakeSlack()
    auth = await slack.auth_test()
    assert auth["team_id"] == "T000TEAM" and auth["user_id"] == "U000BOT"
    assert slack.calls_to("auth.test")


def next_named(kind: str) -> str:
    from tests.fakes import FIXTURES

    return next(p.stem for p in sorted((FIXTURES / "slack").glob(f"*-{kind}*.json")))
