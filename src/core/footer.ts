/** The context line under every reply as data: git state, usage limits, tokens and the fields. */
import { spawn } from "node:child_process";
import type { ModelTokens, Repository } from "../agent/seam.ts";
import type { FooterFields, UsageLimit } from "../chat/seam.ts";
import { getLogger } from "../log.ts";

export const logger = getLogger("awaydesk.core.footer");

export const USAGE_TTL = 300_000;
// For every git call of one footer together: the reply waits for it.
export const GIT_TIMEOUT = 5_000;
// Outputs measured on Claude Code 2.1.280 (2026-09-23); the "with X effort" form is the one
// ccstatusline reads from transcripts.
const EFFORT_OUTPUT = /^(?:Set effort level to|Effort level set to) ([a-z0-9-]+)/i;
const MODEL_OUTPUT = /^Set model to\b(?:.*? with ([a-z0-9-]+) effort)?/is;
// ` 2 files changed, 42 insertions(+), 10 deletions(-)`, as every --shortstat writes it, git 2.54
// (2026-09-27).
const SHORTSTAT_INSERTIONS = /(\d+) insertions?\(\+\)/;
const SHORTSTAT_DELETIONS = /(\d+) deletions?\(-\)/;
// Variables of an outer git (a hook) that would send ours elsewhere.
const GIT_REDIRECTS = ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"];
const MINUTE_MS = 60_000;

/** The limits `/usage` reports; a limit the account does not have, or the text hid, is null. */
export interface Usage {
  readonly session: UsageLimit | null;
  readonly week: UsageLimit | null;
}

/** The branch and the changes of a folder; each null when unknown. */
export type GitState = readonly [
  branch: string | null,
  changes: readonly [inserted: number, deleted: number] | null,
];

/** Which of the footer's values a field is, for a provider that marks each in its own way. */
export type FooterKey =
  | "model"
  | "effort"
  | "branch"
  | "changes"
  | "tokens"
  | "context"
  | "session_limit"
  | "week_limit";

/** One of the footer's values: `!status` shows `label: value`, a provider's footer its own mark. */
export interface FooterField {
  readonly key: FooterKey;
  readonly label: string;
  readonly value: string;
}

/**
 * The usage cache: `/usage` costs a model call on a client of its own, so it is read at most once
 * in `ttl` unless `invalidate` says the limits moved.
 */
export class UsageCache {
  current: Usage | null = null;
  private readonly fetch: () => Promise<Usage>;
  private readonly ttl: number;
  private readonly clock: () => number;
  private readonly warn: (message: string) => void;
  private fetchedAt: number | null = null;
  private refreshing = false;

  constructor(
    fetch: () => Promise<Usage>,
    options: {
      readonly ttl?: number;
      readonly clock?: () => number;
      readonly warn?: (message: string) => void;
    } = {},
  ) {
    this.fetch = fetch;
    this.ttl = options.ttl ?? USAGE_TTL;
    this.clock = options.clock ?? (() => performance.now());
    this.warn = options.warn ?? ((message) => logger.warning(message));
  }

  invalidate(): void {
    this.fetchedAt = null;
  }

  /** Refresh when older than the TTL; a failure keeps the old value and waits a full TTL. */
  async refreshIfStale(): Promise<void> {
    if (this.refreshing) {
      return;
    }
    this.refreshing = true;
    try {
      if (this.fetchedAt !== null && this.clock() - this.fetchedAt < this.ttl) {
        return;
      }
      try {
        this.current = await this.fetch();
      } catch (error) {
        // The footer is optional: never let it break a reply. The name only, never the text.
        this.warn(`usage refresh failed: ${error instanceof Error ? error.name : typeof error}`);
      }
      this.fetchedAt = this.clock();
    } finally {
      this.refreshing = false;
    }
  }
}

/**
 * `git` on `repository`: its output, or null when it fails. The caller holds the time limit
 * (`gitState` here, `openfile` for `!open`) through `signal`, which kills the process and
 * rejects with its reason, and chooses commands that never write the index (see `changesOf`).
 */
