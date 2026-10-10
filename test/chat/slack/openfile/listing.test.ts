import assert from "node:assert/strict";
import { chmodSync, mkdirSync, symlinkSync, writeFileSync } from "node:fs";
import { basename, join } from "node:path";
import { test } from "node:test";
import { setImmediate as nextTurn } from "node:timers/promises";
import { trustedRepository } from "../../../../src/agent/claude/trust.ts";
import type { Repository } from "../../../../src/agent/seam.ts";
import {
  folderFiles,
  Listings,
  logger,
  projectFiles,
  type RepositoryLookup,
  rank,
  repositoriesOf,
  usable,
  walkFiles,
} from "../../../../src/chat/slack/openfile/listing.ts";
import { regularFiles } from "../../../../src/chat/slack/openfile/read.ts";
import { AsyncEvent, FakeClock } from "../../../support/fake-slack.ts";
import { committed, gitInit, trust } from "../../../support/git-layouts.ts";
import { GIT_LAYOUT } from "../../../support/platform.ts";
import {
  anyRepository,
  commitAll,
  POSIX,
  repository,
  scratch,
  write,
  writeAll,
} from "./support.ts";

const tmp = scratch();
const BOTH = {
  skip: GIT_LAYOUT.skip || POSIX.skip,
};

/** A repository with one commit, `README`: the session's folder when it is itself one. */
function app(): string {
  return committed(join(tmp.dir, "app"));
}

/** A session's folder: not a repository, trusted in Claude Code, repositories inside it. */
function work(): { work: string; home: string; lookup: RepositoryLookup } {
  const home = join(tmp.dir, "home");
  mkdirSync(home, { recursive: true });
  const folder = join(tmp.dir, "work");
  mkdirSync(folder, { recursive: true });
  trust(home, [folder]);
  return { work: folder, home, lookup: lookupIn(home) };
}

function lookupIn(home: string): RepositoryLookup {
  return (directory, sessionFolder) => trustedRepository(directory, sessionFolder, home);
}

async function roots(folder: string, lookup: RepositoryLookup): Promise<string[]> {
  return (await repositoriesOf(folder, lookup)).map((found) => found.root).toSorted();
}

async function filesOf(listing: Listings, folder: string): Promise<readonly string[]> {
  return (await listing.of(folder)).files;
}

function fakeRepository(root: string): Repository {
  return { root, key: root, gitDir: join(root, ".git"), insideGitDir: false };
}

/** A clock that says when a sleep was asked for, so that a test advances it after that. */
class WatchedClock extends FakeClock {
  readonly slept = new AsyncEvent();

  override sleep(seconds: number, signal?: AbortSignal): Promise<void> {
    this.slept.set();
    return super.sleep(seconds, signal);
  }
}

// --- ranking ---

test("basename matches come first then the shorter path", () => {
  const paths = [
    "setup/readme.md",
    "src/awaydesk/setup.py",
    "tests/test_setup.py",
    "docs/setup.md",
    "SETUP",
  ];
  assert.deepEqual(rank(paths, "setup"), [
    "SETUP",
    "docs/setup.md",
    "tests/test_setup.py",
    "src/awaydesk/setup.py",
    "setup/readme.md",
  ]);
});

test("ranking ignores case and leaves out what does not contain the words", () => {
  assert.deepEqual(rank(["Docs/Setup.md", "src/app.py"], "sETUP.m"), ["Docs/Setup.md"]);
});

// --- the file index ---

test("the index is the tracked files and the untracked ones not ignored", GIT_LAYOUT, async () => {
  const folder = app();
  write(folder, ".gitignore", "*.log\nbuild/\n");
  write(folder, "src/tracked.py");
  commitAll(folder);
  write(folder, "src/new.py");
  write(folder, "debug.log");
  write(folder, "build/out.js");
  const found = (await projectFiles(await repository(folder), folder)) ?? [];
  assert.deepEqual(found.toSorted(), [".gitignore", "README", "src/new.py", "src/tracked.py"]);
});

