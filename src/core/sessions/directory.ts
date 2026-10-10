/** Whether a folder can hold a session, the folder a typed path names, and `!status`'s terminal line. */
import { realpathSync, statSync } from "node:fs";
import { opendir, stat } from "node:fs/promises";
import { homedir } from "node:os";
import { basename, dirname, join, resolve, sep } from "node:path";
import * as texts from "../texts.ts";
import { DirectoryMissing, DirectoryUnreadable, DirectoryUntrusted } from "./errors.ts";

function errorCode(error: unknown): string | undefined {
  return error instanceof Error && "code" in error && typeof error.code === "string"
    ? error.code
    : undefined;
}

/**
 * Rejects with the `DirectoryUnavailable` that keeps a session from starting in `directory`,
 * the first found in the order the owner can act on: missing, unreadable, untrusted.
 */
export async function checkDirectory(
  directory: string,
  trusted: (folder: string) => Promise<boolean>,
): Promise<void> {
  // One stat and one directory open on a local folder, as a session start always made them.
  const found = await stat(directory).catch(() => null);
  if (found === null || !found.isDirectory()) throw new DirectoryMissing(directory);
  try {
    await (await opendir(directory)).close();
  } catch (error) {
    // Python's `PermissionError` is these two.
    if (errorCode(error) === "EACCES" || errorCode(error) === "EPERM") {
      throw new DirectoryUnreadable(directory);
    }
    throw error;
  }
  if (!(await trusted(directory))) throw new DirectoryUntrusted(directory);
}

/** `~` and `~/x` to the home directory, as `Path.expanduser` does (`~user` is not expanded). */
function expandUser(path: string): string {
  if (path === "~") return homedir();
  return path.startsWith("~/") ? join(homedir(), path.slice(2)) : path;
}

/** Absolute, symlinks resolved as far as the path exists, as `Path.resolve()` does. */
export function resolvePath(path: string): string {
  const rest: string[] = [];
  let head = resolve(path);
  for (;;) {
    try {
      return join(realpathSync(head), ...rest);
    } catch (error) {
      const parent = dirname(head);
      // Only a part that is not there is kept as written: anything else is not ours to hide.
      if (errorCode(error) !== "ENOENT" && errorCode(error) !== "ENOTDIR") throw error;
      if (parent === head) return resolve(path);
      rest.unshift(basename(head));
      head = parent;
    }
  }
}

/** The real directory `raw` names, if it exists under `root`; guards against a typo only. */
export function resolveDirectory(raw: string, root: string): string | null {
  // A relative path is read under the root: the daemon's own working directory means nothing
  // to the owner (launchd starts it in `/`).
  let path: string;
  try {
    path = realpathSync(resolve(root, expandUser(raw)));
    if (!statSync(path).isDirectory()) return null;
  } catch {
    return null; // it names nothing: Python's `resolve` kept the path, and `is_dir` said no
  }
  const base = root.endsWith(sep) ? root : root + sep;
  return path === root || path.startsWith(base) ? path : null;
}

/** `shlex.quote`: the word as one shell argument, quoted only when it needs to be. */
export function shellQuote(word: string): string {
  if (word === "") return "''";
  if (/^[A-Za-z0-9_@%+=:,./-]+$/.test(word)) return word;
  return `'${word.replaceAll("'", "'\"'\"'")}'`;
}

// Slack's markdown block: the characters that start inline formatting there, each escapable
// with a backslash. The same set as the Slack provider's `markdownEscape`, which the core may
// not import: a reply's text is markdown on both sides of the chat seam.
const MARKDOWN_INLINE = /([\\`*_{}[\]()&~])/g;

/**
 * The `!status` line with the command that forks a session in the terminal, run from the
 * thread's folder, where `!resume` in its channel then finds the fork.
 */
export function terminalLine(directory: string, sessionId: string): string {
  const command = texts.fill(texts.TERMINAL_COMMAND, {
    directory: shellQuote(directory),
    session: sessionId,
  });
  // A code span cannot hold a backtick: such a command is plain text, escaped.
  const shown = command.includes("`") ? command.replace(MARKDOWN_INLINE, "\\$1") : `\`${command}\``;
  return texts.fill(texts.STATUS_TERMINAL, { command: shown });
}
