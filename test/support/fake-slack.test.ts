/**
 * The proof that `FakeSlack` answers as Python's did: the Slack tests of `tests/test_fakes.py`,
 * the TypeScript half (the funnel, the errors, the normalisation), and a replay of every golden
 * of the reply sink onto a fresh fake.
 */
import assert from "node:assert/strict";
import { readdirSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import {
  type FetchFunction,
  WebAPIHTTPError,
  WebAPIPlatformError,
  WebAPIRateLimitedError,
  WebAPIRequestError,
  WebClient,
} from "@slack/web-api";
import {
  AsyncEvent,
  BOT,
  CHANNEL,
  connectionReset,
  FakeClock,
  FakeSlack,
  httpError,
  networkDown,
  ResetAfterApply,
  rateLimited,
  rejected,
  SlowAfterApply,
  THREAD,
  timedOut,
} from "./fake-slack.ts";
import { GOLDEN, golden, type Json, type JsonObject } from "./fixtures.ts";

// ported from tests/test_fakes.py

test("fake slack returns recorded responses", async () => {
  const slack = new FakeSlack();
  const auth = await slack.auth.test();
  assert.ok(auth.team_id === "T000TEAM" && auth.user_id === "U000BOT");
  assert.ok(slack.callsTo("auth.test").length > 0);
});

// The funnel: every method the provider uses ends in `apiCall`.

/** The bound method of a client by its Web API name: `chat.postMessage` is `client.chat.postMessage`. */
function bound(slack: WebClient, method: string): (args: JsonObject) => Promise<unknown> {
  let at: unknown = slack;
  for (const step of method.split(".")) at = (at as Record<string, unknown> | undefined)?.[step];
  if (typeof at !== "function") throw new Error(`no bound method ${method}`);
  return at as (args: JsonObject) => Promise<unknown>;
}

const THROUGH_THE_FUNNEL: Array<[string, JsonObject]> = [
  ["chat.postMessage", { channel: CHANNEL, text: "hi" }],
  [
    "chat.startStream",
    {
      channel: CHANNEL,
      thread_ts: THREAD,
      recipient_team_id: "T000TEAM",
      recipient_user_id: "U000ALICE",
      task_display_mode: "timeline",
      chunks: [{ type: "markdown_text", text: "hi" }],
    },
  ],
  ["chat.appendStream", { channel: CHANNEL, ts: "1790000000.000009", chunks: [] }],
  ["chat.stopStream", { channel: CHANNEL, ts: "1790000000.000009" }],
  ["chat.update", { channel: CHANNEL, ts: "1790000000.000009", text: "x" }],
  ["assistant.threads.setStatus", { channel_id: CHANNEL, thread_ts: THREAD, status: "working" }],
  ["views.publish", { user_id: "U000ALICE", view: { type: "home", blocks: [] } }],
  ["reactions.add", { channel: CHANNEL, name: "eyes", timestamp: THREAD }],
  ["reactions.remove", { channel: CHANNEL, name: "eyes", timestamp: THREAD }],
  ["conversations.info", { channel: CHANNEL }],
  ["conversations.members", { channel: CHANNEL, limit: 10 }],
];

for (const [method, args] of THROUGH_THE_FUNNEL) {
  test(`the bound method ends in the fake's funnel [${method}]`, async () => {
    const slack = new FakeSlack();
    await bound(slack, method)(args);
    assert.deepEqual(slack.apiCalls, [{ method, args }]);
  });
}

test("a file upload goes through its two Slack calls and posts the bytes to the upload url", async () => {
  const slack = new FakeSlack();
  const done = await slack.files.uploadV2({
    channel_id: CHANNEL,
    thread_ts: THREAD,
    filename: "notes.txt",
    content: "hello",
    initial_comment: "here",
  });
  assert.equal(done.ok, true);
  assert.deepEqual(slack.apiCalls, [
    { method: "files.getUploadURLExternal", args: { filename: "notes.txt", length: 5 } },
    {
      method: "files.completeUploadExternal",
      args: {
        files: [{ id: "F000FILE", title: "notes.txt" }],
        channel_id: CHANNEL,
        initial_comment: "here",
        thread_ts: THREAD,
      },
    },
  ]);
  assert.deepEqual(
    slack.uploaded.map(({ url, data }) => [url, data.toString()]),
    [["https://files.slack.com/upload/v1/ABC123...", "hello"]],
  );
});

test("the generic call and the upload helper reach the funnel too", async () => {
  const slack = new FakeSlack();
  await slack.apiCall("auth.test");
  await slack.apiCall("files.uploadV2", { channel_id: CHANNEL, filename: "a.txt", content: "b" });
  assert.deepEqual(
    slack.apiCalls.map((call) => call.method),
    ["auth.test", "files.getUploadURLExternal", "files.completeUploadExternal"],
  );
});

test("a fake is built with no token and no network", () => {
  const slack = new FakeSlack();
  assert.equal(slack.token, undefined);
});

// The errors: the same classes and fields as the client throws on the wire.

/** What the real client throws when its `fetch` does this, with no retry. */
async function realError(fetch: FetchFunction, rejectRateLimitedCalls = false): Promise<unknown> {
  const client = new WebClient(undefined, {
    fetch,
    retryConfig: { retries: 0 },
    rejectRateLimitedCalls,
    logLevel: "error" as never,
  });
  try {
    await client.apiCall("chat.update", {});
  } catch (error) {
    return error;
  }
  throw new Error("the real client did not throw");
}

function response(status: number, body: string, headers: Record<string, string> = {}) {
  return {
    ok: status === 200,
    status,
    statusText: "x",
    url: "https://slack.com/api/chat.update",
    headers: {
      get: (name: string) => headers[name] ?? null,
      entries: () => Object.entries(headers),
    },
    arrayBuffer: async () => new ArrayBuffer(0),
    json: async () => JSON.parse(body) as unknown,
    text: async () => body,
  };
}

/** The fields a provider can tell an error by. */
function fieldsOf(error: unknown): unknown {
  assert.ok(error instanceof Error);
  const own = error as Error & Record<string, unknown>;
  const original = own.original as (Error & { cause?: Error & { code?: string } }) | undefined;
  return {
    class: error.constructor.name,
    name: error.name,
    message: error.message,
    code: own.code,
    data: own.data,
    retryAfter: own.retryAfter,
    statusCode: own.statusCode,
    original: original && { name: original.name, message: original.message },
    cause: original?.cause && { code: original.cause.code, message: original.cause.message },
  };
}

test("a rejected call is the platform error the client throws", async () => {
  const real = await realError(async () =>
    response(200, JSON.stringify({ ok: false, error: "channel_not_found" })),
  );
  const fake = rejected("channel_not_found");
  assert.ok(fake instanceof WebAPIPlatformError);
  assert.deepEqual(fieldsOf(fake), fieldsOf(real));
  assert.equal(fake.code, "slack_webapi_platform_error");
  assert.equal(fake.data.error, "channel_not_found");
});

test("a connection reset is a request error whose cause carries the system code", async () => {
  const real = await realError(async () => {
    throw new TypeError("fetch failed", {
      cause: Object.assign(new Error("read ECONNRESET"), { code: "ECONNRESET" }),
    });
  });
  const fake = connectionReset();
  assert.ok(fake instanceof WebAPIRequestError);
  assert.deepEqual(fieldsOf(fake), fieldsOf(real));
  assert.equal(fake.code, "slack_webapi_request_error");
});

test("a network that is down is a request error", () => {
  const fake = networkDown();
  assert.ok(fake instanceof WebAPIRequestError);
  assert.equal((fake.original.cause as { code?: string }).code, "ENETUNREACH");
});

test("a timeout is a request error wrapping the TimeoutError the client's own timer raises", async () => {
  // `timeout` makes the client pass `AbortSignal.timeout`, which aborts `fetch` with this.
  const client = new WebClient(undefined, {
    timeout: 5,
    retryConfig: { retries: 0 },
    logLevel: "error" as never,
    fetch: (_url, init) =>
      new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(init.signal?.reason));
      }),
  });
  // The timer of `AbortSignal.timeout` does not keep the event loop alive, and on Node 22 the
  // loop ends before it fires when nothing else is pending.
  const awake = setInterval(() => {}, 1000);
  try {
    const real = await client.apiCall("chat.update", {}).catch((error: unknown) => error);
    assert.deepEqual(fieldsOf(timedOut()), fieldsOf(real));
  } finally {
    clearInterval(awake);
  }
});

