import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import {
  existsSync,
  linkSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  renameSync,
  rmdirSync,
  rmSync,
  symlinkSync,
  unlinkSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { afterEach, beforeEach, mock, test } from "node:test";
import { logger, trustedRepository, workspaceTrusted } from "../../../src/agent/claude/trust.ts";
import {
  addWorktree,
  bareLayout,
  committed,
  git,
  gitFindsARepository,
  gitInit,
  gitTakesItFor,
  trust,
} from "../../support/git-layouts.ts";

let tmp = "";
let home = "";
let warnings: string[] = [];

beforeEach(() => {
  tmp = realpathSync(mkdtempSync(join(tmpdir(), "awaydesk-trust-")));
  home = join(tmp, "home");
  mkdirSync(home);
  warnings = [];
  mock.method(logger, "warning", (message: string) => warnings.push(message));
});

afterEach(() => {
  mock.restoreAll();
  rmSync(tmp, { recursive: true, force: true });
});

// Symlinks, FIFOs, hard links and POSIX modes: the daemon is not supported on Windows yet.
const POSIX = { skip: process.platform === "win32" };

/** `promise`, or a failure when it is not settled within `ms`: a read blocked on a FIFO. */
async function within<T>(promise: Promise<T>, ms: number): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  const expired = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(`not settled within ${ms} ms`)), ms);
  });
  try {
    return await Promise.race([promise, expired]);
  } finally {
    clearTimeout(timer);
  }
}

function mkfifo(path: string): void {
  execFileSync("mkfifo", [path]);
}

/** Count the `JSON.parse` calls that read a trust record, passing each through to the real one. */
function countRecordParses(): () => number {
  const real = JSON.parse;
  let parses = 0;
  mock.method(JSON, "parse", (text: string, reviver?: Parameters<typeof real>[1]) => {
    if (typeof text === "string" && text.includes("hasTrustDialogAccepted")) {
      parses += 1;
    }
    return real(text, reviver);
  });
  return () => parses;
}

test("a repository is trusted at its root", async () => {
  const repo = gitInit(join(tmp, "code", "app"));
  mkdirSync(join(repo, "src"));
  trust(home, [repo]);
  assert.ok(await workspaceTrusted(repo, home));
  assert.ok(await workspaceTrusted(join(repo, "src"), home));
});

test("a trusted parent does not cover a repository inside it", async () => {
  // "the trust covers any subdirectory ... apart from a git repository nested inside it,
  // such as a clone" (permissions reference, read 2026-09-25)
  const repo = gitInit(join(tmp, "code", "clone"));
  trust(home, [join(tmp, "code")]);
  assert.ok(!(await workspaceTrusted(repo, home)));
});

test("a folder outside git is covered by a trusted parent", async () => {
  const folder = join(tmp, "notes", "day");
  mkdirSync(folder, { recursive: true });
  trust(home, [join(tmp, "notes")]);
  assert.ok(await workspaceTrusted(folder, home));
});

test("a refused or unknown folder is not trusted", async () => {
  const repo = gitInit(join(tmp, "app"));
  assert.ok(!(await workspaceTrusted(repo, home))); // no record at all
  trust(home, [repo], false);
  assert.ok(!(await workspaceTrusted(repo, home)));
  writeFileSync(join(home, ".claude.json"), "{not json");
  assert.ok(!(await workspaceTrusted(repo, home)));
});

test("a worktree follows its main checkout", async () => {
  const repo = gitInit(join(tmp, "app"));
  const worktree = addWorktree(repo, join(tmp, "wt"));
  trust(home, [repo]);
  assert.ok(await workspaceTrusted(worktree, home));
});

test("the answer needs no git", async () => {
  // The check reads the filesystem: a Mac without the command line tools answers the same.
  const repo = gitInit(join(tmp, "notes", "clone"));
  trust(home, [join(tmp, "notes")]);
  const path = process.env.PATH;
  process.env.PATH = join(tmp, "empty"); // no git anywhere
  try {
    assert.ok(await workspaceTrusted(join(tmp, "notes"), home));
    assert.ok(!(await workspaceTrusted(repo, home)));
  } finally {
    if (path === undefined) {
      delete process.env.PATH;
    } else {
      process.env.PATH = path;
    }
  }
});

test("the trust record is read again only when it changes", async () => {
  const folder = join(tmp, "notes");
  mkdirSync(folder);
  trust(home, [folder]);
  const parses = countRecordParses();
  assert.ok((await workspaceTrusted(folder, home)) && (await workspaceTrusted(folder, home)));
  assert.equal(parses(), 1); // a `!bind` list checks many folders against one record
  trust(home, [folder], false);
  utimesSync(join(home, ".claude.json"), 1, 1); // a different mtime, as a real write gives
  assert.ok(!(await workspaceTrusted(folder, home)));
});

