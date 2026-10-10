import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { basename, join } from "node:path";
import { test } from "node:test";
import { setImmediate as nextTurn } from "node:timers/promises";
import type { Repository } from "../../../../src/agent/seam.ts";
import {
  changedIn,
  changedSince,
  startCommit,
} from "../../../../src/chat/slack/openfile/changed.ts";
import { projectFiles } from "../../../../src/chat/slack/openfile/listing.ts";
import { newestFirst } from "../../../../src/chat/slack/openfile/read.ts";
import { FakeClock } from "../../../support/fake-slack.ts";
import { commitAt, committed, git, gitAt, gitInit } from "../../../support/git-layouts.ts";
import { GIT_LAYOUT } from "../../../support/platform.ts";
import {
  commitAll,
  commitOf,
  dated,
  datedRepo,
  emptyTree,
  repository,
  scratch,
  started,
  T0,
  write,
} from "./support.ts";

const tmp = scratch();

/** A repository with one commit, `README`: the session's folder when it is itself one. */
function app(): string {
  return committed(join(tmp.dir, "app"));
}

/** A session's folder that is not a repository, with repositories inside it. */
function work(): string {
  const folder = join(tmp.dir, "work");
  mkdirSync(folder, { recursive: true });
  return folder;
}

function fakeRepository(root: string): Repository {
  return { root, key: root, gitDir: join(root, ".git"), insideGitDir: false };
}

// --- the start commit and the changed files ---

test("the start is where head was when the thread started", GIT_LAYOUT, async () => {
  const repo = dated(join(tmp.dir, "dated"));
  const found = await repository(repo);
  assert.equal(await startCommit(found, started(T0 + 250)), commitOf(repo, 1));
  assert.equal(await startCommit(found, started(T0 + 200)), commitOf(repo, 1));
  assert.equal(await startCommit(found, started(T0 + 199)), commitOf(repo, 2));
  // A thread started after the last move: HEAD itself.
  assert.equal(await startCommit(found, started(T0 + 300)), commitOf(repo, 0));
  assert.equal(await startCommit(found, started(T0 + 9999)), commitOf(repo, 0));
});

test("a reflog that does not go back that far gives its oldest entry", GIT_LAYOUT, async () => {
  // What `git reflog expire` leaves: the first entry gone. git answers the oldest it has (and
  // warns on stderr, which the daemon never reads).
  const repo = dated(join(tmp.dir, "dated"));
  const log = join(repo, ".git", "logs", "HEAD");
  const lines = readFileSync(log, "utf8").split(/(?<=\n)/);
  writeFileSync(log, lines.slice(1).join(""));
  assert.equal(await startCommit(await repository(repo), started(T0 + 10)), commitOf(repo, 2));
});

test("a repository made after the thread began counts every file", GIT_LAYOUT, async () => {
  // git answers the first commit itself for a time before the log began, so the first commit's
  // files would never be listed: the start is the empty tree.
  const repo = dated(join(tmp.dir, "dated"));
  const found = await repository(repo);
  const start = await startCommit(found, started(T0 + 10));
  assert.equal(start, emptyTree(repo));
  assert.notEqual(start, commitOf(repo, 2));
  assert.deepEqual(((await changedSince(found, repo, start)) ?? []).toSorted(), [
    "c1.py",
    "c2.py",
    "c3.py",
  ]);
  // A thread that began once the first commit was made counts from it, as before.
  assert.equal(await startCommit(found, started(T0 + 100)), commitOf(repo, 2));
  assert.deepEqual(await changedIn(repo, [found], started(T0 + 10)), ["c1.py", "c2.py", "c3.py"]);
});

test("a clone made after the thread began counts every file", GIT_LAYOUT, async () => {
  // The log of a clone starts with `clone: from ...`, whose old value is null as well.
  const source = dated(join(tmp.dir, "dated"));
  const clone = join(tmp.dir, "clone");
  gitAt(tmp.dir, T0 + 500, "clone", "-q", source, clone);
  const found = await repository(clone);
  assert.equal(await startCommit(found, started(T0 + 400)), emptyTree(clone));
  assert.equal(await startCommit(found, started(T0 + 600)), commitOf(clone, 0));
});

test("the empty tree is the one of the repository s hash algorithm", GIT_LAYOUT, async () => {
  const repo = join(tmp.dir, "sha256");
  mkdirSync(repo);
  git(repo, "init", "-q", "-b", "main", "--object-format=sha256");
  commitAt(repo, "a.py", T0 + 100);
  const start = await startCommit(await repository(repo), started(T0 + 10));
  assert.equal(start, emptyTree(repo));
  assert.equal((start ?? "").length, 64);
});

