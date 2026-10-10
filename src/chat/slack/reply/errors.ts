/** What a failed Slack call says, in words that are safe to log and to decide on. */
import {
  WebAPIHTTPError,
  WebAPIPlatformError,
  WebAPIRateLimitedError,
  WebAPIRequestError,
  type WebClient,
} from "@slack/web-api";

/** Where a line goes; ids, sizes and error codes only, never message content. */
export interface Logger {
  debug(message: string): void;
  info(message: string): void;
  warning(message: string): void;
  error(message: string): void;
}

export const logger: Logger = {
  debug: (message) => console.error(`DEBUG chat.slack.reply.sinks: ${message}`),
  info: (message) => console.error(`INFO chat.slack.reply.sinks: ${message}`),
  warning: (message) => console.error(`WARNING chat.slack.reply.sinks: ${message}`),
  error: (message) => console.error(`ERROR chat.slack.reply.sinks: ${message}`),
};

// chat.update errors that refuse the content itself (reference, read 2026-09-25): a plain retry
// can pass where the blocks did not. A transient error such as `ratelimited` is not one.
export const REFUSED_CONTENT: ReadonlySet<string> = new Set([
  "invalid_blocks",
  "invalid_blocks_format",
  "msg_too_long",
  "invalid_arguments",
]);
// Slack's answers about a stream's state (measured 2026-09-28): it is over, or still open.
export const NOT_STREAMING = "message_not_in_streaming_state";
// The one refusal of an append's content measured on `chat.appendStream` (2026-10-01, slack-sdk
// 3.44.1), and the one a `chat.update` of the same message was seen to pass.
export const TOO_LONG = "msg_too_long";
export const STILL_STREAMING = "streaming_state_conflict";

/** The system's code for a request that never got an answer (`ECONNRESET`), else the error's name. */
function requestFailure(original: Error): string {
  let cause: unknown = original;
  for (let depth = 0; depth < 4 && cause instanceof Error; depth += 1) {
    const code = (cause as { code?: unknown }).code;
    if (typeof code === "string" && code) return code;
    cause = cause.cause;
  }
  return original.name;
}

/** Slack's error code, or the error's type: never the request or its content. */
export function describe(error: unknown): string {
  if (error instanceof WebAPIPlatformError) return String(error.data.error);
  // The code Slack gives a call past its rate limit (HTTP 429).
  if (error instanceof WebAPIRateLimitedError) return "ratelimited";
  if (error instanceof WebAPIHTTPError) return `http_${error.statusCode}`;
  if (error instanceof WebAPIRequestError) return requestFailure(error.original);
  return error instanceof Error ? error.constructor.name : typeof error;
}

// What Slack says of a message whose blocks, once it has translated the markdown ones, pass 50
// (measured 2026-10-08 on `chat.update` and `chat.postMessage`, slack-sdk 3.45.0).
export const TOO_MANY_BLOCKS = "no more than 50 items allowed";
export const JSON_POINTER = /\[json-pointer:([^\]]*)\]/g;

/** The sentences Slack adds to a refused write, which say what in the payload it refused. */
export function refusalNotes(error: unknown): string[] {
  if (!(error instanceof WebAPIPlatformError)) return [];
  const notes = error.data.response_metadata?.messages ?? [];
  return notes.map((note) => String(note));
}

export function tooManyBlocks(error: unknown): boolean {
  return refusalNotes(error).some((note) => note.includes(TOO_MANY_BLOCKS));
}

/**
 * `describe`, with where in the payload Slack pointed and whether it counted too many blocks:
 * paths and a fixed phrase, never Slack's own sentence, which can quote a value.
 */
export function describeRefusal(error: unknown): string {
  let words = describe(error);
  const where = new Set<string>();
  for (const note of refusalNotes(error)) {
    for (const match of note.matchAll(JSON_POINTER)) where.add(match[1] ?? "");
  }
  if (where.size > 0) words += ` at ${[...where].sort().join(", ")}`;
  if (tooManyBlocks(error)) words += ", over 50 blocks once translated";
  return words;
}

/**
 * Whether a failed call may have been applied: Slack answering with an error says it was not
 * (an error code, a rate limit, an HTTP status), anything else (a reset, a timeout) says nothing.
 */
export function unknownOutcome(error: unknown): boolean {
  return !(
    error instanceof WebAPIPlatformError ||
    error instanceof WebAPIRateLimitedError ||
    error instanceof WebAPIHTTPError
  );
}

/**
 * Delete a request (an approval, a question, a hold) once it is decided or stale:
 * `message_not_found` counts as done, as everywhere else this project deletes one. Shared by the
 * session, the handlers and the crash repair, so the one behaviour lives in one place.
 */
export async function deleteRequest(
  slack: WebClient,
  request: { readonly channel: string; readonly ts: string },
  options: { readonly logger?: Logger; readonly now?: () => number } = {},
): Promise<void> {
  const log = options.logger ?? logger;
  const { channel, ts } = request;
  try {
    await slack.chat.delete({ channel, ts });
  } catch (error) {
    if (describe(error) !== "message_not_found") {
      log.warning(`could not remove a request in ${channel}: ${describe(error)}`);
      return;
    }
  }
  // Issue #71: a message ts is its post time in epoch seconds, so the age of a request when it
  // goes is read from the log with no timer kept for it. Ids and a duration only.
  const now = (options.now ?? (() => Date.now() / 1000))();
  const posted = Number(ts);
  const age =
    ts.trim() === "" || Number.isNaN(posted)
      ? "age unknown"
      : `${(now - posted).toFixed(1)}s after it was posted`;
  log.info(`removed a request in ${channel}: message ${ts}, ${age}`);
}