test("a batch of checks parses the record once", async () => {
  const folders = Array.from({ length: 8 }, (_, i) => join(tmp, `f${i}`));
  for (const folder of folders) {
    mkdirSync(folder);
  }
  trust(home, folders);
  utimesSync(join(home, ".claude.json"), 2, 2); // a record no earlier test cached
  const parses = countRecordParses();
  const verdicts = await Promise.all(folders.map((folder) => workspaceTrusted(folder, home)));
  assert.ok(verdicts.every(Boolean) && parses() === 1);
});

type Plant = (folder: string, app: string) => [string, string];

const gitfileAbsolute: Plant = (folder, app) => {
  writeFileSync(join(folder, ".git"), `gitdir: ${app}/.git\n`);
  return ["--git-common-dir", join(app, ".git")];
};

const gitfileRelative: Plant = (folder, app) => {
  writeFileSync(join(folder, ".git"), `gitdir: ${relative(folder, join(app, ".git"))}\n`);
  return ["--git-common-dir", join(app, ".git")];
};

const gitfileToAWorktreeSAdminDir: Plant = (folder, app) => {
  writeFileSync(join(folder, ".git"), `gitdir: ${app}/.git/worktrees/wt\n`);
  return ["--git-common-dir", join(app, ".git")];
};

const symlinkToTheGitDir: Plant = (folder, app) => {
  symlinkSync(join(app, ".git"), join(folder, ".git"));
  return ["--git-common-dir", join(app, ".git")];
};

const symlinkToAWorktreeSGitfile: Plant = (folder, app) => {
  symlinkSync(join(app, "..", "wt", ".git"), join(folder, ".git"));
  return ["--git-common-dir", join(app, ".git")];
};

const hardLinkToAWorktreeSGitfile: Plant = (folder, app) => {
  linkSync(join(app, "..", "wt", ".git"), join(folder, ".git"));
  return ["--git-common-dir", join(app, ".git")];
};

const commondirInAGitDir: Plant = (folder, app) => {
  mkdirSync(join(folder, ".git"));
  writeFileSync(join(folder, ".git", "HEAD"), "ref: refs/heads/main\n");
  writeFileSync(join(folder, ".git", "commondir"), `${app}/.git\n`);
  return ["--git-common-dir", join(app, ".git")];
};

const commondirInABareLayout: Plant = (folder, app) => {
  writeFileSync(join(folder, "HEAD"), "ref: refs/heads/main\n");
  writeFileSync(join(folder, "commondir"), `${app}/.git\n`);
  return ["--git-common-dir", join(app, ".git")];
};

const bareLayoutWithCoreWorktree: Plant = (folder, app) => {
  rmdirSync(folder);
  bareLayout(folder, { "core.bare": "false", "core.worktree": app });
  return ["--show-toplevel", app];
};

const PLANTED: [string, Plant][] = [
  ["gitfile_absolute", gitfileAbsolute],
  ["gitfile_relative", gitfileRelative],
  ["gitfile_to_a_worktree_s_admin_dir", gitfileToAWorktreeSAdminDir],
  ["symlink_to_the_git_dir", symlinkToTheGitDir],
  ["symlink_to_a_worktree_s_gitfile", symlinkToAWorktreeSGitfile],
  ["hard_link_to_a_worktree_s_gitfile", hardLinkToAWorktreeSGitfile],
  ["commondir_in_a_git_dir", commondirInAGitDir],
  ["commondir_in_a_bare_layout", commondirInABareLayout],
  ["bare_layout_with_core_worktree", bareLayoutWithCoreWorktree],
];

const NESTED: [string, boolean][] = [
  ["under-a-trusted-folder", false],
  ["in-a-clone", true],
];

/** A repository the owner trusted, with a registered worktree `wt` beside it, in a folder `code`
 * the owner trusted too. */
function makeApp(): string {
  const repo = committed(join(tmp, "code", "app"));
  git(repo, "worktree", "add", "-q", join(tmp, "code", "wt"));
  trust(home, [repo, join(tmp, "code")]);
  return realpathSync(repo);
}

for (const [id, plant] of PLANTED) {
  for (const [nestedId, nested] of NESTED) {
    test(
      `a folder that claims a trusted repository is not trusted [${id}-${nestedId}]`,
      POSIX,
      async () => {
        const app = makeApp();
        const code = join(app, "..");
        const folder = join(
          nested ? join(committed(join(code, "clone")), "vendor") : code,
          "planted",
        );
        mkdirSync(folder, { recursive: true });
        const [asked, claimed] = plant(folder, app);
        // The control: git itself takes the folder for the trusted repository.
        assert.ok(gitTakesItFor(folder, asked, claimed));
        assert.ok(!(await workspaceTrusted(folder, home)));
      },
    );
  }
}

