"""Session setup: the message asked before the first prompt of a new session, with the model,
the effort and bypass. Pure functions: the blocks, and the owner's choice read back from
`state.values` (`slack_app.py` posts and waits; `sessions.ThreadSession.apply_setup` applies it).

Every default means "pass nothing": the model and the effort the CLI would pick on its own.
"""

from dataclasses import dataclass
from typing import Any

from awaydesk import texts

SETUP_BLOCK = "setup"  # the one `actions` block that holds every control
SETUP_MODEL = "setup_model"
SETUP_EFFORT = "setup_effort"
SETUP_BYPASS = "setup_bypass"
SETUP_START = "setup_start"

DEFAULT = "default"
_BYPASS_ON = "on"
_OPTION_TEXT_LIMIT = 75  # a plain_text option's own limit in Slack


@dataclass(frozen=True)
class Choice:
    model: str = DEFAULT
    effort: str = DEFAULT
    bypass: bool = False


def effort_levels(models: list[dict[str, Any]], model: str) -> list[str]:
    """The levels `model` accepts, in the CLI's order; empty for a model without effort."""
    for entry in models:
        if entry.get("value") == model and entry.get("supportsEffort"):
            return [str(level) for level in entry.get("supportedEffortLevels") or []]
    return []


def _option(label: str, value: str, description: dict[str, Any] | None = None) -> dict[str, Any]:
    option: dict[str, Any] = {
        "text": {"type": "plain_text", "text": label[:_OPTION_TEXT_LIMIT]},
        "value": value,
    }
    if description:
        option["description"] = description
    return option


def _model_options(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _option(
            str(m.get("displayName") or m["value"]),
            str(m["value"]),
            # The CLI's own words, shown under the name (plain_text, at most 75 characters).
            {"type": "plain_text", "text": str(m["description"])[:_OPTION_TEXT_LIMIT]}
            if m.get("description")
            else None,
        )
        for m in models
    ]


def _effort_options(models: list[dict[str, Any]], model: str) -> list[dict[str, Any]]:
    levels = [DEFAULT, *effort_levels(models, model)]
    return [_option(texts.SETUP_EFFORT_OPTION.format(level=level), level) for level in levels]


def _initial(options: list[dict[str, Any]], value: str) -> dict[str, Any]:
    return next((o for o in options if o["value"] == value), options[0])


def _select(action_id: str, options: list[dict[str, Any]], value: str) -> dict[str, Any]:
    return {
        "type": "static_select",
        "action_id": action_id,
        "options": options,
        "initial_option": _initial(options, value),
    }


def setup_blocks(
    setup_id: str, models: list[dict[str, Any]], choice: Choice
) -> list[dict[str, Any]]:
    """The setup message: a header and one row of controls (Slack wraps it on a narrow screen):
    a Model select (left out when the CLI listed no model), an Effort select with the chosen
    model's levels, the Bypass checkbox and Start. `choice` is what each control shows; Start
    carries `setup_id`, the one thing that resolves the question."""
    elements: list[dict[str, Any]] = []
    if models:
        elements.append(_select(SETUP_MODEL, _model_options(models), choice.model))
    elements.append(_select(SETUP_EFFORT, _effort_options(models, choice.model), choice.effort))
    bypass = _option(
        texts.SETUP_BYPASS_OPTION,
        _BYPASS_ON,
        {"type": "mrkdwn", "text": texts.SETUP_BYPASS_DESCRIPTION},
    )
    checkboxes: dict[str, Any] = {
        "type": "checkboxes",
        "action_id": SETUP_BYPASS,
        "options": [bypass],
    }
    if choice.bypass:
        checkboxes["initial_options"] = [bypass]
    elements.append(checkboxes)
    elements.append(
        {
            "type": "button",
            "action_id": SETUP_START,
            "value": setup_id,
            "style": "primary",
            "text": {"type": "plain_text", "text": texts.SETUP_START_BUTTON},
        }
    )
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": texts.SETUP_HEADER}},
        {"type": "actions", "block_id": SETUP_BLOCK, "elements": elements},
    ]


def read_choice(values: dict[str, Any], models: list[dict[str, Any]]) -> Choice:
    """The owner's choice from a payload's `state.values`. A control with no selection, or one
    naming something the CLI did not list, reads as its default; an effort the chosen model does
    not support reads as `Default`, which is what the message shows after a model change."""

    row = values.get(SETUP_BLOCK) or {}

    def selected(action: str) -> str:
        control = row.get(action) or {}
        return str((control.get("selected_option") or {}).get("value") or DEFAULT)

    model = selected(SETUP_MODEL)
    if model != DEFAULT and model not in {m.get("value") for m in models}:
        model = DEFAULT
    effort = selected(SETUP_EFFORT)
    if effort not in effort_levels(models, model):
        effort = DEFAULT
    picked = (row.get(SETUP_BYPASS) or {}).get("selected_options")
    bypass = any(o.get("value") == _BYPASS_ON for o in picked or [])
    return Choice(model, effort, bypass)


def summary(models: list[dict[str, Any]], choice: Choice) -> str:
    """The one line that replaces the controls once the owner pressed Start."""
    name = next(
        (str(m.get("displayName") or m["value"]) for m in models if m["value"] == choice.model),
        choice.model,
    )
    effort = texts.SETUP_EFFORT_DEFAULT if choice.effort == DEFAULT else choice.effort
    return texts.SETUP_SUMMARY.format(
        model=name, effort=effort, bypass="on" if choice.bypass else "off"
    )