test("the index of a subfolder is relative to it and stops at its edge", GIT_LAYOUT, async () => {
  const folder = app();
  // A folder named with glob characters: a pathspec must take it literally.
  const sub = join(folder, "we*ird [x]");
  write(folder, "we*ird [x]/in.py");
  write(folder, "we*ird [x]/deep/er.py");
  write(folder, "weeird x/outside.py");
  write(folder, "other.py");
  commitAll(folder);
  const found = (await projectFiles(await repository(folder), sub)) ?? [];
  assert.deepEqual(found.toSorted(), ["deep/er.py", "in.py"]);
});

test("the index follows a folder reached through a symlink", BOTH, async () => {
  const folder = app();
  write(folder, "sub/in.py");
  commitAll(folder);
  symlinkSync(join(folder, "sub"), join(tmp.dir, "via"));
  assert.deepEqual(await projectFiles(await repository(folder), join(tmp.dir, "via")), ["in.py"]);
});

test("a folder without a repository has no source", GIT_LAYOUT, async () => {
  const plain = join(tmp.dir, "plain");
  mkdirSync(plain);
  assert.equal(usable(await anyRepository(plain)), null);
});

test("a git dir with no work tree is no source", GIT_LAYOUT, async () => {
  const folder = app();
  const inside = await repository(join(folder, ".git"));
  assert.ok(inside.insideGitDir);
  assert.equal(usable(inside), null);
  assert.notEqual(usable(await repository(folder)), null);
});

// --- the repositories of a session's folder ---

test("a folder inside a repository has that repository", GIT_LAYOUT, async () => {
  const folder = app();
  const home = join(tmp.dir, "home");
  mkdirSync(home);
  trust(home, [folder]);
  mkdirSync(join(folder, "src"));
  assert.deepEqual(await roots(folder, lookupIn(home)), [folder]);
  assert.deepEqual(await roots(join(folder, "src"), lookupIn(home)), [folder]);
});

test("a plain folder has the usable repositories two levels below it", GIT_LAYOUT, async () => {
  const { work: folder, lookup } = work();
  const one = gitInit(join(folder, "one"));
  const two = gitInit(join(folder, "group", "two"));
  gitInit(join(folder, "group", "deeper", "three")); // three levels down: not found
  assert.deepEqual(await roots(folder, lookup), [one, two].toSorted());
});

test("the repositories below a folder are looked up together", GIT_LAYOUT, async () => {
  const { work: folder } = work();
  for (const name of ["a", "b", "c", "d"]) gitInit(join(folder, name));
  let active = 0;
  let peak = 0;
  const slow: RepositoryLookup = async (directory, sessionFolder) => {
    if (directory === sessionFolder) return null;
    active += 1;
    peak = Math.max(peak, active);
    // A turn of the event loop: the lookups that run together are all started by now.
    await nextTurn();
    active -= 1;
    return repository(directory);
  };
  assert.deepEqual(
    await roots(folder, slow),
    ["a", "b", "c", "d"].map((name) => join(folder, name)),
  );
  assert.equal(peak, 4); // not one after the other
});

test("a repository that is not usable is left out", BOTH, async () => {
  const { work: folder, lookup } = work();
  gitInit(join(folder, "a"));
  mkdirSync(join(folder, "broken"));
  writeFileSync(join(folder, "broken", ".git"), "not a gitfile\n"); // a `.git` that names no git dir
  symlinkSync(gitInit(join(tmp.dir, "elsewhere")), join(folder, "link"));
  assert.deepEqual(await roots(folder, lookup), [join(folder, "a")]);
});

test("the search never goes into a repository for more", GIT_LAYOUT, async () => {
  const { work: folder, lookup } = work();
  const outer = gitInit(join(folder, "outer"));
  gitInit(join(outer, "vendored"));
  assert.deepEqual(await roots(folder, lookup), [outer]);
});

test("hidden folders are not searched for a repository", GIT_LAYOUT, async () => {
  const { work: folder, lookup } = work();
  gitInit(join(folder, ".hidden"));
  assert.deepEqual(await roots(folder, lookup), []);
});

