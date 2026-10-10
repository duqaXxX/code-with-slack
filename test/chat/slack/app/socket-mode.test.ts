/**
 * The Socket Mode connection the daemon listens on (`socketReceiver`), on the real
 * `@slack/bolt` 5.1.0 receiver and `@slack/socket-mode` 3.1.0 client with their network replaced.
 *
 * Not a port: Python's `slack_bolt` logged nothing of the sort. Two questions, answered here:
 * what the libraries write (nothing of their own text: a refusal's sentence, an error's message
 * and the WebSocket URL's ticket can all be in it), and how the daemon learns that the
 * connection is gone for good (it is not told: only an unhandled rejection shows it).
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { join } from "node:path";
import { test } from "node:test";
import type { App } from "@slack/bolt";
import { type FetchFunction, WebClient } from "@slack/web-api";
import { socketReceiver } from "../../../../src/chat/slack/app/app.ts";
import { silentLogger } from "../../../../src/chat/slack/quiet-logger.ts";
import { setLevel, setWriter } from "../../../../src/log.ts";

const ROOT = join(import.meta.dirname, "..", "..", "..", "..");
const APP_TOKEN = "xap" + "p-1";
const MARKER = "MARKER_VALUE_7";

/** Slack answering `apps.connections.open` with a refusal that quotes the marker. */
function refusing(error: string): FetchFunction {
  return async (url) => {
    const body = JSON.stringify({
      ok: false,
      error,
      response_metadata: { messages: [`[ERROR] unsupported type: ${MARKER} [json-pointer:/x]`] },
    });
    return {
      ok: true,
      status: 200,
      statusText: "OK",
      url: String(url),
      headers: { get: () => null, entries: () => [] },
      arrayBuffer: async () => new ArrayBuffer(0),
      json: async () => JSON.parse(body),
      text: async () => body,
    };
  };
}

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

test("the Socket Mode client writes none of the library's text when Slack refuses the connection", async () => {
  const receiver = socketReceiver(APP_TOKEN, {
    clientOptions: { fetch: refusing("invalid_auth") },
  });
  const written = await everythingWritten(async () => {
    await assert.rejects(
      receiver.start(),
      (error: unknown) => (error as { data?: { error?: string } }).data?.error === "invalid_auth",
    );
  });
  assert.equal(
    written.some((line) => line.includes(MARKER)),
    false,
  );
  // Only that the library reported something, by its level: no sentence of its own.
  for (const line of written)
    assert.match(line, /^\S+ \S+ (ERROR|WARNING) awaydesk\..*: socket mode /);
});

test("an error Bolt's receiver reports is written without its message", async () => {
  const receiver = socketReceiver(APP_TOKEN);
  // What the receiver calls for each payload: `App.processEvent`, which here throws a message
  // that quotes the marker, as `${error}` of a listener's failure would.
  receiver.init({
    processEvent: async () => {
      throw new Error(`could not read ${MARKER}`);
    },
  } as unknown as App);
  const written = await everythingWritten(async () => {
    receiver.client.emit("slack_event", {
      ack: async () => {},
      body: { type: "event_callback" },
      type: "events_api",
    });
    for (let round = 0; round < 10; round += 1) {
      await new Promise<void>((resolve) => setImmediate(resolve));
    }
  });
  assert.equal(
    written.some((line) => line.includes(MARKER)),
    false,
  );
  assert.equal(written.length, 1);
  assert.match(
    written[0] ?? "",
    / ERROR awaydesk\.chat\.slack\.libraries: socket mode reported an error/,
  );
});

test("the connection's states are logged by their names", async () => {
  const receiver = socketReceiver(APP_TOKEN);
  const written = await everythingWritten(async () => {
    receiver.client.emit("connected");
    receiver.client.emit("reconnecting");
    receiver.client.emit("disconnected");
  });
  assert.deepEqual(
    written.map((line) => line.replace(/^\S+ \S+ /, "")),
    [
      "INFO awaydesk.chat.slack.app: socket mode: connected",
      "WARNING awaydesk.chat.slack.app: socket mode: reconnecting",
      "INFO awaydesk.chat.slack.app: socket mode: disconnected",
    ],
  );
});