test("a folder named like a worktree with a trailing space is not it", POSIX, async () => {
  const app = makeApp();
  const twin = join(app, "..", "wt ");
  mkdirSync(twin);
  writeFileSync(join(twin, ".git"), `gitdir: ${app}/.git/worktrees/wt\n`);
  assert.ok(await workspaceTrusted(join(app, "..", "wt"), home));
  assert.ok(!(await workspaceTrusted(twin, home)));
});

const BROKEN: [string, string, string][] = [
  ["pointer", "gitdir: /nonexistent\n", "pointer-gitdir: /nonexistent\\n"],
  ["x not a gitdir y", "gitdir: nowhere\n", "x not a gitdir y-gitdir: nowhere\\n"],
  ["not a git repository", "garbage\n", "not a git repository-garbage\\n"],
  ["empty", "", "empty-"],
];

for (const [name, gitfile, id] of BROKEN) {
  for (const [nestedId, nested] of NESTED) {
    test(`a broken gitfile is not read as outside a repository [${id}-${nestedId}]`, async () => {
      const app = makeApp();
      // git repeats the path in its errors: a folder's name must not decide what the error means.
      const folder = join(nested ? committed(join(app, "..", "clone")) : join(app, ".."), name);
      mkdirSync(folder);
      writeFileSync(join(folder, ".git"), gitfile);
      assert.ok(!(await workspaceTrusted(folder, home)));
    });
  }
}

for (const entry of [".git", ".git/HEAD", ".git/config"]) {
  test(`a fifo in the git metadata does not hold the answer [${entry}]`, POSIX, async () => {
    // An archive can carry a FIFO; git blocks on HEAD and config (measured 2026-10-04).
    const app = makeApp();
    const folder = join(app, "..", "piped");
    if (entry === ".git") {
      mkdirSync(folder);
    } else {
      committed(folder);
      unlinkSync(join(folder, entry));
    }
    mkfifo(join(folder, entry));
    assert.ok(!(await within(workspaceTrusted(folder, home), 2000)));
  });
}

const ENTRIES = ["HEAD", "objects", "refs", "commondir"] as const;

/** Python's `itertools.combinations` over `ENTRIES` for every size, in its order. */
function combinations(): (typeof ENTRIES)[number][][] {
  const found: (typeof ENTRIES)[number][][] = [];
  const pick = (size: number, from: number, chosen: (typeof ENTRIES)[number][]): void => {
    if (chosen.length === size) {
      found.push(chosen);
      return;
    }
    for (let i = from; i < ENTRIES.length; i++) {
      const entry = ENTRIES[i];
      if (entry !== undefined) {
        pick(size, i + 1, [...chosen, entry]);
      }
    }
  };
  for (let size = 0; size <= ENTRIES.length; size++) {
    pick(size, 0, []);
  }
  return found;
}

for (const entries of combinations()) {
  test(`a folder git takes for a bare repository is never covered by its parent [${entries.join("+")}]`, async () => {
    // Pins the reading of git's own test (setup.c, is_git_directory, v2.54.0) to the git
    // installed here: wherever git finds a repository, a trusted parent must not cover it.
    const app = makeApp();
    const folder = join(app, "..", "layout");
    mkdirSync(folder);
    for (const entry of entries) {
      if (entry === "HEAD") {
        writeFileSync(join(folder, entry), "ref: refs/heads/main\n");
      } else if (entry === "commondir") {
        writeFileSync(join(folder, entry), `${app}/.git\n`);
      } else {
        mkdirSync(join(folder, entry));
      }
    }
    assert.equal(await workspaceTrusted(folder, home), !gitFindsARepository(folder));
  });
}

test("legitimate layouts keep their trust", POSIX, async () => {
  const code = join(tmp, "code");
  const repo = committed(join(code, "app"));
  mkdirSync(join(repo, "src"));
  const inside = addWorktree(repo, join(repo, "wt-in"));
  const lib = committed(join(code, "lib"));
  git(repo, "submodule", "add", "-q", lib, "sub");
  git(code, "init", "-q", "--bare", "store.git");
  git(join(code, "store.git"), "fetch", "-q", lib, "HEAD:refs/heads/main");
  git(join(code, "store.git"), "worktree", "add", "-q", join(code, "of-bare"), "main");
  mkdirSync(join(code, "sep"));
  git(join(code, "sep"), "init", "-q", "--separate-git-dir", join(code, "sep-git-dir"));
  const container = join(code, "container");
  mkdirSync(container);
  git(container, "clone", "-q", "--bare", lib, ".git");
  git(container, "worktree", "add", "-q", join(container, "main"), "HEAD");
  mkdirSync(join(container, "plain"));
  const lineBreak = join(code, "wt\nnl");
  git(repo, "worktree", "add", "-q", "--detach", lineBreak);
  const decomposed = join(code, "caffè".normalize("NFD"));
  mkdirSync(decomposed);
  const underNfd = addWorktree(repo, join(decomposed, "wt"));
  symlinkSync(repo, join(code, "link"));
  mkdirSync(join(code, "notes", "empty", ".git"), { recursive: true }); // no git dir: git walks past it
  mkdirSync(join(code, "notes", "named", "sub"), { recursive: true });
  writeFileSync(join(code, "notes", "named", "HEAD"), "a file of that name, no repository\n");
  trust(home, [
    repo,
    join(repo, "sub"),
    join(code, "of-bare"),
    join(code, "sep"),
    container,
    join(code, "notes"),
  ]);
  for (const folder of [
    join(repo, "src"),
    inside,
    join(repo, "sub"),
    join(code, "of-bare"),
    join(code, "sep"),
    container,
    join(container, "plain"),
    join(container, "main"),
    join(repo, ".git"),
    join(repo, ".git", "refs"),
    lineBreak,
    underNfd,
    join(code, "link", "src"),
    join(code, "notes", "empty"),
    join(code, "notes", "named", "sub"),
  ]) {
    assert.ok(await workspaceTrusted(folder, home), folder);
  }
  // git records the worktree's path in the composed form (git 2.54.0 on macOS), which names
  // the same folder only where the filesystem folds the two forms together.
  const composed = join(code, "caffè".normalize("NFC"), "wt");
  if (existsSync(composed)) {
    assert.ok(await workspaceTrusted(composed, home));
  }
});