test("without a reflog there is no start", GIT_LAYOUT, async () => {
  const repo = gitInit(join(tmp.dir, "unlogged"));
  git(repo, "config", "core.logAllRefUpdates", "false");
  commitAt(repo, "a.py", T0 + 100);
  assert.ok(!existsSync(join(repo, ".git", "logs")));
  assert.equal(await startCommit(await repository(repo), started(T0 + 150)), null);
  // The list is then what is uncommitted or untracked now.
  write(repo, "b.py");
  commitAt(repo, "c.py", T0 + 200);
  write(repo, "d.py");
  assert.deepEqual(await changedIn(repo, [await repository(repo)], started(T0 + 150)), ["d.py"]);
});

test("a repository with no commit has no start and lists what is staged", GIT_LAYOUT, async () => {
  const fresh = gitInit(join(tmp.dir, "fresh"));
  write(fresh, "staged.py");
  git(fresh, "add", "staged.py");
  write(fresh, "untracked.py");
  assert.equal(await startCommit(await repository(fresh), started(T0)), null);
  const found = await changedIn(fresh, [await repository(fresh)], started(T0));
  assert.deepEqual(found.toSorted(), ["staged.py", "untracked.py"]);
});

test("a linked worktree has its own start", GIT_LAYOUT, async () => {
  const main = gitInit(join(tmp.dir, "main"));
  commitAt(main, "a.py", T0 + 100);
  const second = commitAt(main, "b.py", T0 + 200);
  const wt = join(tmp.dir, "wt");
  gitAt(main, T0 + 250, "worktree", "add", "-q", wt, "-b", "feature");
  const third = commitAt(wt, "c.py", T0 + 400);
  const found = await repository(wt);
  assert.equal(found.key, main);
  assert.notEqual(found.gitDir, join(main, ".git"));
  assert.equal(await startCommit(found, started(T0 + 300)), second);
  assert.equal(await startCommit(found, started(T0 + 450)), third);
  // The main checkout's own log is another one: it never saw the worktree's commit.
  assert.equal(await startCommit(await repository(main), started(T0 + 450)), second);
  assert.deepEqual(await changedSince(found, wt, second), ["c.py"]);
});

test(
  "a thread that started before a branch switch counts from where it began",
  GIT_LAYOUT,
  async () => {
    const repo = gitInit(join(tmp.dir, "switching"));
    commitAt(repo, "base.py", T0 + 100);
    const mainTip = commitAt(repo, "on_main.py", T0 + 200);
    gitAt(repo, T0 + 300, "switch", "-q", "-c", "feature");
    commitAt(repo, "on_feature.py", T0 + 400);
    // Begun on main, now on feature: what feature holds beyond main's tip is what changed.
    const start = await startCommit(await repository(repo), started(T0 + 250));
    assert.equal(start, mainTip);
    assert.deepEqual(await changedSince(await repository(repo), repo, start), ["on_feature.py"]);
    // Begun on feature, now back on main: the files of feature are not there to be opened.
    gitAt(repo, T0 + 500, "switch", "-q", "main");
    const begunOnFeature = await startCommit(await repository(repo), started(T0 + 450));
    assert.equal(begunOnFeature, git(repo, "rev-parse", "feature"));
    assert.deepEqual(await changedSince(await repository(repo), repo, begunOnFeature), []);
  },
);

for (const ts of ["", "abc", "1790000000.x", "-5", "1790000000 +0000", "@{1}", "../x"]) {
  test(
    `a thread ts that is no epoch second gives no start [${ts === "" ? "empty" : ts}]`,
    GIT_LAYOUT,
    async () => {
      const repo = dated(join(tmp.dir, "dated"));
      assert.equal(await startCommit(await repository(repo), ts), null);
    },
  );
}

test("a start that git cannot read is none", GIT_LAYOUT, async () => {
  const repo = dated(join(tmp.dir, "dated"));
  const found = await repository(repo);
  const broken = { ...found, gitDir: join(repo, "no-such-git-dir") };
  assert.equal(await startCommit(broken, started(T0 + 250)), null);
});