test("a rate limit is the error the client throws when it rejects rate limited calls", async () => {
  const real = await realError(async () => response(429, "", { "retry-after": "3" }), true);
  const fake = rateLimited(3);
  assert.ok(fake instanceof WebAPIRateLimitedError);
  assert.deepEqual(fieldsOf(fake), fieldsOf(real));
});

test("an http error is the error the client throws on a status other than 200", async () => {
  const real = await realError(async () => response(500, ""));
  const fake = httpError(500, "x");
  assert.ok(fake instanceof WebAPIHTTPError);
  assert.equal(fake.statusCode, 500);
  assert.equal(fieldsOf(fake) && (fieldsOf(real) as { class: string }).class, "WebAPIHTTPError");
});

test("a scripted error is thrown and the call is still recorded", async () => {
  const slack = new FakeSlack();
  slack.responses["chat.update"] = connectionReset();
  await assert.rejects(slack.chat.update({ channel: CHANNEL, ts: "1.1", text: "x" }), (error) => {
    assert.ok(error instanceof WebAPIRequestError);
    return true;
  });
  assert.equal(slack.callsTo("chat.update").length, 1);
});

test("an answer with ok false is thrown as the platform error, with the library's metadata", async () => {
  const slack = new FakeSlack();
  slack.responses["conversations.info"] = { ok: false, error: "channel_not_found" };
  await assert.rejects(slack.conversations.info({ channel: CHANNEL }), (error) => {
    assert.ok(error instanceof WebAPIPlatformError);
    assert.deepEqual(error.data, {
      ok: false,
      error: "channel_not_found",
      response_metadata: {},
    });
    return true;
  });
});

