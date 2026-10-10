/**
 * The Slack client the calls that create or grow a message go through: one that never sends a
 * call twice.
 *
 * A start, an append, a stop and a post are not idempotent, and a connection can reset after
 * Slack applied the call, so a retry would duplicate a message or its text, or stop a stream
 * twice. The sink reads the thread back instead, and adopts what landed.
 *
 * The Python daemon had one client and a retry handler that looked at the request's URL
 * (`ConnectionRetryUnlessCreating`). `@slack/web-api` 8.2.0 decides a retry for the whole client:
 * `WebClient.makeRequest` runs every request under `pRetry(task, this.retryConfig)`, and p-retry
 * 4.6.2 retries any error that is not its `AbortError` (a request that failed on the connection
 * is a `WebAPIRequestError`, an HTTP status other than 200 a `WebAPIHTTPError`, both retried).
 * Nothing in that path reads the method, and the client takes no option per call. So the policy
 * is a second client, built with the two documented options that switch every retry off:
 *
 * - `retryConfig: { retries: 0 }` (`WebClientOptions.retryConfig`): no error is sent again;
 * - `rejectRateLimitedCalls: true` (`WebClientOptions.rejectRateLimitedCalls`): an HTTP 429 is
 *   thrown at once as `WebAPIRateLimitedError`, where the client would otherwise wait out the
 *   `Retry-After` and throw an untyped error that only a retry turns into an answer.
 *
 * What differs from slack-sdk: its rate-limit handler still retried these four calls (up to 3
 * times, after `Retry-After`), which is safe since a rate-limited call never ran. The library
 * cannot retry one kind of error and not the other, so here a rate-limited start, append, stop or
 * post fails as a refusal (`ratelimited`, known not to have been applied) and is written again
 * by the reply's next write, or by the end's one retry.
 */
import { WebClient, type WebClientOptions } from "@slack/web-api";

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

/** The two options that make a client send each call once, whatever else it is built with. */
export const SENT_ONCE = {
  retryConfig: { retries: 0 },
  rejectRateLimitedCalls: true,
} as const satisfies WebClientOptions;

/**
 * The client a reply's `CREATING_METHODS` go through. `options` may set anything but the two
 * options of `SENT_ONCE`.
 */
export function creatingClient(
  token: string | undefined,
  options: WebClientOptions = {},
): WebClient {
  return new WebClient(token, { timeout: REQUEST_TIMEOUT_MS, ...options, ...SENT_ONCE });
}
