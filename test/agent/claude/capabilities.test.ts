/**
 * What the Claude back end declares: table 4.4 of the seams design, column "Claude". The
 * permission modes are the SDK's own names for them (`PermissionMode`, TypeScript SDK 0.3.296).
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import { CAPABILITIES } from "../../../src/agent/claude/capabilities.ts";

test("the Claude back end supports every capability of the seam", () => {
  assert.deepEqual(CAPABILITIES, {
    subagents: true,
    backgroundTasks: true,
    compaction: true,
    effort: true,
    effortLevels: ["low", "medium", "high", "xhigh", "max"],
    liveEffort: true,
    permissionModes: { default: "default", auto: "auto", bypass: "bypassPermissions" },
    changedInput: true,
    questions: true,
    usageLimits: true,
    models: true,
    commands: true,
    promptDuringTurn: true,
    sessionListing: true,
  });
});

test("the declared capabilities cannot be changed by who reads them", () => {
  assert.ok(Object.isFrozen(CAPABILITIES));
  assert.ok(Object.isFrozen(CAPABILITIES.permissionModes));
});
