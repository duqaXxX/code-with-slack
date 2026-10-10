/**
 * The footer's branch and changes: git runs only on a repository the owner trusted, named to it
 * outright, so no folder's own config or attributes decide what runs.
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  chmodSync,
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  realpathSync,
  renameSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterEach, beforeEach, test as nodeTest } from "node:test";
import { trustedRepository } from "../../src/agent/claude/trust.ts";
import type { Repository } from "../../src/agent/seam.ts";
import { gitState } from "../../src/core/footer.ts";
import { addWorktree, bareLayout, committed, git, trust } from "../support/git-layouts.ts";
import { GIT_LAYOUT } from "../support/platform.ts";

const test = GIT_LAYOUT.skip ? nodeTest.skip : nodeTest;

type Lookup = (directory: string) => Promise<Repository | null>;

let tmp = "";
let marker = "";
let app = "";
let repository: Lookup;

/**
 * The footer's lookup of a session started in `sessionFolder`. The tests below plant their
 * repositories outside it, as a folder the agent moved to: only what lies inside the session's
 * folder has a way in beside the owner's own trust (`trust.test.ts`).
 */
function lookupFor(home: string, sessionFolder: string): Lookup {
  return (directory) => trustedRepository(directory, sessionFolder, home);
}

/** A fresh home for `trust`, in the test's own directory. */
function newHome(): string {
  const home = join(tmp, "home");
  mkdirSync(home, { recursive: true });
  return home;
}

beforeEach(() => {
  tmp = realpathSync.native(mkdtempSync(join(tmpdir(), "awaydesk-footer-git-")));
  // The file a planted filter touches when git runs it.
  marker = join(tmp, "filter-ran");
  // A repository the owner trusted, in a folder `code` trusted too.
  app = realpathSync(committed(join(tmp, "code", "app")));
  trust(newHome(), [app, dirname(app)]);
  repository = lookupFor(join(tmp, "home"), join(tmp, "base"));
});

afterEach(() => {
  rmSync(tmp, { recursive: true, force: true });
});

const POSIX = { skip: process.platform === "win32" };

function cleanFilter(): string {
  return `sh -c 'touch "${marker}"; cat'`;
}

/**
 * Someone else's repository: a clean filter in its own config, bound to every path, and a
 * tracked file whose content git must read to diff it.
 */
function planted(path: string): string {
  committed(path);
  git(path, "config", "filter.planted.clean", cleanFilter());
  mkdirSync(join(path, ".git", "info"), { recursive: true });
  writeFileSync(join(path, ".git", "info", "attributes"), "* filter=planted\n");
  writeFileSync(join(path, "README"), "two\nthree\n");
  return path;
}

function aRepositoryUnderATrustedFolder(): string {
  return planted(join(dirname(app), "clone"));
}

function anEmbeddedBareLayout(): string {
  const seed = planted(join(dirname(app), "seed"));
  const layout = join(app, "vendor");
  renameSync(join(seed, ".git"), layout);
  mkdirSync(join(layout, "tree"));
  writeFileSync(join(layout, "tree", "README"), "two\nthree\n");
  git(layout, "config", "-f", "config", "core.bare", "false");
  git(layout, "config", "-f", "config", "core.worktree", join(layout, "tree"));
  return layout;
}

function coreWorktreeNamingTheTrustedRoot(): string {
  const clone = planted(join(dirname(app), "clone"));
  git(clone, "config", "core.worktree", app);
  return clone;
}

