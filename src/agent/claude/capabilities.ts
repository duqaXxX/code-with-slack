/** What the Claude back end supports: the reference for every capability of the seam. */
import type { Capabilities } from "../seam.ts";

export const CAPABILITIES: Capabilities = Object.freeze({
  subagents: true,
  backgroundTasks: true,
  compaction: true,
  effort: true,
  // The SDK's `EffortLevel`.
  effortLevels: Object.freeze(["low", "medium", "high", "xhigh", "max"]),
  // The TypeScript SDK changes the effort of a live session (`applyFlagSettings`, 36 ms and no
  // reconnect, measured 2026-10-10 on SDK 0.3.296).
  liveEffort: true,
  permissionModes: Object.freeze({
    default: "default",
    auto: "auto",
    bypass: "bypassPermissions",
  }),
  changedInput: true,
  questions: true,
  // On a claude.ai subscription, which is the login the daemon runs on.
  usageLimits: true,
  models: true,
  commands: true,
  promptDuringTurn: true,
  sessionListing: true,
});
