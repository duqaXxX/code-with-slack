/**
 * The retry policy of the two Slack clients, measured on the real `WebClient` of
 * `@slack/web-api` 8.2.0 with its `fetch` option replaced: what the library itself sends again,
 * and what `sharedClient` and `repliesClient` send again by the policy of `clients.ts`
 * (slack-sdk's handlers, as `make_clients` in `__main__.py` chose them), counted in requests.
 *
 * `the connection retry skips the calls that create a message` is the test of that name in
 * `tests/test_sinks.py`, which asked slack-sdk's retry handler; here the question is put to the
 * client. The rest have no Python test: Python's policy was slack-sdk's own code.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  type FetchFunction,
  LogLevel,
  WebAPIPlatformError,
  WebAPIRateLimitedError,
  WebAPIRequestError,
  WebClient,
} from "@slack/web-api";
import {
  type ClientOptions,
  CREATING_METHODS,
  repliesClient,
  sharedClient,
} from "../../../../src/chat/slack/reply/clients.ts";
import { describe, unknownOutcome } from "../../../../src/chat/slack/reply/errors.ts";
import type { Clock } from "../../../../src/clock.ts";
import { setLevel, setWriter } from "../../../../src/log.ts";
import { UPLOAD_DONE, UPLOAD_URL } from "../../../support/fake-slack.ts";

type FetchResponse = Awaited<ReturnType<FetchFunction>>;

/** What Slack, or the network, does to one request. */
type Step = "reset" | "timeout" | "429" | "500" | "refused" | "ok";

/** The method a request went to: the last part of its URL. */
function methodOf(url: string | URL): string {
  return String(url).replace(/\/+$/, "").split("/").at(-1) ?? "";
}

function respond(url: string | URL, status: number, body: string, retryAfter?: string) {
  const headers: [string, string][] = retryAfter === undefined ? [] : [["retry-after", retryAfter]];
  const response: FetchResponse = {
    ok: status === 200,
    status,
    statusText: status === 200 ? "OK" : "Error",
    url: String(url),
    headers: {
      get: (name) => headers.find(([key]) => key === name.toLowerCase())?.[1] ?? null,
      entries: () => headers,
    },
    arrayBuffer: async () => new ArrayBuffer(0),
    json: async () => JSON.parse(body || "{}"),
    text: async () => body,
  };
  return response;
}

/**
 * A network that does `steps` to the requests in turn, the last one for every request after
 * them, and keeps the method of each in `requests`.
 */
function network(requests: string[], ...steps: Step[]): FetchFunction {
  return async (url) => {
    requests.push(methodOf(url));
    const step = steps[Math.min(requests.length, steps.length) - 1] ?? "ok";
    if (step === "reset" || step === "refused" || step === "timeout") {
      if (step === "timeout") {
        throw new DOMException("The operation was aborted due to timeout", "TimeoutError");
      }
      const code = step === "reset" ? "ECONNRESET" : "ECONNREFUSED";
      const cause = Object.assign(new Error(`connect ${code}`), { code });
      throw new TypeError("fetch failed", { cause });
    }
    if (step === "429") return respond(url, 429, "", "30");
    if (step === "500") return respond(url, 500, "oops");
    return respond(url, 200, JSON.stringify({ ok: true }));
  };
}

/** Slack answering `{ok: false, error}` to every request. */
function refusing(requests: string[]): FetchFunction {
  return async (url) => {
    requests.push(methodOf(url));
    return respond(url, 200, JSON.stringify({ ok: false, error: "channel_not_found" }));
  };
}

/** A clock that records the pauses asked of it and ends each at once. */
class Pauses implements Clock {
  readonly slept: number[] = [];

  async sleep(seconds: number): Promise<void> {
    this.slept.push(seconds);
  }

  time(): number {
    return 0;
  }
}

const QUIET = { logLevel: LogLevel.ERROR };
// No jitter: a pause is exactly what the policy asks for.
const STEADY = { random: () => 0 };

function replies(fetch: FetchFunction, clock: Clock = new Pauses()): WebClient {
  return repliesClient(undefined, { ...QUIET, ...STEADY, clock, fetch });
}

function shared(fetch: FetchFunction, clock: Clock = new Pauses()): WebClient {
  return sharedClient(undefined, { ...QUIET, ...STEADY, clock, fetch });
}

