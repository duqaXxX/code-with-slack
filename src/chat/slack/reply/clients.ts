/**
 * The daemon's two Slack clients on one token, with the retry policy the Python daemon had
 * (`make_clients` in `__main__.py`, slack-sdk's handlers, and `ConnectionRetryUnlessCreating` in
 * `render/sinks.py`):
 *
 * - every call that Slack rate limited (HTTP 429) is sent again after the `Retry-After` Slack
 *   gave, up to 3 times. That is safe for every method: a rate limited call never ran.
 * - a call that failed on the connection (a reset, a refused or unreachable host: a
 *   `WebAPIRequestError` that is not a timeout) is sent again once, after a backoff pause.
 *   `sharedClient` does so for every method, which is safe for a post that is an approval, a
 *   question or a notice. `repliesClient`, the one a reply is written with, does so for every
 *   method but the four of `CREATING_METHODS`: a start, an append, a stop and a post are not
 *   idempotent, a connection can reset after Slack applied the call, and a second send would
 *   duplicate a message or its text, or stop a stream twice. The sink reads the thread back
 *   instead, and adopts what landed.
 * - nothing else is sent again: Slack answering with an error (`WebAPIPlatformError`), an HTTP
 *   status other than 200 and 429, a timeout (slack-sdk retried neither, and a timed out call may
 *   have run).
 *
 * `@slack/web-api` 8.2.0 retries every failure or none: `WebClient.makeRequest` ends in
 * `pRetry(task, this.retryConfig)`, with no option per method and none per error. So both clients
 * are built with the two documented options that switch its retries off (`SENT_ONCE`), which
 * makes the library report each failure once, as a typed error, and `PolicyClient.apiCall` applies
 * the policy around `WebClient.apiCall`. A retry counts toward one budget per call, as slack-sdk's
 * `RetryState.current_attempt` did: a connection retry is allowed while no retry was made, a rate
 * limit retry while fewer than 3 were.
 *
 * Differences from slack-sdk: a 429 with no valid `Retry-After` is thrown as the library's plain
 * error at once (slack-sdk waited 1 to 2 seconds and retried; Slack always sends the header); and
 * the pause before a retry is taken from the injected `Clock`, so a test crosses it without
 * waiting. `files.uploadV2` is passed through untouched: it is not a Slack method but the
 * library's three step upload, whose `files.getUploadURLExternal` and
 * `files.completeUploadExternal` come back through `apiCall` and are retried there, one by one.
 */
import {
  type WebAPICallResult,
  WebAPIRateLimitedError,
  WebAPIRequestError,
  WebClient,
  type WebClientOptions,
} from "@slack/web-api";
import { type Clock, systemClock } from "../../../clock.ts";

/** The four calls that create or grow a message: never sent again after a failed connection. */
export const CREATING_METHODS = [
  "chat.startStream",
  "chat.appendStream",
  "chat.stopStream",
  "chat.postMessage",
] as const;

// What slack-sdk's client waited for an answer by default (`AsyncWebClient(timeout=30)`). The
// library's default is no timeout at all (`WebClientOptions.timeout`, 0), and a pass of the sink
// waits for its call to answer.
export const REQUEST_TIMEOUT_MS = 30_000;

/** The two options that make the library report each failure once, whatever else it is built with. */
export const SENT_ONCE = {
  retryConfig: { retries: 0 },
  rejectRateLimitedCalls: true,
} as const satisfies WebClientOptions;

// `AsyncRateLimitErrorRetryHandler(max_retry_count=3)` and slack-sdk's connection handler default.
export const RATE_LIMIT_RETRIES = 3;
export const CONNECTION_RETRIES = 1;
// `BackoffRetryIntervalCalculator(backoff_factor=0.5)`: 0.5 * 2^attempt seconds, plus a jitter.
const BACKOFF_FACTOR = 0.5;

/** What a client is built with: the library's options but its retries, which the policy owns. */
export interface ClientOptions extends Omit<WebClientOptions, keyof typeof SENT_ONCE> {
  /** Waits the pause before a retry. */
  readonly clock?: Clock;
  /** The jitter added to a pause, in [0, 1) seconds, as Python's `random.random()`. */
  readonly random?: () => number;
}

/** A request that failed before an answer, and not by the client's own timeout. */
function failedOnConnection(error: unknown): boolean {
  return (
    error instanceof WebAPIRequestError &&
    error.original.name !== "TimeoutError" &&
    error.original.name !== "AbortError"
  );
}

/** A `WebClient` that sends each call by the policy at the top of this file. */
class PolicyClient extends WebClient {
  readonly #notRetried: ReadonlySet<string>;
  readonly #clock: Clock;
  readonly #random: () => number;

  constructor(token: string | undefined, notRetried: readonly string[], options: ClientOptions) {
    const { clock, random, ...rest } = options;
    super(token, { timeout: REQUEST_TIMEOUT_MS, ...rest, ...SENT_ONCE });
    this.#notRetried = new Set(notRetried);
    this.#clock = clock ?? systemClock;
    this.#random = random ?? Math.random;
  }

  /** Seconds to wait before sending the call again, or null when it is not sent again. */
  #pause(method: string, error: unknown, retries: number): number | null {
    if (error instanceof WebAPIRateLimitedError) {
      return retries < RATE_LIMIT_RETRIES ? error.retryAfter + this.#random() : null;
    }
    if (
      failedOnConnection(error) &&
      retries < CONNECTION_RETRIES &&
      !this.#notRetried.has(method)
    ) {
      return BACKOFF_FACTOR * 2 ** retries + this.#random();
    }
    return null;
  }

  override async apiCall(
    method: string,
    options: Record<string, unknown> = {},
  ): Promise<WebAPICallResult> {
    if (method === "files.uploadV2") return super.apiCall(method, options);
    for (let retries = 0; ; retries += 1) {
      try {
        return await super.apiCall(method, options);
      } catch (error) {
        const pause = this.#pause(method, error, retries);
        if (pause === null) throw error;
        await this.#clock.sleep(pause);
      }
    }
  }
}

/** Python's shared client: connection and rate limit retries for every call. */
export function sharedClient(token: string | undefined, options: ClientOptions = {}): WebClient {
  return new PolicyClient(token, [], options);
}

/** Python's replies client: the shared client's policy, except that `CREATING_METHODS` are not resent on a connection failure. */
export function repliesClient(token: string | undefined, options: ClientOptions = {}): WebClient {
  return new PolicyClient(token, CREATING_METHODS, options);
}