test("a folder in an untrusted repository has nothing below it", GIT_LAYOUT, async () => {
  const home = join(tmp.dir, "home");
  mkdirSync(home);
  const outer = gitInit(join(tmp.dir, "outer"));
  const folder = join(outer, "packages");
  const inner = gitInit(join(folder, "lib"));
  const lookup = lookupIn(home);
  assert.deepEqual(await roots(folder, lookup), []);
  trust(home, [inner]); // a repository the owner trusted on its own stays usable
  assert.deepEqual(await roots(folder, lookup), [inner]);
  trust(home, [outer]);
  assert.deepEqual(await roots(folder, lookup), [outer]);
});

// --- what the folder offers to search ---

test("a plain folder is walked for regular files only", POSIX, async () => {
  const plain = join(tmp.dir, "plain");
  writeAll(plain, "a.txt", "docs/b.md", "docs/deep/c.md", ".hidden/d.txt");
  write(join(tmp.dir, "outside"), "secret.txt");
  symlinkSync(join(tmp.dir, "outside"), join(plain, "dir-link"));
  symlinkSync(join(plain, "a.txt"), join(plain, "file-link"));
  writeAll(plain, ".git/config", "sub/.git/config");
  mkdirSync(join(plain, "empty-dir"));
  const listing = new Listings(anyRepository);
  assert.deepEqual((await filesOf(listing, plain)).toSorted(), [
    ".hidden/d.txt",
    "a.txt",
    "docs/b.md",
    "docs/deep/c.md",
  ]);
});

test("a folder inside a repository lists what git does not ignore", GIT_LAYOUT, async () => {
  const folder = app();
  write(folder, ".gitignore", ".venv/\n*.log\n");
  writeAll(folder, "src/tracked.py");
  commitAll(folder);
  writeAll(folder, "src/new.py", ".venv/lib/pkg.py", "debug.log");
  const listing = new Listings(anyRepository);
  assert.deepEqual((await filesOf(listing, folder)).toSorted(), [
    ".gitignore",
    "README",
    "src/new.py",
    "src/tracked.py",
  ]);
  assert.deepEqual((await filesOf(new Listings(anyRepository), join(folder, "src"))).toSorted(), [
    "new.py",
    "tracked.py",
  ]);
});

test(
  "the files of nested repositories come from git and the rest from disk",
  GIT_LAYOUT,
  async () => {
    const { work: folder, lookup } = work();
    const nested = gitInit(join(folder, "awaydesk-workspace"));
    write(nested, ".gitignore", ".venv/\n");
    writeAll(nested, "docs/setup.md", ".venv/lib/pkg.py");
    commitAll(nested);
    writeAll(nested, "docs/new.md", ".venv/lib/more.py");
    const deep = gitInit(join(folder, "a", "b", "c")); // three levels down: walked like any folder
    writeAll(deep, "x.txt", ".venv/y.txt");
    writeAll(folder, "notes.md", "a/readme.md");
    const listing = new Listings(lookup);
    assert.deepEqual((await filesOf(listing, folder)).toSorted(), [
      "a/b/c/.venv/y.txt",
      "a/b/c/x.txt",
      "a/readme.md",
      "awaydesk-workspace/.gitignore",
      "awaydesk-workspace/docs/new.md",
      "awaydesk-workspace/docs/setup.md",
      "notes.md",
    ]);
  },
);

test("the files of a repository that is not usable are walked", BOTH, async () => {
  const { work: folder, home } = work();
  // A symlinked folder is not entered, so a repository behind a link is not listed at all.
  const elsewhere = committed(join(tmp.dir, "elsewhere"));
  symlinkSync(elsewhere, join(folder, "link"));
  write(folder, "plain.txt");
  assert.deepEqual(await filesOf(new Listings(lookupIn(home)), folder), ["plain.txt"]);
});

test("a walk that runs out of time returns what it found and says so", async () => {
  writeAll(tmp.dir, "top.txt", "one/mid.txt", "one/two/low.txt");
  const visits = [false, true]; // the root is read, then the time is up
  let visited = 0;
  assert.deepEqual(await walkFiles(tmp.dir, new Set(), () => visits[visited++] ?? true), [
    ["top.txt"],
    false,
  ]);
  const [done, complete] = await walkFiles(tmp.dir, new Set(), () => false);
  assert.deepEqual(done.toSorted(), ["one/mid.txt", "one/two/low.txt", "top.txt"]);
  assert.ok(complete);
});

