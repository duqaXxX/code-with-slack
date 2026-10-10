/**
 * Files the owner attaches to a message: checked, downloaded, and handed to Claude.
 *
 * A message with files arrives as `subtype: file_share` with a `files` array (measured 2026-09-25,
 * although Slack's reference page for the `file_share` message subtype, read 2026-09-25, calls it
 * legacy and no longer served). An image Claude can read goes into the prompt as an image part, as
 * an image pasted into the terminal does (Agent SDK "Streaming Input", read 2026-09-25); any other
 * file is saved to a private folder and its path joins the prompt, as a file dropped into the
 * terminal does. A file past a limit, or one that fails to download, stops the whole message: a
 * prompt without its attachment would mislead Claude.
 */
import { lstatSync } from "node:fs";
import { lstat, mkdir, readdir, unlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { ImagePart, PromptContent, TextPart } from "../../agent/seam.ts";
import * as texts from "../../core/texts.ts";
import { getLogger } from "../../log.ts";

/** A Slack file object, read field by field since it is wire data. */
export type SlackFile = object;

/** What `fetch` is to `download`: tests pass a fake that answers as Slack did. */
export type FetchFn = (url: string, init: RequestInit) => Promise<Response>;

/** An image that arrived: its type and its bytes. */
export interface DownloadedImage {
  readonly mediaType: string;
  readonly data: Uint8Array;
}

// Claude's vision limits (platform.claude.com, "Vision", read 2026-09-25): JPEG, PNG, GIF and
// WebP, 10 MB base64-encoded per image through the API, 8000x8000 px.
export const IMAGE_TYPES: ReadonlySet<string> = new Set([
  "image/jpeg",
  "image/png",
  "image/gif",
  "image/webp",
]);
export const IMAGE_LIMIT = 7_500_000; // bytes, whose base64 form is 10,000,000 characters
export const IMAGE_SIDE = 8000;
// Per message (the maintainer, 2026-09-25): images stay in the conversation and are sent again
// at every turn, and a request is capped at 32 MB (Vision, read 2026-09-25).
export const IMAGES_PER_MESSAGE = 5;
export const IMAGES_LIMIT = 15 * 1024 * 1024;
export const FILE_LIMIT = 100 * 1024 * 1024;
// The files that are not images Claude receives (the maintainer, 2026-09-25: the most common
// ones, those its Read tool opens): any text/* type, which covers source code, plus these.
export const FILE_TYPES: ReadonlySet<string> = new Set([
  "application/pdf",
  "application/json",
  "application/xml",
  "application/yaml",
  "application/x-yaml",
  "application/javascript",
  "application/x-sh",
  "application/sql",
  "application/toml",
  "application/x-ipynb+json",
]);
// Saved files outlive a restart, since the resumed conversation names them (the maintainer,
// 2026-09-25).
export const KEEP_SECONDS = 3 * 24 * 3600;
// A file's URL receives the bot token: only Slack's own file host may have it.
export const FILE_HOST = "files.slack.com";
const ORIGIN_PROTOCOL = "https:";
export const DOWNLOAD_TIMEOUT_MS = 120_000;

export const logger = getLogger("awaydesk.chat.slack.attachments");

/** A file could not be downloaded or saved; the message says why, for the owner. */
export class DownloadFailed extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "DownloadFailed";
  }
}

function field(file: SlackFile, key: string): unknown {
  return (file as Record<string, unknown>)[key];
}

/** Python's `int(value or 0)` on a field that is a number or a numeric string. */
function integer(value: unknown): number {
  const number = Number(value || 0);
  return Number.isFinite(number) ? Math.trunc(number) : 0;
}

/** Python's `f"{size / (1024 * 1024):.1f}MB"`: a tie rounds to even, which `toFixed` does not. */
function megabytes(size: number): string {
  const value = size / (1024 * 1024);
  // Only a quarter of a megabyte is both exact in binary and a tie at one decimal.
  const quarters = value * 4;
  if (Number.isInteger(quarters) && quarters % 2 !== 0) {
    const down = Math.floor(value * 10);
    const tenths = down % 2 === 0 ? down : down + 1;
    return `${(tenths / 10).toFixed(1)}MB`;
  }
  return `${value.toFixed(1)}MB`;
}

/** The parts of an `https://files.slack.com/...` URL; null for any other host or scheme. */
function slackFileUrl(raw: string): URL | null {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  return url.protocol === ORIGIN_PROTOCOL && url.hostname === FILE_HOST ? url : null;
}

