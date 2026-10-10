/**
 * The changed files of a session: those added or modified since the thread started, committed or
 * not, and the untracked ones that are not ignored. The measurements behind the git commands are
 * in the `listing.ts` header.
 */
import type { Repository } from "../../../agent/seam.ts";
import { type Clock, monotonicClock } from "../../../clock.ts";
import { GIT_TIMEOUT, runGit } from "../../../core/footer.ts";
import { len, strip } from "../reply/chars.ts";
import { GIT_CHAINS, relation, scoped, under } from "./listing.ts";
import { Gate, TIMED_OUT, within } from "./waiting.ts";

// Python's `^(\d+)(?:\.\d+)?$`: `\d` is any decimal digit there and `$` also matches before a
// final newline.
const EPOCH_SECONDS = /^(\p{Nd}+)(?:\.\p{Nd}+)?\n?$/u;
const DECIMAL = /^\p{Nd}+$/u;

/**
 * Where the repository's HEAD was when the thread started, which `threadTs` (the Slack ts of its
 * root, epoch seconds) says: `HEAD@{<seconds> +0000}`, read from HEAD's own reflog (a linked
 * worktree has its own). A commit, or the empty tree when the repository was made after the thread
 * began (below), so that every file counts. Null when git has no answer: no reflog (a repository
 * with `core.logAllRefUpdates` off, or a first commit not made yet), or a `threadTs` that is no
 * time.
 *
 * Measured on git 2.54.0 (2026-10-05, 2026-10-06). The bare number is read as a time
 * (`HEAD@{1790000150}`) and so is the internal format with its zone, which is the one used: it
 * leaves no room for git's approximate dates. A time before the oldest entry gives that entry's
 * old commit (a log that `git reflog expire` shortened) or, when its old value is null, its new
 * one: the first commit, or a clone's tip, which would leave that commit's own files out of what
 * changed. The two are told apart by commands that read the log and write nothing:
 *
 * - `rev-list -g --until=<seconds> +0000 --count HEAD` is 0 when every entry is after the thread's
 *   start (it counts the entries up to that time, as `HEAD@{<time>}` does);
 * - `rev-list -g --count HEAD` is the number of entries, N, and `rev-parse --verify --quiet
 *   HEAD@{N}` is the oldest entry's old value, which fails when it is null (the repository was
 *   made, or cloned, since); a shortened log answers;
 * - `hash-object -t tree /dev/null` is the empty tree of the repository's hash algorithm.
 *
 * A log that git cannot be asked this about keeps the answer `HEAD@{<time>}` gave.
 */
export async function startCommit(
  repository: Repository,
  threadTs: string,
  signal?: AbortSignal,
): Promise<string | null> {
  const seconds = EPOCH_SECONDS.exec(threadTs);
  if (seconds === null) return null;
  const when = `${seconds[1]} +0000`;
  const found = await runGit(
    repository,
    ["rev-parse", "--verify", "--quiet", `HEAD@{${when}}`],
    signal,
  );
  const start = strip(found ?? "") || null;
  if (start === null) return null;
  const until = await runGit(
    repository,
    ["rev-list", "-g", `--until=${when}`, "--count", "HEAD"],
    signal,
  );
  if (until === null || strip(until) !== "0") return start;
  const entries = await runGit(repository, ["rev-list", "-g", "--count", "HEAD"], signal);
  if (entries === null || !DECIMAL.test(strip(entries))) return start;
  const oldest = await runGit(
    repository,
    ["rev-parse", "--verify", "--quiet", `HEAD@{${strip(entries)}}`],
    signal,
  );
  if (oldest) return start;
  const emptied = await runGit(repository, ["hash-object", "-t", "tree", "/dev/null"], signal);
  return strip(emptied ?? "") || start;
}

/**
 * The files in the part of the repository that is `folder` or inside it, added or modified since
 * `start`, committed or not, and the untracked ones that are not ignored, as paths from `folder`.
 * Not checked for existence: a deleted file may be named (`newestFirst` drops it). With no `start`,
 * only what is not committed. Null when git fails.
 */
export async function changedSince(
  repository: Repository,
  folder: string,
  start: string | null,
  signal?: AbortSignal,
): Promise<string[] | null> {
  const found = await relation(repository, folder);
  if (found === null) return null;
  const [stripped, add] = found;
  const scope = scoped(stripped);
  const status = await runGit(
    repository,
    [
      "--literal-pathspecs",
      "--no-optional-locks",
      "status",
      "--porcelain=v1",
      "-z",
      "--untracked-files=all",
      "--ignore-submodules=all",
      "--no-renames",
      ...scope,
    ],
    signal,
  );
  if (status === null) return null;
  // `XY path`: two status letters and a space come before the path.
  const names = status
    .split("\0")
    .filter((entry) => len(entry) > 3)
    .map((entry) => entry.slice(3));
  if (start !== null) {
    const committed = await runGit(
      repository,
      [
        "--literal-pathspecs",
        "diff-tree",
        "-r",
        "-z",
        "--name-only",
        "--no-renames",
        "--diff-filter=AMT",
        start,
        "HEAD",
        ...scope,
      ],
      signal,
    );
    // A start that no longer resolves (rewritten history): the uncommitted files alone.
    names.push(...(committed ?? "").split("\0"));
  }
  return under(names.join("\0"), stripped, add);
}

export interface ChangedInOptions {
  readonly clock?: Clock;
  /** How many repositories git runs in at once. */
  readonly chains?: number;
  /** `startCommit` and `changedSince`, unless a test says otherwise. */
  readonly start?: typeof startCommit;
  readonly since?: typeof changedSince;
}

/**
 * The changed files of every repository of `repositories` (see `changedSince`), each from where
 * its own HEAD was when the thread `threadTs` started (`startCommit`), as paths from `folder`,
 * each once. A repository whose git fails or runs over `GIT_TIMEOUT` adds none.
 */
export async function changedIn(
  folder: string,
  repositories: readonly Repository[],
  threadTs: string,
  options: ChangedInOptions = {},
): Promise<string[]> {
  const clock = options.clock ?? monotonicClock;
  const start = options.start ?? startCommit;
  const since = options.since ?? changedSince;
  const gate = new Gate(options.chains ?? GIT_CHAINS);

  // The turn is waited for outside the time limit: that is git's own.
  const changed = (repository: Repository): Promise<string[]> =>
    gate.run(async () => {
      const found = await within(clock, GIT_TIMEOUT / 1000, async (signal) => {
        const from = await start(repository, threadTs, signal);
        return (await since(repository, folder, from, signal)) ?? [];
      });
      return found === TIMED_OUT ? [] : found;
    });

  const parts = await Promise.all(repositories.map(changed));
  return [...new Set(parts.flat())];
}
