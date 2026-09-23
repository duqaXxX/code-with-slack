from typing import Any

from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from code_with_slack.approvals import (
    Answer,
    Approvals,
    Approve,
    Deny,
    approval_blocks,
    question_blocks,
    read_answers,
    to_permission,
)
from tests.fakes import FIXTURES, sdk_json, slack_payload


def action_ids(blocks: list[dict[str, Any]]) -> list[str]:
    ids = []
    for block in blocks:
        ids += [e["action_id"] for e in block.get("elements", []) if "action_id" in e]
        if "accessory" in block:
            ids.append(block["accessory"]["action_id"])
    return ids


def test_approval_blocks_carry_the_id_and_the_input() -> None:
    ctx = ToolPermissionContext(tool_use_id="toolu_1", title="Claude wants to run ls")
    blocks = approval_blocks("abc", "Bash", {"command": "ls -la"}, ctx)
    assert action_ids(blocks) == ["approval_allow", "approval_deny"]
    values = {e["value"] for b in blocks for e in b.get("elements", []) if "value" in e}
    assert values == {"abc"}
    text = str(blocks)
    assert "Claude wants to run ls" in text and "ls -la" in text


async def test_resolve_only_in_the_same_channel_and_only_once() -> None:
    approvals = Approvals()
    approval_id, pending = approvals.open("C000CHAN", "Bash: ls")
    assert approvals.resolve(approval_id, "C000OTHER", Approve()) is None
    assert approvals.resolve(approval_id, "C000CHAN", Approve()) is pending
    assert await pending.future == Approve()
    assert approvals.resolve(approval_id, "C000CHAN", Deny()) is None


async def test_deny_all_releases_every_pending_request_of_a_channel() -> None:
    approvals = Approvals()
    _, a = approvals.open("C000CHAN", "a")
    _, b = approvals.open("C000CHAN", "b")
    _, other = approvals.open("C000OTHER", "c")
    assert {p.title for p in approvals.deny_all("C000CHAN")} == {"a", "b"}
    assert await a.future == Deny() and await b.future == Deny()
    assert not other.future.done()


async def test_ids_are_unguessable_and_unique() -> None:
    approvals = Approvals()
    ids = {approvals.open("C000CHAN", "t")[0] for _ in range(100)}
    assert len(ids) == 100 and all(len(i) >= 16 for i in ids)


def recorded_questions() -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = sdk_json("ask-can-use-tool")["input"]["questions"]
    return questions


def test_question_blocks_follow_the_recorded_questions() -> None:
    questions = recorded_questions()
    blocks = question_blocks("abc", questions)
    selects = [b["accessory"] for b in blocks if "accessory" in b]
    assert len(selects) == len(questions)
    for select, q in zip(selects, questions, strict=True):
        assert select["type"] == (
            "multi_static_select" if q.get("multiSelect") else "static_select"
        )
        assert [o["value"] for o in select["options"]] == [str(i) for i in range(len(q["options"]))]
    assert "question_submit" in action_ids(blocks) and "question_skip" in action_ids(blocks)


def recorded_state() -> dict[str, Any]:
    # The Submit click: its state holds what the owner picked in both menus.
    name = next(
        p.stem
        for p in sorted((FIXTURES / "slack").glob("*-block_actions.json"))
        if slack_payload(p.stem)["actions"][0]["action_id"] == "question_submit"
    )
    values: dict[str, Any] = slack_payload(name)["state"]["values"]
    return values


def test_read_answers_from_the_recorded_state() -> None:
    questions = [
        {
            "question": "Colour?",
            "options": [{"label": "red"}, {"label": "blue"}],
            "multiSelect": False,
        },
        {
            "question": "Also?",
            "options": [{"label": "a"}, {"label": "b"}, {"label": "c"}],
            "multiSelect": True,
        },
    ]
    assert read_answers(questions, recorded_state()) == {"Colour?": "blue", "Also?": ["a", "b"]}


def test_read_answers_is_none_until_every_question_is_answered() -> None:
    questions = [{"question": "Colour?", "options": [{"label": "red"}], "multiSelect": False}]
    assert read_answers(questions, {}) is None


def test_to_permission_matches_the_documented_shapes() -> None:
    tool_input = {"command": "ls"}
    allow = to_permission(Approve(), tool_input, None)
    assert isinstance(allow, PermissionResultAllow) and allow.updated_input == tool_input
    assert isinstance(to_permission(Deny(), tool_input, None), PermissionResultDeny)
    questions = recorded_questions()
    answered = to_permission(Answer({"Q": "A"}), {"questions": questions}, questions)
    assert isinstance(answered, PermissionResultAllow)
    assert answered.updated_input == {"questions": questions, "answers": {"Q": "A"}}