/** Why `file` cannot reach Claude, as the end of `UPLOAD_FAILED`; null when it can. */
export function refusal(file: SlackFile): string | null {
  const access = field(file, "file_access");
  if ((access === undefined ? "visible" : access) !== "visible") return texts.UPLOAD_NOT_SHARED;
  if (slackFileUrl(String(field(file, "url_private_download") || "")) === null) {
    return texts.UPLOAD_NOT_SHARED;
  }
  const mimetype = String(field(file, "mimetype") || "");
  const size = integer(field(file, "size"));
  if (mimetype.startsWith("image/")) {
    if (!IMAGE_TYPES.has(mimetype)) return texts.fill(texts.UPLOAD_IMAGE_TYPE, { mimetype });
    if (size > IMAGE_LIMIT) {
      return texts.fill(texts.UPLOAD_IMAGE_SIZE, {
        size: megabytes(size),
        limit: megabytes(IMAGE_LIMIT),
      });
    }
    const width = field(file, "original_w");
    const height = field(file, "original_h");
    if (integer(width) > IMAGE_SIDE || integer(height) > IMAGE_SIDE) {
      return texts.fill(texts.UPLOAD_IMAGE_SIDE, {
        width: String(width ?? "None"),
        height: String(height ?? "None"),
      });
    }
  } else if (!(mimetype.startsWith("text/") || FILE_TYPES.has(mimetype))) {
    return texts.fill(texts.UPLOAD_FILE_TYPE, { mimetype: mimetype || "unknown" });
  } else if (size > FILE_LIMIT) {
    return texts.fill(texts.UPLOAD_FILE_SIZE, {
      size: megabytes(size),
      limit: megabytes(FILE_LIMIT),
    });
  }
  return null;
}

/** Why the images of one message, together, cannot reach Claude; null when they can. */
export function imagesRefusal(files: readonly SlackFile[]): string | null {
  const images = files.filter(isImage);
  if (images.length > IMAGES_PER_MESSAGE) {
    return texts.fill(texts.UPLOAD_TOO_MANY, {
      count: images.length,
      limit: IMAGES_PER_MESSAGE,
    });
  }
  const total = images.reduce((sum, file) => sum + integer(field(file, "size")), 0);
  if (total > IMAGES_LIMIT) {
    return texts.fill(texts.UPLOAD_TOO_HEAVY, {
      size: megabytes(total),
      limit: megabytes(IMAGES_LIMIT),
    });
  }
  return null;
}

/** Whether Claude reads this file as an image. */
export function isImage(file: SlackFile): boolean {
  const mimetype = field(file, "mimetype");
  return typeof mimetype === "string" && IMAGE_TYPES.has(mimetype);
}

/** The most bytes `download` takes of this file. */
export function limitFor(file: SlackFile): number {
  return isImage(file) ? IMAGE_LIMIT : FILE_LIMIT;
}

/** `Name: message (cause)`, at most 200 characters, so the owner sees why and not a class alone. */
function failureDetail(error: unknown): string {
  if (!(error instanceof Error)) return String(error).slice(0, 200);
  // A timeout carries a sentence of the runtime's own; the name says it.
  if (error.name === "TimeoutError") return "TimeoutError";
  let detail = error.message ? `${error.name}: ${error.message}` : error.name;
  if (error.cause instanceof Error && error.cause.message) detail += ` (${error.cause.message})`;
  return detail.slice(0, 200);
}

export interface DownloadOptions {
  /** The `fetch` to call; the global one by default. */
  readonly fetch?: FetchFn;
  /** The caller's cancel: it propagates as the abort, not as a `DownloadFailed`. */
  readonly signal?: AbortSignal;
}

/**
 * The file at `url`, fetched with the bot token (Slack's file object reference, read 2026-09-25);
 * throws `DownloadFailed` past `limit` bytes or on any failure.
 */
export async function download(
  url: string,
  token: string,
  mimetype: string,
  limit: number,
  options: DownloadOptions = {},
): Promise<Uint8Array> {
  const target = slackFileUrl(url);
  if (target === null) {
    // The token goes to Slack's file host alone, whoever calls this.
    throw new DownloadFailed(`not a ${FILE_HOST} URL`);
  }
  const call = options.fetch ?? fetch;
  const timeout = AbortSignal.timeout(DOWNLOAD_TIMEOUT_MS);
  const signal = options.signal ? AbortSignal.any([options.signal, timeout]) : timeout;
  try {
    const response = await call(target.href, {
      headers: { Authorization: `Bearer ${token}` },
      // A redirect could carry the token to another host: none is followed.
      redirect: "manual",
      signal,
    });
    if (response.status === 302) {
      // Measured 2026-09-25: Slack's answer when the token lacks the scope.
      throw new DownloadFailed(
        "HTTP 302: Slack redirects instead of sending the file when the app lacks the " +
          "`files:read` scope",
      );
    }
    if (response.status !== 200) throw new DownloadFailed(`HTTP ${response.status}`);
    const contentType = (response.headers.get("content-type") ?? "").split(";")[0]?.trim();
    if (contentType?.toLowerCase() === "text/html" && mimetype !== "text/html") {
      throw new DownloadFailed(
        "Slack sent a web page instead of the file: the app may lack the `files:read` scope",
      );
    }
    return await readBody(response, limit);
  } catch (error) {
    if (error instanceof DownloadFailed || options.signal?.aborted) throw error;
    throw new DownloadFailed(failureDetail(error), { cause: error });
  }
}

