import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { constants, mkdirSync, symlinkSync, unlinkSync, utimesSync, writeFileSync } from "node:fs";
import { open } from "node:fs/promises";
import { join } from "node:path";
import { test } from "node:test";
import {
  NotAFile,
  newestFirst,
  type Opened,
  readOnce,
  readOpenable,
  regularFiles,
  SNIPPET_LIMIT,
  TooLarge,
  titleOf,
  uploadFile,
} from "../../../../src/chat/slack/openfile/read.ts";
import { FakeSlack } from "../../../support/fake-slack.ts";
import { POSIX, scratch, write } from "./support.ts";

const tmp = scratch();

/** The session's folder: `app` under the scratch folder. */
function app(): string {
  const path = join(tmp.dir, "app");
  mkdirSync(path, { recursive: true });
  return path;
}

// --- what may be shared ---

test("a regular file inside the folder is read", async () => {
  const folder = app();
  write(folder, "docs/a b.md", "# a\n");
  assert.deepEqual(await readOpenable(folder, "docs/a b.md"), Buffer.from("# a\n"));
  assert.deepEqual(await readOpenable(folder, "./docs/../docs/a b.md"), Buffer.from("# a\n"));
});

for (const relative of ["", "..", "../outside.txt", "missing.txt", "docs", "a\0b"]) {
  test(`what is not a file inside the folder is refused [${relative === "" ? "empty" : relative.replace("\0", "\\0")}]`, async () => {
    const folder = app();
    write(folder, "docs/a.md");
    write(tmp.dir, "outside.txt");
    await assert.rejects(readOpenable(folder, relative), NotAFile);
  });
}

test("an absolute path is refused even when it is a file", async () => {
  const outside = write(tmp.dir, "outside.txt");
  await assert.rejects(readOpenable(app(), outside), NotAFile);
});

test("a symlink that leaves the folder is refused after it is resolved", POSIX, async () => {
  const folder = app();
  const outside = write(tmp.dir, "outside/secret.txt");
  symlinkSync(outside, join(folder, "link.txt"));
  symlinkSync(join(tmp.dir, "outside"), join(folder, "dir-link"));
  const inside = write(folder, "real.txt", "inside\n");
  symlinkSync(inside, join(folder, "alias.txt"));
  for (const relative of ["link.txt", "dir-link/secret.txt"]) {
    await assert.rejects(readOpenable(folder, relative), NotAFile);
  }
  // A link that stays inside the folder is a file.
  assert.deepEqual(await readOpenable(folder, "alias.txt"), Buffer.from("inside\n"));
});

test("a path swapped for a link after it was resolved is not followed", POSIX, async () => {
  // The check and the read are one open: a file that becomes a link between resolving its path
  // and opening it is refused, not read from wherever the link leads. Python patched `_locate` to
  // hand back the stale path; here `readOnce` is handed it, since it is the part that opens.
  const folder = app();
  const outside = write(tmp.dir, "outside/secret.txt", "OUTSIDE\n");
  const inside = write(folder, "notes.md", "inside\n");
  unlinkSync(inside);
  symlinkSync(outside, inside);
  await assert.rejects(readOnce(inside), NotAFile);
});

test("a fifo is refused and never waited on", POSIX, async () => {
  const folder = app();
  execFileSync("mkfifo", [join(folder, "pipe")]);
  await assert.rejects(readOpenable(folder, "pipe"), NotAFile);
});

test("a file over one megabyte is too large", async () => {
  const folder = app();
  writeFileSync(join(folder, "big.bin"), Buffer.alloc(1 << 20, "x"));
  writeFileSync(join(folder, "bigger.bin"), Buffer.alloc((1 << 20) + 1, "x"));
  assert.equal((await readOpenable(folder, "big.bin")).length, 1 << 20);
  await assert.rejects(readOpenable(folder, "bigger.bin"), TooLarge);
});

test("a file that grows while it is read never gives more than the limit", async () => {
  // `fstat` said it fits; by the time it is read it holds more. Python patched `os.fstat`; here
  // the handle's `stat` is wrapped to grow the file when it is asked.
  const folder = app();
  const path = write(folder, "growing.log", "x".repeat(10));
  const grows = async (target: string): Promise<Opened> => {
    const handle = await open(target, constants.O_RDONLY);
    return {
      stat: async () => {
        const found = await handle.stat();
        writeFileSync(target, Buffer.alloc(SNIPPET_LIMIT * 3, "x"));
        return found;
      },
      read: (buffer, offset, length, position) => handle.read(buffer, offset, length, position),
      close: () => handle.close(),
    };
  };
  await assert.rejects(readOnce(path, grows), TooLarge);
});