// How the daemon learns the connection is gone for good. `SocketModeClient` (3.1.0,
// `dist/src/SocketModeClient.js`) reconnects from its `close` listener with
// `delayReconnectAttempt(this.start)`, which nobody awaits: `start` asks `retrieveWSSURL` for a
// new WebSocket URL, and a failure that `retrieveWSSURL` calls unrecoverable (a request error,
// an HTTP error, `invalid_auth` and the other four platform codes of
// `UnrecoverableSocketModeStartError`) is rethrown there. The client emits `connecting`,
// `connected`, `reconnecting`, `disconnecting`, `disconnected` and `authenticated`, and
// `disconnected` only when reconnecting is off or `disconnect()` was called. So the rejection is
// the one sign: the daemon logs it by its code and goes on, deaf until its token is fixed and it
// is restarted (`installProcessHandlers` in `src/main.ts`). A request error or an HTTP error
// reaches that point only once the client's own 100 retries are spent. Run in a child process,
// since an unhandled rejection in this one is the test runner's to fail.
test("a reconnect that cannot get a new URL shows only as an unhandled rejection", () => {
  const script = `
    import { socketReceiver } from "./src/chat/slack/app/app.ts";
    const body = JSON.stringify({ ok: false, error: "invalid_auth" });
    const fetch = async (url) => ({
      ok: true, status: 200, statusText: "OK", url: String(url),
      headers: { get: () => null, entries: () => [] },
      arrayBuffer: async () => new ArrayBuffer(0),
      json: async () => JSON.parse(body), text: async () => body,
    });
    const unhandled = [];
    process.on("unhandledRejection", (reason) => unhandled.push(reason?.data?.error ?? "?"));
    const receiver = socketReceiver(${JSON.stringify(APP_TOKEN)}, {
      clientOptions: { fetch }, clientPingTimeout: 10,
    });
    const events = [];
    for (const name of ["connecting", "connected", "reconnecting", "disconnecting", "disconnected", "authenticated", "error"]) {
      receiver.client.on(name, () => events.push(name));
    }
    receiver.client.emit("close"); // the socket closed: a reconnect is scheduled
    await new Promise((resolve) => setTimeout(resolve, 500));
    console.log(JSON.stringify({ events, unhandled }));
    process.exit(0);
  `;
  const result = spawnSync(process.execPath, ["--input-type=module", "-e", script], {
    cwd: ROOT,
    encoding: "utf8",
  });
  const lines = result.stdout.trim().split("\n");
  const seen = JSON.parse(lines.at(-1) ?? "{}") as { events: string[]; unhandled: string[] };
  assert.deepEqual(seen.unhandled, ["invalid_auth"]);
  // Told that it was reconnecting, never that it gave up.
  assert.deepEqual(seen.events, ["reconnecting"]);
});

