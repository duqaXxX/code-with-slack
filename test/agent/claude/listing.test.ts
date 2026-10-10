/**
 * The listing of a folder's sessions, their dates and the ids alive, from `test_resume.py`
 * (`by_last_activity`) and `__main__._alive_sessions`, which had no test of its own.
 *
 * The transcripts are written where Claude Code keeps them, under a `CLAUDE_CONFIG_DIR` of the
 * test, in the shape read from real transcripts (CLI 2.1.280). The folder's name in them is
 * derived here independently of `listing.ts`, and the SDK's own `listSessions` has to find them:
 * the test fails when the package names its folders differently.
 */
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { closeSync, constants, openSync } from "node:fs";
import { chmod, mkdir, mkdtemp, realpath, rm, symlink, utimes, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { type TestContext, test } from "node:test";
import {
  aliveSessions,
  byLastActivity,
  directorySessions,
  LONG_PROJECT_KEY,
  lastMessageMs,
  logger,
  projectKey,
  projectsDir,
  RESUME_ROWS,
} from "../../../src/agent/claude/listing.ts";
import type { ListedSession } from "../../../src/agent/seam.ts";

const LIMIT = { timeout: 10_000 };
const HOUR = 3_600_000;
const NOW = new Date(2026, 8, 25, 12, 0).getTime();

/** A config directory and a project folder of the test, with `CLAUDE_CONFIG_DIR` pointing at it. */
async function workspace(t: TestContext): Promise<{ config: string; project: string }> {
  const root = await realpath(await mkdtemp(join(tmpdir(), "awaydesk-listing-")));
  const previous = process.env.CLAUDE_CONFIG_DIR;
  process.env.CLAUDE_CONFIG_DIR = join(root, "config");
  t.after(() => {
    if (previous === undefined) delete process.env.CLAUDE_CONFIG_DIR;
    else process.env.CLAUDE_CONFIG_DIR = previous;
  });
  const project = join(root, "project");
  await mkdir(project);
  return { config: join(root, "config"), project };
}

/** The folder Claude Code keeps `directory`'s transcripts in, by the plain rule. */
function transcriptFolder(config: string, directory: string): string {
  return join(config, "projects", directory.replace(/[^a-zA-Z0-9]/g, "-"));
}

/** A transcript entry with the keys a real one carries (read from real transcripts, CLI 2.1.280). */
function message(kind: "user" | "assistant", when: string, sid: string, text: string) {
  return {
    type: kind,
    timestamp: when,
    sessionId: sid,
    uuid: `${kind}-${when}`,
    parentUuid: null,
    message: { role: kind, content: text },
  };
}

// Claude Code appends these after the last message when a session's artifacts change, with no
// timestamp (seen 2026-09-25): they move the file's mtime, not its activity.
function ledger(sid: string) {
  return { type: "artifact-autoreact-ledger", v: 1, sessionId: sid, artifacts: {} };
}

async function writeTranscript(
  config: string,
  directory: string,
  sid: string,
  lines: readonly object[],
): Promise<string> {
  const folder = transcriptFolder(config, directory);
  await mkdir(folder, { recursive: true });
  const path = join(folder, `${sid}.jsonl`);
  await writeFile(path, lines.map((line) => `${JSON.stringify(line)}\n`).join(""));
  return path;
}

function listed(id: string, hoursAgo: number): ListedSession {
  return {
    id,
    title: id,
    customTitle: null,
    branch: null,
    size: null,
    lastModified: NOW - hoursAgo * HOUR,
  };
}

// The tests of test_resume.py that were left for this module.

test("a session s time is its last message not its file", LIMIT, async (t) => {
  const { config, project } = await workspace(t);
  const old = "68da9311-0000-4000-8000-00000000000a";
  const fresh = "68da9311-0000-4000-8000-00000000000b";
  const stale = await writeTranscript(config, project, old, [
    message("user", "2026-09-22T15:01:10.504Z", old, "Livesqlbench"),
    message("assistant", "2026-09-22T15:40:44.477Z", old, "done"),
    ledger(old),
    ledger(old),
  ]);
  const recent = await writeTranscript(config, project, fresh, [
    message("user", "2026-09-24T18:08:03.063Z", fresh, "Replies"),
    message("assistant", "2026-09-25T09:58:00.438Z", fresh, "ok"),
  ]);
  await utimes(recent, 1_790_000_000, 1_790_000_000); // the older file on disk
  await utimes(stale, 1_790_100_000, 1_790_100_000); // touched later by its ledger

  const found = await directorySessions(project);
  assert.deepEqual(found.map((session) => session.id).sort(), [old, fresh].sort());
  const dated = await byLastActivity(project, found);
  assert.deepEqual(
    dated.map((session) => session.id),
    [fresh, old], // ordered by activity, as the picker
  );
  assert.equal(dated[1]?.lastModified, Date.UTC(2026, 8, 22, 15, 40, 44, 477));
});

test("dating stops once the rest cannot enter the list", LIMIT, async (t) => {
  // A file's mtime bounds its last message from above: past the list's rows, older files cannot
  // overtake them, so their transcripts are not read.
  const { config, project } = await workspace(t);
  await mkdir(transcriptFolder(config, project), { recursive: true });
  const sessions = Array.from({ length: RESUME_ROWS + 10 }, (_, index) =>
    listed(`68da9311-0000-4000-8000-${String(index).padStart(12, "0")}`, index),
  );
  const reads: string[] = [];
  const dated = await byLastActivity(project, sessions, {
    readStamp: async (path) => {
      reads.push(path);
      return null; // no stamp: the mtime stands
    },
  });
  assert.deepEqual(dated, sessions);
  assert.equal(reads.length, RESUME_ROWS);
});

test("dates falling back to file times are logged", LIMIT, async (t) => {
  // A change in the package's naming of folders must not bring the wrong dates back unnoticed.
  const { project } = await workspace(t);
  const warnings: string[] = [];
  t.mock.method(logger, "warning", (line: string) => warnings.push(line));
  const sessions = [listed("68da9311-0000-4000-8000-000000000001", 1)];
  assert.deepEqual(await byLastActivity(project, sessions), sessions);
  assert.equal(warnings.length, 1);
  assert.match(warnings[0] as string, /file times/);
});

// What the Python tests did not pin.

test("a session with no stamp in its transcript keeps its file's time", LIMIT, async (t) => {
  const { config, project } = await workspace(t);
  const sid = "68da9311-0000-4000-8000-00000000000c";
  await writeTranscript(config, project, sid, [ledger(sid)]);
  const [session] = await byLastActivity(project, [listed(sid, 5)]);
  assert.equal(session?.lastModified, NOW - 5 * HOUR);
});

test("a transcript's tail is read from its last user or assistant entry", LIMIT, async (t) => {
  const { config, project } = await workspace(t);
  const sid = "68da9311-0000-4000-8000-00000000000d";
  // 400 KB of entries before the last message: only the end of the file is read.
  const filler = Array.from({ length: 2_000 }, (_, index) =>
    message("user", "2026-01-01T00:00:00.000Z", sid, `${index} ${"x".repeat(200)}`),
  );
  await writeTranscript(config, project, sid, [
    ...filler,
    message("assistant", "2026-09-21T10:00:00.000Z", sid, "last"),
    ledger(sid),
  ]);
  const [session] = await byLastActivity(project, [listed(sid, 5)]);
  assert.equal(session?.lastModified, Date.UTC(2026, 8, 21, 10, 0, 0, 0));
});

test(
  "the sessions of a folder are listed newest first, without its worktrees",
  LIMIT,
  async (t) => {
    const { config, project } = await workspace(t);
    const older = "68da9311-0000-4000-8000-0000000000e1";
    const newer = "68da9311-0000-4000-8000-0000000000e2";
    const one = await writeTranscript(config, project, older, [
      message("user", "2026-09-20T10:00:00.000Z", older, "Older question"),
    ]);
    const two = await writeTranscript(config, project, newer, [
      message("user", "2026-09-21T10:00:00.000Z", newer, "Newer question"),
    ]);
    await utimes(one, 1_790_000_000, 1_790_000_000);
    await utimes(two, 1_790_100_000, 1_790_100_000);
    const found = await directorySessions(project);
    assert.deepEqual(
      found.map((session) => [session.id, session.title, session.lastModified]),
      [
        [newer, "Newer question", 1_790_100_000_000],
        [older, "Older question", 1_790_000_000_000],
      ],
    );
  },
);

// The ids alive, for the state's pruning.

test("the ids alive are the listed sessions and every transcript file there", LIMIT, async (t) => {
  const { config, project } = await workspace(t);
  const titled = "68da9311-0000-4000-8000-0000000000f1";
  const bare = "68da9311-0000-4000-8000-0000000000f2";
  await writeTranscript(config, project, titled, [
    message("user", "2026-09-20T10:00:00.000Z", titled, "A question"),
  ]);
  // Metadata only: the SDK's listing skips it, and a thread may still hold it.
  await writeTranscript(config, project, bare, [ledger(bare)]);
  const listing = await directorySessions(project);
  assert.deepEqual(
    listing.map((session) => session.id),
    [titled],
  );
  assert.deepEqual(await aliveSessions(project), new Set([titled, bare]));
});

test("a folder with no transcripts folder has the listing alone", LIMIT, async (t) => {
  const { project } = await workspace(t);
  assert.deepEqual(await aliveSessions(project), new Set());
});

test(
  "a folder named past the plain limit cannot tell when its transcripts are not found",
  LIMIT,
  async (t) => {
    const { project } = await workspace(t);
    const long = join(project, "d".repeat(LONG_PROJECT_KEY));
    await mkdir(long);
    assert.ok((await projectKey(long)).length > LONG_PROJECT_KEY);
    assert.equal(await aliveSessions(long), null);
  },
);

test("a transcripts folder that cannot be read cannot tell", LIMIT, async (t) => {
  const { config, project } = await workspace(t);
  const sid = "68da9311-0000-4000-8000-0000000000f3";
  await writeTranscript(config, project, sid, [
    message("user", "2026-09-20T10:00:00.000Z", sid, "A question"),
  ]);
  const folder = transcriptFolder(config, project);
  await chmod(folder, 0o000);
  t.after(() => chmod(folder, 0o755));
  // A superuser reads it anyway, and Windows has no such mode.
  if (process.platform === "win32" || process.getuid?.() === 0)
    return t.skip("no unreadable folder");
  assert.equal(await aliveSessions(project), null);
});

test("a long folder name keeps its first 200 characters and a hash", LIMIT, async (t) => {
  const { project } = await workspace(t);
  const long = join(project, "e".repeat(150), "f".repeat(150));
  await mkdir(long, { recursive: true });
  const key = await projectKey(long);
  assert.equal(
    key.slice(0, LONG_PROJECT_KEY),
    (await realpath(long)).replace(/[^a-zA-Z0-9]/g, "-").slice(0, LONG_PROJECT_KEY),
  );
  assert.match(key.slice(LONG_PROJECT_KEY), /^-[0-9a-z]+$/);
});

test("the projects directory is the config directory's, else the owner's home", () => {
  assert.equal(
    projectsDir({ CLAUDE_CONFIG_DIR: "/srv/config" }, "/home/dev"),
    join("/srv/config", "projects"),
  );
  assert.equal(projectsDir({}, "/home/dev"), join("/home/dev", ".claude", "projects"));
  assert.equal(
    projectsDir({ CLAUDE_CONFIG_DIR: "" }, "/home/dev"),
    join("/home/dev", ".claude", "projects"),
  );
});

// The review round of 2026-10-10: a transcript is a regular file, opened without following a link
// and without waiting on a FIFO. Python's `_last_message_ms` used `path.open("rb")`, which follows
// a symlink and blocks on a FIFO; the stricter read is a difference, not a port.

const POSIX = { ...LIMIT, skip: process.platform === "win32" };
const ENTRY = `${JSON.stringify({ type: "user", timestamp: "2026-09-25T10:00:00.000Z" })}\n`;

async function folderOf(t: TestContext): Promise<string> {
  const root = await realpath(await mkdtemp(join(tmpdir(), "awaydesk-stamp-")));
  t.after(() => rm(root, { recursive: true, force: true }));
  return root;
}

test("the last message time of a regular transcript is read", LIMIT, async (t) => {
  const file = join(await folderOf(t), "a.jsonl");
  await writeFile(file, ENTRY);
  assert.equal(await lastMessageMs(file), Date.parse("2026-09-25T10:00:00.000Z"));
});

test("a transcript that is a symlink is not read", POSIX, async (t) => {
  const root = await folderOf(t);
  await writeFile(join(root, "elsewhere.jsonl"), ENTRY);
  await symlink(join(root, "elsewhere.jsonl"), join(root, "a.jsonl"));
  assert.equal(await lastMessageMs(join(root, "a.jsonl")), null);
});

test("a transcript that is a FIFO does not hold the read", POSIX, async (t) => {
  const file = join(await folderOf(t), "a.jsonl");
  execFileSync("mkfifo", [file]);
  let timer: NodeJS.Timeout | undefined;
  const expired = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error("the read is blocked on the FIFO")), 2_000);
  });
  try {
    assert.equal(await Promise.race([lastMessageMs(file), expired]), null);
  } finally {
    clearTimeout(timer);
    // A read that is still blocked in the pool is released by a writer, so the run can end.
    closeSync(openSync(file, constants.O_RDWR | constants.O_NONBLOCK));
  }
});
