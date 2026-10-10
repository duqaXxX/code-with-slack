import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { pathToFileURL } from "node:url";
import {
  broken,
  CLAIMS,
  type Claim,
  canCertify,
  certificate,
  checklist,
  claim,
  evaluate,
  type Observation,
  observation,
  result,
} from "../../probe/claims.ts";
import { changes, notOffered, recorded, report } from "../../probe/commands.ts";
import { certified, pinnedTo } from "../../probe/main.ts";

const ROOT = join(import.meta.dirname, "..", "..");
const GESTURE = claim(
  "P90",
  "gesture",
  "a gesture",
  "src/agent/claude/session.ts#ClaudeSession",
  "do it",
);
const MODEL = claim(
  "P91",
  "model",
  "a model act",
  "src/agent/claude/requests.ts#toRequest",
  "ask for it",
);

const OBSERVATIONS: [string, Claim, Observation | null, string][] = [
  ["gesture, caused and holds", GESTURE, observation(true, true), "HOLDS"],
  ["gesture, caused and absent", GESTURE, observation(true, false), "BROKEN"],
  ["gesture, not caused", GESTURE, observation(false, false), "UNPROVEN"],
  ["gesture, not attempted", GESTURE, null, "UNPROVEN"],
  ["model, caused and holds", MODEL, observation(true, true), "HOLDS"],
  // Claude decides a model claim: its absence proves nothing, so it is never broken.
  ["model, caused and absent", MODEL, observation(true, false), "UNPROVEN"],
  ["model, not caused", MODEL, observation(false, false), "UNPROVEN"],
];

for (const [name, subject, seen, outcome] of OBSERVATIONS) {
  test(`an observation becomes an outcome [${name}]`, () => {
    assert.equal(evaluate(subject, seen).outcome, outcome);
  });
}

test("a retired claim stays retired whatever the run saw", () => {
  const retired = claim(
    "P92",
    "gesture",
    "gone",
    "src/agent/claude/session.ts#ClaudeSession",
    "n/a",
    "measured",
  );
  assert.equal(evaluate(retired, observation(true, false)).outcome, "RETIRED");
});

test("a release is certified only when every gesture claim holds", () => {
  const holds = evaluate(GESTURE, observation(true, true));
  const unproven = evaluate(GESTURE, null);
  const modelUnproven = evaluate(MODEL, null);
  assert.ok(canCertify([holds, modelUnproven]));
  // A run that caused nothing breaks nothing, and certifies nothing either.
  assert.ok(!canCertify([unproven, modelUnproven]));
  assert.ok(!broken([unproven]) && broken([evaluate(GESTURE, observation(true, false))]));
});

test("the checklist lists what the run could not prove", () => {
  const results = [evaluate(GESTURE, observation(true, true)), evaluate(MODEL, null)];
  const text = checklist(results);
  assert.ok(
    text.includes("P91 a model act") && text.includes("ask for it") && text.includes("toRequest"),
  );
  assert.ok(!text.includes("a gesture"));
  assert.equal(checklist(results.slice(0, 1)), "");
});

test("the certificate holds ids and versions only", () => {
  const results = [evaluate(GESTURE, observation(true, true, "x")), evaluate(MODEL, null)];
  assert.deepEqual(certificate(results, "2.1.283", "2026-09-27"), {
    cli: "2.1.283",
    date: "2026-09-27",
    holds: ["P90"],
    open: ["P91"],
    retired: [],
  });
});

/**
 * What a claim's `guards` names: `<file of the repository>#<export>` or
 * `<file>#<class>.<member>`, resolved by importing the file. A renamed symbol would send
 * whoever reads a broken claim to nothing.
 */
async function guarded(guards: string): Promise<unknown> {
  const [file, symbol] = guards.split("#");
  assert.ok(file !== undefined && symbol !== undefined, `${guards} is not <file>#<symbol>`);
  const loaded: Record<string, unknown> = await import(pathToFileURL(join(ROOT, file)).href);
  const [name, ...members] = symbol.split(".");
  let target = loaded[name ?? ""];
  assert.ok(target !== undefined, `${file} exports no ${name}`);
  for (const member of members) {
    const holder = target as { prototype?: Record<string, unknown> } & Record<string, unknown>;
    const next: unknown = holder[member] ?? holder.prototype?.[member];
    assert.ok(next !== undefined, `${name} has no ${member}`);
    target = next;
  }
  return target;
}

for (const each of CLAIMS) {
  test(`every claim names a symbol that exists [${each.id}]`, async () => {
    await guarded(each.guards);
  });
}

test("claim ids are unique", () => {
  assert.equal(new Set(CLAIMS.map((c) => c.id)).size, CLAIMS.length);
});

test("every claim belongs to exactly one scene", async () => {
  const { SCENES } = await import("../../probe/scenes.ts");
  const owned = Object.values(SCENES).flat();
  assert.deepEqual([...owned].sort(), CLAIMS.map((c) => c.id).sort());
});

test("a model claim cannot be made broken by hand", () => {
  assert.throws(() => result(MODEL, "BROKEN", ""));
});

test("a behaviour cannot hold without its event", () => {
  assert.throws(() => observation(false, true));
});

for (const id of ["p1", "P0", "1", "P1 "]) {
  test(`a claim id is p and a number [${id}]`, () => {
    assert.throws(() =>
      claim(id, "gesture", "t", "src/agent/claude/session.ts#ClaudeSession", "how"),
    );
  });
}

