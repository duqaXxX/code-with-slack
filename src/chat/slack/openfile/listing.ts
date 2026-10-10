/**
 * Which files the search offers. Inside a repository the daemon's git may run in
 * (`trustedRepository`: the one holding the session's folder, or the ones found at most two levels
 * below it) the files come from git, through the footer's `runGit`: the tracked ones and the
 * untracked ones that are not ignored. That is what the terminal's `@` file picker does, since its
 * setting `respectGitignore` defaults to `true` and leaves out the files that match `.gitignore`
 * patterns (code.claude.com/docs/en/settings-reference, read 2026-10-05). Everywhere else the
 * folder is walked: regular files only, no symlinked folder entered, no `.git` entered.
 *
 * Every git command of `!open` (here and in `changed.ts`) is plumbing or `status` under
 * `--no-optional-locks`, because `git diff` refreshes and rewrites the index under `index.lock`
 * (see `changesOf` in the footer), and a lock left by a killed command would stop the owner's own
 * `git add` and commit. Measured on git 2.54.0 (2026-10-05, and 2026-10-06 for the reflog
 * commands) on a scratch repository, with the index's inode, mtime and sha256 compared before and
 * after, in four states of the work tree (clean; a tracked file touched and left unchanged, which
 * is a stat-dirty file; a tracked file edited; an untracked file):
 *
 * - never written, in any state: `rev-parse --verify HEAD@{<time>}` and `HEAD@{<n>}`, `rev-list
 *   -g --count` (also with `--until`), `hash-object -t tree /dev/null`, `ls-files --cached
 *   --others --exclude-standard`, `diff-tree -r`, `diff-files`, `diff-index` and
 *   `--no-optional-locks status`;
 * - written for the stat-dirty file: `status` without `--no-optional-locks`, `diff --name-only`
 *   and `--no-optional-locks diff --name-only`;
 * - reported as changed when only touched: `diff-files` and `diff-index` (the footer's commands);
 *   not reported: `status`, which compares the content of a file whose stat differs.
 *
 * So the committed changes come from `diff-tree` and the rest from `status`, which also lists the
 * untracked files. `test/chat/slack/openfile/changed.test.ts` repeats the measurement on every run.
 */
import { lstat, readdir } from "node:fs/promises";
import { join, sep } from "node:path";
import type { Repository } from "../../../agent/seam.ts";
import { type Clock, monotonicClock } from "../../../clock.ts";
import { foldersWithin } from "../../../core/folders.ts";
import { runGit } from "../../../core/footer.ts";
import { getLogger } from "../../../log.ts";
import { len } from "../reply/chars.ts";
import { describe } from "../reply/errors.ts";
import { compareCodePoints, relativeTo } from "./paths.ts";
import { realpathLoose, regularFiles } from "./read.ts";
import { Gate, TIMED_OUT, waitFor, within } from "./waiting.ts";

export const logger = getLogger("awaydesk.chat.slack.openfile");

// A folder's listing is kept for LISTING_TTL (LISTING_PARTIAL_TTL when it is not complete), so
// the keystrokes of one search do not walk the folder again, and a listing stops at
// LISTING_BUDGET with what it found; LISTING_KEPT folders are kept at most. All in seconds.
export const LISTING_BUDGET = 2.0;
export const LISTING_TTL = 30.0;
export const LISTING_PARTIAL_TTL = 3.0;
export const LISTING_KEPT = 16;
// The repositories found in a folder are kept for a shorter time than its files: one made in the
// session (by Claude Code, in a subfolder) should show within moments. At most GIT_CHAINS
// repositories are asked at once, the rest wait their turn.
export const REPOSITORIES_TTL = 5.0;
export const GIT_CHAINS = 4;

/**
 * Answers for a repository: the one holding a directory, usable for a session started in a folder
 * (`trustedRepository`, whose arguments they are).
 */
export type RepositoryLookup = (
  directory: string,
  sessionFolder: string,
) => Promise<Repository | null>;

