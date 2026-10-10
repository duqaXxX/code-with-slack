import assert from "node:assert/strict";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  realpathSync,
  rmSync,
  statSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, test } from "node:test";
import { locate, Unkeyed } from "../../src/agent/claude/trust.ts";
import type { ModelTokens, Repository } from "../../src/agent/seam.ts";
import type { FooterFields } from "../../src/chat/seam.ts";
import {
  effortChange,
  footerFields,
  formatStatusFields,
  formatTokens,
  formatUntil,
  gitState,
  sessionTokens,
  type Usage,
  UsageCache,
} from "../../src/core/footer.ts";
import { type Json, sdkRecords } from "../support/fixtures.ts";
import { git, gitInit } from "../support/git-layouts.ts";
import { GIT_LAYOUT } from "../support/platform.ts";

// 2026-09-23 21:00 in Europe/Berlin (UTC+2 in September).
const NOW = Date.UTC(2026, 8, 23, 19, 0);
const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** A footer that knows nothing; a test sets what it is about. */
function fields(known: Partial<FooterFields> = {}): FooterFields {
  return {
    bypass: false,
    model: null,
    effort: null,
    folder: null,
    branch: null,
    changes: null,
    sessionTokens: null,
    contextPercent: null,
    sessionLimit: null,
    weekLimit: null,
    ...known,
  };
}

let tmp = "";

beforeEach(() => {
  tmp = realpathSync.native(mkdtempSync(join(tmpdir(), "awaydesk-footer-")));
});

afterEach(() => {
  rmSync(tmp, { recursive: true, force: true });
});

test("status fields list the footer s values one per line", () => {
  const data = fields({
    bypass: true,
    branch: "main",
    model: "claude-opus-5-5",
    contextPercent: 6.4,
    sessionTokens: 12_345,
    sessionLimit: { percent: 3, resetsAt: NOW + 2 * HOUR + 10 * MINUTE },
    weekLimit: { percent: 25, resetsAt: null },
    effort: "high",
    folder: "/work/personal/app",
  });
  // Bypass and the folder are left out: the status's Mode and Directory lines show them.
  assert.deepEqual(formatStatusFields(data, NOW), [
    "Model: `claude-opus-5-5`",
    "Effort: `high`",
    "Branch: `main`",
    "Session tokens: `12.3k`",
    "Context: `6%`",
    "5h limit: `3% ↻ 2h`",
    "7d limit: `25%`",
  ]);
});

for (const [delta, shown, name] of [
  [59 * MINUTE, "59m", "59 minutes"],
  [47 * HOUR + 59 * MINUTE, "47h", "47 hours 59 minutes"],
  [2 * DAY, "2d", "2 days"],
  [6 * DAY + 23 * HOUR + 59 * MINUTE, "6d 23h", "6 days 23 hours 59 minutes"],
  [-5 * MINUTE, "0m", "minus 5 minutes"],
] as const) {
  test(`format until [${name}]`, () => {
    assert.equal(formatUntil(delta), shown);
  });
}

test("status fields leave out what is not known", () => {
  assert.deepEqual(formatStatusFields(fields(), NOW), []);
});

test("session tokens sum every model", () => {
  const result = sdkRecords("tools").findLast((record) => record.type === "result");
  const usage = result?.modelUsage as { [model: string]: { [key: string]: Json } } | undefined;
  assert.ok(usage && Object.keys(usage).length > 0);
  // What the `turn_ended` event carries: the four counts of each model.
  const tokens: Record<string, ModelTokens> = {};
  let expected = 0;
  for (const [model, u] of Object.entries(usage)) {
    tokens[model] = {
      input: u.inputTokens as number,
      output: u.outputTokens as number,
      cacheRead: u.cacheReadInputTokens as number,
      cacheCreation: u.cacheCreationInputTokens as number,
    };
    expected +=
      (u.inputTokens as number) +
      (u.outputTokens as number) +
      (u.cacheReadInputTokens as number) +
      (u.cacheCreationInputTokens as number);
  }
  assert.equal(sessionTokens(tokens), expected);
  assert.equal(expected, 936 + 328 + 26_480 + 4_645);
});

test("session tokens are unknown when the turn used no model", () => {
  assert.equal(sessionTokens({}), null);
  const one = { input: 1, output: 2, cacheRead: 3, cacheCreation: 4 };
  assert.equal(sessionTokens({ a: one, b: one }), 20);
});

test("cache fetches once per ttl and after invalidate", async () => {
  let calls = 0;
  let now = 0;
  const usage: Usage = { session: { percent: 5, resetsAt: null }, week: null };
  const cache = new UsageCache(
    async () => {
      calls += 1;
      return usage;
    },
    { ttl: 300_000, clock: () => now },
  );
  await cache.refreshIfStale();
  await cache.refreshIfStale();
  assert.equal(calls, 1);
  assert.deepEqual(cache.current?.session, { percent: 5, resetsAt: null });
  cache.invalidate();
  await cache.refreshIfStale();
  assert.equal(calls, 2);
  now = 301_000;
  await cache.refreshIfStale();
  assert.equal(calls, 3);
});

test("a refresh already running is not started twice", async () => {
  let calls = 0;
  let release: () => void = () => {};
  const cache = new UsageCache(async () => {
    calls += 1;
    await new Promise<void>((resolve) => {
      release = resolve;
    });
    return { session: null, week: null };
  });
  const first = cache.refreshIfStale();
  await cache.refreshIfStale();
  assert.equal(calls, 1);
  release();
  await first;
  assert.deepEqual(cache.current, { session: null, week: null });
});

