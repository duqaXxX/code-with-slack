/**
 * What `!open` may share: a path under the session's folder that is a regular file, read once and
 * handed to Slack as bytes. The path is untrusted (an option's value comes back from Slack), so it
 * is resolved with its links, held to the folder, and opened by a descriptor that refuses a link.
 */
import { type BigIntStats, constants, type Stats } from "node:fs";
import { open, realpath, stat } from "node:fs/promises";
import { basename, dirname, isAbsolute, join, posix, resolve, sep } from "node:path";
import type { WebClient } from "@slack/web-api";
import { compareCodePoints, relativeTo } from "./paths.ts";

// Slack's documented limit for a snippet: `snippet_too_large` on `files.getUploadURLExternal`
// (docs.slack.dev/reference/methods/files.getUploadURLExternal, read 2026-10-05).
export const SNIPPET_LIMIT = 1 << 20;

// Windows has neither flag; the descriptor check below still holds there.
const OPEN_FLAGS = constants.O_RDONLY | (constants.O_NONBLOCK ?? 0) | (constants.O_NOFOLLOW ?? 0);

/** The path is not a regular file inside the folder, once its links are resolved. */
export class NotAFile extends Error {
  constructor(options?: ErrorOptions) {
    super("not a regular file inside the folder", options);
    this.name = "NotAFile";
  }
}

/** The file is over SNIPPET_LIMIT. */
export class TooLarge extends Error {
  constructor() {
    super("over the snippet limit");
    this.name = "TooLarge";
  }
}

/**
 * The part of an open file `readOnce` uses, which `FileHandle` has: `fstat` and `read` go
 * through the descriptor that was opened, and a test can wrap it.
 */
export interface Opened {
  stat(): Promise<Stats>;
  read(
    buffer: Buffer,
    offset: number,
    length: number,
    position: null,
  ): Promise<{ bytesRead: number }>;
  close(): Promise<void>;
}

/** Python's non-strict `os.path.realpath`: the links of what exists are resolved, the rest kept. */
export async function realpathLoose(path: string): Promise<string> {
  const absolute = resolve(path);
  try {
    return await realpath(absolute);
  } catch {
    const parent = dirname(absolute);
    if (parent === absolute) return absolute;
    return join(await realpathLoose(parent), basename(absolute));
  }
}

/**
 * The regular file `relative` names under `real` (a resolved folder), with its stat. Throws
 * `NotAFile` for anything else: a path that leaves the folder by `..`, an absolute path or a link,
 * a directory, a missing file. `relative` is untrusted.
 *
 * `realpath` of `fs/promises` is libuv's `realpath(3)`, which follows a `..` after the link
 * before it, as Python's `os.path.realpath` does; the path is joined as text, never normalized
 * first, since `path.join` would drop `link/..` lexically.
 */
async function locate(
  real: string,
  relative: string,
): Promise<{ target: string; found: BigIntStats }> {
  if (!relative || relative.includes("\0")) throw new NotAFile();
  let target: string;
  let found: BigIntStats;
  try {
    // `real / relative`: an absolute `relative` replaces `real`.
    target = await realpath(isAbsolute(relative) ? relative : `${real}${sep}${relative}`);
    if (relativeTo(real, target) === null) throw new NotAFile();
    found = await stat(target, { bigint: true });
  } catch (error) {
    throw error instanceof NotAFile ? error : new NotAFile({ cause: error });
  }
  if (!found.isFile()) throw new NotAFile();
  return { target, found };
}

/** The path as the shared file is titled: `./docs/../docs/a.md` as `docs/a.md`. */
export function titleOf(relative: string): string {
  // Python's `posixpath.normpath`: no trailing slash, and exactly two leading slashes are kept.
  let normal = posix.normalize(relative);
  if (normal.length > 1 && normal.endsWith("/")) normal = normal.slice(0, -1);
  if (relative.startsWith("//") && !relative.startsWith("///") && !normal.startsWith("//")) {
    normal = `/${normal}`;
  }
  return normal;
}