export function runGit(
  repository: Repository,
  args: readonly string[],
  signal?: AbortSignal,
): Promise<string | null> {
  if (repository.gitDir === null) {
    return Promise.resolve(null);
  }
  const env = { ...process.env };
  for (const name of GIT_REDIRECTS) {
    delete env[name];
  }
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason);
      return;
    }
    const child = spawn(
      "git",
      [
        // A diff must not run the repo's own fsmonitor command.
        "-c",
        "core.fsmonitor=false",
        // Named, never found: git left to search from the folder would take a planted `.git`
        // file, a bare layout or a `core.worktree` at its word (git(1), `--git-dir`).
        "--git-dir",
        repository.gitDir as string,
        ...args,
      ],
      { cwd: repository.root, env, stdio: ["ignore", "pipe", "ignore"] },
    );
    const chunks: Buffer[] = [];
    const onAbort = () => {
      // Also when the time limit or the session closing cancels the footer: no git left running.
      child.kill("SIGKILL");
      reject(signal?.reason);
    };
    signal?.addEventListener("abort", onAbort, { once: true });
    child.stdout.on("data", (chunk: Buffer) => chunks.push(chunk));
    child.once("error", () => {
      signal?.removeEventListener("abort", onAbort);
      resolve(null);
    });
    child.once("close", (code) => {
      signal?.removeEventListener("abort", onAbort);
      resolve(code === 0 ? Buffer.concat(chunks).toString("utf8") : null);
    });
  });
}

/** Insertions and deletions from a `--shortstat`, which leaves out a zero count. */
export function shortstatLines(out: string): [inserted: number, deleted: number] {
  const insertions = SHORTSTAT_INSERTIONS.exec(out);
  const deletions = SHORTSTAT_DELETIONS.exec(out);
  return [insertions ? Number(insertions[1]) : 0, deletions ? Number(deletions[1]) : 0];
}

/**
 * Lines inserted and deleted since the last commit, staged and unstaged, as ccstatusline's
 * git-changes counts them for the terminal (untracked files not counted). Null where there is
 * no work tree.
 *
 * Plumbing only: `git diff` refreshes and rewrites the index under `index.lock` (measured on
 * git 2.54, `--no-optional-locks` included), and a lock left by a killed diff would stop every
 * `git add` and commit in the repo. `diff-files` and `diff-index` never write it.
 *
 * A submodule counts by its commit alone. Without `--ignore-submodules=dirty` git runs
 * `git status` inside every nested repository the index names, under that repository's own
 * config and filters (measured on git 2.54, 2026-10-04).
 */
async function changesOf(
  repository: Repository,
  signal: AbortSignal,
): Promise<[inserted: number, deleted: number] | null> {
  const unstaged = await runGit(
    repository,
    ["diff-files", "--shortstat", "--ignore-submodules=dirty"],
    signal,
  );
  if (unstaged === null) {
    return null;
  }
  // `--`: git runs at the root, where a file named HEAD would make the revision ambiguous.
  let staged = await runGit(
    repository,
    ["diff-index", "--cached", "--shortstat", "HEAD", "--"],
    signal,
  );
  if (staged === null) {
    // No commit yet: what is staged is compared with the empty tree, as `git diff --cached`.
    const empty = await runGit(repository, ["hash-object", "-t", "tree", "/dev/null"], signal);
    if (empty === null) {
      return null;
    }
    staged = await runGit(
      repository,
      ["diff-index", "--cached", "--shortstat", empty.trim()],
      signal,
    );
    if (staged === null) {
      return null;
    }
  }
  const [added, removed] = shortstatLines(unstaged);
  const [addedStaged, removedStaged] = shortstatLines(staged);
  return [added + addedStaged, removed + removedStaged];
}

/** `promise`, or the signal's reason when it aborts first. */
function until<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(signal.reason);
      return;
    }
    const onAbort = () => reject(signal.reason);
    signal.addEventListener("abort", onAbort, { once: true });
    promise.then(resolve, reject).finally(() => signal.removeEventListener("abort", onAbort));
  });
}

/**
 * The branch and the changes of the repository holding `here`, each null when unknown.
 *
 * `repository` answers only for a repository the owner trusted in Claude Code, or one inside the
 * folder the session started in (`trustedRepository`): anywhere else no git runs, since a diff
 * runs the filters a repository's config names. Every call shares one `timeout` (milliseconds).
 */
export async function gitState(
  here: string,
  repository: (folder: string) => Promise<Repository | null>,
  timeout: number = GIT_TIMEOUT,
): Promise<GitState> {
  let branch: string | null = null;
  let changes: [number, number] | null = null;
  const limit = new AbortController();
  const timer = setTimeout(() => limit.abort(new Error("git timeout")), timeout);
  const { signal } = limit;
  try {
    const found = await until(repository(here), signal);
    if (found === null || found.gitDir === null) {
      return [null, null];
    }
    const out = await runGit(found, ["branch", "--show-current"], signal);
    branch = out ? out.trim() || null : null;
    if (!found.insideGitDir) {
      changes = await changesOf(found, signal);
    }
  } catch (error) {
    // What the limit cut off stays unknown; anything else is not ours to hide.
    if (!signal.aborted) {
      throw error;
    }
  } finally {
    clearTimeout(timer);
  }
  return [branch, changes];
}