// The network gone for a long time. `SocketModeClient` asks for the new WebSocket URL through a
// web client of its own, and by itself gives it `retryConfig { retries: 100, factor: 1.3 }` with
// no upper wait (`SocketModeClient.js`, the constructor): the wait before the next try grows to
// about a third of the time already spent offline, 18 minutes after an hour. The daemon caps
// it (`RECONNECT_WAIT_SECONDS`). Eighty minutes offline pass on mocked timers, in a child
// process: the timers are the libraries' own. Eighty, since the list of 100 waits lasts 88 and
// what comes after it is the test below this one.
test("with the network gone the connection is tried again at least every minute", () => {
  const script = `
    import { mock } from "node:test";
    import { socketReceiver, RECONNECT_WAIT_SECONDS } from "./src/chat/slack/app/app.ts";
    mock.timers.enable({ apis: ["setTimeout", "Date"] });
    const body = JSON.stringify({ ok: true, url: "wss://localhost.invalid/link" });
    let online = false;
    const attempts = [];
    const fetch = async (url) => {
      attempts.push(Date.now());
      if (!online) throw new TypeError("fetch failed");
      return {
        ok: true, status: 200, statusText: "OK", url: String(url),
        headers: { get: () => null, entries: () => [] },
        arrayBuffer: async () => new ArrayBuffer(0),
        json: async () => JSON.parse(body), text: async () => body,
      };
    };
    const unhandled = [];
    process.on("unhandledRejection", (reason) => unhandled.push(String(reason?.code ?? reason)));
    const receiver = socketReceiver(${JSON.stringify(APP_TOKEN)}, { clientOptions: { fetch } });
    let authenticated = null;
    receiver.client.on("authenticated", () => { authenticated ??= Date.now(); });
    const second = async () => {
      mock.timers.tick(1000);
      for (let turn = 0; turn < 5; turn += 1) await new Promise((resolve) => setImmediate(resolve));
    };
    receiver.client.emit("close"); // the socket closed: a reconnect is scheduled
    for (let passed = 0; passed < 4800; passed += 1) await second();
    const offline = attempts.length;
    const gaps = attempts.slice(1).map((at, index) => at - attempts[index]);
    online = true;
    const back = Date.now();
    for (let passed = 0; passed < 600 && authenticated === null; passed += 1) await second();
    console.log(JSON.stringify({
      offline, longest: Math.max(...gaps), unhandled, limit: RECONNECT_WAIT_SECONDS,
      waited: authenticated === null ? null : authenticated - back,
    }));
    process.exit(0);
  `;
  const result = spawnSync(process.execPath, ["--input-type=module", "-e", script], {
    cwd: ROOT,
    encoding: "utf8",
  });
  const lines = result.stdout.trim().split("\n");
  const seen = JSON.parse(lines.at(-1) ?? "{}") as {
    offline: number;
    longest: number;
    unhandled: string[];
    limit: number;
    waited: number | null;
  };
  assert.equal(seen.limit, 60);
  // At least one try in each of the 80 minutes.
  assert.ok(seen.offline >= 80, `${seen.offline} tries in 80 minutes`);
  assert.ok(seen.longest <= 60_000, `${seen.longest} ms between two tries`);
  // Nothing was rejected, and the first try with the network back got in.
  assert.deepEqual(seen.unhandled, []);
  assert.ok(seen.waited !== null && seen.waited <= 60_000, `${seen.waited} ms after the network`);
});

// Past the list of waits. `forever` makes the `retry` package (0.13.1, under `p-retry` 4.6.2)
// go on with the last wait once its list is used up, where it would otherwise reject, and the
// client would be left with no connection and nothing trying. Real timers, a few milliseconds
// each: what is checked is the libraries on this Node, with a list of two waits for the 100.
test("retries told to go on for good outlast their list of waits", async () => {
  let online = false;
  let attempts = 0;
  const body = JSON.stringify({ ok: true, url: "wss://localhost.invalid/link" });
  const fetch: FetchFunction = async (url) => {
    attempts += 1;
    if (!online) throw new TypeError("fetch failed");
    return {
      ok: true,
      status: 200,
      statusText: "OK",
      url: String(url),
      headers: { get: () => null, entries: () => [] },
      arrayBuffer: async () => new ArrayBuffer(0),
      json: async () => JSON.parse(body),
      text: async () => body,
    };
  };
  const client = new WebClient("xap" + "p-1", {
    fetch,
    logger: silentLogger(),
    retryConfig: { forever: true, retries: 2, factor: 1, minTimeout: 5, maxTimeout: 5 },
  });
  const answer = client.apiCall("apps.connections.open");
  const deadline = Date.now() + 5_000;
  while (attempts < 12 && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.ok(attempts >= 12, `${attempts} tries: the list of two waits was the end of it`);
  online = true;
  assert.equal((await answer).ok, true);
});
