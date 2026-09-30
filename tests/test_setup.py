from typing import Any

from code_with_slack import texts
from code_with_slack.setup import (
    DEFAULT,
    SETUP_BLOCK,
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

# The CLI's own list (tests/fixtures/sdk/server-info.json, Claude Code 2.1.286): the first entry
# is `default`, and Haiku carries no effort fields at all.
MODELS: list[dict[str, Any]] = sdk_json("server-info")["models"]


def state(
    model: str | None = None, effort: str | None = None, bypass: bool = False
) -> dict[str, Any]:
    """`state.values` as Slack sends it for these controls: keyed by block_id, then action_id,
    and all four controls share one `actions` block. static_select: recorded in
    tests/fixtures/slack/000-block_actions.json (`selected_option` is null when nothing is
    chosen). checkboxes: `selected_options`, an empty list when none is ticked: verified live on
    2026-09-30 (a ticked Start ran bypassPermissions in real Slack, CLI 2.1.285, slack-bolt
    1.30.0), and the field slack_sdk 3.44.1 models on `ViewStateValue`."""

    def option(value: str) -> dict[str, Any]:
        return {"text": {"type": "plain_text", "text": value}, "value": value}

    return {
        SETUP_BLOCK: {
            SETUP_MODEL: {
                "type": "static_select",
                "selected_option": option(model) if model else None,
            },
            SETUP_EFFORT: {
                "type": "static_select",
                "selected_option": option(effort) if effort else None,
            },
            SETUP_BYPASS: {
                "type": "checkboxes",
                "selected_options": [option("on")] if bypass else [],
            },
        }
    }


def controls(blocks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The elements of the setup's one `actions` block, by action_id."""
    (row,) = [b for b in blocks if b["type"] == "actions"]
    assert row["block_id"] == SETUP_BLOCK
    return {e["action_id"]: e for e in row["elements"]}


def test_the_message_opens_with_one_header_and_one_row_of_controls() -> None:
    blocks = setup_blocks("id", MODELS, Choice())
    assert [b["type"] for b in blocks] == ["section", "actions"]
    assert blocks[0]["text"] == {"type": "mrkdwn", "text": "*Choose how this session starts*"}
    assert list(controls(blocks)) == [SETUP_MODEL, SETUP_EFFORT, SETUP_BYPASS, SETUP_START]


def test_the_model_select_lists_the_cli_s_models_in_order_with_default_first() -> None:
    select = controls(setup_blocks("id", MODELS, Choice()))[SETUP_MODEL]
    assert [o["value"] for o in select["options"]] == [m["value"] for m in MODELS]
    assert select["options"][0]["text"]["text"] == MODELS[0]["displayName"]
    assert select["initial_option"]["value"] == DEFAULT


def test_a_model_option_carries_the_cli_s_description_cut_to_75() -> None:
    select = controls(setup_blocks("id", MODELS, Choice()))[SETUP_MODEL]
    first = select["options"][0]
    assert first["description"] == {
        "type": "plain_text",
        "text": MODELS[0]["description"][:75],
    }
    long = [{"value": "m", "displayName": "M", "description": "x" * 200}]
    option = controls(setup_blocks("id", long, Choice()))[SETUP_MODEL]["options"][0]
    assert len(option["description"]["text"]) == 75
    bare = [{"value": "m", "displayName": "M"}]
    assert (
        "description" not in controls(setup_blocks("id", bare, Choice()))[SETUP_MODEL]["options"][0]
    )


def test_the_effort_select_is_default_then_the_model_s_levels() -> None:
    select = controls(setup_blocks("id", MODELS, Choice()))[SETUP_EFFORT]
    options = select["options"]
    assert [o["value"] for o in options] == [DEFAULT, *effort_levels(MODELS, "opus")]
    assert [o["text"]["text"] for o in options][:2] == ["Effort: default", "Effort: low"]
    assert select["initial_option"]["value"] == DEFAULT


def test_a_model_without_effort_offers_only_default() -> None:
    found = controls(setup_blocks("id", MODELS, Choice(model="haiku")))
    assert [o["value"] for o in found[SETUP_EFFORT]["options"]] == [DEFAULT]
    assert found[SETUP_MODEL]["initial_option"]["value"] == "haiku"


def test_the_bypass_checkbox_starts_unchecked_and_keeps_a_tick() -> None:
    assert "initial_options" not in controls(setup_blocks("id", MODELS, Choice()))[SETUP_BYPASS]
    ticked = controls(setup_blocks("id", MODELS, Choice(bypass=True)))[SETUP_BYPASS]
    assert [o["value"] for o in ticked["initial_options"]] == ["on"]
    assert ticked["options"][0]["description"] == {
        "type": "mrkdwn",
        "text": texts.SETUP_BYPASS_DESCRIPTION,
    }


def test_start_carries_the_setup_id_and_is_primary() -> None:
    button = controls(setup_blocks("the-id", MODELS, Choice()))[SETUP_START]
    assert (button["value"], button["style"]) == ("the-id", "primary")


def test_no_listed_model_leaves_the_model_select_out() -> None:
    blocks = setup_blocks("id", [], Choice())
    assert list(controls(blocks)) == [SETUP_EFFORT, SETUP_BYPASS, SETUP_START]


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
