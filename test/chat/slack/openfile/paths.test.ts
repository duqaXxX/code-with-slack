// Not in the Python suite: Python's `Path` and `sorted` needed no test of their own.
import assert from "node:assert/strict";
import { join, resolve } from "node:path";
import { test } from "node:test";
import { compareCodePoints, relativeTo } from "../../../../src/chat/slack/openfile/paths.ts";

test("paths sort by code point where an array sort goes by UTF-16 unit", () => {
  const astral = "\u{1F600}"; // two units starting at D83D, before U+FFFD in UTF-16
  const bmp = "�";
  assert.ok(astral < bmp); // UTF-16 order puts the astral character first...
  assert.ok(compareCodePoints(astral, bmp) > 0); // ...and code point order puts it last
  assert.deepEqual([astral, bmp, "a", "B"].toSorted(compareCodePoints), ["B", "a", bmp, astral]);
  assert.equal(compareCodePoints("a", "ab"), -1);
  assert.equal(compareCodePoints("ab", "ab"), 0);
});

test("a path is relative to a root when it lies under it, the root itself included", () => {
  // Built with the system's separator: the answer is a path of this system.
  const root = resolve("a", "b");
  assert.equal(relativeTo(root, join(root, "c", "d")), join("c", "d"));
  assert.equal(relativeTo(root, root), "");
  assert.equal(relativeTo(root, `${root}c`), null);
  assert.equal(relativeTo(root, resolve("a")), null);
  assert.equal(relativeTo(root, join(root, "..hidden")), "..hidden");
});