test("the connection retry skips the calls that create a message", async () => {
  const methods = ["chat.update", "reactions.add", ...CREATING_METHODS];
  // What the library does when left to itself: a call that failed on the connection is sent again.
  const sentAgain: string[] = [];
  const retrying = new WebClient(undefined, {
    ...QUIET,
    retryConfig: { retries: 1, minTimeout: 0, maxTimeout: 0 },
    fetch: network(sentAgain, "reset"),
  });
  for (const method of methods) {
    await assert.rejects(retrying.apiCall(method, {}), WebAPIRequestError);
  }
  assert.deepEqual(
    sentAgain,
    methods.flatMap((method) => [method, method]),
  );
  // The client replies are written with: the four that create a message are sent once.
  const sentOnce: string[] = [];
  const client = replies(network(sentOnce, "reset"));
  for (const method of methods) {
    const failure = await client.apiCall(method, {}).catch((error: unknown) => error);
    assert.ok(failure instanceof WebAPIRequestError);
    // The sink reads the thread back: nothing says whether Slack applied the call.
    assert.equal(unknownOutcome(failure), true);
    assert.equal(describe(failure), "ECONNRESET");
  }
  assert.deepEqual(
    sentOnce,
    methods.flatMap((method) =>
      CREATING_METHODS.includes(method as never) ? [method] : [method, method],
    ),
  );
});

test("the shared client sends a call that failed on the connection once more, whichever it is", async () => {
  const requests: string[] = [];
  const client = shared(network(requests, "reset"));
  for (const method of ["chat.update", ...CREATING_METHODS]) {
    await assert.rejects(client.apiCall(method, {}), WebAPIRequestError);
  }
  assert.deepEqual(
    requests,
    ["chat.update", ...CREATING_METHODS].flatMap((method) => [method, method]),
  );
});

test("a reset then an answer on chat.update is one call that succeeds, after a pause", async () => {
  const requests: string[] = [];
  const clock = new Pauses();
  const result = await replies(network(requests, "reset", "ok"), clock).chat.update({
    channel: "C000CHAN",
    ts: "1790000000.000001",
    text: "x",
  });
  assert.equal(result.ok, true);
  assert.deepEqual(requests, ["chat.update", "chat.update"]);
  // slack-sdk's backoff: 0.5 * 2 ** 0 seconds, plus a jitter.
  assert.deepEqual(clock.slept, [0.5]);
});

test("a reset on chat.postMessage is thrown by the replies client and sent again by the shared one", async () => {
  for (const [name, make] of [
    ["replies", replies],
    ["shared", shared],
  ] as const) {
    const requests: string[] = [];
    const clock = new Pauses();
    const failure = await make(network(requests, "reset", "ok"), clock)
      .chat.postMessage({ channel: "C000CHAN", text: "x" })
      .then(
        () => null,
        (error: unknown) => error,
      );
    // Only the shared client has a second send to make; the replies one reports the reset.
    if (name === "replies") {
      assert.ok(failure instanceof WebAPIRequestError, name);
      assert.deepEqual(requests, ["chat.postMessage"], name);
      assert.deepEqual(clock.slept, [], name);
    } else {
      assert.equal(failure, null, name);
      assert.deepEqual(requests, ["chat.postMessage", "chat.postMessage"], name);
    }
  }
});

test("a rate limited call is sent again after the Retry-After Slack gave", async () => {
  // Safe for every call, the creating ones too: a rate limited call never ran.
  for (const method of ["chat.update", ...CREATING_METHODS]) {
    const requests: string[] = [];
    const clock = new Pauses();
    const result = await replies(network(requests, "429", "ok"), clock).apiCall(method, {});
    assert.equal(result.ok, true, method);
    assert.deepEqual(requests, [method, method], method);
    assert.deepEqual(clock.slept, [30], method);
  }
});

test("a rate limited call is given up after 3 more sends, and thrown as a rate limit", async () => {
  for (const make of [replies, shared]) {
    const requests: string[] = [];
    const clock = new Pauses();
    const failure = await make(network(requests, "429"), clock)
      .apiCall("chat.postMessage", {})
      .catch((error: unknown) => error);
    assert.ok(failure instanceof WebAPIRateLimitedError);
    assert.equal(failure.retryAfter, 30);
    assert.equal(describe(failure), "ratelimited");
    // Known not applied: the sink may send it again at once.
    assert.equal(unknownOutcome(failure), false);
    assert.equal(requests.length, 4);
    assert.deepEqual(clock.slept, [30, 30, 30]);
  }
});

