import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

// The script is plain JavaScript (it runs on the runner's own node) and the tsconfig does not
// read .mjs files, so the module is loaded by URL and given the type its JSDoc declares.
interface Run {
  pinned: string;
  latest: string;
  latestCli: string;
  fixtureCli: string;
  outcome: string;
  lastCommented: string;
}
interface ReleaseReport {
  title: string;
  body: string;
  comment: string | null;
}
const SCRIPT_URL = new URL("../../.github/scripts/sdk-release-report.mjs", import.meta.url);
const SCRIPT = fileURLToPath(SCRIPT_URL);
const { report } = (await import(SCRIPT_URL.href)) as {
  report: (run: Run) => ReleaseReport | null;
};
const BASE = { pinned: "0.2.158", latest: "0.2.160", latestCli: "2.1.285", fixtureCli: "2.1.280" };

/** The report of a run that is not the pinned version: never null. */
function issue(overrides: Partial<Run>): ReleaseReport {
  const out = report({ ...BASE, outcome: "pass", lastCommented: "", ...overrides });
  assert.notEqual(out, null);
  return out as ReleaseReport;
}

test("a new release names its versions and asks for the checks", () => {
  const out = issue({});
  assert.equal(out.title, "@anthropic-ai/claude-agent-sdk 0.2.160: test awaydesk against it");
  for (const value of ["0.2.158", "0.2.160", "2.1.285", "2.1.280", "pass"]) {
    assert.ok(out.body.includes(value), value);
  }
  // The bundled CLI changed: the message shapes the fixtures record may have changed with it.
  assert.ok(out.body.includes("re-record"));
});

test("each new version is announced once", () => {
  assert.ok(issue({ lastCommented: "0.2.159" }).comment?.includes("0.2.160"));
  assert.equal(issue({ lastCommented: "0.2.160" }).comment, null);
});

test("a failing suite is announced even for a version already named", () => {
  const comment = issue({ outcome: "fail", lastCommented: "0.2.160" }).comment;
  assert.ok(comment?.includes("fail"));
});

test("the same cli asks for no new recording", () => {
  assert.ok(!issue({ latestCli: "2.1.280" }).body.includes("re-record"));
});

test("a version already pinned needs no issue", () => {
  assert.equal(report({ ...BASE, latest: "0.2.158", outcome: "pass", lastCommented: "" }), null);
});

for (const version of ["0.2.160; rm -rf /", "", "latest", "1..2"]) {
  test(`a version that is not a version is refused [${JSON.stringify(version)}]`, () => {
    assert.throws(() => issue({ latest: version }), /not a version/);
  });
}

test("a failed install is announced too", () => {
  const comment = issue({ outcome: "install failed", lastCommented: "0.2.160" }).comment;
  assert.ok(comment?.includes("install failed"));
});

// The outcome comes from the job that ran the release under test, and lands in the issue.
for (const outcome of [
  "",
  "passed",
  "PASS",
  "pass\n\n[the fix](https://example.com)",
  "fail | see #1 |",
]) {
  test(`an outcome that is not a known word is refused [${JSON.stringify(outcome)}]`, () => {
    assert.throws(() => issue({ outcome }), /not an outcome/);
  });
}

test("an unknown outcome is refused even with nothing to report", () => {
  assert.throws(
    () => report({ ...BASE, latest: "0.2.158", outcome: "pass; true", lastCommented: "" }),
    /not an outcome/,
  );
});

test("a run that names no outcome still reports", () => {
  // "skipped" is what the script falls back to when OUTCOME is unset.
  const comment = issue({ outcome: "skipped", lastCommented: "0.2.160" }).comment;
  assert.ok(comment?.includes("skipped"));
});

// Not in the Python file, which loaded the module: the entry point reads the environment of the
// workflow and prints the JSON the shell script pipes into jq.
function run(env: Record<string, string>) {
  const clean: NodeJS.ProcessEnv = { PATH: process.env.PATH, ...env };
  return spawnSync(process.execPath, [SCRIPT], { env: clean, encoding: "utf8" });
}
const ENV = {
  PINNED: BASE.pinned,
  LATEST: BASE.latest,
  LATEST_CLI: BASE.latestCli,
  FIXTURE_CLI: BASE.fixtureCli,
};

test("run as a script it prints the report as json", () => {
  const out = run({ ...ENV, OUTCOME: "pass", LAST_COMMENTED: "0.2.159" });
  assert.equal(out.status, 0);
  const parsed = JSON.parse(out.stdout);
  assert.deepEqual(Object.keys(parsed), ["title", "body", "comment"]);
  assert.equal(parsed.title, "@anthropic-ai/claude-agent-sdk 0.2.160: test awaydesk against it");
  assert.equal(
    parsed.comment,
    "@anthropic-ai/claude-agent-sdk 0.2.160 is out (bundled CLI 2.1.285); tests: pass.",
  );
});

test("run as a script with the pinned version it prints null", () => {
  assert.equal(run({ ...ENV, LATEST: BASE.pinned }).stdout, "null\n");
});

test("run as a script with no outcome it falls back to skipped", () => {
  const parsed = JSON.parse(run(ENV).stdout);
  assert.equal(
    parsed.comment,
    "The test suite on @anthropic-ai/claude-agent-sdk 0.2.160: skipped.",
  );
});

test("run as a script a refused value exits non-zero", () => {
  const out = run({ ...ENV, LATEST: "0.2.160; rm -rf /" });
  assert.notEqual(out.status, 0);
  assert.equal(out.stdout, "");
});
