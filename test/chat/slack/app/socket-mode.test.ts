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
import type { FetchFunction } from "@slack/web-api";
import { socketReceiver } from "../../../../src/chat/slack/app/app.ts";
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
