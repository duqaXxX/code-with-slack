/**
 * Git folders built for the trust and footer tests: the layouts an owner makes, and the ones an
 * archive can carry (a `git clone` delivers no `.git` entry). Measured on git 2.54.0, 2026-10-04.
 */
import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, realpathSync, renameSync, statSync, writeFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";

const IDENTITY = ["-c", "user.name=alice", "-c", "user.email=alice@example.com"];

/** The environment of a spawned git: the variables of an outer git (a hook) removed. */
function gitEnv(extra: Record<string, string> = {}): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...process.env, ...extra };
  for (const name of ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"]) {
    delete env[name];
  }
  return env;
}

/** Whether two paths are the same directory entry on disk, by device and inode. */
export function sameFile(a: string, b: string): boolean {
  const first = statSync(a, { bigint: true });
  const second = statSync(b, { bigint: true });
  return first.dev === second.dev && first.ino === second.ino;
}

/**
 * Write Claude Code's own record of trusted folders (permissions reference, 2026-09-25:
 * `projects["<path>"].hasTrustDialogAccepted` in `~/.claude.json`).
 */
export function trust(home: string, paths: readonly string[], accepted = true): void {
  const projects: Record<string, { hasTrustDialogAccepted: boolean }> = {};
  for (const path of paths) {
    projects[realpathSync(path)] = { hasTrustDialogAccepted: accepted };
  }
  writeFileSync(join(home, ".claude.json"), JSON.stringify({ projects }));
}

/** `git` run in `cwd` with a fixed identity; its stdout, stripped. Throws when git fails. */
export function git(cwd: string, ...args: string[]): string {
  const out = execFileSync("git", [...IDENTITY, "-c", "protocol.file.allow=always", ...args], {
    cwd,
    env: gitEnv(),
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  return out.trim();
}

/**
 * `git` run with the epoch second `when` as its committer and author date: the time the reflog
 * records for what it does.
 */
export function gitAt(cwd: string, when: number, ...args: string[]): void {
  const env = gitEnv({
    GIT_COMMITTER_DATE: `${when} +0000`,
    GIT_AUTHOR_DATE: `${when} +0000`,
  });
  execFileSync("git", [...IDENTITY, ...args], { cwd, env, stdio: ["ignore", "pipe", "pipe"] });
}

/** Commit a new file at the epoch second `when`; the commit's id. */
export function commitAt(root: string, name: string, when: number): string {
  mkdirSync(dirname(join(root, name)), { recursive: true });
  writeFileSync(join(root, name), "x\n");
  gitAt(root, when, "add", "-A");
  gitAt(root, when, "commit", "-q", "-m", name);
  return git(root, "rev-parse", "HEAD");
}

export function gitInit(path: string): string {
  mkdirSync(path, { recursive: true });
  git(path, "init", "-q", "-b", "main");
  return path;
}

/** A repository on `main` with one tracked file, `README`. */
export function committed(path: string): string {
  gitInit(path);
  writeFileSync(join(path, "README"), "one\n");
  git(path, "add", "-A");
  git(path, "commit", "-q", "-m", "x");
  return path;
}

export function addWorktree(repo: string, path: string): string {
  git(repo, "commit", "-q", "--allow-empty", "-m", "x");
  git(repo, "worktree", "add", "-q", path);
  return path;
}

/** A folder that is itself a git dir: HEAD, objects, refs and a config of its own. */
export function bareLayout(path: string, config: Record<string, string> = {}): string {
  const seed = committed(join(dirname(path), `${basename(path)}-seed`));
  renameSync(join(seed, ".git"), path);
  for (const [key, value] of Object.entries(config)) {
    git(path, "config", "-f", "config", key, value);
  }
  return path;
}

/** A folder git takes for a git dir by its entries alone: a HEAD, `objects` and `refs`. */
export function gitDirAt(path: string): string {
  mkdirSync(join(path, "objects"), { recursive: true });
  mkdirSync(join(path, "refs"));
  writeFileSync(join(path, "HEAD"), "ref: refs/heads/main\n");
  return path;
}

/** Whether git, run in `folder`, answers `rev-parse <asked>` with `claimed`. */
export function gitTakesItFor(folder: string, asked: string, claimed: string): boolean {
  return sameFile(git(folder, "rev-parse", "--path-format=absolute", asked), claimed);
}

export function gitFindsARepository(folder: string): boolean {
  const found = spawnSync("git", ["rev-parse", "--git-dir"], {
    cwd: folder,
    env: gitEnv(),
    stdio: "ignore",
  });
  return found.status === 0;
}