test("a path in another case names the same folder", POSIX, async (t) => {
  const repo = gitInit(join(tmp, "code", "app"));
  mkdirSync(join(tmp, "notes", "day"), { recursive: true });
  if (!existsSync(join(tmp, "CODE"))) {
    t.skip("this filesystem tells the two cases apart");
    return;
  }
  trust(home, [repo, join(tmp, "notes")]);
  assert.ok(await workspaceTrusted(join(tmp, "CODE", "APP"), home));
  assert.ok(await workspaceTrusted(join(tmp, "NOTES", "day"), home));
});

test("a worktree linked with relative paths follows its main checkout", async (t) => {
  const repo = committed(join(tmp, "app"));
  const worktree = join(tmp, "wt");
  try {
    git(repo, "worktree", "add", "-q", "--relative-paths", worktree);
  } catch {
    t.skip("git older than 2.48 has no --relative-paths");
    return;
  }
  trust(home, [repo]);
  assert.ok(await workspaceTrusted(worktree, home));
  git(repo, "worktree", "move", worktree, join(tmp, "moved"));
  assert.ok(await workspaceTrusted(join(tmp, "moved"), home));
});

test("a worktree renamed by hand is trusted again once repaired", async () => {
  // The main checkout still registers the old path: nothing on its side names the new one.
  const repo = gitInit(join(tmp, "app"));
  const worktree = addWorktree(repo, join(tmp, "wt"));
  trust(home, [repo]);
  const renamed = join(tmp, "renamed");
  renameSync(worktree, renamed);
  assert.ok(!(await workspaceTrusted(renamed, home)));
  git(renamed, "worktree", "repair");
  assert.ok(await workspaceTrusted(renamed, home));
});

test("known limit a folder at the path of a deleted worktree passes for it", async () => {
  // Trust is by path, as in Claude Code: until `git worktree prune`, the main checkout still
  // registers that path, and whatever sits there is its worktree.
  const repo = gitInit(join(tmp, "app"));
  const worktree = addWorktree(repo, join(tmp, "wt"));
  trust(home, [repo]);
  rmSync(worktree, { recursive: true });
  mkdirSync(worktree);
  writeFileSync(join(worktree, ".git"), `gitdir: ${repo}/.git/worktrees/wt\n`);
  assert.ok(await workspaceTrusted(worktree, home));
});

test("a folder s name never reaches the log", async () => {
  const app = makeApp();
  const logged: string[] = [];
  for (const level of ["debug", "info", "warning"] as const) {
    mock.method(logger, level, (message: string) => logged.push(message));
  }
  const folder = join(app, "..", "x\nERROR forged line");
  mkdirSync(folder);
  writeFileSync(join(folder, ".git"), "gitdir: /nonexistent\n");
  assert.ok(!(await workspaceTrusted(folder, home)));
  assert.ok(!logged.join("\n").includes("forged"));
});

/** The first of `length` symlinks, each naming the next: more than the resolver follows. */
function symlinkChain(holder: string, length = 1200): string {
  mkdirSync(holder);
  mkdirSync(join(holder, "end"));
  for (let n = 0; n < length; n++) {
    symlinkSync(join(holder, n + 1 < length ? `l${n + 1}` : "end"), join(holder, `l${n}`));
  }
  return join(holder, "l0");
}