test("an answer is a copy: changing it changes no later answer", async () => {
  const slack = new FakeSlack();
  const first = (await slack.auth.test()) as unknown as JsonObject;
  first.user_id = "changed";
  assert.equal(((await slack.auth.test()) as unknown as JsonObject).user_id, BOT);
});

// The normalisation of what reaches the funnel.

test("the recorded arguments drop the token and the empty fields", async () => {
  const slack = new FakeSlack();
  await slack.chat.postMessage({
    channel: CHANNEL,
    text: "hi",
    token: "xoxb-per-call",
    thread_ts: undefined,
    username: null as unknown as string,
  });
  assert.deepEqual(slack.apiCalls[0]?.args, { channel: CHANNEL, text: "hi" });
});

test("blocks and chunks given as JSON strings are kept as parsed values", async () => {
  const slack = new FakeSlack();
  await slack.apiCall("chat.postMessage", {
    channel: CHANNEL,
    text: "hi",
    blocks: JSON.stringify([{ type: "divider" }]),
  });
  await slack.apiCall("chat.appendStream", {
    channel: CHANNEL,
    ts: "1.1",
    chunks: JSON.stringify([{ type: "markdown_text", text: "[]" }]),
  });
  assert.deepEqual(slack.apiCalls[0]?.args.blocks, [{ type: "divider" }]);
  assert.deepEqual(slack.apiCalls[1]?.args.chunks, [{ type: "markdown_text", text: "[]" }]);
});

test("a text that looks like JSON is not parsed", async () => {
  const slack = new FakeSlack();
  await slack.chat.postMessage({ channel: CHANNEL, text: "[1, 2]" });
  assert.equal(slack.apiCalls[0]?.args.text, "[1, 2]");
});

test("the recorded call is a copy of what the caller passed", async () => {
  const slack = new FakeSlack();
  const blocks = [{ type: "divider" }];
  await slack.chat.postMessage({ channel: CHANNEL, text: "hi", blocks });
  blocks.push({ type: "divider" });
  assert.deepEqual(slack.apiCalls[0]?.args.blocks, [{ type: "divider" }]);
  assert.deepEqual(slack.messageBlocks(), [[{ type: "divider" }]]);
});

test("a value that is not JSON is refused instead of being kept as text", async () => {
  const slack = new FakeSlack();
  await assert.rejects(
    slack.apiCall("chat.postMessage", { channel: CHANNEL, text: "x", blocks: [new Date()] }),
    TypeError,
  );
});

// How the fake numbers, answers and refuses.

const START: JsonObject = {
  channel: CHANNEL,
  thread_ts: THREAD,
  recipient_team_id: "T000TEAM",
  recipient_user_id: "U000ALICE",
  chunks: [{ type: "markdown_text", text: "one" }],
};

test("the fake numbers every message it creates, posts and streams alike", async () => {
  const slack = new FakeSlack();
  await slack.chat.postMessage({ channel: CHANNEL, text: "a" });
  await slack.apiCall("chat.startStream", START);
  await slack.chat.postMessage({ channel: CHANNEL, text: "b" });
  assert.deepEqual(slack.createdTs, [
    "1790000000.000001",
    "1790000000.000002",
    "1790000000.000003",
  ]);
  assert.deepEqual(slack.postedTs, ["1790000000.000001", "1790000000.000003"]);
  assert.deepEqual(slack.streamTs, ["1790000000.000002"]);
  assert.deepEqual(slack.streamTexts(), ["one"]);
});