test("a listing is kept for a short time", async () => {
  const plain = join(tmp.dir, "plain");
  writeAll(plain, "first.txt");
  const clock = new FakeClock();
  clock.now = 100;
  const listing = new Listings(anyRepository, { ttl: 10, clock });
  assert.deepEqual(await filesOf(listing, plain), ["first.txt"]);
  writeAll(plain, "second.txt");
  clock.now = 105;
  assert.deepEqual(await filesOf(listing, plain), ["first.txt"]); // a keystroke later: not walked again
  clock.now = 111;
  assert.deepEqual((await filesOf(listing, plain)).toSorted(), ["first.txt", "second.txt"]);
});

test("the listings of two folders are kept apart", async () => {
  writeAll(join(tmp.dir, "x"), "x.txt");
  writeAll(join(tmp.dir, "y"), "y.txt");
  const listing = new Listings(anyRepository);
  assert.deepEqual(await filesOf(listing, join(tmp.dir, "x")), ["x.txt"]);
  assert.deepEqual(await filesOf(listing, join(tmp.dir, "y")), ["y.txt"]);
});

test("a lookup that fails lists nothing and never raises", async (t) => {
  const warnings: string[] = [];
  t.mock.method(logger, "warning", (message: string) => warnings.push(message));
  const broken: RepositoryLookup = async () => {
    throw new Error("EIO");
  };
  const plain = join(tmp.dir, "plain");
  writeAll(plain, "a.txt");
  assert.deepEqual(await filesOf(new Listings(broken), plain), []);
  assert.deepEqual(warnings, ["could not list a folder's files: Error"]);
});

test("concurrent requests for one folder share one listing", async () => {
  const plain = join(tmp.dir, "plain");
  writeAll(plain, "a.txt");
  const lookups: string[] = [];
  const asked = new AsyncEvent();
  const release = new AsyncEvent();
  const slow: RepositoryLookup = async (directory) => {
    lookups.push(directory);
    asked.set();
    await release.wait();
    return null;
  };
  const listing = new Listings(slow);
  const answers = Promise.all(Array.from({ length: 6 }, () => filesOf(listing, plain)));
  await asked.wait();
  release.set();
  assert.deepEqual(await answers, Array(6).fill(["a.txt"]));
  assert.equal(lookups.length, 1); // one listing, not six
});

test("a request that gives up does not stop the listing the others wait for", async () => {
  const plain = join(tmp.dir, "plain");
  writeAll(plain, "a.txt");
  const asked = new AsyncEvent();
  const release = new AsyncEvent();
  let lookups = 0;
  const slow: RepositoryLookup = async () => {
    lookups += 1;
    asked.set();
    await release.wait();
    return null;
  };
  const listing = new Listings(slow);
  const waiting = filesOf(listing, plain);
  await asked.wait();
  const giving = new AbortController();
  const keystroke = listing.of(plain, { signal: giving.signal }); // a keystroke whose time ran out
  giving.abort(new Error("gave up"));
  await assert.rejects(keystroke, /gave up/);
  release.set();
  assert.deepEqual(await waiting, ["a.txt"]);
  assert.deepEqual(await filesOf(listing, plain), ["a.txt"]); // kept: the listing finished all the same
  assert.equal(lookups, 1);
});

test("a listing that ran out of time is not kept as complete", async () => {
  const plain = join(tmp.dir, "plain");
  writeAll(plain, "first.txt");
  const clock = new FakeClock();
  clock.now = 100;
  const lookups: string[] = [];
  const counting: RepositoryLookup = async (directory) => {
    lookups.push(directory);
    return null;
  };
  // A budget of nothing: the walk is out of time at once and finds nothing. The repositories are
  // not kept, so that each lookup counts a listing made.
  const listing = new Listings(counting, {
    ttl: 30,
    partialTtl: 3,
    repositoriesTtl: 0,
    budget: 0,
    clock,
  });
  assert.deepEqual(await filesOf(listing, plain), []);
  const built = lookups.length;
  clock.now = 102;
  await filesOf(listing, plain);
  assert.equal(lookups.length, built); // a moment later: the same answer, not asked again
  clock.now = 104;
  await filesOf(listing, plain);
  assert.ok(lookups.length > built); // past its short life: built again, not kept for 30 seconds
});