test("the pause before a send again carries the jitter", async () => {
  const clock = new Pauses();
  const client = repliesClient(undefined, {
    ...QUIET,
    clock,
    random: () => 0.25,
    fetch: network([], "429", "ok"),
  });
  await client.apiCall("chat.update", {});
  assert.deepEqual(clock.slept, [30.25]);
});

test("a retry of either kind spends the one budget of the call", async () => {
  // A connection retry is allowed while no retry was made; a rate limit one while fewer than 3.
  const afterRateLimit: string[] = [];
  await assert.rejects(
    shared(network(afterRateLimit, "429", "reset", "ok")).apiCall("chat.update", {}),
    WebAPIRequestError,
  );
  assert.equal(afterRateLimit.length, 2);
  const afterReset: string[] = [];
  const clock = new Pauses();
  const second = await shared(network(afterReset, "reset", "429", "429", "ok"), clock).apiCall(
    "chat.update",
    {},
  );
  assert.equal(second.ok, true);
  assert.equal(afterReset.length, 4);
  assert.deepEqual(clock.slept, [0.5, 30, 30]);
});

test("a call Slack refused, a timeout and an HTTP error are never sent again", async () => {
  const refused: string[] = [];
  const failure = await replies(refusing(refused))
    .apiCall("chat.update", {})
    .catch((error: unknown) => error);
  assert.ok(failure instanceof WebAPIPlatformError);
  assert.equal(describe(failure), "channel_not_found");
  assert.deepEqual(refused, ["chat.update"]);
  for (const step of ["timeout", "500"] as const) {
    for (const make of [replies, shared]) {
      const requests: string[] = [];
      await assert.rejects(make(network(requests, step)).apiCall("chat.update", {}));
      assert.deepEqual(requests, ["chat.update"], step);
    }
  }
});

test("a refused connection is sent again once, like a reset", async () => {
  const requests: string[] = [];
  const result = await replies(network(requests, "refused", "ok")).apiCall("auth.test", {});
  assert.equal(result.ok, true);
  assert.deepEqual(requests, ["auth.test", "auth.test"]);
});

/** What the three steps of `files.uploadV2` meet: `resets` is how many requests reset first. */
function uploading(requests: string[], resets: number, resetAt: string): FetchFunction {
  let reset = 0;
  return async (url) => {
    const target = String(url);
    requests.push(target.includes("files.slack.com") ? "bytes" : methodOf(url));
    if (requests.at(-1) === resetAt && reset < resets) {
      reset += 1;
      const cause = Object.assign(new Error("read ECONNRESET"), { code: "ECONNRESET" });
      throw new TypeError("fetch failed", { cause });
    }
    const body = target.includes("getUploadURLExternal")
      ? UPLOAD_URL
      : target.includes("completeUploadExternal")
        ? UPLOAD_DONE
        : { ok: true };
    return respond(url, 200, JSON.stringify(body));
  };
}

test("a file upload is retried one Slack call at a time, never as a whole", async () => {
  const upload = { channel_id: "C000CHAN", filename: "a.txt", content: "b" };
  // A reset on the first call is sent again there, and the upload goes on.
  const first: string[] = [];
  const done = await replies(uploading(first, 1, "files.getUploadURLExternal")).files.uploadV2(
    upload,
  );
  assert.equal(done.ok, true);
  assert.deepEqual(first, [
    "files.getUploadURLExternal",
    "files.getUploadURLExternal",
    "bytes",
    "files.completeUploadExternal",
  ]);
  // A reset on the bytes is the library's own step, which is sent once: the upload fails, and
  // the policy does not start it over.
  const second: string[] = [];
  await assert.rejects(
    replies(uploading(second, 1, "bytes")).files.uploadV2(upload),
    WebAPIRequestError,
  );
  assert.deepEqual(second, ["files.getUploadURLExternal", "bytes"]);
});