test("an open stream refuses an edit and a delete, a closed one refuses an append and a stop", async () => {
  const slack = new FakeSlack();
  await slack.apiCall("chat.startStream", START);
  const ts = "1790000000.000001";
  const refusal = async (call: Promise<unknown>) =>
    call.then(
      () => null,
      (error: unknown) => (error as WebAPIPlatformError).data.error,
    );
  assert.equal(
    await refusal(slack.chat.update({ channel: CHANNEL, ts, text: "x" })),
    "streaming_state_conflict",
  );
  assert.equal(await refusal(slack.chat.delete({ channel: CHANNEL, ts })), "cant_delete_message");
  await slack.chat.stopStream({ channel: CHANNEL, ts });
  assert.equal(
    await refusal(slack.chat.appendStream({ channel: CHANNEL, ts, chunks: [] })),
    "message_not_in_streaming_state",
  );
  assert.equal(
    await refusal(slack.chat.stopStream({ channel: CHANNEL, ts })),
    "message_not_in_streaming_state",
  );
  await slack.chat.update({ channel: CHANNEL, ts, text: "x" });
  assert.equal(slack.messages.get(ts)?.updated, true);
});

test("a stream that expires counts as stopped for the push and refuses an append", async () => {
  const slack = new FakeSlack();
  await slack.apiCall("chat.startStream", START);
  assert.equal(slack.pushes(), 0);
  slack.expire("1790000000.000001");
  assert.equal(slack.pushes(), 1);
  await assert.rejects(
    slack.chat.appendStream({ channel: CHANNEL, ts: "1790000000.000001", chunks: [] }),
    WebAPIPlatformError,
  );
});

test("a post is a push and an edit is not", async () => {
  const slack = new FakeSlack();
  await slack.chat.postMessage({ channel: CHANNEL, text: "a" });
  await slack.chat.update({
    channel: CHANNEL,
    ts: "1790000000.000001",
    text: "b",
    blocks: [{ type: "markdown", text: "b" }],
  });
  assert.equal(slack.pushes(), 1);
  assert.deepEqual(slack.messageTexts(), ["b"]);
  assert.equal(slack.messages.get("1790000000.000001")?.text, "b");
});

/** The text of each message as `conversations.replies` reads it back. */
async function repliesText(slack: FakeSlack): Promise<unknown[]> {
  const read = (await slack.conversations.replies({ channel: CHANNEL, ts: THREAD })) as unknown as {
    messages: JsonObject[];
  };
  return read.messages.map((message) => message.text);
}

test("a stream's text is read back as Slack reads it", async () => {
  const slack = new FakeSlack();
  await slack.apiCall("chat.startStream", {
    ...START,
    chunks: [
      { type: "markdown_text", text: "## Title\n\n**bold** and [a link](https://example.org)\n" },
      { type: "markdown_text", text: "- one\n* two\n" },
      { type: "task_update", id: "t1", title: "Bash: ls", status: "in_progress" },
      { type: "task_update", id: "t1", title: "Bash: ls", status: "complete", output: "ok" },
      { type: "task_update", id: "t1", title: "Bash: ls", status: "complete", output: "!" },
    ],
  });
  assert.deepEqual(slack.messageTexts(), [
    "## Title\n\n**bold** and [a link](https://example.org)\n- one\n* two",
  ]);
  assert.deepEqual(await repliesText(slack), [
    "Title\n\n*bold* and <https://example.org|a link>\n• one\n• two Bash: ls",
  ]);
  assert.deepEqual(slack.messageCards(), [
    [{ id: "t1", title: "Bash: ls", status: "complete", output: "ok!" }],
  ]);
});

test("a stream's text carries the title, subtitle and body of each container", async () => {
  const slack = new FakeSlack();
  await slack.apiCall("chat.startStream", {
    ...START,
    chunks: [
      { type: "markdown_text", text: "x" },
      {
        type: "blocks",
        blocks: [
          {
            type: "container",
            title: { type: "plain_text", text: "Edit" },
            subtitle: { type: "plain_text", text: "1 line" },
            child_blocks: [
              {
                type: "rich_text",
                elements: [
                  { type: "rich_text_preformatted", elements: [{ type: "text", text: "diff" }] },
                ],
              },
            ],
          },
        ],
      },
    ],
  });
  assert.deepEqual(await repliesText(slack), ["x Edit 1 line ```diff```"]);
});