function aCommondirBorrowingTheTrustedRepository(): string {
  // The owner's repository has per-worktree config on; the planted git dir brings its own.
  git(app, "config", "core.repositoryformatversion", "1");
  git(app, "config", "extensions.worktreeConfig", "true");
  const folder = join(dirname(app), "clone");
  mkdirSync(join(folder, ".git"), { recursive: true });
  writeFileSync(join(folder, ".git", "HEAD"), "ref: refs/heads/main\n");
  writeFileSync(join(folder, ".git", "commondir"), `${app}/.git\n`);
  const worktreeConfig = join(folder, ".git", "config.worktree");
  git(dirname(folder), "config", "-f", worktreeConfig, "filter.planted.clean", cleanFilter());
  copyFileSync(join(app, ".git", "index"), join(folder, ".git", "index"));
  writeFileSync(join(folder, ".gitattributes"), "* filter=planted\n");
  writeFileSync(join(folder, "README"), "two\nthree\n");
  return folder;
}

function aGitfileToAPlantedGitDir(): string {
  const code = dirname(app);
  const seed = planted(join(code, "seed"));
  renameSync(join(seed, ".git"), join(code, "planted-git-dir"));
  git(code, "config", "-f", "planted-git-dir/config", "core.worktree", app);
  const folder = join(code, "clone");
  mkdirSync(folder);
  writeFileSync(join(folder, ".git"), `gitdir: ${code}/planted-git-dir\n`);
  return folder;
}

function aSiblingNamedWithATrailingSpace(): string {
  return planted(join(dirname(app), "app "));
}

function aGitfileToAGitDirNamedWithATrailingSpace(): string {
  const seed = planted(join(dirname(app), "seed"));
  renameSync(join(seed, ".git"), join(app, ".git "));
  mkdirSync(join(app, "dep"));
  writeFileSync(join(app, "dep", ".git"), "gitdir: ../.git \n");
  writeFileSync(join(app, "dep", "README"), "two\nthree\n");
  return join(app, "dep");
}

function aGitfileBorrowingTheTrustedGitDir(): string {
  // The filter is the owner's own, in the trusted repository's config: the borrowing folder's
  // attributes choose to run it on files the owner never put there.
  git(app, "config", "filter.owned.clean", cleanFilter());
  mkdirSync(join(app, "dep"));
  writeFileSync(join(app, "dep", ".git"), "gitdir: ../.git\n");
  writeFileSync(join(app, "dep", ".gitattributes"), "* filter=owned\n");
  writeFileSync(join(app, "dep", "README"), "not the owner's file\n");
  return join(app, "dep");
}

const PLANTED: readonly [string, () => string][] = [
  ["a_repository_under_a_trusted_folder", aRepositoryUnderATrustedFolder],
  ["an_embedded_bare_layout", anEmbeddedBareLayout],
  ["core_worktree_naming_the_trusted_root", coreWorktreeNamingTheTrustedRoot],
  ["a_commondir_borrowing_the_trusted_repository", aCommondirBorrowingTheTrustedRepository],
  ["a_gitfile_to_a_planted_git_dir", aGitfileToAPlantedGitDir],
  ["a_sibling_named_with_a_trailing_space", aSiblingNamedWithATrailingSpace],
  ["a_gitfile_to_a_git_dir_named_with_a_trailing_space", aGitfileToAGitDirNamedWithATrailingSpace],
  ["a_gitfile_borrowing_the_trusted_git_dir", aGitfileBorrowingTheTrustedGitDir],
];

/** Whether the planted filter ran since the last look. */
function filterRan(): boolean {
  const ran = existsSync(marker);
  rmSync(marker, { force: true });
  return ran;
}

/** What a footer that lets git find the repository runs in `folder`. */
function gitDiffsThere(folder: string): void {
  const env = { ...process.env };
  for (const name of ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"]) {
    delete env[name];
  }
  spawnSync("git", ["-C", folder, "diff-files", "--shortstat"], { env, stdio: "ignore" });
}

for (const [name, plant] of PLANTED) {
  test(`no filter runs where the owner trusted no repository [${name}]`, async () => {
    const folder = plant();
    filterRan(); // building the folder ran it once
    // The control: git, left to find the repository from the folder, runs the filter.
    gitDiffsThere(folder);
    assert.ok(filterRan());
    assert.deepEqual(await gitState(folder, repository), [null, null]);
    assert.ok(!filterRan());
  });
}