/**
 * The content of the already resolved file `target`, opened once; everything after is read from
 * that descriptor, so what was checked is what is read. Throws `NotAFile` or `TooLarge`.
 *
 * Python's `os.open` is `fs.promises.open` with numeric flags from `fs.constants`
 * (`O_RDONLY | O_NONBLOCK | O_NOFOLLOW`: a file that became a link in between is refused, a FIFO
 * is never waited on); `os.fstat` is the handle's `stat`; `os.read` is the handle's `read` at the
 * current position, up to SNIPPET_LIMIT + 1 bytes; `os.close` is the handle's `close`.
 */
export async function readOnce(
  target: string,
  opener: (path: string) => Promise<Opened> = (path) => open(path, OPEN_FLAGS),
): Promise<Buffer> {
  let handle: Opened;
  try {
    handle = await opener(target);
  } catch (error) {
    throw new NotAFile({ cause: error });
  }
  let total = 0;
  const content = Buffer.allocUnsafe(SNIPPET_LIMIT + 1);
  try {
    const found = await handle.stat();
    if (!found.isFile()) throw new NotAFile();
    if (found.size > SNIPPET_LIMIT) throw new TooLarge();
    while (total <= SNIPPET_LIMIT) {
      const { bytesRead } = await handle.read(content, total, SNIPPET_LIMIT + 1 - total, null);
      if (bytesRead === 0) break;
      total += bytesRead;
    }
  } catch (error) {
    if (error instanceof NotAFile || error instanceof TooLarge) throw error;
    throw new NotAFile({ cause: error });
  } finally {
    await handle.close().catch(() => {});
  }
  if (total > SNIPPET_LIMIT) throw new TooLarge();
  return Buffer.from(content.subarray(0, total));
}

/**
 * The content of the file `relative` names under `folder`. Throws `NotAFile` or `TooLarge`.
 * Slack gets these bytes: its SDK would open a path again, following links and with no size limit.
 */
export async function readOpenable(folder: string, relative: string): Promise<Buffer> {
  const { target } = await locate(await realpathLoose(folder), relative);
  return readOnce(target);
}

/**
 * The paths of `relatives` that are regular files inside `folder`, in order, at most `limit`. The
 * iterable is read only as far as the checks need.
 */
export async function regularFiles(
  folder: string,
  relatives: Iterable<string>,
  limit?: number,
): Promise<string[]> {
  const real = await realpathLoose(folder);
  const found: string[] = [];
  if (limit !== undefined && limit <= 0) return found;
  for (const relative of relatives) {
    try {
      await locate(real, relative);
    } catch (error) {
      if (error instanceof NotAFile) continue;
      throw error;
    }
    found.push(relative);
    // Checked here, not before the next path is taken: the iterable is not read past the limit.
    if (limit !== undefined && found.length >= limit) break;
  }
  return found;
}

/**
 * The paths of `relatives` that are regular files inside `folder`, each once, the most recently
 * modified first (ties by path).
 */
export async function newestFirst(folder: string, relatives: Iterable<string>): Promise<string[]> {
  const real = await realpathLoose(folder);
  const dated: Array<[bigint, string]> = [];
  for (const relative of new Set(relatives)) {
    try {
      const { found } = await locate(real, relative);
      dated.push([found.mtimeNs, relative]);
    } catch (error) {
      if (error instanceof NotAFile) continue;
      throw error;
    }
  }
  dated.sort(([leftTime, left], [rightTime, right]) =>
    leftTime === rightTime ? compareCodePoints(left, right) : leftTime > rightTime ? -1 : 1,
  );
  return dated.map(([, relative]) => relative);
}

/**
 * Shares `content`, the bytes `readOpenable` read from `relative`, into the thread: the basename as
 * the file's name and the path from the folder as its title, nothing posted on success. Rejects
 * with Slack's error.
 *
 * Python passed `content=bytes`. `@slack/web-api` 8.2.0 types `content` as a string
 * (`FileUploadStringContents`, `types/request/files.d.ts`) and encodes it as UTF-8, which would
 * damage a binary file; its `file` field takes a `Buffer` as the bytes (a `string` there is a
 * path read from disk, `dist/file-upload.js` `getFileData`). A Buffer is what is passed.
 */
export async function uploadFile(
  client: WebClient,
  channel: string,
  threadTs: string,
  content: Buffer,
  relative: string,
): Promise<void> {
  const title = titleOf(relative);
  await client.files.uploadV2({
    channel_id: channel,
    thread_ts: threadTs,
    file: content,
    filename: posix.basename(title),
    title,
  });
}