test("a task card read from a block is its rich text as plain strings", async () => {
  const slack = new FakeSlack();
  await slack.chat.postMessage({
    channel: CHANNEL,
    text: "t",
    blocks: [
      {
        type: "task_card",
        task_id: "t9",
        title: "Read",
        status: "complete",
        details: {
          type: "rich_text",
          elements: [
            {
              type: "rich_text_section",
              elements: [
                { type: "text", text: "a" },
                { type: "text", text: "b" },
              ],
            },
          ],
        },
      },
    ],
  });
  assert.deepEqual(slack.messageCards(), [
    [{ id: "t9", title: "Read", status: "complete", details: "ab" }],
  ]);
});

test("the thread read back holds the messages newer than oldest, deleted ones gone", async () => {
  const slack = new FakeSlack();
  await slack.chat.postMessage({ channel: CHANNEL, text: "a" });
  await slack.apiCall("chat.startStream", START);
  await slack.chat.postMessage({ channel: CHANNEL, text: "gone" });
  await slack.chat.delete({ channel: CHANNEL, ts: "1790000000.000003" });
  const all = (await slack.conversations.replies({ channel: CHANNEL, ts: THREAD })) as unknown as {
    messages: JsonObject[];
  };
  assert.deepEqual(
    all.messages.map((m) => [m.ts, m.user, m.text, m.streaming_state]),
    [
      ["1790000000.000001", BOT, "a", undefined],
      ["1790000000.000002", BOT, "one", "in_progress"],
    ],
  );
  const later = (await slack.conversations.replies({
    channel: CHANNEL,
    ts: THREAD,
    oldest: "1790000000.000001",
  })) as unknown as { messages: JsonObject[] };
  assert.deepEqual(
    later.messages.map((m) => m.ts),
    ["1790000000.000002"],
  );
});

// How a test scripts and holds a call.

test("a scripted sequence answers once each and repeats its last", async () => {
  const slack = new FakeSlack();
  slack.responses["chat.stopStream"] = [networkDown(), { ok: true, n: 2 }, { ok: true, n: 3 }];
  await assert.rejects(slack.chat.stopStream({ channel: CHANNEL, ts: "1.1" }), WebAPIRequestError);
  const answers = [];
  for (let i = 0; i < 3; i += 1) {
    answers.push(
      ((await slack.chat.stopStream({ channel: CHANNEL, ts: "1.1" })) as unknown as JsonObject).n,
    );
  }
  assert.deepEqual(answers, [2, 3, 3]);
});

test("a scripted function answers from the call's arguments", async () => {
  const slack = new FakeSlack();
  slack.responses["conversations.info"] = (args) =>
    args.channel === CHANNEL
      ? { ok: true, channel: { id: CHANNEL } }
      : rejected("channel_not_found");
  assert.equal((await slack.conversations.info({ channel: CHANNEL })).ok, true);
  await assert.rejects(slack.conversations.info({ channel: "C000NONE" }), WebAPIPlatformError);
});

test("a method with no answer scripted answers ok", async () => {
  const slack = new FakeSlack();
  const answer = await slack.views.publish({
    user_id: "U000ALICE",
    view: { type: "home", blocks: [] },
  });
  assert.equal(answer.ok, true);
});

test("a delay holds each call before it is recorded", async () => {
  const slack = new FakeSlack();
  slack.delay = 0.02;
  const call = slack.auth.test();
  assert.deepEqual(slack.apiCalls, []);
  await call;
  assert.equal(slack.apiCalls.length, 1);
});

test("a gate holds one method until the event is set, and says that a call arrived", async () => {
  const slack = new FakeSlack();
  slack.gate = new AsyncEvent();
  slack.gateMethod = "chat.postMessage";
  const held = slack.chat.postMessage({ channel: CHANNEL, text: "a" });
  await slack.gated.wait();
  await slack.auth.test();
  assert.deepEqual(
    slack.apiCalls.map((call) => call.method),
    ["auth.test"],
  );
  slack.gate.set();
  await held;
  assert.deepEqual(
    slack.apiCalls.map((call) => call.method),
    ["auth.test", "chat.postMessage"],
  );
});

