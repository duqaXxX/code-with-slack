"""Claude Code's permission requests, answered from Slack.

`can_use_tool` waits on a future that a button click resolves. Clarifying questions arrive the
same way (tool `AskUserQuestion`) and need answers, not a yes or no: the SDK documents that the
answers go back in `updated_input` (code.claude.com/docs/en/agent-sdk/user-input).
"""

import asyncio
import json
import secrets
from dataclasses import dataclass, field
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


def question_blocks(approval_id: str, questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for index, question in enumerate(questions):
        options = []
        for i, option in enumerate(question["options"]):
            entry: dict[str, Any] = {
                "text": {
                    "type": "plain_text",
                    "text": one_line(option["label"], OPTION_TEXT_LIMIT),
                },
                "value": str(i),
            }
            if option.get("description"):
                entry["description"] = {
                    "type": "plain_text",
                    "text": one_line(option["description"], OPTION_TEXT_LIMIT),
                }
            options.append(entry)
        blocks.append(
            {
                "type": "section",
                "block_id": f"q{index}",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{question.get('header', '')}*\n{question['question']}"[
                        :SECTION_LIMIT
                    ],
                },
                "accessory": {
                    "type": "multi_static_select"
                    if question.get("multiSelect")
                    else "static_select",
                    "action_id": "answer",
                    "options": options,
                    "placeholder": {"type": "plain_text", "text": "Choose"},
                },
            }
        )
    blocks.append(
        {
            "type": "actions",
            "elements": [
                _button("question_submit", "Submit", approval_id, "primary"),
                _button("question_skip", "Skip", approval_id),
            ],
        }
    )
    return blocks


def outcome_blocks(text: str) -> list[dict[str, Any]]:
    return [{"type": "context", "elements": [{"type": "mrkdwn", "text": text[:SECTION_LIMIT]}]}]


def read_answers(
    questions: list[dict[str, Any]], state_values: dict[str, Any]
) -> dict[str, str | list[str]] | None:
    """The labels picked for every question, or None while one is unanswered."""
    answers: dict[str, str | list[str]] = {}
    for index, question in enumerate(questions):
        picked = (state_values.get(f"q{index}") or {}).get("answer") or {}
        labels = [
            question["options"][int(o["value"])]["label"]
            for o in picked.get("selected_options")
            or ([picked["selected_option"]] if picked.get("selected_option") else [])
        ]
        if not labels:
            return None
        answers[question["question"]] = labels if question.get("multiSelect") else labels[0]
    return answers


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
