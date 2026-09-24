"""Claude Code's permission requests, answered from Slack.

`can_use_tool` waits on a future that a button click resolves. Clarifying questions arrive the
same way (tool `AskUserQuestion`) and need answers, not a yes or no: the SDK documents that the
answers go back in `updated_input` (code.claude.com/docs/en/agent-sdk/user-input).
"""

import asyncio
import json
import secrets
from dataclasses import dataclass, field, replace
from typing import Any

from claude_agent_sdk.types import (
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from code_with_slack import texts
from code_with_slack.render.renderer import one_line

SECTION_LIMIT = 3000
OPTION_TEXT_LIMIT = 75
LABEL_LIMIT = 2000  # an input block's label


@dataclass(frozen=True)
class Approve:
    pass


@dataclass(frozen=True)
class Deny:
    pass


@dataclass(frozen=True)
class Answer:
    answers: dict[str, str | list[str]]


Decision = Approve | Deny | Answer


@dataclass
class Pending:
    channel_id: str
    title: str
    future: asyncio.Future[Decision]
    questions: list[dict[str, Any]] | None = None
    message_ts: str | None = field(default=None)


class Approvals:
    """Requests waiting for the owner, by an id that only the posted buttons carry."""

    def __init__(self) -> None:
        self._pending: dict[str, Pending] = {}

    def open(
        self, channel_id: str, title: str, questions: list[dict[str, Any]] | None = None
    ) -> tuple[str, Pending]:
        approval_id = secrets.token_urlsafe(16)
        pending = Pending(channel_id, title, asyncio.get_running_loop().create_future(), questions)
        self._pending[approval_id] = pending
        return approval_id, pending

    def get(self, approval_id: str) -> Pending | None:
        return self._pending.get(approval_id)

    def resolve(self, approval_id: str, channel_id: str, decision: Decision) -> Pending | None:
        """Resolve once, and only from the channel the request was posted in."""
        pending = self._pending.get(approval_id)
        if pending is None or pending.channel_id != channel_id or pending.future.done():
            return None
        pending.future.set_result(decision)
        del self._pending[approval_id]
        return pending

    def deny_all(self, channel_id: str) -> list[Pending]:
        denied = []
        for approval_id, pending in list(self._pending.items()):
            if pending.channel_id == channel_id and not pending.future.done():
                pending.future.set_result(Deny())
                del self._pending[approval_id]
                denied.append(pending)
        return denied

    def discard(self, approval_id: str) -> None:
        self._pending.pop(approval_id, None)


def _button(action_id: str, label: str, value: str, style: str | None = None) -> dict[str, Any]:
    button: dict[str, Any] = {
        "type": "button",
        "action_id": action_id,
        "value": value,
        "text": {"type": "plain_text", "text": label},
    }
    if style:
        button["style"] = style
    return button


def approval_blocks(
    approval_id: str, tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
) -> list[dict[str, Any]]:
    heading = context.title or texts.APPROVAL_PROMPT.format(tool=tool_name)
    detail = json.dumps(tool_input, indent=2, ensure_ascii=False)
    code = "```\n" + detail[: SECTION_LIMIT - 10] + "\n```"
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": heading[:SECTION_LIMIT]}}
    ]
    if context.description:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": context.description[:SECTION_LIMIT]}],
            }
        )
    blocks += [
        {"type": "section", "text": {"type": "mrkdwn", "text": code}},
        {
            "type": "actions",
            "elements": [
                _button("approval_allow", "Approve", approval_id, "primary"),
                _button("approval_deny", "Deny", approval_id, "danger"),
            ],
        },
    ]
    return blocks


QUESTION_FORM = "question_form"
TAB_ACTION = "question_tab_"  # + the question's index: action ids must differ within a block
TYPED_LIMIT = 500  # per question: four of them keep the draft under private_metadata's 3,000