const NO_PATH: [string, (folder: string) => void][] = [
  [
    "nul_in_the_gitfile",
    (folder) => writeFileSync(join(folder, ".git"), Buffer.from("gitdir: /srv/\x00/x\n")),
  ],
  [
    "gitfile_to_a_long_chain_of_symlinks",
    (folder) =>
      writeFileSync(join(folder, ".git"), `gitdir: ${symlinkChain(join(folder, "..", "chain"))}\n`),
  ],
  [
    "git_symlink_to_a_long_chain_of_symlinks",
    (folder) => symlinkSync(symlinkChain(join(folder, "..", "chain")), join(folder, ".git")),
  ],
];

for (const [id, plant] of NO_PATH) {
  test(`metadata that names no path leaves the folder untrusted [${id}]`, POSIX, async () => {
    // One such folder under the allowed root must not take the whole `!bind` list down with it.
    const app = makeApp();
    const folder = join(app, "..", "planted");
    mkdirSync(folder);
    plant(folder);
    assert.ok(!(await workspaceTrusted(folder, home)));
  });
}

test("a worktree registry planted in a trusted folder that is no repository lends nothing", async () => {
  // `code` is trusted and outside git. A `.git` there that holds a worktree registry and no
  // repository does not make `code` a main checkout.
  const code = join(tmp, "code");
  const folder = join(committed(join(code, "clone")), "vendor");
  mkdirSync(folder);
  const registry = join(code, ".git", "worktrees", "id");
  mkdirSync(registry, { recursive: true });
  writeFileSync(join(registry, "gitdir"), `${folder}/.git\n`);
  writeFileSync(join(folder, ".git"), `gitdir: ${registry}\n`);
  trust(home, [code]);
  assert.ok(!(await workspaceTrusted(folder, home)));
});

/** The record with the paths as written, a symlink in them not resolved. */
function record(...paths: string[]): void {
  const projects: Record<string, { hasTrustDialogAccepted: boolean }> = {};
  for (const path of paths) {
    projects[path] = { hasTrustDialogAccepted: true };
  }
  writeFileSync(join(home, ".claude.json"), JSON.stringify({ projects }));
}

test("a recorded path that passes through a symlink trusts nothing", POSIX, async () => {
  const real = join(tmp, "elsewhere", "notes");
  mkdirSync(real, { recursive: true });
  symlinkSync(real, join(tmp, "link"));
  symlinkSync(join(real, ".."), join(tmp, "parent"));
  record(join(tmp, "link"));
  assert.ok(!(await workspaceTrusted(real, home)));
  record(join(tmp, "parent", "notes"));
  assert.ok(!(await workspaceTrusted(real, home)));
  record(real);
  assert.ok(await workspaceTrusted(real, home));
});

test("the git dir reached in another case is the repository s own", POSIX, async (t) => {
  const repo = committed(join(tmp, "app"));
  if (!existsSync(join(repo, ".GIT"))) {
    t.skip("this filesystem tells the two cases apart");
    return;
  }
  trust(home, [repo]);
  assert.ok(await workspaceTrusted(join(repo, ".GIT", "refs"), home));
});

for (const kind of ["fifo", "symlink"]) {
  test(`a registration that is no regular file registers nothing [${kind}]`, POSIX, async () => {
    const app = makeApp();
    const worktree = join(app, "..", "wt");
    const registration = join(app, ".git", "worktrees", "wt", "gitdir");
    const written = readFileSync(registration, "utf8");
    unlinkSync(registration);
    if (kind === "fifo") {
      mkfifo(registration);
    } else {
      writeFileSync(join(app, "..", "elsewhere"), written);
      symlinkSync(join(app, "..", "elsewhere"), registration);
    }
    trust(home, [app]); // the folder `code` no longer, or it would cover the worktree as a folder
    assert.ok(!(await within(workspaceTrusted(worktree, home), 2000)));
  });
}

const LINE_ENDS: [string, string][] = [
  ["crlf", "\r\n"],
  ["two-line-feeds", "\n\n"],
];

for (const [id, tail] of LINE_ENDS) {
  test(`a registration git reads through other line ends registers the worktree [${id}]`, async () => {
    // git strips trailing whitespace from `worktrees/<id>/gitdir` (worktree.c,
    // get_linked_worktree, v2.54.0); a file ending in a CRLF is still the worktree's.
    const app = makeApp();
    const registration = join(app, ".git", "worktrees", "wt", "gitdir");
    writeFileSync(registration, `${readFileSync(registration, "utf8").replace(/\n+$/, "")}${tail}`);
    const listed = git(app, "worktree", "list", "--porcelain");
    assert.ok(listed.includes("wt") && !listed.includes("prunable")); // the control: git still has it
    trust(home, [app]); // the folder `code` no longer, or it would cover the worktree as a folder
    assert.ok(await workspaceTrusted(join(app, "..", "wt"), home));
  });
}

