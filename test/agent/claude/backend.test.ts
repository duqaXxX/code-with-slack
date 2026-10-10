/**
 * `ClaudeBackend` over a fake `query()`: what it passes to the session at a start, and the
 * questions it forwards to the trust code and to the listing. Those two are tested in
 * `trust.test.ts` and `listing.test.ts`; here only that the backend asks them of the right
 * folder and home.
 */
import assert from "node:assert/strict";
import { mkdir, mkdtemp, realpath, rm, utimes, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, test } from "node:test";
import { ClaudeBackend } from "../../../src/agent/claude/backend.ts";
import { CAPABILITIES } from "../../../src/agent/claude/capabilities.ts";
import { ResumeRefused } from "../../../src/agent/claude/session.ts";
import type { RequestHandler, StartOptions } from "../../../src/agent/seam.ts";
import { FakeSdk, fail } from "../../support/fake-query.ts";
import { type JsonObject, sdkRecords } from "../../support/fixtures.ts";
import { gitInit, trust } from "../../support/git-layouts.ts";
import { GIT_LAYOUT } from "../../support/platform.ts";

const LIMIT = { timeout: 10_000 };
const START: StartOptions = {
  folder: "/srv/alice/app",
  resume: null,
  settingsSources: ["user", "project", "local"],
  model: null,
  effort: null,
  permissionMode: null,
};
const NO_REQUESTS: RequestHandler = {
  permission: async () => ({ allow: true }),
  question: async () => ({ answered: false, message: "" }),
};

let tmp = "";
let home = "";

beforeEach(async () => {
  tmp = await realpath(await mkdtemp(join(tmpdir(), "awaydesk-backend-")));
  home = join(tmp, "home");
  await mkdir(home);
  await claudeRecord({});
});

afterEach(async () => {
  await rm(tmp, { recursive: true, force: true });
});

async function claudeRecord(content: unknown): Promise<void> {
  await writeFile(join(home, ".claude.json"), JSON.stringify(content));
}

test("the capabilities are the Claude back end's", () => {
  assert.equal(new ClaudeBackend().capabilities, CAPABILITIES);
});

test("a start returns a session once Claude Code answered", LIMIT, async () => {
  const sdk = new FakeSdk();
  const session = await new ClaudeBackend({ query: sdk.query, home }).start(START, NO_REQUESTS);
  assert.equal(sdk.queries.length, 1);
  assert.equal(sdk.only.options.cwd, START.folder);
  assert.equal((await session.info()).permissionMode, "bypassPermissions");
  await session.close();
  assert.equal(sdk.only.closed, true);
});

test("a start carries the Chrome flag when the owner enabled it by default", LIMIT, async () => {
  await claudeRecord({ claudeInChromeDefaultEnabled: true });
  const sdk = new FakeSdk();
  const backend = new ClaudeBackend({ query: sdk.query, home });
  await (await backend.start(START, NO_REQUESTS)).close();
  assert.deepEqual(sdk.only.options.extraArgs, { "replay-user-messages": null, chrome: null });
});

test("a start carries no Chrome flag when the owner did not enable it", LIMIT, async () => {
  const sdk = new FakeSdk();
  const backend = new ClaudeBackend({ query: sdk.query, home });
  await (await backend.start(START, NO_REQUESTS)).close();
  assert.deepEqual(sdk.only.options.extraArgs, { "replay-user-messages": null });
});

test("the Chrome setting is read at every start", LIMIT, async () => {
  const sdk = new FakeSdk();
  const backend = new ClaudeBackend({ query: sdk.query, home });
  await (await backend.start(START, NO_REQUESTS)).close();
  await claudeRecord({ claudeInChromeDefaultEnabled: true });
  await (await backend.start(START, NO_REQUESTS)).close();
  const flags = sdk.queries.map((query) => Object.keys(query.options.extraArgs ?? {}));
  assert.deepEqual(flags, [["replay-user-messages"], ["replay-user-messages", "chrome"]]);
});