test("regular files keeps what exists in order and stops at the limit", async () => {
  const folder = app();
  for (const name of ["a.py", "b.py", "c.py"]) write(folder, name);
  const names = ["a.py", "gone.py", "docs", "b.py", "c.py"];
  mkdirSync(join(folder, "docs"));
  assert.deepEqual(await regularFiles(folder, names), ["a.py", "b.py", "c.py"]);
  assert.deepEqual(await regularFiles(folder, names, 2), ["a.py", "b.py"]);
});

test("regular files reads its paths only as far as the limit needs", async () => {
  // What Python's `test_a_search_checks_files_only_until_the_limit...` counted by patching
  // `_locate`: a path is checked as it is taken from the iterable, and no more are taken.
  const folder = app();
  const names = Array.from(
    { length: 40 },
    (_, index) => `part${String(index).padStart(2, "0")}.csv`,
  );
  for (const name of names) write(folder, name);
  const taken: string[] = [];
  function* watched(): Generator<string> {
    for (const name of names) {
      taken.push(name);
      yield name;
    }
  }
  assert.equal((await regularFiles(folder, watched(), 10)).length, 10);
  assert.equal(taken.length, 10);
});

// --- the changed files, newest first ---

test("the changed files come newest first and only the existing ones", async () => {
  const folder = app();
  const stamps: Record<string, number> = { "old.py": 0, "new.py": 200, "middle.py": 100 };
  for (const [name, offset] of Object.entries(stamps)) {
    const path = write(folder, name);
    utimesSync(path, 1_780_000_000 + offset, 1_780_000_000 + offset);
  }
  mkdirSync(join(folder, "dir"));
  assert.deepEqual(
    await newestFirst(folder, ["old.py", "dir", "new.py", "missing.py", "middle.py", "new.py"]),
    ["new.py", "middle.py", "old.py"],
  );
});

test("a changed file that leaves the folder through a link is not listed", POSIX, async () => {
  const folder = app();
  symlinkSync(write(tmp.dir, "outside.txt"), join(folder, "leak.txt"));
  assert.deepEqual(await newestFirst(folder, ["leak.txt"]), []);
});

test("files modified at the same time come in the order of their paths", async () => {
  // Extra, not in the Python file: the tie-break of `newest_first`, in code point order.
  const folder = app();
  for (const name of ["b.py", "B.py", "a.py"]) {
    utimesSync(write(folder, name), 1_780_000_000, 1_780_000_000);
  }
  assert.deepEqual(await newestFirst(folder, ["b.py", "a.py", "B.py"]), ["B.py", "a.py", "b.py"]);
});

// --- the path and the upload (extra: Python tested these with the handlers) ---

test(
  "a path that goes back up through a link is resolved where the link leads",
  POSIX,
  async () => {
    // Extra: `realpath` follows `..` after the link before it, as Python's does. Lexical
    // normalization would turn `link/../inside.txt` into the folder's own file.
    const folder = app();
    write(folder, "inside.txt", "inside\n");
    write(tmp.dir, "outside/sub/x.txt");
    symlinkSync(join(tmp.dir, "outside", "sub"), join(folder, "link"));
    await assert.rejects(readOpenable(folder, "link/../inside.txt"), NotAFile);
  },
);

test("the title is the path normalized the way posixpath.normpath does", () => {
  assert.equal(titleOf("./docs/../docs/a.md"), "docs/a.md");
  assert.equal(titleOf("docs//a.md"), "docs/a.md");
  assert.equal(titleOf("docs/"), "docs");
  assert.equal(titleOf("a.md"), "a.md");
});

test("a file is shared as its bytes, named by its basename and titled by its path", async () => {
  const slack = new FakeSlack();
  const bytes = Buffer.from([0x00, 0xff, 0xfe, 0x41, 0x80]);
  await uploadFile(slack, "C000CHAN", "1790000000.000001", bytes, "./docs/../docs/a.bin");
  const [url, done] = slack.apiCalls;
  assert.equal(url?.method, "files.getUploadURLExternal");
  assert.equal(url?.args.filename, "a.bin");
  assert.equal(url?.args.length, bytes.length);
  assert.equal(done?.method, "files.completeUploadExternal");
  assert.equal(done?.args.channel_id, "C000CHAN");
  assert.equal(done?.args.thread_ts, "1790000000.000001");
  assert.deepEqual(done?.args.files, [{ id: "F000FILE", title: "docs/a.bin" }]);
  // The bytes reached the upload URL exactly, a binary file included.
  assert.deepEqual(
    slack.uploaded.map((posted) => posted.data),
    [bytes],
  );
});