test("stricter than git where only the names match", POSIX, async () => {
  // Two readings kept on the closed side: entries named like a git dir's that git itself
  // rejects, and a `.git` symlink that names nothing.
  const app = makeApp();
  const lookalike = join(app, "..", "lookalike");
  for (const entry of ["objects", "refs"]) {
    mkdirSync(join(lookalike, entry), { recursive: true });
  }
  writeFileSync(join(lookalike, "HEAD"), "no ref at all\n");
  const dangling = join(app, "..", "dangling");
  mkdirSync(dangling);
  symlinkSync(join(app, "..", "nothing-here"), join(dangling, ".git"));
  assert.ok(!gitFindsARepository(lookalike));
  assert.ok(!(await workspaceTrusted(lookalike, home)));
  assert.ok(!(await workspaceTrusted(dangling, home)));
});

// --- the daemon's own git in a repository inside the session's folder ---

/** The folder a session started in: not a repository, and trusted in Claude Code. */
function makeWork(): string {
  const folder = join(tmp, "work");
  mkdirSync(folder);
  trust(home, [folder]);
  return realpathSync(folder);
}

for (const depth of [1, 2, 3]) {
  for (const [id, inside] of [
    ["at-the-root", "."],
    ["in-a-subfolder", "src"],
  ] as const) {
    test(`a repository inside the session s folder is usable without its own trust [${id}-${depth}]`, async () => {
      const work = makeWork();
      const repo = gitInit(join(work, ...["a", "b", "c"].slice(0, depth)));
      mkdirSync(join(repo, "src"));
      assert.ok(!(await workspaceTrusted(repo, home))); // the start gate does not take it
      const found = await trustedRepository(join(repo, inside), work, home);
      assert.ok(found !== null && found.root === realpathSync(repo));
    });
  }
}

test("a repository outside the session s folder needs its own trust", async () => {
  // The agent may `cd` anywhere: only what lies inside the folder it started in is covered.
  const work = makeWork();
  const outside = gitInit(join(tmp, "elsewhere"));
  const sibling = gitInit(join(tmp, "work-sibling")); // the same prefix, not inside `work`
  assert.equal(await trustedRepository(outside, work, home), null);
  assert.equal(await trustedRepository(sibling, work, home), null);
  trust(home, [work, outside]);
  assert.notEqual(await trustedRepository(outside, work, home), null);
});

test("an untrusted session folder covers nothing inside it", async () => {
  const folder = join(tmp, "work");
  const repo = gitInit(join(folder, "app"));
  assert.equal(await trustedRepository(repo, folder, home), null); // no record at all
  trust(home, [folder], false);
  assert.equal(await trustedRepository(repo, folder, home), null);
  trust(home, [folder]);
  assert.notEqual(await trustedRepository(repo, folder, home), null); // the control
});

test("a session folder that is a repository does not cover itself", async () => {
  // Claude Code keys a repository on its own root: a trusted parent does not cover it, and the
  // folder's own root is not "inside" it.
  const repo = gitInit(join(tmp, "code", "app"));
  trust(home, [join(tmp, "code")]);
  assert.equal(await trustedRepository(repo, repo, home), null);
  assert.equal(await trustedRepository(join(repo, ".git"), repo, home), null);
  trust(home, [repo]);
  assert.notEqual(await trustedRepository(repo, repo, home), null);
});

test("a session folder inside a repository covers only what that repository trusts", async () => {
  // A session in a subfolder of a repository the owner never trusted: the folder fails the start
  // gate, so a repository inside it is not covered either.
  const outer = gitInit(join(tmp, "outer"));
  const folder = join(outer, "packages");
  const inner = gitInit(join(folder, "lib"));
  assert.equal(await trustedRepository(inner, folder, home), null);
  trust(home, [outer]);
  assert.notEqual(await trustedRepository(inner, folder, home), null);
});

test(
  "a symlink in the session s folder to a repository elsewhere is not inside",
  POSIX,
  async () => {
    const work = makeWork();
    const elsewhere = gitInit(join(tmp, "elsewhere", "repo"));
    mkdirSync(join(elsewhere, "src"));
    symlinkSync(elsewhere, join(work, "link"));
    assert.equal(await trustedRepository(join(work, "link"), work, home), null);
    assert.equal(await trustedRepository(join(work, "link", "src"), work, home), null);
    // The control: the same repository really inside the folder.
    gitInit(join(work, "real"));
    assert.notEqual(await trustedRepository(join(work, "real"), work, home), null);
  },
);

test("a session folder reached through a symlink covers what is inside it", POSIX, async () => {
  const work = makeWork();
  const repo = gitInit(join(work, "app"));
  symlinkSync(work, join(tmp, "via"));
  const found = await trustedRepository(join(tmp, "via", "app"), join(tmp, "via"), home);
  assert.ok(found !== null && found.root === realpathSync(repo));
});

