import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { CLAIMS } from "../../probe/claims.ts";
import { byHand, FEATURES, features, testModuleExists } from "../../probe/features.ts";

function tableAt(text: string): string {
  const path = join(mkdtempSync(join(tmpdir(), "awaydesk-probe-test-")), "features.md");
  writeFileSync(path, text);
  return path;
}

test("every probe claim is mapped to a feature and nothing else is", () => {
  const mapped = features().flatMap((f) => [...f.claims]);
  assert.deepEqual(mapped.sort(), CLAIMS.map((c) => c.id).sort());
});

for (const feature of features()) {
  test(`every row names test modules that exist [${feature.name.slice(0, 40)}]`, () => {
    assert.ok(
      feature.tests.length > 0,
      "a feature no test covers still names where its checks live",
    );
    for (const module of feature.tests) assert.ok(testModuleExists(module), module);
  });
}

test("the checklist lists only what is left by hand", () => {
  const table = tableAt(
    "| Feature | Test suite | Probe | By hand |\n|---|---|---|---|\n" +
      "| A | `test_a` | P1 | none |\n| B | `test_b` | none | Look at it |\n",
  );
  assert.deepEqual(
    features(table).map((f) => f.claims),
    [["P1"], []],
  );
  const text = byHand(table);
  assert.ok(text.includes("B") && text.includes("Look at it") && !text.includes("  [ ] A"));
});

test("a changed header is an error not an empty map", () => {
  const table = tableAt("| Feature | Tests |\n|---|---|\n| A | x |\n");
  assert.throws(() => features(table));
  assert.equal(FEATURES.endsWith("features.md"), true);
});

const ROWS: [string, string][] = [
  ["too few cells", "| A | `test_a` | none |"],
  ["a bar inside a cell", "| A | `x | y` | none | none |"],
  ["too many cells", "| A | `test_a` | P1 | none | extra |"],
];

for (const [name, row] of ROWS) {
  test(`a row with the wrong number of cells is an error [${name}]`, () => {
    // A row dropped in silence would leave both the checks and the checklist.
    const table = tableAt(
      `| Feature | Test suite | Probe | By hand |\n|---|---|---|---|\n${row}\n`,
    );
    assert.throws(() => features(table));
  });
}
