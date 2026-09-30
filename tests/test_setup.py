from typing import Any

from code_with_slack import texts
from code_with_slack.setup import (
    DEFAULT,
    SETUP_BYPASS,
    SETUP_EFFORT,
    SETUP_MODEL,
    SETUP_START,
    Choice,
    effort_levels,
    read_choice,
    setup_blocks,
    summary,
)
from tests.fakes import sdk_json

# The CLI's own list (tests/fixtures/sdk/server-info.json, Claude Code 2.1.285): the first entry
# is `default`, and Haiku carries no effort fields at all.
MODELS: list[dict[str, Any]] = sdk_json("server-info")["models"]


def state(
    model: str | None = None, effort: str | None = None, bypass: bool = False
) -> dict[str, Any]:
    """`state.values` as Slack sends it for these controls. static_select: recorded in
    tests/fixtures/slack/000-block_actions.json (`selected_option` is null when nothing is
    chosen). checkboxes: `selected_options`, an empty list when none is ticked: the field
    slack_sdk 3.44.1 models on `ViewStateValue` (`slack_sdk/models/views/__init__.py`). Slack's
    reference pages for the checkboxes element and the block_actions payload, read 2026-09-30,
    show no example of it, so the shape is unverified against a recorded payload."""

    def option(value: str) -> dict[str, Any]:
        return {"text": {"type": "plain_text", "text": value}, "value": value}

    return {
        SETUP_MODEL: {
            SETUP_MODEL: {
                "type": "static_select",
                "selected_option": option(model) if model else None,
            }
        },
        SETUP_EFFORT: {
            SETUP_EFFORT: {
                "type": "static_select",
                "selected_option": option(effort) if effort else None,
            }
        },
        SETUP_BYPASS: {
            SETUP_BYPASS: {
                "type": "checkboxes",
                "selected_options": [option("on")] if bypass else [],
            }
        },
    }


def test_the_model_select_lists_the_cli_s_models_in_order_with_default_first() -> None:
    blocks = setup_blocks("id", MODELS, Choice())
    select = blocks[0]["accessory"]
    assert [o["value"] for o in select["options"]] == [m["value"] for m in MODELS]
    assert select["options"][0]["text"]["text"] == MODELS[0]["displayName"]
    assert select["initial_option"]["value"] == DEFAULT


def test_the_effort_select_is_default_then_the_model_s_levels() -> None:
    blocks = setup_blocks("id", MODELS, Choice())
    options = blocks[1]["accessory"]["options"]
    assert [o["value"] for o in options] == [DEFAULT, *effort_levels(MODELS, "opus")]
    assert blocks[1]["accessory"]["initial_option"]["value"] == DEFAULT


def test_a_model_without_effort_offers_only_default() -> None:
    blocks = setup_blocks("id", MODELS, Choice(model="haiku"))
    assert [o["value"] for o in blocks[1]["accessory"]["options"]] == [DEFAULT]
    assert blocks[0]["accessory"]["initial_option"]["value"] == "haiku"


def test_the_bypass_checkbox_starts_unchecked_and_keeps_a_tick() -> None:
    assert "initial_options" not in setup_blocks("id", MODELS, Choice())[2]["elements"][0]
    ticked = setup_blocks("id", MODELS, Choice(bypass=True))[2]["elements"][0]
    assert [o["value"] for o in ticked["initial_options"]] == ["on"]


def test_start_carries_the_setup_id_and_is_primary() -> None:
    button = setup_blocks("the-id", MODELS, Choice())[-1]["elements"][0]
    assert (button["action_id"], button["value"], button["style"]) == (
        SETUP_START,
        "the-id",
        "primary",
    )


def test_no_listed_model_leaves_the_model_select_out() -> None:
    blocks = setup_blocks("id", [], Choice())
    assert [b["block_id"] for b in blocks] == [SETUP_EFFORT, SETUP_BYPASS, SETUP_START]


def test_an_untouched_form_reads_as_all_defaults() -> None:
    assert read_choice(state(), MODELS) == Choice()
    assert read_choice({}, MODELS) == Choice()  # nothing at all in the payload


def test_each_control_is_read_back() -> None:
    assert read_choice(state("opus", "high", True), MODELS) == Choice("opus", "high", True)


def test_an_effort_the_model_does_not_support_reads_as_default() -> None:
    assert read_choice(state("haiku", "high"), MODELS) == Choice("haiku", DEFAULT)


def test_a_model_the_cli_did_not_list_reads_as_default() -> None:
    assert read_choice(state("gpt-x"), MODELS) == Choice()


def test_the_summary_names_the_model_by_its_display_name() -> None:
    assert summary(MODELS, Choice("opus", "high", True)) == texts.SETUP_SUMMARY.format(
        model="Opus 5.5", effort="high", bypass="on"
    )
    assert summary(MODELS, Choice()).endswith("Effort: Default · Bypass: off")