test("a worktree is inside only when its main checkout is", async () => {
  // A worktree is keyed on its main checkout, wherever the worktree itself is.
  const work = makeWork();
  const mainOutside = gitInit(join(tmp, "main"));
  const away = addWorktree(mainOutside, join(work, "wt-of-an-outside-main"));
  assert.equal(await trustedRepository(away, work, home), null);
  const mainInside = gitInit(join(work, "main"));
  const beside = addWorktree(mainInside, join(work, "wt-of-an-inside-main"));
  const found = await trustedRepository(beside, work, home);
  assert.ok(found !== null && found.key === realpathSync(mainInside));
  // The main checkout trusted by the owner covers its worktree wherever it is.
  trust(home, [work, mainOutside]);
  assert.notEqual(await trustedRepository(away, work, home), null);
});

type Layout = (work: string, outside: string) => string;

const aGitfileNamingAnOutsideGitDir: Layout = (work, outside) => {
  const folder = join(work, "gitfile");
  mkdirSync(folder);
  writeFileSync(join(folder, ".git"), `gitdir: ${outside}/.git\n`);
  return folder;
};

const aGitfileNamingAnOutsideGitDirByARelativePath: Layout = (work, outside) => {
  const folder = join(work, "gitfile");
  mkdirSync(folder);
  writeFileSync(join(folder, ".git"), `gitdir: ${relative(folder, join(outside, ".git"))}\n`);
  return folder;
};

const aGitSymlinkToAnOutsideGitDir: Layout = (work, outside) => {
  const folder = join(work, "gitlink");
  mkdirSync(folder);
  symlinkSync(join(outside, ".git"), join(folder, ".git"));
  return folder;
};

const aWorktreeOfAnOutsideCheckoutMovedInByHand: Layout = (work, outside) => {
  const moved = join(work, "wt-moved");
  renameSync(addWorktree(outside, join(outside, "..", "wt")), moved);
  return moved;
};

const aGitDirWhoseCommondirNamesAnOutsideRepository: Layout = (work, outside) => {
  const folder = join(work, "commondir");
  mkdirSync(join(folder, ".git"), { recursive: true });
  writeFileSync(join(folder, ".git", "HEAD"), "ref: refs/heads/main\n");
  writeFileSync(join(folder, ".git", "commondir"), `${outside}/.git\n`);
  return folder;
};

const aGitDirWhoseCommondirIsASymlinkToAnOutsideName: Layout = (work, outside) => {
  const folder = join(work, "commondir-link");
  mkdirSync(join(folder, ".git"), { recursive: true });
  writeFileSync(join(folder, ".git", "HEAD"), "ref: refs/heads/main\n");
  writeFileSync(join(folder, "..", "names-outside"), `${outside}/.git\n`);
  symlinkSync(join(folder, "..", "names-outside"), join(folder, ".git", "commondir"));
  return folder;
};

function aCommondirNamingAnOutsideRepositoryByALinkAndALineEnd(
  work: string,
  outside: string,
  name: string,
  lineEnd: string,
): string {
  // git strips every trailing CR and LF from `commondir` (setup.c, get_common_dir_noenv,
  // v2.54.0), so `link` is the name whatever follows it; a name that kept its `\r` would be a
  // path that does not exist, which resolves to a place inside the folder.
  const folder = join(work, name);
  mkdirSync(join(folder, ".git"), { recursive: true });
  writeFileSync(join(folder, ".git", "HEAD"), "ref: refs/heads/main\n");
  symlinkSync(join(outside, ".git"), join(folder, ".git", "link"));
  writeFileSync(join(folder, ".git", "commondir"), `link${lineEnd}`);
  return folder;
}

const aGitDirWhoseCommondirEndsInCrlf: Layout = (work, outside) =>
  aCommondirNamingAnOutsideRepositoryByALinkAndALineEnd(work, outside, "commondir-crlf", "\r\n");

const aGitDirWhoseCommondirEndsInTwoLineFeeds: Layout = (work, outside) =>
  aCommondirNamingAnOutsideRepositoryByALinkAndALineEnd(work, outside, "commondir-lflf", "\n\n");

const LEADING_OUTSIDE: [string, Layout][] = [
  ["a_gitfile_naming_an_outside_git_dir", aGitfileNamingAnOutsideGitDir],
  [
    "a_gitfile_naming_an_outside_git_dir_by_a_relative_path",
    aGitfileNamingAnOutsideGitDirByARelativePath,
  ],
  ["a_git_symlink_to_an_outside_git_dir", aGitSymlinkToAnOutsideGitDir],
  ["a_worktree_of_an_outside_checkout_moved_in_by_hand", aWorktreeOfAnOutsideCheckoutMovedInByHand],
  [
    "a_git_dir_whose_commondir_names_an_outside_repository",
    aGitDirWhoseCommondirNamesAnOutsideRepository,
  ],
  [
    "a_git_dir_whose_commondir_is_a_symlink_to_an_outside_name",
    aGitDirWhoseCommondirIsASymlinkToAnOutsideName,
  ],
  ["a_git_dir_whose_commondir_ends_in_crlf", aGitDirWhoseCommondirEndsInCrlf],
  ["a_git_dir_whose_commondir_ends_in_two_line_feeds", aGitDirWhoseCommondirEndsInTwoLineFeeds],
];

