import json
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
    Draft,
    absorb,
    approval_blocks,
    draft_answers,
    first_unanswered,
    question_blocks,
    question_view,
    to_permission,
)
from tests.fakes import sdk_json


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


def test_the_channel_shows_one_line_with_answer_and_skip() -> None:
    questions = recorded_questions()
    blocks = question_blocks("abc", questions)
    assert len(blocks) == 2
    assert all(q["header"] in blocks[0]["text"]["text"] for q in questions)
    assert action_ids(blocks) == ["question_open", "question_skip"]
    assert all(e["value"] == "abc" for e in blocks[1]["elements"])


TWO = [
    {
        "question": "Colour?",
        "header": "Colour",
        "options": [{"label": "red", "description": "Warm"}, {"label": "blue"}],
        "multiSelect": False,
    },
    {
        "question": "Sizes?",
        "header": "Sizes",
        "options": [{"label": "s"}, {"label": "l"}],
        "multiSelect": True,
    },
]


def tab_labels(view: dict[str, Any]) -> list[str]:
    tabs = next(b for b in view["blocks"] if b.get("block_id") == "tabs")
    return [e["text"]["text"] for e in tabs["elements"]]


def test_the_form_shows_the_active_question_under_tabs() -> None:
    view = question_view(Draft("abc", "C1"), TWO)
    assert view["type"] == "modal" and view["callback_id"] == "question_form"
    assert Draft.load(view["private_metadata"]) == Draft("abc", "C1")
    assert tab_labels(view) == ["Colour", "Sizes"]
    tabs = next(b for b in view["blocks"] if b.get("block_id") == "tabs")
    assert tabs["elements"][0].get("style") == "primary" and "style" not in tabs["elements"][1]
    inputs = [b for b in view["blocks"] if b["type"] == "input"]
    assert [b["block_id"] for b in inputs] == ["q0", "o0"]
    assert inputs[0]["element"]["type"] == "radio_buttons"
    assert inputs[0]["element"]["options"][0]["description"]["text"] == "Warm"
    assert inputs[1]["element"]["type"] == "plain_text_input"
    assert inputs[1]["element"]["max_length"] == 500


def test_the_second_tab_holds_checkboxes_and_restores_its_picks() -> None:
    draft = Draft("abc", "C1", active=1, picks={1: [0, 1]}, typed={1: "xl"})
    inputs = [b for b in question_view(draft, TWO)["blocks"] if b["type"] == "input"]
    assert [b["block_id"] for b in inputs] == ["q1", "o1"]
    element = inputs[0]["element"]
    assert element["type"] == "checkboxes"
    assert [o["value"] for o in element["initial_options"]] == ["0", "1"]
    assert inputs[1]["element"]["initial_value"] == "xl"


def test_an_answered_tab_is_ticked() -> None:
    assert tab_labels(question_view(Draft("abc", "C1", picks={0: [1]}), TWO)) == [
        "✓ Colour",
        "Sizes",
    ]


def test_a_single_question_has_no_tabs() -> None:
    view = question_view(Draft("abc", "C1"), TWO[:1])
    assert all(b.get("block_id") != "tabs" for b in view["blocks"])


def test_absorb_keeps_what_the_active_tab_shows() -> None:
    state = {
        "q1": {"answer": {"type": "checkboxes", "selected_options": [{"value": "1"}]}},
        "o1": {"other": {"type": "plain_text_input", "value": " xl "}},
    }
    draft = absorb(Draft("abc", "C1", active=1, picks={0: [0]}), state)
    assert draft.picks == {0: [0], 1: [1]} and draft.typed == {1: "xl"}


def test_answers_are_the_labels_and_the_typed_text() -> None:
    draft = Draft("abc", "C1", picks={1: [0]}, typed={0: "purple", 1: "xl"})
    assert draft_answers(draft, TWO) == {"Colour?": "purple", "Sizes?": ["s", "xl"]}
    assert first_unanswered(draft, TWO) is None


def test_the_first_unanswered_question_is_found() -> None:
    draft = Draft("abc", "C1", picks={0: [1]})
    assert first_unanswered(draft, TWO) == 1
    assert draft_answers(draft, TWO) is None


def test_the_draft_round_trips_under_slack_s_limit() -> None:
    draft = Draft(
        "abc",
        "C1",
        active=3,
        picks={i: [0, 1, 2, 3] for i in range(4)},
        typed={i: "x" * 500 for i in range(4)},
    )
    text = draft.dump()
    assert len(text) <= 3000 and Draft.load(text) == draft
    assert json.loads(text)["a"] == "abc"


def test_to_permission_matches_the_documented_shapes() -> None:
    tool_input = {"command": "ls"}
    allow = to_permission(Approve(), tool_input, None)
    assert isinstance(allow, PermissionResultAllow) and allow.updated_input == tool_input
    assert isinstance(to_permission(Deny(), tool_input, None), PermissionResultDeny)
    questions = recorded_questions()
    answered = to_permission(Answer({"Q": "A"}), {"questions": questions}, questions)
    assert isinstance(answered, PermissionResultAllow)
    assert answered.updated_input == {"questions": questions, "answers": {"Q": "A"}}