test("a listing whose git part timed out is not kept as complete", BOTH, async () => {
  const folder = app();
  write(folder, "src/found.py");
  commitAll(folder);
  const clock = new WatchedClock();
  clock.now = 100;
  const listing = new Listings(anyRepository, { ttl: 30, partialTtl: 3, budget: 0.2, clock });
  const stub = join(tmp.dir, "bin", "git");
  write(join(tmp.dir, "bin"), "git", "#!/bin/sh\nexec sleep 30\n");
  chmodSync(stub, 0o755);
  const path = process.env.PATH ?? "";
  process.env.PATH = `${join(tmp.dir, "bin")}:${path}`;
  try {
    const pending = filesOf(listing, folder);
    await clock.slept.wait(); // the budget's clock is running: git does not answer in time
    await clock.advance(0.2);
    assert.deepEqual(await pending, []);
  } finally {
    process.env.PATH = path;
  }
  clock.now = 102;
  assert.deepEqual(await filesOf(listing, folder), []); // still the short-lived answer
  clock.now = 104;
  assert.ok((await filesOf(listing, folder)).includes("src/found.py")); // asked again, long before 30 seconds
});

test("a listing that fails is not kept", async (t) => {
  t.mock.method(logger, "warning", () => {});
  const plain = join(tmp.dir, "plain");
  writeAll(plain, "a.txt");
  let failing = true;
  const flaky: RepositoryLookup = async () => {
    if (failing) throw new Error("EIO");
    return null;
  };
  const listing = new Listings(flaky);
  assert.deepEqual(await filesOf(listing, plain), []);
  failing = false;
  assert.deepEqual(await filesOf(listing, plain), ["a.txt"]);
});

test("the kept listings are bounded and the expired ones go", async () => {
  const clock = new FakeClock();
  clock.now = 100;
  const listing = new Listings(anyRepository, { ttl: 10, limit: 3, clock });
  const folders = Array.from({ length: 5 }, (_, index) => join(tmp.dir, `f${index}`));
  for (const folder of folders) {
    writeAll(folder, "a.txt");
    await filesOf(listing, folder);
  }
  assert.deepEqual(listing.keptListings(), folders.slice(2)); // the oldest went first
  clock.now = 111;
  await filesOf(listing, folders[0] as string);
  assert.deepEqual(listing.keptListings(), [folders[0]]); // the others expired and were removed
});

test("a kept listing that finds no match is made again before saying so", async () => {
  const plain = join(tmp.dir, "plain");
  writeAll(plain, "first.txt");
  const clock = new FakeClock();
  clock.now = 100;
  const listing = new Listings(anyRepository, { ttl: 30, clock });
  assert.deepEqual((await listing.search(plain, "second")).paths, []);
  writeAll(plain, "second.txt");
  clock.now = 105; // well inside the listing's life
  const found = await listing.search(plain, "second");
  assert.deepEqual(found.paths, ["second.txt"]);
  assert.equal(found.count, 1);
  assert.ok(found.complete);
  // A match in the kept listing is answered from it, as before: no second walk.
  writeAll(plain, "second-more.txt");
  clock.now = 106;
  assert.deepEqual((await listing.search(plain, "second")).paths, ["second.txt"]);
});

test("a listing made for the request is not made again", async () => {
  const plain = join(tmp.dir, "plain");
  writeAll(plain, "first.txt");
  const walks: string[] = [];
  const counting: RepositoryLookup = async (directory) => {
    walks.push(directory);
    return null;
  };
  const listing = new Listings(counting, { repositoriesTtl: 0 });
  assert.deepEqual((await listing.search(plain, "nothing")).paths, []);
  assert.equal(walks.length, 1); // nothing was kept: the one listing is fresh, and it is the answer
});

