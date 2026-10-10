import assert from "node:assert/strict";
import { test } from "node:test";
import { agentInfo } from "../../src/agent/claude/info.ts";
import type { ModelInfo } from "../../src/agent/seam.ts";
import {
  choiceOf,
  DEFAULT,
  DEFAULT_CHOICE,
  effortLevels,
  summaryOf,
} from "../../src/core/setup.ts";
import { sdkJson } from "../support/fixtures.ts";

// The CLI's own list (tests/fixtures/sdk/server-info.json, Claude Code 2.1.286): the first entry
// is `default`, and Haiku carries no effort fields at all.
const MODELS: readonly ModelInfo[] = agentInfo(sdkJson("server-info")).models;

const untouched = { model: DEFAULT, effort: DEFAULT, bypass: false };

test("the models of the recording keep the cli s order and fields", () => {
  assert.equal(MODELS[0]?.value, DEFAULT);
  assert.equal(MODELS[0]?.displayName, "Default (recommended)");
  const haiku = MODELS.find((m) => m.value === "haiku");
  assert.deepEqual([haiku?.supportsEffort, haiku?.supportedEffortLevels], [false, []]);
});

test("a model offers its levels in the cli s order", () => {
  assert.deepEqual(effortLevels(MODELS, "opus"), ["low", "medium", "high", "xhigh", "max"]);
});

test("a model without effort, or one not listed, offers no level", () => {
  assert.deepEqual(effortLevels(MODELS, "haiku"), []);
  assert.deepEqual(effortLevels(MODELS, "gpt-x"), []);
});

test("an untouched selection reads as all defaults", () => {
  assert.deepEqual(choiceOf(MODELS, untouched), DEFAULT_CHOICE);
  assert.deepEqual(choiceOf([], untouched), DEFAULT_CHOICE);
});

test("each part of a selection is kept when the models list it", () => {
  const selection = { model: "opus", effort: "high", bypass: true };
  assert.deepEqual(choiceOf(MODELS, selection), selection);
});

test("an effort the model does not support reads as default", () => {
  const choice = choiceOf(MODELS, { model: "haiku", effort: "high", bypass: false });
  assert.deepEqual(choice, { model: "haiku", effort: DEFAULT, bypass: false });
});

test("a model the agent did not list reads as default, with its effort", () => {
  const choice = choiceOf(MODELS, { model: "gpt-x", effort: "high", bypass: true });
  // The default model is listed by the recording, so its levels apply once the model is reset.
  assert.deepEqual(choice, { model: DEFAULT, effort: "high", bypass: true });
  assert.deepEqual(choiceOf([], { model: "gpt-x", effort: "high", bypass: false }), DEFAULT_CHOICE);
});

test("the summary names the model by its display name", () => {
  assert.deepEqual(summaryOf(MODELS, { model: "opus", effort: "high", bypass: true }), {
    model: "Opus 5.5",
    effort: "high",
    bypass: true,
  });
});

test("the summary keeps the value of a model the agent did not list", () => {
  assert.deepEqual(summaryOf(MODELS, { model: "gpt-x", effort: DEFAULT, bypass: false }), {
    model: "gpt-x",
    effort: null,
    bypass: false,
  });
});
