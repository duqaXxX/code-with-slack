/**
 * What Claude Code says of itself, into the seam's types: the answer to the initialize request
 * (`server-info.json`, Claude Code 2.1.286), the context usage (`context-usage.json`) and a
 * listed session (`SDKSessionInfo`, TypeScript SDK 0.3.296).
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import { agentInfo, contextUsage, listedSession } from "../../../src/agent/claude/info.ts";
import { type JsonObject, sdkJson } from "../../support/fixtures.ts";

const SERVER_INFO = sdkJson("server-info") as JsonObject;
const LEVELS = ["low", "medium", "high", "xhigh", "max"];

// Models.

test("the server info gives each model with what the setup shows of it", () => {
  const { models } = agentInfo(SERVER_INFO);
  assert.equal(models.length, 12);
  assert.deepEqual(models.slice(0, 2), [
    {
      value: "default",
      displayName: "Default (recommended)",
      description: "Sonnet 5.5 · Efficient for routine tasks",
      supportsEffort: true,
      supportedEffortLevels: LEVELS,
    },
    {
      value: "opus",
      displayName: "Opus 5.5",
      description: "For complex work and everyday tasks",
      supportsEffort: true,
      supportedEffortLevels: LEVELS,
    },
  ]);
});

test("a model without effort has no level", () => {
  const haiku = agentInfo(SERVER_INFO).models.find((model) => model.value === "haiku");
  assert.deepEqual(haiku, {
    value: "haiku",
    displayName: "Haiku 4.5",
    description: "Fastest for quick answers",
    supportsEffort: false,
    supportedEffortLevels: [],
  });
});

test("a model keeps the levels the agent lists for it, in its order", () => {
  const older = agentInfo(SERVER_INFO).models.find((model) => model.value === "claude-opus-4-6");
  assert.deepEqual(older?.supportedEffortLevels, ["low", "medium", "high", "max"]);
});

test("a model with no value is left out", () => {
  // The setup keys every option on the value: an entry without one cannot be picked.
  const models = [{ displayName: "No value" }, { value: "" }, { value: 7 }, "opus", null];
  assert.deepEqual(agentInfo({ models }).models, []);
});

test("a model with no name of its own is shown by its value", () => {
  for (const displayName of [undefined, "", null, 7]) {
    const [model] = agentInfo({ models: [{ value: "opus", displayName }] }).models;
    assert.deepEqual(model, {
      value: "opus",
      displayName: "opus",
      description: null,
      supportsEffort: false,
      supportedEffortLevels: [],
    });
  }
});

// Commands.

test("the server info gives every command with its hint and its aliases", () => {
  const { commands } = agentInfo(SERVER_INFO);
  assert.equal(commands.length, 54);
  assert.deepEqual(
    commands.find((command) => command.name === "compact"),
    {
      name: "compact",
      description: "Free up context by summarizing the conversation so far",
      argumentHint: "<optional custom summarization instructions>",
      aliases: [],
    },
  );
  assert.deepEqual(commands.find((command) => command.name === "clear")?.aliases, ["reset", "new"]);
  assert.deepEqual(
    commands.filter((command) => command.aliases.length > 0).map((command) => command.name),
    [
      "code-review",
      "doctor",
      "loop",
      "schedule",
      "clear",
      "config",
      "rename",
      "usage",
      "list-agents",
    ],
  );
  assert.deepEqual(
    commands.slice(0, 3).map((command) => command.name),
    ["deep-research", "design", "design-sync"],
  );
});

test("a command with only a name has no description, no hint and no alias", () => {
  assert.deepEqual(agentInfo({ commands: [{ name: "bare" }] }).commands, [
    { name: "bare", description: "", argumentHint: "", aliases: [] },
  ]);
});

test("a command with no name is left out", () => {
  const commands = [{ description: "No name" }, { name: "" }, { name: 7 }, "clear", null];
  assert.deepEqual(agentInfo({ commands }).commands, []);
});

// The permission mode.

test("the server info names the permission mode the session starts in", () => {
  assert.equal(agentInfo(SERVER_INFO).permissionMode, "bypassPermissions");
});

test("a server info that names no mode gives none", () => {
  const { current_permission_mode: _, ...without } = SERVER_INFO;
  assert.equal(agentInfo(without).permissionMode, null);
  assert.equal(agentInfo({ current_permission_mode: "" }).permissionMode, null);
});

test("what is no server info gives no model, no command and no mode", () => {
  for (const info of [null, undefined, {}, "info", [], { models: "opus", commands: {} }]) {
    assert.deepEqual(agentInfo(info), { models: [], commands: [], permissionMode: null });
  }
});

// Context usage.

test("the context usage gives the model and the share of its window in use", () => {
  assert.deepEqual(contextUsage(sdkJson("context-usage")), {
    model: "claude-haiku-4-5-20251001",
    percentage: 7,
  });
});

test("a context usage that lacks a value gives none for it", () => {
  assert.deepEqual(contextUsage({ percentage: 12.5 }), { model: null, percentage: 12.5 });
  assert.deepEqual(contextUsage({ model: "opus", percentage: "7" }), {
    model: "opus",
    percentage: null,
  });
  for (const usage of [null, undefined, {}, "usage"]) {
    assert.deepEqual(contextUsage(usage), { model: null, percentage: null });
  }
});

// Listed sessions.

test("a listed session crosses with its title, branch, size and last change", () => {
  const listed = listedSession({
    sessionId: "68da9311-0000-4000-8000-000000000001",
    summary: "Fix the footer",
    lastModified: 1791600000000,
    fileSize: 20480,
    customTitle: "footer",
    firstPrompt: "the footer is wrong",
    gitBranch: "main",
    cwd: "/home/dev/project",
  });
  assert.deepEqual(listed, {
    id: "68da9311-0000-4000-8000-000000000001",
    title: "Fix the footer",
    customTitle: "footer",
    branch: "main",
    size: 20480,
    lastModified: 1791600000000,
  });
});

test("a listed session with no title of its own, no branch and no size has none", () => {
  const bare = { sessionId: "68da9311", summary: "hello", lastModified: 1791600000000 };
  assert.deepEqual(listedSession(bare), {
    id: "68da9311",
    title: "hello",
    customTitle: null,
    branch: null,
    size: null,
    lastModified: 1791600000000,
  });
  assert.equal(listedSession({ ...bare, gitBranch: "" }).branch, null);
});
