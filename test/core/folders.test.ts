import assert from "node:assert/strict";
import { chmodSync, mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join, relative } from "node:path";
import { after, test } from "node:test";
import { bindableFolders, FOLDER_ROWS, TRUST_BATCH } from "../../src/core/folders.ts";

// Permission bits mean nothing on Windows, and root reads what a mode forbids.
const NEEDS_PERMISSIONS = { skip: process.platform === "win32" || process.getuid?.() === 0 };

const made: string[] = [];

function scratch(): string {
  const directory = mkdtempSync(join(tmpdir(), "awd-folders-"));
  made.push(directory);
  return directory;
}

after(() => {
  for (const directory of made) rmSync(directory, { recursive: true, force: true });
});

async function trustedUnlessNamedUntrusted(directory: string): Promise<boolean> {
  return !basename(directory).includes("untrusted");
}

function tree(root: string, ...paths: string[]): void {
  for (const path of paths) mkdirSync(join(root, path), { recursive: true });
}

/** The listed folders as `Path.relative_to(root).as_posix()` shows them: the root is ".". */
function relatives(root: string, folders: string[]): string[] {
  return folders.map((folder) => relative(root, folder).split("\\").join("/") || ".");
}

test("lists trusted folders two levels deep in path order", async () => {
  const root = scratch();
  tree(root, "b/one/too-deep", "a", "b/two", "b/untrusted", "c-untrusted/inner", ".hidden");
  const found = await bindableFolders(root, trustedUnlessNamedUntrusted);
  // A trusted folder inside an untrusted one still starts a session, so it is listed.
  const listed = [".", "a", "b", "b/one", "b/two", "c-untrusted/inner"];
  assert.deepEqual(relatives(root, found), listed);
});

test("never descends into a git repository", async () => {
  // A repository is one project: its src/ and tests/ are not folders to bind on their own.
  const root = scratch();
  tree(root, "repo/.git", "repo/src", "worktree/src", "plain/sub");
  writeFileSync(join(root, "worktree", ".git"), "gitdir: elsewhere\n"); // a worktree's .git
  const found = await bindableFolders(root, trustedUnlessNamedUntrusted);
  assert.deepEqual(relatives(root, found).toSorted(), [
    ".",
    "plain",
    "plain/sub",
    "repo",
    "worktree",
  ]);
});

test("skips a symlink that would lead outside", async () => {
  const base = scratch();
  const root = join(base, "root");
  tree(base, "root/app", "outside");
  symlinkSync(join(base, "outside"), join(root, "link"));
  const found = await bindableFolders(root, trustedUnlessNamedUntrusted);
  assert.deepEqual(found, [root, join(root, "app")]);
});

test("stops one past the rows shown", async () => {
  const root = scratch();
  const checked: string[] = [];

  async function counting(directory: string): Promise<boolean> {
    checked.push(directory);
    return true;
  }

  tree(
    root,
    ...Array.from({ length: FOLDER_ROWS + 10 }, (_, i) => `f${String(i).padStart(2, "0")}`),
  );
  const found = await bindableFolders(root, counting);
  assert.equal(found.length, FOLDER_ROWS + 1);
  assert.ok(checked.length < FOLDER_ROWS + 1 + TRUST_BATCH); // the batch that crossed the line, at most
});

// The Python tests that read `bind_blocks` (each folder is a row with a bind button, more folders
// than rows, no trusted folder, the root is shown as written) test Slack blocks, which the chat
// provider builds: they are ported with it.

test("the root is listed and a repository root alone", async () => {
  const root = scratch();
  tree(root, ".git", "src", "tests");
  assert.deepEqual(await bindableFolders(root, trustedUnlessNamedUntrusted), [root]);
});

test("higher levels fill the rows first", async () => {
  // A folder with many subfolders must not hide the projects beside it. The Python test read the
  // rows `bind_blocks` makes of the first FOLDER_ROWS folders; here those folders are read off the
  // list itself.
  const root = scratch();
  tree(
    root,
    ...Array.from(
      { length: FOLDER_ROWS + 5 },
      (_, i) => `archive/old${String(i).padStart(2, "0")}`,
    ),
    "work",
  );
  const found = await bindableFolders(root, trustedUnlessNamedUntrusted);
  const shown = relatives(root, found.slice(0, FOLDER_ROWS));
  assert.ok(shown.includes("work"));
});

test("an unreadable folder is skipped, not fatal", NEEDS_PERMISSIONS, async () => {
  const root = scratch();
  tree(root, "locked", "open");
  chmodSync(join(root, "locked"), 0o600); // listable name, but no stat inside it
  let found: string[];
  try {
    found = await bindableFolders(root, trustedUnlessNamedUntrusted);
  } finally {
    chmodSync(join(root, "locked"), 0o700);
  }
  assert.ok(found.includes(join(root, "open")));
});

test("trust is checked a batch at a time", async () => {
  // Each check runs git: in sequence, a root with many untrusted folders is slow to list.
  const root = scratch();
  let running = 0;
  let most = 0;

  async function slow(_directory: string): Promise<boolean> {
    running += 1;
    most = Math.max(most, running);
    await new Promise((resolve) => setTimeout(resolve, 10));
    running -= 1;
    return false;
  }

  tree(
    root,
    ...Array.from({ length: 3 * TRUST_BATCH }, (_, i) => `f${String(i).padStart(2, "0")}`),
  );
  assert.deepEqual(await bindableFolders(root, slow), []);
  assert.equal(most, TRUST_BATCH);
});

test("an unreadable root is an error, not an empty list", NEEDS_PERMISSIONS, async () => {
  // An empty list would tell the owner to trust folders; the real cause is the root.
  const base = scratch();
  const root = join(base, "root");
  tree(base, "root/a");
  chmodSync(root, 0o000);
  try {
    await assert.rejects(bindableFolders(root, trustedUnlessNamedUntrusted), { code: "EACCES" });
  } finally {
    chmodSync(root, 0o700);
  }
});