test("the changed files are those added or modified since the start", GIT_LAYOUT, async () => {
  const folder = app();
  write(folder, "kept.py");
  write(folder, "gone.py");
  write(folder, "reverted.py", "one\n");
  commitAll(folder);
  const start = git(folder, "rev-parse", "HEAD");
  // Committed since the start.
  write(folder, "committed_new.py");
  write(folder, "kept.py", "changed\n");
  write(folder, "reverted.py", "two\n");
  commitAll(folder);
  write(folder, "reverted.py", "one\n");
  commitAll(folder);
  // Not committed: staged, unstaged and untracked; a deletion; an ignored file.
  write(folder, "staged.py");
  git(folder, "add", "staged.py");
  write(folder, "README", "edited\n");
  write(folder, "untracked.py");
  rmSync(join(folder, "gone.py"));
  write(folder, ".gitignore", "ignored.py\n");
  write(folder, "ignored.py");
  const found = await changedSince(await repository(folder), folder, start);
  assert.notEqual(found, null);
  // `reverted.py` was committed changed and committed back: no net change since the start.
  assert.deepEqual((found ?? []).toSorted(), [
    ".gitignore",
    "README",
    "committed_new.py",
    "gone.py", // named, though gone: dropped when the list is made, below
    "kept.py",
    "staged.py",
    "untracked.py",
  ]);
  assert.deepEqual((await newestFirst(folder, found ?? [])).toSorted(), [
    ".gitignore",
    "README",
    "committed_new.py",
    "kept.py",
    "staged.py",
    "untracked.py",
  ]);
});

test("without a start commit only the uncommitted files count", GIT_LAYOUT, async () => {
  const folder = app();
  write(folder, "untracked.py");
  write(folder, "README", "edited\n");
  const found = await changedSince(await repository(folder), folder, null);
  assert.deepEqual((found ?? []).toSorted(), ["README", "untracked.py"]);
});

test("the changed files of a subfolder are relative to it", GIT_LAYOUT, async () => {
  const folder = app();
  write(folder, "sub/a.py");
  commitAll(folder);
  const start = git(folder, "rev-parse", "HEAD");
  write(folder, "sub/b.py");
  write(folder, "elsewhere.py");
  commitAll(folder);
  write(folder, "sub/c.py");
  write(folder, "elsewhere2.py");
  const found = await changedSince(await repository(folder), join(folder, "sub"), start);
  assert.deepEqual((found ?? []).toSorted(), ["b.py", "c.py"]);
});

// --- git never writes the index here (measured on git 2.54.0, 2026-10-05) ---

function indexState(repo: string): [bigint, bigint, string] {
  const index = join(repo, ".git", "index");
  const found = statSync(index, { bigint: true });
  return [found.ino, found.mtimeNs, createHash("sha256").update(readFileSync(index)).digest("hex")];
}

/** A tracked file whose stat differs from the index and whose content does not. */
function touched(repo: string): void {
  const future = Date.now() / 1000 + 1000;
  utimesSync(join(repo, "README"), future, future);
}

function edited(repo: string): void {
  write(repo, "README", "edited\n");
}

function untracked(repo: string): void {
  write(repo, "new.py");
}

/** Files older than the index, so that no entry is racily clean and hides a change. */
function settled(repo: string): void {
  const past = Date.now() / 1000 - 100;
  const visit = (directory: string): void => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      if (entry.name === ".git") continue;
      const path = join(directory, entry.name);
      if (entry.isDirectory()) visit(path);
      else if (entry.isFile()) utimesSync(path, past, past);
    }
  };
  visit(repo);
  git(repo, "update-index", "--refresh");
}

const CHANGES: Record<string, (repo: string) => void> = {
  clean: () => {},
  touched,
  edited,
  untracked,
};

for (const [name, change] of Object.entries(CHANGES)) {
  test(`no command of open writes the index or leaves a lock [${name}]`, GIT_LAYOUT, async () => {
    const folder = app();
    write(folder, "sub/more.py");
    commitAll(folder);
    const start = git(folder, "rev-parse", "HEAD");
    write(folder, "sub/after.py");
    commitAll(folder);
    settled(folder);
    change(folder);
    const before = indexState(folder);
    const found = await repository(folder);
    // Each command ran and answered, so that an unchanged index is not a command that failed:
    // a thread that began before this repository was made runs every command of `startCommit`
    // (the time, the entries up to it, all the entries, the first one's old value, the empty tree).
    assert.equal(await startCommit(found, started(1)), emptyTree(folder));
    assert.equal(
      await startCommit(found, started(Math.trunc(Date.now() / 1000) + 1)),
      git(folder, "rev-parse", "HEAD"),
    );
    await projectFiles(found, folder);
    await projectFiles(found, join(folder, "sub"));
    await changedSince(found, folder, start);
    await changedSince(found, join(folder, "sub"), start);
    assert.deepEqual(indexState(folder), before);
    assert.ok(!existsSync(join(folder, ".git", "index.lock")));
  });
}

