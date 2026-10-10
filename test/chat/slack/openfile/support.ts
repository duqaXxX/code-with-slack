/** Scratch folders and git repositories for the `!open` tests. */
import { mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterEach, beforeEach } from "node:test";
import { locate, Unkeyed } from "../../../../src/agent/claude/trust.ts";
import type { Repository } from "../../../../src/agent/seam.ts";
import { commitAt, git, gitInit } from "../../../support/git-layouts.ts";

/** Symbolic links and FIFOs are a POSIX matter; Windows is not a supported host yet. */
export const POSIX = {
  skip: process.platform === "win32" ? "links and FIFOs are read by POSIX rules" : false,
};

/** The scratch folder of the running test, made fresh before it and removed after. */
export function scratch(): { readonly dir: string } {
  const state = { dir: "" };
  beforeEach(() => {
    state.dir = realpathSync.native(mkdtempSync(join(tmpdir(), "awaydesk-openfile-")));
  });
  afterEach(() => {
    rmSync(state.dir, { recursive: true, force: true });
  });
  return state;
}

export function write(root: string, name: string, text = "x\n"): string {
  const path = join(root, name);
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, text);
  return path;
}

export function writeAll(root: string, ...names: string[]): void {
  for (const name of names) write(root, name);
}

export function commitAll(root: string, message = "x"): void {
  git(root, "add", "-A");
  git(root, "commit", "-q", "-m", message);
}

/** The repository holding `path`, as the daemon's own lookup finds it. */
export async function repository(path: string): Promise<Repository> {
  const found = await locate(path);
  if (found === null) throw new Error(`no repository at ${path}`);
  return found;
}

/** `trustedRepository` for a test about something else: every repository counts as trusted. */
export async function anyRepository(directory: string): Promise<Repository | null> {
  try {
    return await locate(directory);
  } catch (error) {
    if (error instanceof Unkeyed) return null;
    throw error;
  }
}

// The reflog's times are the committer date: a repository built with fixed ones answers the same
// whatever day the tests run.
export const T0 = 1_790_000_000;

/** A thread's `thread_ts` for a thread that started at the epoch second `when`. */
export function started(when: number): string {
  return `${when}.000100`;
}

/** The empty tree of the repository's own hash algorithm, as git computes it. */
export function emptyTree(repo: string): string {
  return git(repo, "hash-object", "-t", "tree", "/dev/null");
}

/** A repository whose HEAD moved at T0+100, T0+200 and T0+300. */
export function dated(path: string): string {
  const repo = gitInit(path);
  for (const step of [1, 2, 3]) commitAt(repo, `c${step}.py`, T0 + step * 100);
  return repo;
}

/** A repository with one commit at T0+100. */
export function datedRepo(path: string): string {
  const repo = gitInit(path);
  commitAt(repo, "base.py", T0 + 100);
  return repo;
}

/** `commit_of(repo, back)`: the id of `HEAD~back`. */
export function commitOf(repo: string, back: number): string {
  return git(repo, "rev-parse", `HEAD~${back}`);
}
