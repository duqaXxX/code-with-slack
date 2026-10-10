/**
 * The anti-leak gate is the one check whose failure is irreversible, so it is tested both ways:
 * it fires on real leaks and stays quiet on shapes that only look like them.
 *
 * Every sample is built from fragments: written whole, it would be a leak itself and the
 * maintainer's own commit hook would block this file.
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { test as nodeTest } from "node:test";
import { fileURLToPath } from "node:url";

// The script runs under bash and reads POSIX paths; the Windows runner's bash and paths differ.
const test = process.platform === "win32" ? nodeTest.skip : nodeTest;

const SCRIPT = fileURLToPath(
  new URL("../../.github/scripts/scan-sensitive-diff.sh", import.meta.url),
);

/** The scan's exit status for a diff holding `body` under a fixed header. */
function scan(body: string): number | null {
  const diff = `--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n${body}\n`;
  const env: NodeJS.ProcessEnv = { ...process.env };
  for (const name of ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"]) {
    delete env[name];
  }
  return spawnSync("bash", [SCRIPT], { input: diff, encoding: "utf8", env }).status;
}

test("ordinary diff passes", () => {
  assert.equal(scan("+total = len(calls)"), 0);
});

test("a python decorator is not an email address", () => {
  assert.equal(scan("+" + "@pytest.mark.parametrize"), 0);
});

test("real home path is blocked", () => {
  assert.equal(scan('+p = "/Us' + 'ers/carol/Documents/notes"'), 1);
  assert.equal(scan('+p = "/ho' + 'me/carol/notes"'), 1);
});

test("neutral home placeholder is allowed", () => {
  assert.equal(scan('+p = "/ho' + 'me/dev/project"'), 0);
});

test("personal email is blocked noreply is not", () => {
  assert.equal(scan("+# contact carol" + "@" + "gmail.com"), 1);
  assert.equal(scan('+author = "1234+carol' + "@" + 'users.noreply.github.com"'), 0);
});

test("slack tokens are blocked", () => {
  assert.equal(scan('+token = "xox' + 'b-1234567890-abcdefghij"'), 1);
  assert.equal(scan('+token = "xap' + 'p-1-A0B1C2D3E4-5678"'), 1);
  assert.equal(scan("+url = 'https://hooks.sla" + "ck.com/services/T0/B0/xyz'"), 1);
});

test("synthetic slack ids are allowed", () => {
  assert.equal(scan('+OWNER = "U000ALICE"; TEAM = "T000TEAM"'), 0);
});

test("provider secrets are blocked", () => {
  assert.equal(scan('+k = "sk-' + "ant-api03-" + "A".repeat(40) + '"'), 1);
  assert.equal(scan('+k = "gh' + "p_" + "a".repeat(30) + '"'), 1);
  assert.equal(scan("+-----BEG" + "IN RSA PRIVATE KEY-----"), 1);
});

test("tracker reference is blocked", () => {
  assert.equal(scan("+see https://linear" + ".app/team/issue/X-1"), 1);
});

test("removed lines are ignored", () => {
  assert.equal(scan('-p = "/Us' + 'ers/carol/x"'), 0);
});