test("a reset after apply keeps what the call did and throws once", async () => {
  const slack = new ResetAfterApply();
  slack.resetNext = "chat.postMessage";
  await assert.rejects(slack.chat.postMessage({ channel: CHANNEL, text: "a" }), WebAPIRequestError);
  assert.deepEqual(slack.postedTs, ["1790000000.000001"]);
  await slack.chat.postMessage({ channel: CHANNEL, text: "b" });
  assert.deepEqual(slack.postedTs, ["1790000000.000001", "1790000000.000002"]);
});

test("a slow answer after apply has applied the call when it comes back", async () => {
  const slack = new SlowAfterApply();
  slack.slowMethod = "chat.postMessage";
  slack.slowFor = 0.02;
  const call = slack.chat.postMessage({ channel: CHANNEL, text: "a" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(slack.postedTs, ["1790000000.000001"]);
  await call;
});

// The clock.

test("a sleep ends when the clock reaches its time", async () => {
  const clock = new FakeClock();
  const woke: string[] = [];
  const sleeping = clock.sleep(280).then(() => woke.push("deadline"));
  await clock.advance(279);
  assert.deepEqual(woke, []);
  assert.equal(clock.time(), 279);
  await clock.advance(1);
  await sleeping;
  assert.deepEqual(woke, ["deadline"]);
});

test("advance wakes the sleepers that are due in the order they began to sleep", async () => {
  const clock = new FakeClock();
  const woke: string[] = [];
  const late = clock.sleep(10).then(() => woke.push("began first, due later"));
  const early = clock.sleep(1).then(() => woke.push("began second, due sooner"));
  const pending = clock.sleep(100).then(() => woke.push("not due"));
  await clock.advance(10);
  await Promise.all([late, early]);
  assert.deepEqual(woke, ["began first, due later", "began second, due sooner"]);
  void pending;
});

test("advance lets what woke run to its next wait", async () => {
  const clock = new FakeClock();
  const seen: number[] = [];
  const loop = (async () => {
    for (let i = 0; i < 2; i += 1) {
      await clock.sleep(5);
      seen.push(clock.time());
    }
  })();
  await clock.advance(5);
  assert.deepEqual(seen, [5]);
  await clock.advance(5);
  await loop;
  assert.deepEqual(seen, [5, 10]);
});

test("a sleep that is aborted rejects and is not woken later", async () => {
  const clock = new FakeClock();
  const abort = new AbortController();
  const sleeping = clock.sleep(5, abort.signal);
  abort.abort(new Error("cancelled"));
  await assert.rejects(sleeping, /cancelled/);
  await clock.advance(10);
  await assert.rejects(clock.sleep(1, abort.signal), /cancelled/);
});

// The goldens: the Slack calls the Python reply sink made, replayed onto a fresh fake.

const GOLDENS = readdirSync(join(GOLDEN, "slack"))
  .filter((file) => file.endsWith(".json"))
  .map((file) => file.slice(0, -".json".length))
  .sort();

/** The library adds an empty `response_metadata` to every answer; the golden holds Slack's own. */
function slacksOwn(answer: unknown): Json {
  const { response_metadata: metadata, ...rest } = answer as JsonObject;
  const empty =
    typeof metadata === "object" && metadata !== null && Object.keys(metadata).length === 0;
  return empty ? rest : (answer as JsonObject);
}

test("there is a golden for every recording", () => {
  assert.equal(GOLDENS.length, 42);
});

for (const name of GOLDENS) {
  test(`the fake answers as Python's did [${name}]`, async () => {
    const recorded = golden("slack", name);
    const calls = recorded.slack_calls as JsonObject[] | undefined;
    if (calls === undefined) {
      assert.equal(
        typeof recorded.input_raises,
        "string",
        "a golden with neither calls nor a raise",
      );
      return;
    }
    const slack = new FakeSlack();
    for (const [index, call] of calls.entries()) {
      const method = call.method as string;
      const answer = await bound(slack, method)(structuredClone(call.args as JsonObject));
      assert.deepEqual(slacksOwn(answer), call.answer, `the answer to call ${index}, ${method}`);
    }
    assert.deepEqual(
      slack.apiCalls,
      calls.map((call) => ({ method: call.method, args: call.args })),
    );
    assert.deepEqual(
      {
        message_blocks: slack.messageBlocks(),
        message_cards: slack.messageCards(),
        message_texts: slack.messageTexts(),
        pushes: slack.pushes(),
      },
      recorded.final,
    );
  });
}