/** `repository` when it has a work tree git can list, else null. */
export function usable(repository: Repository | null): Repository | null {
  if (repository === null || repository.gitDir === null || repository.insideGitDir) return null;
  return repository;
}

/**
 * How the folder and the repository's root meet, as `[strip, add]`: git's paths are relative to
 * the root, and a path from the folder is that path less `strip` and with `add` before it. A
 * folder inside the repository strips its own path from the root (`""` at the root itself), a
 * repository inside the folder adds its own path from the folder, and any other pair is null.
 */
export async function relation(
  repository: Repository,
  folder: string,
): Promise<[strip: string, add: string] | null> {
  const real = await realpathLoose(folder);
  const inside = relativeTo(repository.root, real);
  if (inside !== null) return [inside.split(sep).join("/"), ""];
  const held = relativeTo(real, repository.root);
  if (held !== null) return ["", `${held.split(sep).join("/")}/`];
  return null;
}

/**
 * The paths of a NUL-separated git output that lie under `strip`, as paths from the folder, each
 * once.
 */
export function under(output: string, strip: string, add: string): string[] {
  const start = strip ? `${strip}/` : "";
  const names = output
    .split("\0")
    .filter((name) => name.startsWith(start))
    .map((name) => name.slice(start.length));
  return [...new Set(names.filter((name) => name).map((name) => `${add}${name}`))];
}

export function scoped(strip: string): string[] {
  return strip ? ["--", strip] : [];
}

/** The folders under `folder`, as `!bind` lists them, that hold a `.git` entry. */
async function repositoryFolders(folder: string): Promise<string[]> {
  let below: string[];
  try {
    below = await foldersWithin(folder);
  } catch (error) {
    // Python caught `OSError`: a system error has a code.
    if (typeof (error as { code?: unknown } | null)?.code === "string") return [];
    throw error;
  }
  const holding = await Promise.all(
    below.map((path) =>
      path === folder
        ? false
        : lstat(join(path, ".git")).then(
            () => true,
            () => false,
          ),
    ),
  );
  return below.filter((_path, index) => holding[index]);
}

/**
 * The repositories the daemon's git may run in for a session started in `folder`: the one holding
 * the folder when `lookup` finds it usable, else the usable ones found at most two levels below
 * it (the depth `!bind` lists folders to), none of them inside another.
 */
export async function repositoriesOf(
  folder: string,
  lookup: RepositoryLookup,
): Promise<Repository[]> {
  const held = usable(await lookup(folder, folder));
  if (held !== null) return [held];
  const below = await repositoryFolders(folder);
  const looked = await Promise.all(below.map((candidate) => lookup(candidate, folder)));
  const found = new Map<string, Repository>();
  for (const repository of looked.map(usable)) {
    if (repository !== null) found.set(repository.root, repository);
  }
  return [...found.values()];
}

/**
 * The regular files under `root` as paths from it, the shallower first: a symlink is neither
 * followed nor listed, no `.git` is entered, nor any folder in `skip` (paths). Stops when
 * `expired()` is true, with what it has found; the flag says the walk reached its end.
 */
export async function walkFiles(
  root: string,
  skip: ReadonlySet<string>,
  expired: () => boolean,
): Promise<[files: string[], complete: boolean]> {
  const found: string[] = [];
  const queue: Array<[directory: string, prefix: string]> = [[root, ""]];
  let next = 0;
  while (next < queue.length && !expired()) {
    const [directory, prefix] = queue[next] as [string, string];
    next += 1;
    try {
      for (const entry of await readdir(directory, { withFileTypes: true })) {
        if (entry.name === ".git") continue;
        const path = join(directory, entry.name);
        // `Dirent` describes the entry itself: a symlink is neither a directory nor a file.
        if (entry.isDirectory()) {
          if (!skip.has(path)) queue.push([path, `${prefix}${entry.name}/`]);
        } else if (entry.isFile()) {
          found.push(`${prefix}${entry.name}`);
        }
      }
    } catch (error) {
      // A folder the daemon may not open (macOS privacy, permissions) lists nothing.
      logger.debug(`skipped an unreadable folder: ${(error as { code?: unknown }).code}`);
    }
  }
  return [found, next >= queue.length];
}

