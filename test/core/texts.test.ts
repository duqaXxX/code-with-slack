import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import * as texts from "../../src/core/texts.ts";
import { GOLDEN } from "../support/fixtures.ts";

// What `awaydesk.texts` holds in Python, written by `tests/golden.py`: a golden is never edited
// by hand.
interface TextsGolden {
  constants: Record<string, string>;
  sequences: Record<string, string[]>;
  fills: Record<string, { values: Record<string, string>; output: string }>;
  functions: string[];
  calls: { function: string; args: unknown[]; kwargs: Record<string, unknown>; output: string }[];
}

const recorded = JSON.parse(readFileSync(join(GOLDEN, "texts.json"), "utf8")) as TextsGolden;
const exported = texts as unknown as Record<string, unknown>;

test("the golden holds what the Python module holds", () => {
  assert.ok(Object.keys(recorded.constants).length > 200);
  assert.deepEqual(Object.keys(recorded.sequences), ["HELP_WORDS"]);
});

test("every name of the Python module is exported", () => {
  const python = [
    ...Object.keys(recorded.constants),
    ...Object.keys(recorded.sequences),
    ...recorded.functions,
  ];
  assert.deepEqual(
    python.filter((name) => !(name in exported)),
    [],
  );
});

test("the module exports no name the Python module lacks, but its fill helper", () => {
  const python = new Set([
    ...Object.keys(recorded.constants),
    ...Object.keys(recorded.sequences),
    ...recorded.functions,
  ]);
  assert.deepEqual(
    Object.keys(exported)
      .filter((name) => !python.has(name))
      .sort(),
    ["fill"],
  );
});

for (const [name, value] of Object.entries(recorded.constants)) {
  test(`the constant ${name} is the Python text`, () => {
    assert.equal(exported[name], value);
  });
}

for (const [name, value] of Object.entries(recorded.sequences)) {
  test(`the sequence ${name} is the Python tuple`, () => {
    assert.deepEqual(exported[name], value);
  });
}

for (const [name, { values, output }] of Object.entries(recorded.fills)) {
  test(`fill gives the output of Python's format for ${name}`, () => {
    assert.equal(texts.fill(exported[name] as string, values), output);
  });
}

// A function of the module is called with its recorded positional arguments, and with the
// keyword arguments as one trailing object when there are any.
for (const call of recorded.calls) {
  test(`the function ${call.function} gives its recorded output for ${JSON.stringify(call.args)}`, () => {
    const function_ = exported[call.function] as (...args: unknown[]) => string;
    const extra = Object.keys(call.kwargs).length > 0 ? [call.kwargs] : [];
    assert.equal(function_(...call.args, ...extra), call.output);
  });
}

test("fill takes a number for a field", () => {
  assert.equal(
    texts.fill(texts.UPLOAD_TOO_MANY, { count: 12, limit: 5 }),
    "Nothing was sent to Claude: the message has 12 images, over the 5 one message takes.",
  );
  assert.equal(texts.fill("{count} of {count}", { count: 3 }), "3 of 3");
});

test("fill refuses a field with no value, as format raised a KeyError", () => {
  assert.throws(() => texts.fill(texts.BIND_LIST, {}), /\{root\}/);
});

test("fill leaves a value that holds braces alone", () => {
  assert.equal(texts.fill("{a}{b}", { a: "{b}", b: "x" }), "{b}x");
});
