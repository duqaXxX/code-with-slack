/**
 * `claudeInChromeDefaultEnabled` is a Boolean key at the top level of `~/.claude.json` (Claude
 * Code settings reference, read 2026-10-09; seen as `true` in a real record on Claude Code 2.1.294
 * after `/chrome`, "Enabled by default").
 */
import assert from "node:assert/strict";
import { mkdir, mkdtemp, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, mock, test } from "node:test";
import { chromeEnabled, logger } from "../../../src/agent/claude/chrome.ts";

let tmp = "";
let home = "";

beforeEach(async () => {
  tmp = await realpath(await mkdtemp(join(tmpdir(), "awaydesk-chrome-")));
  home = join(tmp, "home");
  await mkdir(home);
});

afterEach(async () => {
  mock.restoreAll();
  await rm(tmp, { recursive: true, force: true });
});

async function record(content: unknown): Promise<void> {
  await writeFile(join(home, ".claude.json"), JSON.stringify(content));
}

test("chrome is on when the owner enabled it by default", async () => {
  await record({ claudeInChromeDefaultEnabled: true, projects: {} });
  assert.ok(await chromeEnabled(home));
});

const OFF: [string, unknown][] = [
  ["content0", { claudeInChromeDefaultEnabled: false }],
  ["content1", { projects: {} }], // the key is unset until the owner chooses in `/chrome`
  ["content2", { claudeInChromeDefaultEnabled: "true" }],
  ["content3", { claudeInChromeDefaultEnabled: 1 }],
  ["content4", { projects: { "/code/app": { claudeInChromeDefaultEnabled: true } } }],
  ["content5", ["claudeInChromeDefaultEnabled"]],
];

for (const [id, content] of OFF) {
  test(`chrome is off for anything but the key set to true [${id}]`, async () => {
    await record(content);
    assert.ok(!(await chromeEnabled(home)));
  });
}

test("a record that cannot be read leaves chrome off", async () => {
  const warnings: string[] = [];
  mock.method(logger, "warning", (message: string) => warnings.push(message));
  assert.ok(!(await chromeEnabled(home))); // no file at all
  await writeFile(join(home, ".claude.json"), "{not json");
  assert.ok(!(await chromeEnabled(home)));
  // Python named the exceptions (FileNotFoundError, JSONDecodeError); here the errno code and the
  // error's own name.
  assert.deepEqual(warnings, [
    "could not read Claude Code's Chrome setting: ENOENT",
    "could not read Claude Code's Chrome setting: SyntaxError",
  ]);
});

test("a change holds from the next read", async () => {
  await record({ claudeInChromeDefaultEnabled: false });
  assert.ok(!(await chromeEnabled(home)));
  await record({ claudeInChromeDefaultEnabled: true });
  assert.ok(await chromeEnabled(home));
});