for (const [id, layout] of LEADING_OUTSIDE) {
  test(
    `a layout inside the folder that leads git outside it is not covered [${id}]`,
    POSIX,
    async () => {
      // The folder's trust covers a repository inside it, and git must read that repository's own
      // config: a `.git` that points elsewhere makes git read someone else's.
      const work = makeWork();
      const outside = committed(join(tmp, "outside"));
      assert.equal(await trustedRepository(outside, work, home), null); // the control: nothing trusted
      const folder = layout(work, outside);
      assert.equal(await trustedRepository(folder, work, home), null);
      // The owner's own trust of the folder itself is the first way in, and stays as it was.
      trust(home, [work, folder]);
      assert.notEqual(await trustedRepository(folder, work, home), null);
    },
  );
}

for (const [id, layout] of [
  ["a_git_dir_whose_commondir_ends_in_crlf", aGitDirWhoseCommondirEndsInCrlf],
  ["a_git_dir_whose_commondir_ends_in_two_line_feeds", aGitDirWhoseCommondirEndsInTwoLineFeeds],
] as const) {
  test(`git reads the common dir through those line ends [${id}]`, POSIX, () => {
    // The control of the layouts above: git itself ends up in the outside repository.
    const work = makeWork();
    const outside = realpathSync(committed(join(tmp, "outside")));
    const folder = layout(work, outside);
    assert.ok(gitTakesItFor(folder, "--git-common-dir", join(outside, ".git")));
  });
}

test("a commondir git cannot read covers nothing", async () => {
  // An empty `commondir` is a fatal error in git ("failed to read"): not a layout to guess at.
  const work = makeWork();
  const folder = join(work, "commondir-empty");
  mkdirSync(join(folder, ".git"), { recursive: true });
  writeFileSync(join(folder, ".git", "HEAD"), "ref: refs/heads/main\n");
  writeFileSync(join(folder, ".git", "commondir"), Buffer.alloc(0));
  assert.ok(!gitFindsARepository(folder));
  assert.equal(await trustedRepository(folder, work, home), null);
});

test("a git dir outside the folder that the owner trusted is not a second way in", async () => {
  const work = makeWork();
  const outside = committed(join(tmp, "outside"));
  trust(home, [work, outside]);
  const folder = aGitfileNamingAnOutsideGitDir(work, outside);
  // The folder is no repository the owner trusted, and its git dir is not the folder's.
  assert.equal(await trustedRepository(folder, work, home), null);
  assert.notEqual(await trustedRepository(outside, work, home), null);
});

test("layouts that stay inside the folder are covered", POSIX, async () => {
  const work = makeWork();
  const gitfile = gitInit(join(work, "elsewhere-inside", "store"));
  mkdirSync(join(work, "viafile"));
  writeFileSync(join(work, "viafile", ".git"), `gitdir: ${join(gitfile, ".git")}\n`);
  mkdirSync(join(work, "vialink"));
  symlinkSync(join(gitfile, ".git"), join(work, "vialink", ".git"));
  for (const folder of [join(work, "viafile"), join(work, "vialink")]) {
    assert.notEqual(await trustedRepository(folder, work, home), null);
  }
  // A worktree whose main checkout is inside: its git dir and its common dir are too.
  const main = committed(join(work, "main"));
  const beside = addWorktree(main, join(work, "wt"));
  const found = await trustedRepository(beside, work, home);
  assert.ok(found !== null && found.key === realpathSync(main));
});

test("the start gate still refuses a repository the owner did not trust", async () => {
  const work = makeWork();
  const repo = gitInit(join(work, "app"));
  assert.notEqual(await trustedRepository(repo, work, home), null);
  assert.ok(!(await workspaceTrusted(repo, home))); // `!bind` and a session's start stay as they were
  assert.ok(await workspaceTrusted(work, home));
});

test("a layout with no key is not covered inside the session s folder", async () => {
  const work = makeWork();
  const layout = bareLayout(join(work, "vendor"));
  assert.equal(await trustedRepository(layout, work, home), null);
  assert.equal(await trustedRepository(join(layout, "refs"), work, home), null);
});

test("the inside check needs no git", async () => {
  const work = makeWork();
  const repo = gitInit(join(work, "app"));
  const outside = gitInit(join(tmp, "elsewhere"));
  const path = process.env.PATH;
  process.env.PATH = join(tmp, "empty"); // no git anywhere
  try {
    assert.notEqual(await trustedRepository(repo, work, home), null);
    assert.equal(await trustedRepository(outside, work, home), null);
  } finally {
    if (path === undefined) {
      delete process.env.PATH;
    } else {
      process.env.PATH = path;
    }
  }
});