/**
 * Whether a command's output changes the effort level, and to what (null: back to unknown).
 *
 * `/effort` and `/model` run no Stop hook, the footer's other source (Claude Code 2.1.280), so
 * the footer follows their output. A model change without an effort clears the level.
 */
export function effortChange(output: string): [changed: boolean, effort: string | null] {
  const text = output.trim();
  const effort = EFFORT_OUTPUT.exec(text);
  if (effort) {
    return [true, (effort[1] as string).toLowerCase()];
  }
  const model = MODEL_OUTPUT.exec(text);
  if (model) {
    return [true, model[1] ? model[1].toLowerCase() : null];
  }
  return [false, null];
}

/** The tokens of a turn summed over its models; null when it used none. */
export function sessionTokens(tokens: Readonly<Record<string, ModelTokens>>): number | null {
  const models = Object.values(tokens);
  if (models.length === 0) {
    return null;
  }
  return models.reduce((sum, t) => sum + t.input + t.output + t.cacheRead + t.cacheCreation, 0);
}

/**
 * `value` with `digits` decimals as Python's `format` writes it. A tie such as 6.5 or 12.25
 * (exact in binary) goes to the even digit there and to the larger one in `toFixed`.
 */
function formatFixed(value: number, digits: number): string {
  const rounded = value.toFixed(digits);
  // `toFixed` writes the exact binary value up to 100 digits: a tie is a 5 and then zeros.
  const exact = value.toFixed(digits + 30);
  const tail = exact.slice(exact.length - 30);
  if (!/^50*$/.test(tail)) {
    return rounded;
  }
  const kept = exact.slice(0, exact.length - 30).replace(/\.$/, "");
  return Number(rounded.at(-1)) % 2 === 0 ? rounded : kept;
}

export function formatTokens(count: number): string {
  if (count >= 1_000_000) {
    return `${formatFixed(count / 1_000_000, 1)}M`;
  }
  if (count >= 1_000) {
    return `${formatFixed(count / 1_000, 1)}k`;
  }
  return String(count);
}

/** The time to a reset, `delta` in milliseconds: minutes, then hours, then days and hours. */
export function formatUntil(delta: number): string {
  const minutes = Math.max(0, Math.floor(delta / MINUTE_MS));
  if (minutes < 60) {
    return `${minutes}m`;
  }
  let hours = Math.floor(minutes / 60);
  if (hours < 48) {
    return `${hours}h`;
  }
  // Days and hours, as the terminal's weekly reset timer (ccstatusline) counts down a week.
  const days = Math.floor(hours / 24);
  hours %= 24;
  return hours ? `${days}d ${hours}h` : `${days}d`;
}

/** `3% ↻ 2h`: the share used, and the time to its reset when known. */
export function formatLimit(limit: UsageLimit, now: number): string {
  if (limit.resetsAt === null) {
    return `${limit.percent}%`;
  }
  return `${limit.percent}% ↻ ${formatUntil(limit.resetsAt - now)}`;
}

/**
 * The values the footer and `!status` both show, in the footer's order; what is not known is left
 * out. One list, so the two never write a value differently.
 */
export function footerFields(data: FooterFields, now: number): FooterField[] {
  const fields: FooterField[] = [];
  if (data.model) {
    fields.push({ key: "model", label: "Model", value: data.model });
  }
  if (data.effort) {
    fields.push({ key: "effort", label: "Effort", value: data.effort });
  }
  if (data.branch) {
    fields.push({ key: "branch", label: "Branch", value: data.branch });
  }
  if (data.changes !== null) {
    fields.push({
      key: "changes",
      label: "Uncommitted",
      value: `(+${data.changes[0]},-${data.changes[1]})`,
    });
  }
  if (data.sessionTokens !== null) {
    fields.push({
      key: "tokens",
      label: "Session tokens",
      value: formatTokens(data.sessionTokens),
    });
  }
  if (data.contextPercent !== null) {
    fields.push({
      key: "context",
      label: "Context",
      value: `${formatFixed(data.contextPercent, 0)}%`,
    });
  }
  if (data.sessionLimit) {
    fields.push({
      key: "session_limit",
      label: "5h limit",
      value: formatLimit(data.sessionLimit, now),
    });
  }
  if (data.weekLimit) {
    fields.push({ key: "week_limit", label: "7d limit", value: formatLimit(data.weekLimit, now) });
  }
  return fields;
}

/**
 * The footer's values as `!status` lines, one per field; bypass and the folder are left out,
 * since the status's Mode and Directory lines already show them.
 */
export function formatStatusFields(data: FooterFields, now: number): string[] {
  return footerFields(data, now).map((field) => `${field.label}: \`${field.value}\``);
}