test("a search says whether the listing it came from was cut", async () => {
  const plain = join(tmp.dir, "plain");
  writeAll(plain, "first.txt");
  const cut = new Listings(anyRepository, { budget: 0 }); // out of time at once
  const found = await cut.search(plain, "first");
  assert.deepEqual(found.paths, []);
  assert.ok(!found.complete);
  const whole = await new Listings(anyRepository).search(plain, "first");
  assert.deepEqual(whole.paths, ["first.txt"]);
  assert.ok(whole.complete);
});

test("a search checks files only until the limit and counts by name past it", async () => {
  const plain = join(tmp.dir, "plain");
  writeAll(plain, ...Array.from({ length: 40 }, (_, i) => `part${String(i).padStart(2, "0")}.csv`));
  // Python counted the calls of `_locate`; a path is checked as it is taken from the iterable
  // `regularFiles` is given, so the paths taken are the paths checked.
  const checked: string[] = [];
  const counting: typeof regularFiles = (folder, relatives, limit) => {
    function* watched(): Generator<string> {
      for (const relative of relatives) {
        checked.push(relative);
        yield relative;
      }
    }
    return regularFiles(folder, watched(), limit);
  };
  const listing = new Listings(anyRepository, { regular: counting });
  const found = await listing.search(plain, "part", { limit: 10 });
  assert.equal(found.paths.length, 10);
  assert.equal(found.count, 40);
  assert.equal(checked.length, 10);
  checked.length = 0;
  // Fewer files than the limit: every match was checked, so the count is exact.
  const few = await listing.search(plain, "part0", { limit: 20 });
  assert.equal(few.paths.length, 10);
  assert.equal(few.count, 10);
  assert.equal(checked.length, 10);
  checked.length = 0;
  assert.equal((await listing.search(plain, "part")).paths.length, 40); // no limit: all are checked
  assert.equal(checked.length, 40);
});

test("the repositories of a folder are kept for a short while", GIT_LAYOUT, async () => {
  const { work: folder, lookup } = work();
  gitInit(join(folder, "one"));
  const asked: string[] = [];
  const clock = new FakeClock();
  clock.now = 100;
  const counting: RepositoryLookup = async (directory, sessionFolder) => {
    asked.push(directory);
    return lookup(directory, sessionFolder);
  };
  const listing = new Listings(counting, { repositoriesTtl: 5, clock });
  const first = await listing.repositories(folder);
  let made = asked.length;
  assert.deepEqual(
    first.map((found) => found.root),
    [join(folder, "one")],
  );
  assert.ok(made > 0);
  clock.now = 104;
  assert.deepEqual(await listing.repositories(folder), first);
  assert.equal(asked.length, made);
  assert.deepEqual(await listing.repositories(folder, { again: true }), first);
  assert.ok(asked.length > made);
  made = asked.length;
  clock.now = 110;
  await listing.repositories(folder);
  assert.ok(asked.length > made); // past its short life
});

test("a nested repository s index is relative to the folder above it", GIT_LAYOUT, async () => {
  const { work: folder } = work();
  const nested = gitInit(join(folder, "app"));
  writeAll(nested, "docs/setup.md");
  commitAll(nested);
  assert.deepEqual(await projectFiles(await repository(nested), folder), ["app/docs/setup.md"]);
});

// --- the changes of every repository of the folder ---

test("the listing runs git in at most a few repositories at once", async () => {
  const { work: folder } = work();
  let active = 0;
  let peak = 0;
  const listed: typeof projectFiles = async (found) => {
    active += 1;
    peak = Math.max(peak, active);
    await nextTurn();
    active -= 1;
    return [`${basename(found.root)}/a.py`];
  };
  const repositories = Array.from({ length: 8 }, (_, i) =>
    fakeRepository(join(folder, `r${String(i).padStart(2, "0")}`)),
  );
  const [files, complete] = await folderFiles(folder, repositories, 5, {
    chains: 2,
    list: listed,
    clock: new FakeClock(),
  });
  assert.ok(complete);
  assert.equal(files.length, 8);
  assert.equal(peak, 2);
});