test("a nested repository staged as a gitlink is not entered", async () => {
  const nested = planted(join(app, "nested"));
  writeFileSync(join(nested, "README"), "two\n"); // the committed size: only its content tells
  git(app, "add", "nested");
  git(app, "commit", "-q", "-m", "gitlink");
  filterRan();
  // The control: a plain diff asks the nested repository whether it is dirty, under its config.
  gitDiffsThere(app);
  assert.ok(filterRan());
  assert.deepEqual(await gitState(app, repository), ["main", [0, 0]]);
  assert.ok(!filterRan());
});

test("a submodule counts by its commit alone", async () => {
  // git never looks inside a submodule for the footer, which is what keeps it out of a planted
  // one. Its files therefore count nothing, and a checked-out commit that differs counts one
  // line each way, also where the submodule is set to `ignore = all`.
  git(app, "submodule", "add", "-q", committed(join(tmp, "lib")), "sub");
  git(app, "config", "-f", ".gitmodules", "submodule.sub.ignore", "all");
  git(app, "commit", "-q", "-am", "sub");
  writeFileSync(join(app, "sub", "new"), "untracked\n");
  writeFileSync(join(app, "sub", "README"), "changed\n");
  assert.deepEqual(await gitState(app, repository), ["main", [0, 0]]);
  git(join(app, "sub"), "commit", "-q", "-am", "moved");
  assert.deepEqual(await gitState(app, repository), ["main", [1, 1]]);
});

test("inside the trusted repository s git dir the branch alone shows", async () => {
  assert.deepEqual(await gitState(join(app, ".git"), repository), ["main", null]);
  assert.deepEqual(await gitState(join(app, ".git", "refs"), repository), ["main", null]);
});

test("a bare layout shows nothing and a trusted bare container its branch", async () => {
  const layout = bareLayout(join(dirname(app), "layout"));
  const container = join(dirname(app), "container");
  mkdirSync(container);
  git(container, "clone", "-q", "--bare", app, ".git");
  trust(newHome(), [dirname(app), container]);
  const lookup = lookupFor(join(tmp, "home"), join(tmp, "base"));
  assert.deepEqual(await gitState(layout, lookup), [null, null]);
  assert.deepEqual(await gitState(container, lookup), ["main", null]); // no work tree to diff
});

test("a worktree shows its own branch and changes", async () => {
  const worktree = addWorktree(app, join(dirname(app), "feature-x"));
  writeFileSync(join(worktree, "README"), "one\nmore\n");
  assert.deepEqual(await gitState(worktree, repository), ["feature-x", [1, 0]]);
});

test("inside a worktree s admin dir the branch is that worktree s", async () => {
  addWorktree(app, join(dirname(app), "feature-x"));
  assert.deepEqual(await gitState(join(app, ".git", "worktrees", "feature-x"), repository), [
    "feature-x",
    null,
  ]);
});

test("a file named head at the root does not change the count", async () => {
  // git runs at the root: `HEAD` there must still be read as the commit, not as that file.
  mkdirSync(join(app, "src"));
  writeFileSync(join(app, "HEAD"), "a file\n".repeat(500));
  git(app, "add", "-A");
  git(app, "commit", "-q", "-m", "a file named HEAD");
  assert.deepEqual(await gitState(join(app, "src"), repository), ["main", [0, 0]]);
});

test("a gitfile s trailing space belongs to the path", async () => {
  // As git reads it (setup.c, read_gitfile_gently): only the line end is dropped.
  const code = dirname(app);
  for (const [name, branch] of [
    ["store ", "spaced"],
    ["store", "decoy"],
  ] as const) {
    const seed = committed(join(code, `seed-${branch}`));
    git(seed, "switch", "-q", "-c", branch);
    renameSync(join(seed, ".git"), join(code, name));
  }
  const folder = join(code, "sep");
  mkdirSync(folder);
  writeFileSync(join(folder, ".git"), "gitdir: ../store \n");
  trust(newHome(), [folder]);
  const lookup = lookupFor(join(tmp, "home"), join(tmp, "base"));
  assert.equal((await gitState(folder, lookup))[0], "spaced");
});