/** The body of `response`, cut off with a `DownloadFailed` as soon as it passes `limit`. */
async function readBody(response: Response, limit: number): Promise<Uint8Array> {
  if (response.body === null) return new Uint8Array(0);
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    length += value.length;
    if (length > limit) {
      await reader.cancel().catch(() => undefined);
      throw new DownloadFailed(`larger than ${megabytes(limit)}`);
    }
  }
  return Buffer.concat(chunks);
}

/** `.name` of a Python `PurePath`: the last part, with `.` parts and empty parts dropped. */
function lastPart(name: string): string {
  // Python's Path splits on `\` only on Windows, and so does this.
  const parts = name.split(process.platform === "win32" ? /[\\/]/ : "/");
  return parts.filter((part) => part !== "" && part !== ".").at(-1) ?? "";
}

/**
 * The file's name in the uploads folder: its Slack id first, so names never collide, and only the
 * last part of the name, so it cannot point elsewhere.
 */
export function savedName(file: SlackFile): string {
  const id = field(file, "id");
  if (id === undefined || id === null) throw new TypeError("a Slack file has an id");
  const name = lastPart(String(field(file, "name") || ""));
  return name !== "" && name !== "." && name !== ".." ? `${id}-${name}` : String(id);
}

/** The folder of the saved files: `$TMPDIR/awaydesk`. */
export function uploadsDir(): string {
  return join(tmpdir(), "awaydesk");
}

/** The folder is a real directory of this user that no one else can open. */
function isPrivate(folder: string): boolean {
  let info: ReturnType<typeof lstatSync>;
  try {
    info = lstatSync(folder);
  } catch {
    return false;
  }
  if (!info.isDirectory()) return false;
  // Windows has no owner id and no mode bits to read.
  if (process.platform === "win32") return true;
  return info.uid === process.getuid?.() && (info.mode & 0o077) === 0;
}

/** Remove the saved files older than `KEEP_SECONDS`, from a folder only the owner can read. */
export async function prepareUploads(folder: string, now: () => number = Date.now): Promise<void> {
  await mkdir(folder, { mode: 0o700, recursive: true });
  if (!isPrivate(folder)) {
    // Never touched: `save` refuses to write there, and old files are not removed.
    logger.warning(`the uploads folder ${folder} is not private: it is left untouched`);
    return;
  }
  const cutoff = now() - KEEP_SECONDS * 1000;
  for (const name of await readdir(folder)) {
    const path = join(folder, name);
    try {
      if ((await lstat(path)).mtimeMs < cutoff) await unlink(path);
    } catch {
      // A file that vanished or cannot be removed stays for the next start.
    }
  }
}

/** Write `data` as the file's saved copy and return its path; the folder must be private. */
export async function save(folder: string, file: SlackFile, data: Uint8Array): Promise<string> {
  // macOS may clean the temporary folder while the daemon runs.
  await mkdir(folder, { mode: 0o700, recursive: true });
  if (!isPrivate(folder)) {
    throw new DownloadFailed(`could not be saved (the uploads folder ${folder} is not private)`);
  }
  const path = join(folder, savedName(file));
  try {
    await writeFile(path, data);
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    const name = code ?? (error instanceof Error ? error.name : "Error");
    throw new DownloadFailed(`could not be saved (${name})`, { cause: error });
  }
  return path;
}

/** The owner's text with the saved files' paths; with images, one message of content parts. */
export function promptFor(
  text: string,
  images: readonly DownloadedImage[],
  paths: readonly string[],
): PromptContent {
  if (paths.length > 0) {
    const listed = paths.map((path) => `- ${path}`).join("\n");
    text = text ? `${text}\n\nAttached files:\n${listed}` : `Attached files:\n${listed}`;
  }
  if (images.length === 0) return text;
  const parts: (TextPart | ImagePart)[] = text ? [{ type: "text", text }] : [];
  for (const { mediaType, data } of images) {
    parts.push({ type: "image", mediaType, data: Buffer.from(data).toString("base64") });
  }
  return parts;
}