test("a failing fetch keeps the old value and logs no content", async () => {
  const warnings: string[] = [];
  const cache = new UsageCache(
    async () => {
      throw new Error("SECRET-DETAIL");
    },
    { ttl: 0, warn: (message) => warnings.push(message) },
  );
  const kept: Usage = { session: { percent: 1, resetsAt: null }, week: null };
  cache.current = kept;
  await cache.refreshIfStale();
  assert.equal(cache.current, kept);
  assert.equal(warnings.length, 1);
  assert.ok(!warnings.join("\n").includes("SECRET-DETAIL"));
});

/** `trust.trustedRepository` for a test about something else: every repository counts as trusted. */
async function anyRepository(directory: string): Promise<Repository | null> {
  try {
    return await locate(directory);
  } catch (error) {
    if (error instanceof Unkeyed) {
      return null;
    }
    throw error;
  }
}

/** A fresh repository on `feature-x`. */
function repo(): string {
  return gitInit(join(tmp, "repo"));
}

async function gitChanges(folder: string): Promise<readonly [number, number] | null> {
  return (await gitState(folder, anyRepository))[1];
}

test("git branch", GIT_LAYOUT, async () => {
  const folder = repo();
  git(folder, "switch", "-q", "-c", "feature-x");
  assert.deepEqual(await gitState(folder, anyRepository), ["feature-x", [0, 0]]);
  assert.deepEqual(await gitState(join(folder, "missing"), anyRepository), [null, null]);
});

test("git changes add staged and unstaged lines", GIT_LAYOUT, async () => {
  const folder = repo();
  assert.deepEqual(await gitChanges(folder), [0, 0]); // no commit yet, nothing written
  writeFileSync(join(folder, "a.txt"), "one\ntwo\n");
  writeFileSync(join(folder, "b.txt"), "keep\n");
  git(folder, "add", ".");
  git(folder, "commit", "-qm", "x");
  assert.deepEqual(await gitChanges(folder), [0, 0]);
  writeFileSync(join(folder, "a.txt"), "one\nthree\nfour\n"); // unstaged: 2 in, 1 out
  writeFileSync(join(folder, "b.txt"), ""); // staged: 1 out
  git(folder, "add", "b.txt");
  writeFileSync(join(folder, "new.txt"), "untracked\n"); // not counted, as in the terminal
  assert.deepEqual(await gitChanges(folder), [2, 2]);
});

test("git changes outside a repo is unknown", async () => {
  assert.equal(await gitChanges(tmp), null);
  assert.equal(await gitChanges(join(tmp, "missing")), null);
});

for (const [output, expected] of [
  // Measured on Claude Code 2.1.280 (2026-09-23) through the SDK.
  ["Set effort level to high (this session only): Comprehensive implementation", [true, "high"]],
  ["Effort level set to auto (this session only)", [true, "auto"]],
  ["Set model to `Sonnet 5` for this session only", [true, null]],
  // The form ccstatusline reads from transcripts.
  ["Set model to Opus with xhigh effort", [true, "xhigh"]],
  ["Current session: 5% used", [false, null]],
] as const) {
  test(`effort change [${output}]`, () => {
    assert.deepEqual(effortChange(output), expected);
  });
}

test("git changes do not run the repo s fsmonitor", GIT_LAYOUT, async () => {
  const folder = repo();
  const marker = join(tmp, "fsmonitor-ran");
  writeFileSync(join(folder, "a.txt"), "one\n");
  git(folder, "add", ".");
  git(folder, "config", "core.fsmonitor", `touch ${marker}; false`);
  assert.deepEqual(await gitChanges(folder), [1, 0]);
  assert.equal(existsSync(marker), false);
});

test("git changes never write the index", GIT_LAYOUT, async () => {
  // `git diff` would refresh a stale index under index.lock; a killed one would leave the lock
  // and stop every commit in the repo (measured on git 2.54, 2026-09-27).
  const folder = repo();
  writeFileSync(join(folder, "a.txt"), "one\n");
  git(folder, "add", ".");
  git(folder, "commit", "-qm", "x");
  const index = join(folder, ".git", "index");
  const before = statSync(index, { bigint: true }).mtimeNs;
  const later = new Date(Date.now() + 10_000);
  utimesSync(join(folder, "a.txt"), later, later); // stat-dirty: a refresh would rewrite the index
  assert.deepEqual(await gitChanges(folder), [0, 0]);
  assert.equal(statSync(index, { bigint: true }).mtimeNs, before);
});

// What the Python tests leave to `format` and to `datetime`, and `toFixed` and `Date` do otherwise.

test("a tie rounds to the even digit as Python writes it", () => {
  assert.equal(formatTokens(12_250), "12.2k");
  assert.equal(formatTokens(12_750), "12.8k");
  assert.equal(formatTokens(1_250_000), "1.2M");
  assert.equal(formatTokens(999), "999");
  assert.equal(formatTokens(1_000), "1.0k");
  assert.equal(formatTokens(12_345), "12.3k");
  const context = (contextPercent: number) =>
    footerFields(fields({ contextPercent }), NOW).map((field) => field.value);
  assert.deepEqual(context(6.5), ["6%"]);
  assert.deepEqual(context(7.5), ["8%"]);
  assert.deepEqual(context(0.5), ["0%"]);
  assert.deepEqual(context(6.4), ["6%"]);
});

test("git is bounded by one time limit for all its calls", async () => {
  mkdirSync(join(tmp, "folder"));
  const never = new Promise<Repository | null>(() => {});
  const started = Date.now();
  assert.deepEqual(await gitState(join(tmp, "folder"), () => never, 50), [null, null]);
  assert.ok(Date.now() - started < 2_000);
});