test("git is given the repository and never looks for one", async () => {
  // A trusted folder whose `.git` git itself rejects, inside someone else's repository: git
  // left to search from there would walk up into that repository and run its filter.
  const clone = planted(join(dirname(app), "clone"));
  const kept = join(clone, "kept");
  for (const entry of ["objects", "refs"]) {
    mkdirSync(join(kept, ".git", entry), { recursive: true });
  }
  writeFileSync(join(kept, ".git", "HEAD"), "no ref at all\n");
  trust(newHome(), [kept]);
  filterRan();
  gitDiffsThere(kept);
  assert.ok(filterRan()); // the control
  assert.deepEqual(await gitState(kept, lookupFor(join(tmp, "home"), join(tmp, "base"))), [
    null,
    null,
  ]);
  assert.ok(!filterRan());
});

test("every git call shares one time limit", async () => {
  // The reply waits for its footer. Each call here answers within the limit, and the limit
  // still ends the second one: it is for all of them together.
  const stub = join(tmp, "bin", "git");
  mkdirSync(dirname(stub));
  writeFileSync(stub, "#!/bin/sh\nsleep 0.5\n");
  chmodSync(stub, 0o755);
  const path = process.env.PATH;
  process.env.PATH = `${dirname(stub)}:${path}`;
  try {
    assert.deepEqual(await gitState(app, repository, 800), [null, null]);
  } finally {
    process.env.PATH = path;
  }
});

// --- a repository inside the folder the session started in ---

/** The folder a session started in (trusted, not a repository), and its footer's lookup. */
function work(): { folder: string; lookup: Lookup } {
  const home = newHome();
  const folder = realpathSync(join(tmp, "work"));
  trust(home, [folder]);
  return { folder, lookup: lookupFor(home, folder) };
}

beforeEach(() => {
  mkdirSync(join(tmp, "work"));
});

for (const depth of [1, 2, 3]) {
  test(`the footer shows the branch and changes of a repository inside the folder [${depth}]`, async () => {
    const { folder, lookup } = work();
    const repo = committed(join(folder, ...["a", "b", "c"].slice(0, depth)));
    mkdirSync(join(repo, "src"));
    assert.deepEqual(await gitState(repo, lookup), ["main", [0, 0]]);
    writeFileSync(join(repo, "README"), "one\nmore\n");
    assert.deepEqual(await gitState(join(repo, "src"), lookup), ["main", [1, 0]]);
  });
}

test("the footer shows nothing of a repository outside the folder", async () => {
  const { lookup } = work();
  const elsewhere = committed(join(tmp, "elsewhere"));
  assert.deepEqual(await gitState(elsewhere, lookup), [null, null]);
});

test("the footer shows nothing of a symlink to a repository elsewhere", POSIX, async () => {
  const { folder, lookup } = work();
  symlinkSync(committed(join(tmp, "elsewhere")), join(folder, "link"));
  assert.deepEqual(await gitState(join(folder, "link"), lookup), [null, null]);
});

test("known limit a repository inside the folder runs its own filters", async () => {
  // The owner's decision (2026-10-05): a repository inside the folder a session started in is
  // covered by that folder's trust, so a repository the agent clones there gets the footer's
  // `diff-files` under its own config. This pins the consequence: it holds only while the
  // decision does.
  const { folder, lookup } = work();
  planted(join(folder, "clone"));
  filterRan();
  const [branch, changes] = await gitState(join(folder, "clone"), lookup);
  assert.ok(branch === "main" && changes !== null); // `planted` edits a tracked file
  assert.ok(filterRan());
});
