/**
 * The cuts Python's runtime made for the reply's words: `str.split()`, `str.strip()` and
 * `str.splitlines()`. The expected values are CPython 3.12's.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import { blank, oneLine, spaced, splitLines } from "../../../src/core/reply/words.ts";

test("a text on one line has its runs of white space as one space", () => {
  assert.equal(spaced("  ls   -la\n/x\t"), "ls -la /x");
  assert.equal(spaced("one"), "one");
  assert.equal(spaced(" \n "), "");
  // White space to Python and not to JavaScript's `\s`, and the other way round.
  assert.equal(spaced("a\x1cb\x85c"), "a b c");
  assert.equal(spaced("a﻿b"), "a﻿b");
});

test("a text past its limit is cut to the limit with an ellipsis, counted in characters", () => {
  assert.equal(oneLine("x".repeat(500), 80).length, 80);
  assert.ok(oneLine("x".repeat(500), 80).endsWith("x…"));
  assert.equal(oneLine("x".repeat(80), 80), "x".repeat(80));
  // An emoji is one character, as Python's `len` counts it.
  assert.equal(oneLine("🟥".repeat(5), 4), "🟥🟥🟥…");
});

test("a text of white space alone is blank", () => {
  for (const text of ["", " ", "\n\t ", "\x1c\x85 　"]) assert.equal(blank(text), true);
  for (const text of ["a", " a ", "﻿", "​"]) assert.equal(blank(text), false);
});

test("a text is cut in lines where Python cuts it", () => {
  assert.deepEqual(splitLines("a\nb"), ["a", "b"]);
  assert.deepEqual(splitLines("a\nb\n"), ["a", "b"]);
  assert.deepEqual(splitLines("a\n\nb\n\n"), ["a", "", "b", ""]);
  assert.deepEqual(splitLines("\n"), [""]);
  assert.deepEqual(splitLines(""), []);
  assert.deepEqual(splitLines("a\r\nb\rc"), ["a", "b", "c"]);
  // The line boundaries of `str.splitlines` that `\n` and `\r` are not.
  assert.deepEqual(splitLines("a\vb\fc\x1cd\x1de\x1ef\x85g h i"), [
    "a",
    "b",
    "c",
    "d",
    "e",
    "f",
    "g",
    "h",
    "i",
  ]);
  // A unit separator is white space to Python and no line boundary.
  assert.deepEqual(splitLines("a\x1fb"), ["a\x1fb"]);
});