test("a file that was only touched is not reported as changed", GIT_LAYOUT, async () => {
  const folder = app();
  settled(folder);
  touched(folder);
  const found = await changedSince(
    await repository(folder),
    folder,
    git(folder, "rev-parse", "HEAD"),
  );
  assert.deepEqual(found, []);
  // The commands the footer uses report it, which is why they are not used here.
  assert.equal(git(folder, "diff-files", "--name-only"), "README");
});

test("the measurement would notice a write", GIT_LAYOUT, () => {
  // A control: `git status` without `--no-optional-locks` refreshes the index of a touched
  // file, which is what the assertions above are made to catch.
  const folder = app();
  settled(folder);
  touched(folder);
  const before = indexState(folder);
  git(folder, "status", "--porcelain");
  assert.notDeepEqual(indexState(folder), before);
});

// --- the changes of every repository of the folder ---

test("git runs in at most a few repositories at once", async () => {
  const folder = work();
  let active = 0;
  let peak = 0;
  let done = 0;
  const start: typeof startCommit = async () => {
    active += 1;
    peak = Math.max(peak, active);
    await nextTurn();
    return null;
  };
  const since: typeof changedSince = async (found) => {
    active -= 1;
    done += 1;
    return [`${basename(found.root)}.py`];
  };
  const repositories = Array.from({ length: 12 }, (_, i) =>
    fakeRepository(join(folder, `r${String(i).padStart(2, "0")}`)),
  );
  const found = await changedIn(folder, repositories, started(T0), {
    chains: 3,
    start,
    since,
    clock: new FakeClock(),
  });
  assert.equal(done, 12);
  assert.equal(found.length, 12);
  assert.equal(peak, 3);
});

test("the changes of nested repositories are relative to the folder", GIT_LAYOUT, async () => {
  const folder = work();
  const one = datedRepo(join(folder, "one"));
  const two = datedRepo(join(folder, "group", "two"));
  write(one, "a.py");
  commitAt(two, "b.py", T0 + 400);
  write(two, "c.py");
  const found = await changedIn(
    folder,
    [await repository(one), await repository(two)],
    started(T0 + 300),
  );
  assert.deepEqual(found.toSorted(), ["group/two/b.py", "group/two/c.py", "one/a.py"]);
});

test("each repository is counted from where its own head was", GIT_LAYOUT, async () => {
  const folder = work();
  const one = datedRepo(join(folder, "one"));
  const two = datedRepo(join(folder, "two"));
  commitAt(one, "before.py", T0 + 200); // before the thread began
  commitAt(two, "also_before.py", T0 + 250);
  commitAt(one, "during_one.py", T0 + 400);
  commitAt(two, "during_two.py", T0 + 500);
  const found = await changedIn(
    folder,
    [await repository(one), await repository(two)],
    started(T0 + 300),
  );
  assert.deepEqual(found.toSorted(), ["one/during_one.py", "two/during_two.py"]);
});

test("the start comes from the log and survives a new process", GIT_LAYOUT, async () => {
  // Nothing is kept in memory: a daemon restarted after every change counts the same.
  const folder = work();
  const one = datedRepo(join(folder, "one"));
  commitAt(one, "during.py", T0 + 400);
  const first = await changedIn(folder, [await repository(one)], started(T0 + 300));
  const second = await changedIn(folder, [await repository(one)], started(T0 + 300));
  assert.deepEqual(first, ["one/during.py"]);
  assert.deepEqual(second, first);
});

test("a repository whose git fails adds no changes", GIT_LAYOUT, async () => {
  const folder = work();
  const one = datedRepo(join(folder, "one"));
  write(one, "a.py");
  const found = await repository(one);
  const broken = { ...found, gitDir: join(folder, "no-such-git-dir") };
  assert.deepEqual(await changedIn(folder, [broken], started(T0 + 150)), []);
  assert.deepEqual(await changedIn(folder, [found], started(T0 + 150)), ["one/a.py"]);
});

test("no repository has no changes", async () => {
  assert.deepEqual(await changedIn(work(), [], started(T0)), []);
});