/**
 * The tracked files and the untracked ones that are not ignored, in the part of the repository
 * that is `folder` or inside it, as paths from `folder`; null when git fails. Not checked for
 * existence: a tracked file deleted from the work tree is listed (`regularFiles` drops it).
 */
export async function projectFiles(
  repository: Repository,
  folder: string,
  signal?: AbortSignal,
): Promise<string[] | null> {
  const found = await relation(repository, folder);
  if (found === null) return null;
  const [strip, add] = found;
  const out = await runGit(
    repository,
    [
      // Literal: the folder's name is not a glob.
      "--literal-pathspecs",
      "ls-files",
      "-z",
      "--cached",
      "--others",
      "--exclude-standard",
      ...scoped(strip),
    ],
    signal,
  );
  return out === null ? null : under(out, strip, add);
}

export interface FolderFilesOptions {
  readonly clock?: Clock;
  /** How many repositories git runs in at once. */
  readonly chains?: number;
  /** The listing of one repository, `projectFiles` unless a test says otherwise. */
  readonly list?: typeof projectFiles;
}

/**
 * The files `!open` can name under `folder`, as paths from it: from git inside a repository of
 * `repositories`, the folder's own disk anywhere else. Within `budget` seconds: what is found by
 * then is returned, a repository whose listing is not done adding nothing. The flag says nothing
 * was left out: no walk or git listing ran out of time or failed.
 */
export async function folderFiles(
  folder: string,
  repositories: readonly Repository[],
  budget: number,
  options: FolderFilesOptions = {},
): Promise<[files: string[], complete: boolean]> {
  const clock = options.clock ?? monotonicClock;
  const list = options.list ?? projectFiles;
  const deadline = clock.time() + budget;
  const real = await realpathLoose(folder);
  const gate = new Gate(options.chains ?? GIT_CHAINS);

  const listed = async (repository: Repository): Promise<[string[], boolean]> => {
    // Waiting for a turn is part of the budget: it is the listing's own.
    const found = await within(clock, budget, async (signal) => {
      await gate.acquire(signal);
      try {
        return await list(repository, folder, signal);
      } finally {
        gate.release();
      }
    });
    if (found === TIMED_OUT) return [[], false];
    return [found ?? [], found !== null];
  };

  let walked: string[] = [];
  let walkDone = true;
  let parts: Array<[string[], boolean]>;
  if (repositories.some((r) => relativeTo(r.root, real) !== null)) {
    // Every file of the folder is in the repository that holds it.
    parts = await Promise.all(repositories.map(listed));
  } else {
    const skip = new Set(repositories.map((r) => r.root));
    const [walk, ...listings] = await Promise.all([
      walkFiles(real, skip, () => clock.time() >= deadline),
      ...repositories.map(listed),
    ]);
    [walked, walkDone] = walk as [string[], boolean];
    parts = listings as Array<[string[], boolean]>;
  }
  const files = [...new Set([...walked, ...parts.flatMap(([found]) => found)])];
  return [files, walkDone && parts.every(([, done]) => done)];
}

/**
 * A folder's files for the search. `complete` says nothing was left out: no walk or git listing
 * ran out of time or failed. `fresh` says it was made for the request that got it, not kept from
 * an earlier one.
 */
export interface Listing {
  readonly files: readonly string[];
  readonly complete: boolean;
  readonly fresh: boolean;
}

/**
 * What a search for `words` found: the files that exist, at most the limit asked, ranked; `count`
 * is how many there are (the matches by name once the check stopped at the limit); `complete` is
 * false when the listing it came from was cut or failed.
 */