// F2: `@slack/web-api` 8.2.0 writes lines of its own, to standard output and standard error,
// unless it is given a logger. At its default level it prints every `[ERROR]` and `[WARN]` entry
// of a refusal's `response_metadata.messages` (Slack's sentence, which can quote a value of the
// message: `unsupported type: VALUE [json-pointer:/blocks/0]`), every entry of
// `response_metadata.warnings`, and `http request failed` with the failure's message; at its
// debug level the body of each request and each result, which hold the message text and, for
// `apps.connections.open`, the Socket Mode ticket in the WebSocket URL. `describeRefusal`
// withholds Slack's sentence and the setup doc says the log never holds message content, so the
// daemon's clients are given a logger that writes none of the library's text.

const MARKER = "MARKER_VALUE_7";

/** Everything written to the daemon's log and to the process's two outputs while `work` runs. */
async function everythingWritten(work: () => Promise<void>): Promise<string[]> {
  const lines: string[] = [];
  const restoreWriter = setWriter((line) => lines.push(line));
  // What a library's console logger writes (`[ERROR]  web-api:...`), or quotes the marker. The
  // test runner talks to its parent over the same outputs: everything is passed on.
  const ours = /\[(DEBUG|INFO|WARN|ERROR)\]|MARKER_VALUE_7/;
  const tapped = [process.stdout, process.stderr].map((stream) => {
    const original = stream.write;
    stream.write = ((chunk: unknown, ...rest: unknown[]): boolean => {
      if (typeof chunk === "string" && ours.test(chunk)) lines.push(chunk);
      return (original as (...args: unknown[]) => boolean).call(stream, chunk, ...rest);
    }) as typeof stream.write;
    return [stream, original] as const;
  });
  setLevel("DEBUG"); // the most the log would ever take
  try {
    await work();
  } finally {
    for (const [stream, original] of tapped) stream.write = original;
    setLevel("INFO");
    setWriter(restoreWriter);
  }
  return lines;
}

/** Slack answering with a body, whatever the call. */
function answering(body: object): FetchFunction {
  return async (url) => respond(url, 200, JSON.stringify(body));
}

const REFUSAL = {
  ok: false,
  error: "invalid_blocks",
  response_metadata: {
    messages: [
      `[ERROR] unsupported type: ${MARKER} [json-pointer:/blocks/0]`,
      `[WARN] a value that looks odd: ${MARKER}`,
    ],
    warnings: [`warning about ${MARKER}`],
  },
};

// Built with no `logLevel` of the test's: the library's own default is what is under test.
const UNMUTED = [
  ["shared", sharedClient],
  ["replies", repliesClient],
] as const;

/** The client for the test, its pauses ended at once. */
function unmuted(
  build: typeof sharedClient,
  fetch: FetchFunction,
  options: Partial<ClientOptions> = {},
): WebClient {
  return build(undefined, { ...STEADY, clock: new Pauses(), fetch, ...options });
}

for (const [name, build] of UNMUTED) {
  test(`the ${name} client writes none of the library's text when Slack refuses a call`, async () => {
    const client = unmuted(build, answering(REFUSAL));
    const written = await everythingWritten(async () => {
      await assert.rejects(
        client.chat.postMessage({ channel: "C000CHAN", text: `hello ${MARKER}` }),
        WebAPIPlatformError,
      );
    });
    assert.deepEqual(written, []);
  });

  test(`the ${name} client writes none of the library's text when a call fails on the connection`, async () => {
    const client = unmuted(build, async () => {
      throw new TypeError(`fetch failed ${MARKER}`);
    });
    const written = await everythingWritten(async () => {
      await assert.rejects(
        client.chat.postMessage({ channel: "C000CHAN", text: "x" }),
        WebAPIRequestError,
      );
    });
    assert.deepEqual(written, []);
  });

  test(`the ${name} client writes neither a request nor an answer at the debug level`, async () => {
    // What `apps.connections.open` answers carries the WebSocket URL, and with it the ticket.
    const ticket = `wss://example.invalid/link/?ticket=${MARKER}`;
    // Asked for at the debug level, which the library would honour without a logger of ours.
    const client = unmuted(build, answering({ ok: true, url: ticket, ts: "1790000000.000001" }), {
      logLevel: LogLevel.DEBUG,
    });
    const written = await everythingWritten(async () => {
      await client.chat.postMessage({ channel: "C000CHAN", text: `hello ${MARKER}` });
      await client.apps.connections.open({});
    });
    assert.deepEqual(written, []);
  });
}
