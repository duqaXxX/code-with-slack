/**
 * `!bind` alone: the folders under the allowed root where a session can start.
 *
 * Claude Code has no folder picker: the terminal starts in the folder it is launched from. The list
 * holds the allowed root and the folders two levels below it that Claude Code trusts, since a
 * session starts nowhere else. It never descends into a git repository: its subfolders belong to
 * that one project. The Bind button rows are the chat provider's.
 */
import { lstat, opendir, readdir, stat } from "node:fs/promises";
import { join } from "node:path";

/** Where a debug line goes; ids and error names only. */
export interface Logger {
  debug(message: string): void;
}

export const logger: Logger = {
  debug: (message) => console.error(`DEBUG core.folders: ${message}`),
};

export const FOLDER_ROWS = 20;
export const FOLDER_DEPTH = 2;
// Each trust check reads the filesystem: a batch at a time, so a root with many untrusted folders
// lists in a few rounds, and the check stops soon after the rows are filled.
export const TRUST_BATCH = 8;

// What Python's `Path.exists` and `Path.is_dir` read as "not there" rather than as a failure.
const ABSENT = new Set(["ENOENT", "ENOTDIR", "EBADF", "ELOOP"]);

function code(error: unknown): string {
  return (error as { code?: unknown } | null)?.code as string;
}

/** Python's `sorted` on strings: by code point, where `Array.prototype.sort` goes by UTF-16 unit. */
function compareCodePoints(a: string, b: string): number {
  const left = Array.from(a);
  const right = Array.from(b);
  for (let i = 0; i < Math.min(left.length, right.length); i += 1) {
    const x = left[i] as string;
    const y = right[i] as string;
    if (x !== y) return (x.codePointAt(0) as number) - (y.codePointAt(0) as number);
  }
  return left.length - right.length;
}

async function isFolder(directory: string, name: string): Promise<boolean> {
  // A symlink could lead outside the root; hidden folders are tooling (.git, .venv, .claude).
  if (name.startsWith(".")) return false;
  try {
    // `lstat` does not follow a link: a real directory is the only thing that passes.
    return (await lstat(join(directory, name))).isDirectory();
  } catch {
    return false;
  }
}

async function descends(folder: string): Promise<boolean> {
  // `.git` is a directory in a repository and a file in a worktree. A folder that cannot be
  // searched fails here with more than "not there": it has nothing to list.
  try {
    await stat(join(folder, ".git"));
    return false;
  } catch (error) {
    return ABSENT.has(code(error));
  }
}

async function children(folder: string): Promise<string[]> {
  let names: string[];
  try {
    names = (await readdir(folder)).toSorted(compareCodePoints);
  } catch (error) {
    // A subfolder the daemon may not open (macOS privacy, permissions) lists nothing.
    logger.debug(`skipped an unreadable folder: ${code(error)}`);
    return [];
  }
  const found: string[] = [];
  for (const name of names) {
    if (await isFolder(folder, name)) found.push(join(folder, name));
  }
  return found;
}

/** The root, then its folders, then theirs: the higher levels come first. */
export async function foldersWithin(root: string): Promise<string[]> {
  // An unreadable root raises: an empty list would send the owner to trust folders instead.
  await (await opendir(root)).close();
  const levels: string[][] = [[root]];
  for (let depth = 0; depth < FOLDER_DEPTH; depth += 1) {
    const next: string[] = [];
    for (const folder of levels[levels.length - 1] as string[]) {
      if (await descends(folder)) next.push(...(await children(folder)));
    }
    levels.push(next);
  }
  return levels.flat();
}

/**
 * Trusted folders under `root`, the higher levels first, at most `FOLDER_ROWS + 1`: one past the
 * rows shown is enough to say that more exist, and stops the trust checks there.
 */
export async function bindableFolders(
  root: string,
  trusted: (folder: string) => Promise<boolean>,
): Promise<string[]> {
  const candidates = await foldersWithin(root);
  const found: string[] = [];
  for (let start = 0; start < candidates.length; start += TRUST_BATCH) {
    const batch = candidates.slice(start, start + TRUST_BATCH);
    const verdicts = await Promise.all(batch.map((folder) => trusted(folder)));
    found.push(...batch.filter((_folder, index) => verdicts[index]));
    if (found.length > FOLDER_ROWS) return found.slice(0, FOLDER_ROWS + 1);
  }
  return found;
}