export interface Found {
  readonly paths: readonly string[];
  readonly count: number;
  readonly complete: boolean;
}

/**
 * What is made for a folder, kept for as long as its maker says, at most `limit` folders (the
 * oldest go first) with the expired ones removed on the next request. Requests for a folder whose
 * value is being made wait for that one.
 */
class Kept<T> {
  readonly #limit: number;
  readonly #clock: Clock;
  readonly #kept = new Map<string, { until: number; value: T }>();
  readonly #running = new Map<string, Promise<T>>();

  constructor(limit: number, clock: Clock) {
    this.#limit = limit;
    this.#clock = clock;
  }

  /** The folders whose value is kept. */
  folders(): string[] {
    return [...this.#kept.keys()];
  }

  /**
   * The value, and whether it was made for this request (not kept). `make` answers the value and
   * how long to keep it, null for not at all. `again` skips what is kept. A request whose `signal`
   * aborts stops waiting; the work goes on for the others.
   */
  async get(
    folder: string,
    make: () => Promise<[T, number | null]>,
    options: { again?: boolean; signal?: AbortSignal } = {},
  ): Promise<[T, boolean]> {
    const now = this.#clock.time();
    for (const [path, entry] of [...this.#kept]) {
      if (entry.until <= now) this.#kept.delete(path);
    }
    const kept = options.again ? undefined : this.#kept.get(folder);
    if (kept !== undefined) return [kept.value, false];
    let task = this.#running.get(folder);
    if (task === undefined) {
      const started = this.#make(folder, make);
      task = started;
      this.#running.set(folder, started);
      const done = () => {
        if (this.#running.get(folder) === started) this.#running.delete(folder);
      };
      started.then(done, done);
    }
    return [await waitFor(task, options.signal), true];
  }

  async #make(folder: string, make: () => Promise<[T, number | null]>): Promise<T> {
    const started = this.#clock.time();
    const [value, keep] = await make();
    if (keep !== null) {
      this.#kept.delete(folder);
      this.#kept.set(folder, { until: started + keep, value });
      while (this.#kept.size > this.#limit) {
        this.#kept.delete(this.#kept.keys().next().value as string);
      }
    }
    return value;
  }
}

/** The paths that contain `words`, ignoring case: those whose file name does first, then the shorter paths, then by path. */
export function rank(paths: Iterable<string>, words: string): string[] {
  const wanted = words.toLowerCase();
  const found = [...paths].filter((path) => path.toLowerCase().includes(wanted));
  const named = (path: string) =>
    path
      .slice(path.lastIndexOf("/") + 1)
      .toLowerCase()
      .includes(wanted);
  return found.toSorted(
    (left, right) =>
      Number(!named(left)) - Number(!named(right)) ||
      len(left) - len(right) ||
      compareCodePoints(left, right),
  );
}

export interface ListingsOptions {
  readonly ttl?: number;
  readonly partialTtl?: number;
  readonly repositoriesTtl?: number;
  readonly budget?: number;
  readonly limit?: number;
  readonly clock?: Clock;
  /** The check that ranked paths exist, `regularFiles` unless a test says otherwise. */
  readonly regular?: typeof regularFiles;
}

/**
 * The files of a session's folder for the search, and the repositories found in it, kept in
 * memory: Slack asks again on every keystroke, and a folder is not walked each time. A listing is
 * kept `ttl` seconds when it is complete and `partialTtl` when it ran out of time or git failed
 * for part of it, so a folder too large for the budget is not walked on each keystroke but is not
 * taken for complete either. The repositories are kept `repositoriesTtl` seconds. At most `limit`
 * folders of each are kept (the oldest go first), and the expired ones are removed on the next
 * request. Requests for a folder that is being listed wait for that listing. A kept listing is
 * never the reason for "no match": `search` makes it again first.
 */
export class Listings {
  readonly #lookup: RepositoryLookup;
  readonly #ttl: number;
  readonly #partialTtl: number;
  readonly #repositoriesTtl: number;
  readonly #budget: number;
  readonly #clock: Clock;
  readonly #regular: typeof regularFiles;
  readonly #listed: Kept<Listing>;
  readonly #found: Kept<Repository[]>;

  constructor(lookup: RepositoryLookup, options: ListingsOptions = {}) {
    const limit = options.limit ?? LISTING_KEPT;
    this.#lookup = lookup;
    this.#ttl = options.ttl ?? LISTING_TTL;
    this.#partialTtl = options.partialTtl ?? LISTING_PARTIAL_TTL;
    this.#repositoriesTtl = options.repositoriesTtl ?? REPOSITORIES_TTL;
    this.#budget = options.budget ?? LISTING_BUDGET;
    this.#clock = options.clock ?? monotonicClock;
    this.#regular = options.regular ?? regularFiles;
    this.#listed = new Kept<Listing>(limit, this.#clock);
    this.#found = new Kept<Repository[]>(limit, this.#clock);
  }

  /** The folders whose listing is kept. */
  keptListings(): string[] {
    return this.#listed.folders();
  }

  /**
   * The repositories the daemon's git may run in for a session started in `folder`
   * (`repositoriesOf`). Rejects with what the lookup rejects with.
   */
  async repositories(
    folder: string,
    options: { again?: boolean; signal?: AbortSignal } = {},
  ): Promise<Repository[]> {
    const find = async (): Promise<[Repository[], number | null]> => [
      await repositoriesOf(folder, this.#lookup),
      this.#repositoriesTtl,
    ];
    return (await this.#found.get(folder, find, options))[0];
  }

  /**
   * The folder's files as paths from it. Never rejects but for the signal: a listing that fails
   * is empty, incomplete and not kept. `again` makes it anew whatever is kept.
   */
  async of(
    folder: string,
    options: { again?: boolean; signal?: AbortSignal } = {},
  ): Promise<Listing> {
    const [listing, fresh] = await this.#listed.get(
      folder,
      () => this.#list(folder, options.again ?? false),
      options,
    );
    return fresh ? listing : { ...listing, fresh: false };
  }

  async #list(folder: string, again: boolean): Promise<[Listing, number | null]> {
    try {
      const repositories = await this.repositories(folder, { again });
      const [files, complete] = await folderFiles(folder, repositories, this.#budget, {
        clock: this.#clock,
      });
      return [{ files, complete, fresh: true }, complete ? this.#ttl : this.#partialTtl];
    } catch (error) {
      // A search that finds nothing beats a modal that never loads.
      logger.warning(`could not list a folder's files: ${describe(error)}`);
      return [{ files: [], complete: false, fresh: true }, null];
    }
  }

  /**
   * The files of the folder whose path contains `words`, in `rank`'s order, that exist
   * (`regularFiles`), at most `limit` of them checked. Past the limit `count` is the number of
   * matches by name. When a kept listing finds none, the folder is listed again and searched
   * anew: a file made since the listing is not told to be missing.
   */
  async search(
    folder: string,
    words: string,
    options: { limit?: number; signal?: AbortSignal } = {},
  ): Promise<Found> {
    const { limit, signal } = options;
    const listing = await this.of(folder, { signal });
    let found = await this.#matching(folder, listing, words, limit);
    if (found.paths.length === 0 && !listing.fresh) {
      const again = await this.of(folder, { again: true, signal });
      found = await this.#matching(folder, again, words, limit);
    }
    return found;
  }

  async #matching(
    folder: string,
    listing: Listing,
    words: string,
    limit: number | undefined,
  ): Promise<Found> {
    const ranked = rank(listing.files, words);
    const paths = await this.#regular(folder, ranked, limit);
    const count = limit !== undefined && paths.length >= limit ? ranked.length : paths.length;
    return { paths, count, complete: listing.complete };
  }
}
