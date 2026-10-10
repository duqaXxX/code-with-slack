/**
 * The retry policy of the client a reply's creating calls go through, measured on the real
 * `WebClient` of `@slack/web-api` 8.2.0 with its `fetch` option replaced: what the library itself
 * sends again, and what `creatingClient` stops it from sending again.
 *
 * `the connection retry skips the calls that create a message` is the test of that name in
 * `tests/test_sinks.py`, which asked slack-sdk's retry handler; here the question is put to the
 * client, by counting the requests it makes.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  type FetchFunction,
  LogLevel,
  WebAPIRateLimitedError,
  WebAPIRequestError,
  WebClient,
} from "@slack/web-api";
import { CREATING_METHODS, creatingClient } from "../../../../src/chat/slack/reply/clients.ts";
import { describe, unknownOutcome } from "../../../../src/chat/slack/reply/errors.ts";

type FetchResponse = Awaited<ReturnType<FetchFunction>>;

/** The method a request went to: the last part of its URL. */
function methodOf(url: string | URL): string {
  return String(url).replace(/\/+$/, "").split("/").at(-1) ?? "";
}

/** A connection that resets on every request, as `fetch` reports one: Python's `ClientOSError(104)`. */
function resetting(requests: string[]): FetchFunction {
  return async (url) => {
    requests.push(methodOf(url));
    const cause = Object.assign(new Error("read ECONNRESET"), { code: "ECONNRESET" });
    throw new TypeError("fetch failed", { cause });
  };
}

/** Slack answering HTTP 429 with a `Retry-After` to every request. */
function rateLimiting(requests: string[]): FetchFunction {
  return async (url) => {
    requests.push(methodOf(url));
    const response: FetchResponse = {
      ok: false,
      status: 429,
      statusText: "Too Many Requests",
      url: String(url),
      headers: {
        get: (name) => (name.toLowerCase() === "retry-after" ? "30" : null),
        entries: () => [["retry-after", "30"]],
      },
      arrayBuffer: async () => new ArrayBuffer(0),
      json: async () => ({}),
      text: async () => "",
    };
    return response;
  };
}

// The library's own retry, with no pause between the tries: one retry, as slack-sdk's handler made.
const ONE_RETRY = { retries: 1, minTimeout: 0, maxTimeout: 0 };
const QUIET = { logLevel: LogLevel.ERROR };

test("the connection retry skips the calls that create a message", async () => {
  // What the library does when left to itself: a call that failed on the connection is sent again.
  const sentAgain: string[] = [];
  const retrying = new WebClient(undefined, {
    ...QUIET,
    retryConfig: ONE_RETRY,
    fetch: resetting(sentAgain),
  });
  for (const method of ["chat.update", "reactions.add", ...CREATING_METHODS]) {
    await assert.rejects(retrying.apiCall(method, {}), WebAPIRequestError);
  }
  assert.deepEqual(
    sentAgain,
    ["chat.update", "reactions.add", ...CREATING_METHODS].flatMap((method) => [method, method]),
  );
  // The client the creating calls go through: each is sent once, whatever retry it was asked for.
  const sentOnce: string[] = [];
  const creating = creatingClient(undefined, {
    ...QUIET,
    retryConfig: ONE_RETRY,
    fetch: resetting(sentOnce),
  });
  for (const method of CREATING_METHODS) {
    const failure = await creating.apiCall(method, {}).catch((error: unknown) => error);
    assert.ok(failure instanceof WebAPIRequestError);
    // The sink reads the thread back: nothing says whether Slack applied the call.
    assert.equal(unknownOutcome(failure), true);
    assert.equal(describe(failure), "ECONNRESET");
  }
  assert.deepEqual(sentOnce, [...CREATING_METHODS]);
});

test("a rate limited call that creates a message is refused at once and known not applied", async () => {
  const requests: string[] = [];
  const creating = creatingClient(undefined, { ...QUIET, fetch: rateLimiting(requests) });
  for (const method of CREATING_METHODS) {
    const failure = await creating.apiCall(method, {}).catch((error: unknown) => error);
    assert.ok(failure instanceof WebAPIRateLimitedError);
    assert.equal(failure.retryAfter, 30);
    assert.equal(describe(failure), "ratelimited");
    assert.equal(unknownOutcome(failure), false);
  }
  // No wait for the 30 seconds and no second request.
  assert.deepEqual(requests, [...CREATING_METHODS]);
});