def question_blocks(approval_id: str, questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The request in the channel: one line naming the questions, with Answer (which opens the
    form) and Skip. It stays one line however many questions Claude asks."""
    headers = " · ".join(f"*{q.get('header') or q['question']}*" for q in questions)
    count = (
        texts.QUESTIONS_ONE
        if len(questions) == 1
        else texts.QUESTIONS_MANY.format(count=len(questions))
    )
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{count}: {headers}"[:SECTION_LIMIT]},
        },
        {
            "type": "actions",
            "elements": [
                _button("question_open", texts.QUESTION_ANSWER, approval_id, "primary"),
                _button("question_skip", "Skip", approval_id),
            ],
        },
    ]


@dataclass
class Draft:
    """What the owner filled in the form so far, carried by the form itself (private_metadata)
    from one tab to the next: option indexes picked and text typed, per question."""

    approval_id: str
    channel_id: str
    active: int = 0
    picks: dict[int, list[int]] = field(default_factory=dict)
    typed: dict[int, str] = field(default_factory=dict)

    def dump(self) -> str:
        return json.dumps(
            {
                "a": self.approval_id,
                "c": self.channel_id,
                "n": self.active,
                "p": {str(k): v for k, v in self.picks.items()},
                "t": {str(k): v for k, v in self.typed.items()},
            },
            separators=(",", ":"),
        )

    @classmethod
    def load(cls, text: str) -> "Draft":
        data = json.loads(text)
        return cls(
            str(data["a"]),
            str(data["c"]),
            int(data["n"]),
            {int(k): [int(i) for i in v] for k, v in data["p"].items()},
            {int(k): str(v) for k, v in data["t"].items()},
        )


def absorb(draft: Draft, state_values: dict[str, Any]) -> Draft:
    """The draft with what the active tab shows now: Slack sends the form's state with every
    click and with Submit, but only for the blocks on screen."""
    index = draft.active
    picked = (state_values.get(f"q{index}") or {}).get("answer") or {}
    chosen = picked.get("selected_options") or (
        [picked["selected_option"]] if picked.get("selected_option") else []
    )
    typed = ((state_values.get(f"o{index}") or {}).get("other") or {}).get("value") or ""
    picks = {**draft.picks, index: [int(o["value"]) for o in chosen]}
    texts_ = {**draft.typed, index: typed.strip()}
    return replace(
        draft,
        picks={k: v for k, v in picks.items() if v},
        typed={k: v for k, v in texts_.items() if v},
    )


def _answer(draft: Draft, questions: list[dict[str, Any]], index: int) -> str | list[str] | None:
    """A question's answer as Claude Code takes it: the labels picked and the text typed under
    Other, which is the answer itself (tools reference); None when there is neither."""
    question = questions[index]
    labels = [question["options"][i]["label"] for i in draft.picks.get(index, [])]
    typed = draft.typed.get(index)
    if typed:
        labels = [*labels, typed] if question.get("multiSelect") else [typed]
    if not labels:
        return None
    return labels if question.get("multiSelect") else labels[0]


def first_unanswered(draft: Draft, questions: list[dict[str, Any]]) -> int | None:
    return next((i for i in range(len(questions)) if _answer(draft, questions, i) is None), None)


def draft_answers(
    draft: Draft, questions: list[dict[str, Any]]
) -> dict[str, str | list[str]] | None:
    """Every question's answer, or None while one is unanswered."""
    answers = {q["question"]: _answer(draft, questions, i) for i, q in enumerate(questions)}
    return None if None in answers.values() else answers  # type: ignore[return-value]


def _option(index: int, option: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "text": {"type": "plain_text", "text": one_line(option["label"], OPTION_TEXT_LIMIT)},
        "value": str(index),
    }
    if option.get("description"):
        entry["description"] = {
            "type": "plain_text",
            "text": one_line(option["description"], OPTION_TEXT_LIMIT),
        }
    return entry


def question_view(
    draft: Draft, questions: list[dict[str, Any]], notice: str | None = None
) -> dict[str, Any]:
    """The form, as close to the terminal as Slack allows: Slack has no tabs, so buttons named
    after the questions stand in for them, the active one highlighted and an answered one ticked.
    Below, the active question: radio buttons, or checkboxes when several may be picked, each
    with its description, and an Other field (tools reference: "type your own text through the
    Other row"). Both are optional in Slack's eyes: Submit checks that each question has one."""
    blocks: list[dict[str, Any]] = []
    if len(questions) > 1:
        tabs = []
        for i, question in enumerate(questions):
            header = question.get("header") or f"{i + 1}"
            label = f"✓ {header}" if _answer(draft, questions, i) is not None else header
            style = "primary" if i == draft.active else None
            tabs.append(_button(f"{TAB_ACTION}{i}", label, str(i), style))
        blocks.append({"type": "actions", "block_id": "tabs", "elements": tabs})
    if notice:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": notice}]})
    index = draft.active
    question = questions[index]
    options = [_option(i, o) for i, o in enumerate(question["options"])]
    element: dict[str, Any] = {
        "type": "checkboxes" if question.get("multiSelect") else "radio_buttons",
        "action_id": "answer",
        "options": options,
    }
    picked = [options[i] for i in draft.picks.get(index, []) if i < len(options)]
    if picked and question.get("multiSelect"):
        element["initial_options"] = picked
    elif picked:
        element["initial_option"] = picked[0]
    other: dict[str, Any] = {
        "type": "plain_text_input",
        "action_id": "other",
        "max_length": TYPED_LIMIT,
        "placeholder": {"type": "plain_text", "text": texts.QUESTION_OTHER_HINT},
    }
    if draft.typed.get(index):
        other["initial_value"] = draft.typed[index]
    blocks += [
        {
            "type": "input",
            "block_id": f"q{index}",
            "optional": True,
            "label": {"type": "plain_text", "text": one_line(question["question"], LABEL_LIMIT)},
            "element": element,
        },
        {
            "type": "input",
            "block_id": f"o{index}",
            "optional": True,
            "label": {"type": "plain_text", "text": texts.QUESTION_OTHER},
            "element": other,
        },
    ]
    return {
        "type": "modal",
        "callback_id": QUESTION_FORM,
        "private_metadata": draft.dump(),
        "title": {"type": "plain_text", "text": texts.QUESTION_TITLE},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": texts.QUESTION_CLOSE},
        "blocks": blocks,
    }


def to_permission(
    decision: Decision, tool_input: dict[str, Any], questions: list[dict[str, Any]] | None
) -> PermissionResult:
    match decision:
        case Answer(answers=answers):
            return PermissionResultAllow(
                updated_input={"questions": questions or [], "answers": answers}
            )
        case Approve():
            return PermissionResultAllow(updated_input=tool_input)
        case Deny():
            return PermissionResultDeny(
                message=texts.SKIP_MESSAGE if questions else texts.DENY_MESSAGE
            )
