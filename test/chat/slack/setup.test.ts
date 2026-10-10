import assert from "node:assert/strict";
import { test } from "node:test";
import { agentInfo } from "../../../src/agent/claude/info.ts";
import type { ModelInfo } from "../../../src/agent/seam.ts";
import {
  readChoice,
  SETUP_BLOCK,
  SETUP_BYPASS,
  SETUP_EFFORT,
  SETUP_MODEL,
  SETUP_START,
  setupBlocks,
  summary,
} from "../../../src/chat/slack/setup.ts";
import { type Choice, DEFAULT, DEFAULT_CHOICE, effortLevels } from "../../../src/core/setup.ts";
import * as texts from "../../../src/core/texts.ts";
import { sdkJson } from "../../support/fixtures.ts";

// The CLI's own list (tests/fixtures/sdk/server-info.json, Claude Code 2.1.286): the first entry
// is `default`, and Haiku carries no effort fields at all.
const MODELS: readonly ModelInfo[] = agentInfo(sdkJson("server-info")).models;

// biome-ignore lint/suspicious/noExplicitAny: a Slack payload read in a test
type Json = Record<string, any>;

function choice(known: Partial<Choice> = {}): Choice {
  return { ...DEFAULT_CHOICE, ...known };
}

/**
 * `state.values` as Slack sends it for these controls: keyed by block_id, then action_id, and
 * all four controls share one `actions` block. static_select: recorded in
 * tests/fixtures/slack/000-block_actions.json (`selected_option` is null when nothing is
 * chosen). checkboxes: `selected_options`, an empty list when none is ticked: verified live on
 * 2026-09-30 (a ticked Start ran bypassPermissions in real Slack, CLI 2.1.285, slack-bolt
 * 1.30.0), and the field slack_sdk 3.44.1 models on `ViewStateValue`.
 */
function state(model?: string, effort?: string, bypass = false): Json {
  const option = (value: string): Json => ({
    text: { type: "plain_text", text: value },
    value,
  });
  return {
    [SETUP_BLOCK]: {
      [SETUP_MODEL]: { type: "static_select", selected_option: model ? option(model) : null },
      [SETUP_EFFORT]: { type: "static_select", selected_option: effort ? option(effort) : null },
      [SETUP_BYPASS]: { type: "checkboxes", selected_options: bypass ? [option("on")] : [] },
    },
  };
}

/** The elements of the setup's one `actions` block, by action_id. */
function controls(blocks: Json[]): Record<string, Json> {
  const rows = blocks.filter((b) => b.type === "actions");
  assert.equal(rows.length, 1);
  const [row] = rows as [Json];
  assert.equal(row.block_id, SETUP_BLOCK);
  return Object.fromEntries((row.elements as Json[]).map((e) => [e.action_id, e]));
}

test("the message opens with one header and one row of controls", () => {
  const blocks = setupBlocks("id", MODELS, DEFAULT_CHOICE);
  assert.deepEqual(
    blocks.map((b) => b.type),
    ["section", "actions"],
  );
  assert.deepEqual(blocks[0]?.text, { type: "mrkdwn", text: "*Choose how this session starts*" });
  assert.deepEqual(Object.keys(controls(blocks)), [
    SETUP_MODEL,
    SETUP_EFFORT,
    SETUP_BYPASS,
    SETUP_START,
  ]);
});

test("the model select lists the cli s models in order with default first", () => {
  const select = controls(setupBlocks("id", MODELS, DEFAULT_CHOICE))[SETUP_MODEL] as Json;
  assert.deepEqual(
    select.options.map((o: Json) => o.value),
    MODELS.map((m) => m.value),
  );
  assert.equal(select.options[0].text.text, MODELS[0]?.displayName);
  assert.equal(select.initial_option.value, DEFAULT);
});