test("the pin moves only when there is exactly one", () => {
  const name = "@anthropic-ai/claude-agent-sdk";
  assert.equal(
    pinnedTo(`{"dependencies": {"${name}": "0.3.296"}}`, "0.3.297"),
    `{"dependencies": {"${name}": "0.3.297"}}`,
  );
  // A pin written another way would leave the old SDK installed and certify it as the new one.
  for (const other of [
    `{"${name}": "^0.3.296"}`,
    `{"${name}": ">=0.3.296"}`,
    `{"${name}": "0.3.296", "x": {"${name}": "0.3.1"}}`,
    "",
  ]) {
    assert.throws(() => pinnedTo(other, "0.3.297"));
  }
});

test("a certificate file of another shape is refused", () => {
  const path = join(mkdtempSync(join(tmpdir(), "awaydesk-probe-test-")), "certified-versions.json");
  assert.deepEqual(certified(path), {});
  const entry = { cli: "2.1.283", date: "2026-09-27", holds: [], open: [], retired: [] };
  writeFileSync(path, JSON.stringify({ "0.2.160": entry }));
  assert.deepEqual(certified(path), { "0.2.160": entry });
  writeFileSync(path, JSON.stringify({ "0.2.160": { cli: "2.1.283" } }));
  assert.throws(() => certified(path));
});

// --- The commands of a release (probe/commands.ts) ---------------------------------------------

test("the commands not offered are read from the limits page", () => {
  const names = notOffered();
  for (const expected of ["rewind", "plan", "add-dir", "voice"])
    assert.ok(names.includes(expected), expected);
  assert.equal(new Set(names).size, names.length);
});

const PAGES: [string, string][] = [
  [
    "a table in another section",
    "# Limits\n\n## Limits of Slack\n\n| a | b |\n|---|---|\n| x | `/rewind` |\n",
  ],
  [
    "a table that names no command",
    "# Limits\n\n## Limits of the Claude Agent SDK\n\n| a | b |\n|---|---|\n| x | words |\n",
  ],
];

for (const [name, page] of PAGES) {
  test(`a limits page that names no command is an error [${name}]`, () => {
    // A page that lists nothing would make the claim hold on nothing.
    const path = join(mkdtempSync(join(tmpdir(), "awaydesk-probe-test-")), "limits.md");
    writeFileSync(path, page);
    assert.throws(() => notOffered(path), /limits\.md/);
  });
}

test("new and gone commands are told apart", () => {
  const found = changes(
    new Set(["compact", "model", "brand-new"]),
    new Set(["compact", "model", "retired"]),
  );
  assert.deepEqual(found, { new: ["brand-new"], gone: ["retired"] });
  const told = report(found, "9.9.9");
  assert.ok(told.includes("[ ] /brand-new") && told.includes("[ ] /retired"));
  assert.ok(told.includes("tests/fixtures/sdk/server-info.json"));
  const same = report(changes(new Set(["compact"]), new Set(["compact"])), "9.9.9");
  assert.equal(same, "Commands: the same as recorded (Claude Code 9.9.9).");
});

test("the recorded commands are the fixture s", () => {
  const names = recorded();
  for (const expected of ["compact", "model", "clear"]) assert.ok(names.has(expected), expected);
});

// --- The scenes' helpers (probe/scenes.ts): no Python counterpart ------------------------------

test("a wait that ran out or a system error is the machine's, any other failure is the SDK's", async () => {
  const { isEnvironment, ProbeTimeout } = await import("../../probe/scenes.ts");
  assert.ok(isEnvironment(new ProbeTimeout("a turn did not end in time")));
  assert.ok(isEnvironment(Object.assign(new Error("reset"), { code: "ECONNRESET" })));
  assert.ok(!isEnvironment(new TypeError("undefined is not a function")));
  assert.ok(!isEnvironment(Object.assign(new Error("x"), { code: "invalid_request" })));
});

test("a number counts only when it is written alone on a line", async () => {
  const { counted } = await import("../../probe/scenes.ts");
  assert.deepEqual([...counted("from 1 to 2000\n1\n2\n 3 \nthree\n4.5")].sort(), [1, 2, 3]);
});

test("the probe's image is a valid PNG of one pixel", async () => {
  const { onePixelPng } = await import("../../probe/scenes.ts");
  const png = Buffer.from(onePixelPng());
  assert.deepEqual([...png.subarray(0, 8)], [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  assert.equal(png.subarray(12, 16).toString("latin1"), "IHDR");
  assert.equal(png.readUInt32BE(16), 1);
  assert.equal(png.readUInt32BE(20), 1);
  assert.equal(png.subarray(-8, -4).toString("latin1"), "IEND");
});

test("the models are listed when each has a value and a name and its effort levels", async () => {
  const { modelsListed } = await import("../../probe/scenes.ts");
  const fine = {
    value: "a",
    displayName: "A",
    supportsEffort: true,
    supportedEffortLevels: ["low"],
  };
  assert.ok(modelsListed([fine, { value: "b", displayName: "B", supportsEffort: false }]).holds);
  assert.ok(!modelsListed([]).holds);
  assert.ok(!modelsListed([{ value: "a", supportsEffort: false }]).holds);
  assert.ok(!modelsListed([{ ...fine, supportedEffortLevels: [] }]).holds);
  assert.ok(!modelsListed([fine, "not a record"]).holds);
});
