/**
 * Claude Code's sessions of one folder: the SDK's listing, their dates as the terminal's picker
 * shows them, and the ids that are alive.
 *
 * What the public SDK does not give. `SDKSessionInfo.lastModified` is the transcript file's
 * mtime, and `getSessionMessages` returns messages without a time (`SessionMessage` has no
 * timestamp field, SDK 0.3.296 `sdk.d.ts`); no public function names a session's transcript path
 * or a folder's project directory either. The Python daemon read the SDK's private
 * `_find_project_dir` and `_canonicalize_path`; the TypeScript package has no public equivalent,
 * so the folder is derived here the way the package itself derives it (read in `sdk.mjs` 0.3.296,
 * the functions that build `<config dir>/projects/<key>`): the real path, composed to NFC on
 * macOS; every character that is not an ASCII letter or digit turned into `-`; past 200
 * characters, the first 200, a `-` and the base-36 form of the absolute value of the 32-bit
 * string hash `h = 31 * h + unit`. `test/agent/claude/listing.test.ts` writes a transcript where
 * this derivation puts it and has the SDK's own `listSessions` find it, so a change in the
 * package fails a test.
 */
import { open, readdir, realpath, stat } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { listSessions as sdkListSessions } from "@anthropic-ai/claude-agent-sdk";
import { getLogger } from "../../log.ts";
import type { ListedSession } from "../seam.ts";
import { listedSession } from "./info.ts";
import { isRecord, string } from "./wire.ts";

export const logger = getLogger("awaydesk.agent.claude.listing");

/** The rows `!resume` lists: dating stops once the rest cannot enter them. */
export const RESUME_ROWS = 20;
/** The end of a transcript that is read for its last message. */
export const TAIL_BYTES = 256 * 1024;
// Past this many characters the CLI names a project folder with a hash (see the module doc).
export const LONG_PROJECT_KEY = 200;

/**
 * Where Claude Code keeps every project's transcripts: "Claude Code stores session transcripts
 * locally in plaintext under `~/.claude/projects/`" (https://code.claude.com/docs/en/data-usage,
 * read 2026-09-28), under `CLAUDE_CONFIG_DIR` when that is set.
 */
export function projectsDir(
  env: { readonly CLAUDE_CONFIG_DIR?: string | undefined } = process.env,
  home: string = homedir(),
): string {
  const override = env.CLAUDE_CONFIG_DIR;
  const config = override ? override : join(home, ".claude");
  return join(process.platform === "darwin" ? config.normalize("NFC") : config, "projects");
}

/** The real path of `directory`; the path itself when it cannot be resolved. */
async function canonical(directory: string): Promise<string> {
  let path = directory;
  try {
    path = await realpath(directory);
  } catch {
    // A folder that does not exist keeps the name it was given.
  }
  return process.platform === "darwin" ? path.normalize("NFC") : path;
}

function stringHash(text: string): number {
  let hash = 0;
  for (let index = 0; index < text.length; index += 1) {
    hash = ((hash << 5) - hash + text.charCodeAt(index)) | 0;
  }
  return hash;
}

/** The name of `directory`'s folder under the projects directory. */
export async function projectKey(directory: string): Promise<string> {
  const path = await canonical(directory);
  const sanitized = path.replace(/[^a-zA-Z0-9]/g, "-");
  if (sanitized.length <= LONG_PROJECT_KEY) return sanitized;
  return `${sanitized.slice(0, LONG_PROJECT_KEY)}-${Math.abs(stringHash(path)).toString(36)}`;
}

async function isDirectory(path: string): Promise<boolean> {
  try {
    return (await stat(path)).isDirectory();
  } catch {
    return false;
  }
}

/**
 * The folder that holds `directory`'s transcripts, or null when there is none. The hash of a long
 * name is the CLI's to compute and may differ from ours, so the first folder with the same first
 * 200 characters stands in for it.
 */
export async function projectFolder(directory: string, projects: string): Promise<string | null> {
  const key = await projectKey(directory);
  const exact = join(projects, key);
  if (await isDirectory(exact)) return exact;
  if (key.length <= LONG_PROJECT_KEY) return null;
  const prefix = key.slice(0, LONG_PROJECT_KEY + 1);
  try {
    for (const entry of await readdir(projects, { withFileTypes: true })) {
      if (entry.isDirectory() && entry.name.startsWith(prefix)) return join(projects, entry.name);
    }
  } catch {
    // No projects directory: no folder.
  }
  return null;
}

/** The sessions of `directory` alone, newest first. */
export async function directorySessions(directory: string): Promise<ListedSession[]> {
  // The terminal's picker also starts from the current worktree (sessions reference, read
  // 2026-09-25). The programmatic sessions stay in: the daemon's own are among them.
  const found = await sdkListSessions({ dir: directory, includeWorktrees: false });
  return found.map(listedSession).sort((a, b) => b.lastModified - a.lastModified);
}