test("a model option carries the cli s description cut to 75", () => {
  const select = controls(setupBlocks("id", MODELS, DEFAULT_CHOICE))[SETUP_MODEL] as Json;
  const first = select.options[0];
  assert.deepEqual(first.description, {
    type: "plain_text",
    text: String(MODELS[0]?.description).slice(0, 75),
  });
  const model = (description: string | null): ModelInfo => ({
    value: "m",
    displayName: "M",
    description,
    supportsEffort: false,
    supportedEffortLevels: [],
  });
  const long = [model("x".repeat(200))];
  const option = controls(setupBlocks("id", long, DEFAULT_CHOICE))[SETUP_MODEL]?.options[0];
  assert.equal(option.description.text.length, 75);
  const bare = [model(null)];
  const plain = controls(setupBlocks("id", bare, DEFAULT_CHOICE))[SETUP_MODEL]?.options[0];
  assert.ok(!("description" in plain));
});

test("the effort select is default then the model s levels", () => {
  const select = controls(setupBlocks("id", MODELS, DEFAULT_CHOICE))[SETUP_EFFORT] as Json;
  const options: Json[] = select.options;
  assert.deepEqual(
    options.map((o) => o.value),
    [DEFAULT, ...effortLevels(MODELS, "opus")],
  );
  assert.deepEqual(options.map((o) => o.text.text).slice(0, 2), ["Effort: default", "Effort: low"]);
  assert.equal(select.initial_option.value, DEFAULT);
});

test("a model without effort offers only default", () => {
  const found = controls(setupBlocks("id", MODELS, choice({ model: "haiku" })));
  assert.deepEqual(
    (found[SETUP_EFFORT] as Json).options.map((o: Json) => o.value),
    [DEFAULT],
  );
  assert.equal((found[SETUP_MODEL] as Json).initial_option.value, "haiku");
});

test("the bypass checkbox starts unchecked and keeps a tick", () => {
  const unticked = controls(setupBlocks("id", MODELS, DEFAULT_CHOICE))[SETUP_BYPASS] as Json;
  assert.ok(!("initial_options" in unticked));
  const ticked = controls(setupBlocks("id", MODELS, choice({ bypass: true })))[
    SETUP_BYPASS
  ] as Json;
  assert.deepEqual(
    ticked.initial_options.map((o: Json) => o.value),
    ["on"],
  );
  assert.deepEqual(ticked.options[0].description, {
    type: "mrkdwn",
    text: texts.SETUP_BYPASS_DESCRIPTION,
  });
});

test("start carries the setup id and is primary", () => {
  const button = controls(setupBlocks("the-id", MODELS, DEFAULT_CHOICE))[SETUP_START] as Json;
  assert.deepEqual([button.value, button.style], ["the-id", "primary"]);
});

test("no listed model leaves the model select out", () => {
  const blocks = setupBlocks("id", [], DEFAULT_CHOICE);
  assert.deepEqual(Object.keys(controls(blocks)), [SETUP_EFFORT, SETUP_BYPASS, SETUP_START]);
});

test("an untouched form reads as all defaults", () => {
  assert.deepEqual(readChoice(state(), MODELS), DEFAULT_CHOICE);
  assert.deepEqual(readChoice({}, MODELS), DEFAULT_CHOICE); // nothing at all in the payload
});

test("each control is read back", () => {
  assert.deepEqual(readChoice(state("opus", "high", true), MODELS), {
    model: "opus",
    effort: "high",
    bypass: true,
  });
});

test("an effort the model does not support reads as default", () => {
  assert.deepEqual(readChoice(state("haiku", "high"), MODELS), choice({ model: "haiku" }));
});

test("a model the cli did not list reads as default", () => {
  assert.deepEqual(readChoice(state("gpt-x"), MODELS), DEFAULT_CHOICE);
});

test("the summary names the model by its display name", () => {
  assert.equal(
    summary(MODELS, { model: "opus", effort: "high", bypass: true }),
    texts.fill(texts.SETUP_SUMMARY, { model: "Opus 5.5", effort: "high", bypass: "on" }),
  );
  assert.ok(summary(MODELS, DEFAULT_CHOICE).endsWith("Effort: Default · Bypass: off"));
});
