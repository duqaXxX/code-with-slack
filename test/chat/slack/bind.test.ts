import assert from "node:assert/strict";
import { join } from "node:path";
import { test } from "node:test";
import { BIND_ACTION, type BindBlock, bindBlocks } from "../../../src/chat/slack/bind.ts";
import { FOLDER_ROWS } from "../../../src/core/folders.ts";
import * as texts from "../../../src/core/texts.ts";
import { fill } from "../../../src/core/texts.ts";

// ported from the four tests of tests/test_folders.py that read `bind_blocks`

// A directory that need not exist: `bindBlocks` only reads the paths it is given.
const ROOT = join("/srv", "projects");

type FolderRow = Extract<BindBlock, { type: "section" }>;

function rows(blocks: BindBlock[]): FolderRow[] {
  return blocks.filter((block): block is FolderRow => block.type === "section");
}

function firstText(block: BindBlock | undefined): string | undefined {
  return block?.type === "context" ? block.elements[0]?.text : undefined;
}

test("each folder is a row with a bind button the current one marked", () => {
  const folders = [join(ROOT, "a"), join(ROOT, "b", "c&d")];
  const blocks = bindBlocks(ROOT, folders, join(ROOT, "a"));
  assert.equal(firstText(blocks[0]), fill(texts.BIND_LIST, { root: ROOT }));
  const [first, second] = rows(blocks);
  assert.ok(first && second);
  assert.equal(first.text.text, `\`a\`${texts.BIND_CURRENT}`);
  assert.ok(!("accessory" in first));
  assert.equal(second.text.text, "`b/c&amp;d`");
  assert.equal(second.accessory?.action_id, BIND_ACTION);
  assert.equal(BIND_ACTION, "folder_bind");
  assert.equal(second.accessory?.value, "b/c&d");
});

test("more folders than rows says how to reach the others", () => {
  const folders = Array.from({ length: FOLDER_ROWS + 1 }, (_, i) =>
    join(ROOT, `f${String(i).padStart(2, "0")}`),
  );
  const blocks = bindBlocks(ROOT, folders, null);
  assert.equal(rows(blocks).length, FOLDER_ROWS);
  assert.equal(firstText(blocks.at(-1)), fill(texts.BIND_MORE, { rows: FOLDER_ROWS }));
});

test("no trusted folder says how to trust one", () => {
  const blocks = bindBlocks(ROOT, [], null);
  assert.equal(blocks.length, 1);
  assert.equal(firstText(blocks[0]), fill(texts.BIND_EMPTY, { root: ROOT }));
});

test("the root is shown as written", () => {
  const root = join("/srv", "R&D");
  assert.ok(firstText(bindBlocks(root, [join(root, "a")], null)[0])?.includes("R&amp;D"));
  assert.ok(firstText(bindBlocks(root, [], null)[0])?.includes("R&amp;D"));
});