test("a start where Claude Code refuses the resume rejects with ResumeRefused", LIMIT, async () => {
  const failure = new Error("Claude Code returned an error result: No conversation found");
  const refusal = { ...(sdkRecords("interrupt").at(-1) as JsonObject), is_error: true };
  const sdk = new FakeSdk({ initError: failure, start: [refusal, fail(failure)] });
  const backend = new ClaudeBackend({ query: sdk.query, home });
  await assert.rejects(
    backend.start({ ...START, resume: "68da9311-0000-4000-8000-0000000000aa" }, NO_REQUESTS),
    ResumeRefused,
  );
  assert.equal(sdk.only.closed, true);
});

test("a folder is trusted as Claude Code's record says", LIMIT, async () => {
  const trusted = join(tmp, "trusted");
  const other = join(tmp, "other");
  await mkdir(trusted);
  await mkdir(other);
  await claudeRecord({ projects: { [trusted]: { hasTrustDialogAccepted: true } } });
  const backend = new ClaudeBackend({ home });
  assert.equal(await backend.folderTrusted(trusted), true);
  assert.equal(await backend.folderTrusted(other), false);
});

test(
  "a repository inside a trusted session folder may run the daemon's git",
  GIT_LAYOUT,
  async () => {
    const folder = join(tmp, "session");
    const repository = join(folder, "app");
    await mkdir(folder);
    gitInit(repository);
    trust(home, [folder]);
    const backend = new ClaudeBackend({ home });
    assert.deepEqual(await backend.trustedRepository(repository, folder), {
      root: repository,
      key: repository,
      gitDir: join(repository, ".git"),
      insideGitDir: false,
    });
    assert.equal(await backend.trustedRepository(repository, join(tmp, "elsewhere")), null);
  },
);

test(
  "the sessions of a folder are listed through the SDK, the ids alive through the files",
  LIMIT,
  async (t) => {
    const previous = process.env.CLAUDE_CONFIG_DIR;
    process.env.CLAUDE_CONFIG_DIR = join(tmp, "config");
    t.after(() => {
      if (previous === undefined) delete process.env.CLAUDE_CONFIG_DIR;
      else process.env.CLAUDE_CONFIG_DIR = previous;
    });
    const project = join(tmp, "project");
    const folder = join(tmp, "config", "projects", project.replace(/[^a-zA-Z0-9]/g, "-"));
    await mkdir(folder, { recursive: true });
    const sid = "68da9311-0000-4000-8000-0000000000c1";
    const bare = "68da9311-0000-4000-8000-0000000000c2";
    const line = {
      type: "user",
      timestamp: "2026-09-21T10:00:00.000Z",
      sessionId: sid,
      uuid: "u1",
      parentUuid: null,
      gitBranch: "main",
      message: { role: "user", content: "A question" },
    };
    await writeFile(join(folder, `${sid}.jsonl`), `${JSON.stringify(line)}\n`);
    await writeFile(join(folder, `${bare}.jsonl`), `${JSON.stringify({ type: "ledger" })}\n`);
    await utimes(join(folder, `${sid}.jsonl`), 1_790_000_000, 1_790_000_000);
    const backend = new ClaudeBackend({ home });
    const listed = await backend.listSessions(project);
    assert.equal(listed.length, 1);
    assert.deepEqual(
      { id: listed[0]?.id, title: listed[0]?.title, branch: listed[0]?.branch },
      { id: sid, title: "A question", branch: "main" },
    );
    assert.equal(listed[0]?.lastModified, 1_790_000_000_000);
    assert.deepEqual(await backend.aliveSessions(project), new Set([sid, bare]));
    const dated = await backend.datedSessions(project, listed);
    assert.equal(dated[0]?.lastModified, Date.UTC(2026, 8, 21, 10, 0, 0, 0));
  },
);