/**
 * The time of the last user or assistant entry in a transcript, read from its end; null when
 * there is none or the file cannot be read.
 *
 * `listSessions` dates a session by its file's mtime, and Claude Code appends bookkeeping
 * entries with no timestamp to old transcripts (artifact ledgers, seen 2026-09-25 with the
 * bundled CLI 2.1.280): a session untouched for days then reads as minutes old. The terminal's
 * picker shows the last activity, so this reads it. The transcript format is not documented.
 */
export async function lastMessageMs(path: string): Promise<number | null> {
  let tail: string;
  try {
    const file = await open(path, "r");
    try {
      const { size } = await file.stat();
      const start = Math.max(0, size - TAIL_BYTES);
      const buffer = Buffer.alloc(size - start);
      const { bytesRead } = await file.read(buffer, 0, buffer.length, start);
      tail = buffer.subarray(0, bytesRead).toString("utf8");
    } finally {
      await file.close();
    }
  } catch {
    return null;
  }
  for (const line of tail.split(/\r\n|\r|\n/).reverse()) {
    let entry: unknown;
    try {
      entry = JSON.parse(line);
    } catch {
      continue; // the first line of the tail may be cut
    }
    if (isRecord(entry) && (entry.type === "user" || entry.type === "assistant")) {
      const stamp = string(entry.timestamp);
      if (stamp === null) continue;
      const time = Date.parse(stamp);
      return Number.isNaN(time) ? null : time;
    }
  }
  return null;
}

export interface DatingOptions {
  /** The projects directory; Claude Code's own when none. */
  readonly projects?: string;
  /** The rows of the list the dates are for. */
  readonly rows?: number;
  /** Reads a transcript's last message time. */
  readonly readStamp?: (path: string) => Promise<number | null>;
}

/**
 * `sessions` ordered by their last message, as the terminal's picker shows them: every session
 * that can be among the newest `rows` is dated, the rest keep their file's mtime.
 */
export async function byLastActivity(
  directory: string,
  sessions: readonly ListedSession[],
  options: DatingOptions = {},
): Promise<ListedSession[]> {
  const { projects = projectsDir(), rows = RESUME_ROWS, readStamp = lastMessageMs } = options;
  const folder = await projectFolder(directory, projects);
  if (folder === null) {
    // Also what a change in the package's naming would look like: say so.
    logger.warning("found no transcript folder: session dates fall back to file times");
    return [...sessions];
  }
  const byMtime = [...sessions].sort((a, b) => b.lastModified - a.lastModified);
  const dated: ListedSession[] = [];
  const newest: number[] = []; // the `rows` newest stamps dated so far, largest first
  for (const [index, session] of byMtime.entries()) {
    // A file's mtime bounds its last message from above: once `rows` dated sessions are at or
    // above this mtime, no file left can enter the list, so none is read.
    const oldest = newest[rows - 1];
    if (oldest !== undefined && oldest >= session.lastModified) {
      return [...newestFirst(dated), ...byMtime.slice(index)];
    }
    const stamp = (await readStamp(join(folder, `${session.id}.jsonl`))) || session.lastModified;
    dated.push({ ...session, lastModified: stamp });
    newest.push(stamp);
    newest.sort((a, b) => b - a);
    newest.length = Math.min(newest.length, rows);
  }
  return newestFirst(dated);
}

function newestFirst(sessions: readonly ListedSession[]): ListedSession[] {
  return [...sessions].sort((a, b) => b.lastModified - a.lastModified);
}

/**
 * The session ids alive in `directory`, for the state's pruning: those the listing shows,
 * unioned with every transcript file actually there. The listing skips sidechain and
 * metadata-only sessions (the SDK's own rules): a session real enough to be stored in a thread
 * must never be pruned just because it has not built up a title yet. Null is "cannot tell", and
 * the pruning then keeps the folder's threads: past `LONG_PROJECT_KEY` characters the CLI names
 * the folder with a hash we may not reproduce, so a missing folder there proves nothing; and a
 * folder that is there and cannot be read cannot tell either (a listing reads an unreadable
 * folder as an empty one, and every session would look gone).
 */
export async function aliveSessions(
  directory: string,
  projects: string = projectsDir(),
): Promise<ReadonlySet<string> | null> {
  const alive = new Set((await directorySessions(directory)).map((session) => session.id));
  const key = await projectKey(directory);
  const folder = join(projects, key);
  if (!(await isDirectory(folder))) return key.length > LONG_PROJECT_KEY ? null : alive;
  let names: string[];
  try {
    names = await readdir(folder);
  } catch {
    return null;
  }
  for (const name of names) {
    if (name.endsWith(".jsonl")) alive.add(name.slice(0, -".jsonl".length));
  }
  return alive;
}
