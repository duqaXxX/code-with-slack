/**
 * Port of `tests/test_sinks.py`, in the file's order.
 *
 * Python's tests waited on the wall clock: a debounce shrunk to 10 ms, `asyncio.sleep` for it to
 * pass. Here every pause of the sink is on a `FakeClock` and a test moves that clock: `settled()`
 * lets a debounce of zero seconds fire and the loop run dry, `passed(seconds)` crosses a pause.
 * A caller Python cancelled (`task.cancel()`) is a caller whose `AbortSignal` aborts.
 */
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, beforeEach, test } from "node:test";
import type { Preview, TaskStatus, TaskUpdate } from "../../../../src/chat/seam.ts";
import { blank, len } from "../../../../src/chat/slack/reply/chars.ts";
import * as sinks from "../../../../src/chat/slack/reply/sinks.ts";
import {
  Cancelled,
  type Limiter,
  type Logger,
  ReplySink,
  type Task,
  UpdateLimiter,
} from "../../../../src/chat/slack/reply/sinks.ts";
import type { Clock } from "../../../../src/clock.ts";
import { STOPPED } from "../../../../src/core/reply/words.ts";
import { StateStore } from "../../../../src/core/state.ts";
import * as texts from "../../../../src/core/texts.ts";
import {
  AsyncEvent,
  BOT,
  CHANNEL,
  FakeClock,
  FakeSlack,
  networkDown,
  OWNER,
  rejected,
  TEAM,
  THREAD,
} from "../../../support/fake-slack.ts";
import { golden, type Json, type JsonObject } from "../../../support/fixtures.ts";

const WRITE_METHODS = [
  "chat.postMessage",
  "chat.startStream",
  "chat.appendStream",
  "chat.stopStream",
  "chat.update",
];

// Every clock a reply of the running test sleeps on: `settled` and `passed` move them all.
let clocks: FakeClock[] = [];

beforeEach(() => {
  clocks = [];
});

/** A clock whose sleep is over at once and moves its own time: a limiter on it never holds a test. */
class SelfPacedClock implements Clock {
  now = 0;

  async sleep(seconds: number, signal?: AbortSignal): Promise<void> {
    if (signal?.aborted) throw signal.reason;
    // Time always moves, as a real clock's does: a pause too short to change a float would
    // otherwise be asked for again without end.
    const least = this.now + Number.EPSILON * Math.max(1, Math.abs(this.now));
    this.now = Math.max(this.now + seconds, seconds > 0 ? least : this.now);
  }

  time(): number {
    return this.now;
  }
}

/** The lines a sink logged, in order: Python's `caplog`. */
class Recorded implements Logger {
  lines: { level: string; message: string }[] = [];

  debug(message: string): void {
    this.lines.push({ level: "DEBUG", message });
  }

  info(message: string): void {
    this.lines.push({ level: "INFO", message });
  }

  warning(message: string): void {
    this.lines.push({ level: "WARNING", message });
  }

  error(message: string): void {
    this.lines.push({ level: "ERROR", message });
  }

  get text(): string {
    return this.lines.map((line) => `${line.level} ${line.message}`).join("\n");
  }

  messages(): string[] {
    return this.lines.map((line) => line.message);
  }
}

interface ReplyOptions {
  limiter?: Limiter;
  clock?: FakeClock;
  onOpenReply?: (old: string | null, fresh: string | null) => void;
  onWrite?: () => void;
  logger?: Logger;
  /** Zero unless a test is about the debounce itself (Python's `fast` fixture: 10 ms). */
  debounceSeconds?: number;
  /** Python's `monkeypatch.setattr(sinks, "FINAL_RETRY_SECONDS", ...)`. */
  finalRetrySeconds?: number;
}

function reply(slack: FakeSlack, options: ReplyOptions = {}): ReplySink {
  const clock = options.clock ?? new FakeClock();
  if (!clocks.includes(clock)) clocks.push(clock);
  return new ReplySink(slack, {
    channel: CHANNEL,
    threadTs: THREAD,
    teamId: TEAM,
    userId: OWNER,
    botUserId: BOT,
    // Python's default limiter paced on the wall clock; this one never makes a test wait.
    limiter: options.limiter ?? new UpdateLimiter({ clock: new SelfPacedClock() }),
    clock,
    onOpenReply: options.onOpenReply,
    onWrite: options.onWrite,
    logger: options.logger ?? new Recorded(),
    debounceSeconds: options.debounceSeconds ?? 0,
    finalRetrySeconds: options.finalRetrySeconds,
  });
}

/** What a test reaches into, as Python's reached `sink._ending` and patched `sink._flush`. */
function inner(sink: ReplySink): { ending: Task<boolean> | null; flush: () => Promise<boolean> } {
  return sink as unknown as { ending: Task<boolean> | null; flush: () => Promise<boolean> };
}

/** The Slack writes a reply made, in order. */
function methods(slack: FakeSlack): string[] {
  return slack.apiCalls.map((call) => call.method).filter((m) => WRITE_METHODS.includes(m));
}

/** One turn of the event loop, which drains every microtask: `asyncio.sleep(0)`. */
function tick(): Promise<void> {
  return new Promise((resolve) => setImmediate(resolve));
}

/**
 * Long enough for a debounced write: the debounce (zero seconds) fires and what it woke runs to
 * its next wait. Five rounds, as Python's 50 ms held five debounces of 10 ms: a pass that finds
 * the reply changed under it debounces again.
 */
async function settled(): Promise<void> {
  for (let round = 0; round < 5; round += 1) {
    for (const clock of clocks) await clock.advance(0);
    await tick();
  }
}

/** `seconds` pass on every clock of the test, and what that woke runs to its next wait. */
async function passed(seconds: number): Promise<void> {
  for (const clock of clocks) await clock.advance(seconds);
}

/**
 * Python's `asyncio.wait_for(awaitable, 1.0)`: the promise must settle without anything else
 * happening. Nothing in these tests waits on a real timer, so one still pending once the loop
 * has turned over would never settle.
 */
async function soon<T>(promise: Promise<T>): Promise<T> {
  let done = false;
  const guarded = promise.finally(() => {
    done = true;
  });
  guarded.catch(() => {});
  for (let round = 0; round < 50 && !done; round += 1) await tick();
  if (!done) throw new Error("still pending: it waits on something that never comes");
  return guarded;
}

function tool(
  id: string,
  name: string,
  status: TaskStatus = "complete",
  fields: Partial<TaskUpdate> = {},
): TaskUpdate {
  return {
    id,
    title: `${name}: ${id}`,
    status,
    details: null,
    output: null,
    name,
    task: false,
    calls: 0,
    preview: null,
    folded: null,
    ...fields,
  };
}

function preview(
  title: string,
  summary: string,
  body: string,
  language: "" | "diff" = "",
  plain = false,
): Preview {
  return { title, summary, body, language, plain };
}

type Maybe = Json | undefined;

/** `value[a][b]...`, a negative number counting from a list's end. */
function get(value: Maybe, ...path: (string | number)[]): Maybe {
  let here = value;
  for (const key of path) {
    if (Array.isArray(here) && typeof key === "number") here = here.at(key);
    else if (typeof here === "object" && here !== null && !Array.isArray(here)) {
      here = here[String(key)];
    } else return undefined;
  }
  return here;
}

function list(value: Maybe): JsonObject[] {
  assert.ok(Array.isArray(value), "not a list");
  return value.map((item) => {
    assert.ok(typeof item === "object" && item !== null && !Array.isArray(item), "not an object");
    return item;
  });
}

function str(value: Maybe): string {
  assert.equal(typeof value, "string");
  return value as string;
}

/** Python's `[x] = items`. */
function only<T>(items: readonly T[]): T {
  assert.equal(items.length, 1);
  return items[0] as T;
}

/** The last element: Python's `items[-1]`. */
function last<T>(items: readonly T[]): T {
  assert.ok(items.length > 0, "an empty list has no last element");
  return items.at(-1) as T;
}

/** Every chunk of every stream call, in order. */
function allChunks(slack: FakeSlack): JsonObject[] {
  return slack.apiCalls.flatMap((call) => ("chunks" in call.args ? list(call.args.chunks) : []));
}

const scratch: string[] = [];

function tmpPath(): string {
  const directory = mkdtempSync(join(tmpdir(), "awd-sinks-"));
  scratch.push(directory);
  return directory;
}

after(() => {
  for (const directory of scratch) rmSync(directory, { recursive: true, force: true });
});

// The stream: what starts it, what grows it, what ends it.

test("the first content starts the stream and nothing comes before it", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  assert.deepEqual(slack.apiCalls, []); // no placeholder: a reply that says nothing writes nothing
  await sink.text("Looking at the files.");
  await settled();
  const start = only(slack.callsTo("chat.startStream"));
  assert.deepEqual(methods(slack), ["chat.startStream"]);
  assert.ok(start.channel === CHANNEL && start.thread_ts === THREAD);
  assert.deepEqual([start.recipient_team_id, start.recipient_user_id], [TEAM, OWNER]);
  assert.equal(start.task_display_mode, "timeline");
  assert.deepEqual(start.chunks, [{ type: "markdown_text", text: "Looking at the files." }]);
});

test("a turn that opens with a tool starts with its card", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  await sink.task(tool("t1", "Bash", "in_progress"));
  await settled();
  const start = only(slack.callsTo("chat.startStream"));
  assert.deepEqual(start.chunks, [
    { type: "task_update", id: "fold:t1", title: "Bash: t1", status: "in_progress" },
  ]);
});

test("text grows the stream and a card updates in place", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  await sink.text("Let me look.\n\n");
  await settled();
  await sink.task(tool("t1", "Bash", "in_progress"));
  await settled();
  await sink.task(tool("t1", "Bash", "complete"));
  await sink.text("The tree is clean.");
  await settled();
  assert.deepEqual(methods(slack), ["chat.startStream", "chat.appendStream", "chat.appendStream"]);
  assert.deepEqual(slack.messageTexts(), ["Let me look.\n\nThe tree is clean."]);
  // one card, which the stream keeps updating where it first appeared
  assert.deepEqual(slack.messageCards(), [
    [{ id: "fold:t1", title: "Bash: t1", status: "complete" }],
  ]);
  const chunks = slack.callsTo("chat.appendStream").flatMap((a) => list(a.chunks));
  assert.deepEqual(
    chunks.map((c) => c.type),
    ["task_update", "task_update", "markdown_text"],
  );
  assert.ok(slack.callsTo("chat.appendStream").every((a) => a.ts === slack.streamTs[0]));
});

test("changes inside one debounce go in one append", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  await sink.text("A");
  await settled();
  for (const piece of ["B", "C", "D"]) await sink.text(piece);
  await sink.task(tool("t1", "Bash", "in_progress"));
  await sink.task(tool("t1", "Bash", "complete"));
  await settled();
  const append = only(slack.callsTo("chat.appendStream"));
  assert.deepEqual(append.chunks, [
    { type: "markdown_text", text: "BCD" },
    { type: "task_update", id: "fold:t1", title: "Bash: t1", status: "complete" },
  ]);
});

test("the end stops the stream with the footer in one push", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  await sink.text("Done.");
  await settled();
  await sink.finish([]);
  assert.equal(await sink.closeOutFormatted("main · ctx 6%"), true);
  const stop = only(slack.callsTo("chat.stopStream"));
  assert.equal(stop.ts, slack.streamTs[0]);
  assert.deepEqual(stop.blocks, [
    { type: "divider" },
    { type: "context", elements: [{ type: "mrkdwn", text: "main · ctx 6%" }] },
  ]);
  assert.deepEqual(methods(slack), ["chat.startStream", "chat.stopStream"]); // no post, no update
  assert.equal(slack.pushes(), 1);
});

test("a reply that ends before its first write starts and stops its stream", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  await sink.text("Done.");
  await sink.finish([]);
  await sink.closeOutFormatted(null);
  assert.deepEqual(methods(slack), ["chat.startStream", "chat.stopStream"]);
  assert.ok(!("blocks" in only(slack.callsTo("chat.stopStream")))); // no footer, nothing to add
  assert.deepEqual(slack.messageTexts(), ["Done."]);
  assert.equal(slack.pushes(), 1);
});

test("a reply with no content writes nothing", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  await sink.finish([]);
  assert.equal(await sink.closeOutFormatted("footer"), true);
  assert.deepEqual(slack.apiCalls, []);
});

test("no write carries a placeholder", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  await sink.text("Working on it.");
  await sink.task(tool("t1", "Bash", "in_progress"));
  await settled();
  await sink.finish([]);
  await sink.closeOutFormatted("footer");
  const written = JSON.stringify(
    slack.apiCalls.filter((call) => WRITE_METHODS.includes(call.method)).map((call) => call.args),
  );
  assert.ok(!written.includes("writing") && !written.includes("Waiting"));
});

test("a stream pushes only when it stops", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  await sink.text("one");
  await settled();
  await sink.text(" two");
  await settled();
  assert.equal(slack.pushes(), 0); // nothing at the start, nothing while it grows
  await sink.finish([]);
  await sink.closeOutFormatted(null);
  assert.equal(slack.pushes(), 1);
});

test("updates are debounced", async () => {
  // Python's wall clock, on the fake one: a debounce of 0.1 s, a change every 10 ms for 0.3 s.
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock, debounceSeconds: 0.1 });
  await sink.text("x");
  await clock.advance(0.15);
  for (let i = 0; i < 30; i += 1) {
    await sink.text(String(i));
    await clock.advance(0.01);
  }
  await clock.advance(0.3);
  const stamps = slack.callsTo("chat.appendStream").length;
  assert.ok(1 <= stamps && stamps <= 6, `${stamps} appends`); // 0.3 s of writing, at most one append per 0.1 s
});

// The 280 seconds: a stream cannot outlive 5 minutes, and a stopped message takes updates.

test("the stream stops at 280 seconds and the message grows by update", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.text("First words.\n\n");
  await sink.task(tool("t1", "Bash", "complete"));
  await settled();
  await clock.advance(sinks.STREAM_SECONDS - 1);
  assert.deepEqual(slack.callsTo("chat.stopStream"), []);
  await clock.advance(2);
  const stop = only(slack.callsTo("chat.stopStream"));
  assert.ok(!("blocks" in stop) && slack.pushes() === 1); // accepted: the stop pushes
  await sink.text("Later words.");
  await settled();
  assert.ok(last(methods(slack)) === "chat.update" && slack.streamTs.length === 1);
  const update = last(slack.callsTo("chat.update"));
  assert.equal(update.ts, slack.streamTs[0]);
  assert.deepEqual(get(update, "blocks", 0), { type: "markdown", text: "First words." });
  assert.ok(
    get(update, "blocks", 1, "type") === "task_card" &&
      get(update, "blocks", 1, "task_id") === "fold:t1",
  );
  assert.deepEqual(get(update, "blocks", 2), { type: "markdown", text: "Later words." });
  assert.equal(update.text, "First words."); // short, the banner of the message
  assert.equal(slack.pushes(), 1); // an update never pushes
});

test("a stream stopped early with nothing changed needs no update", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.text("All of it.");
  await sink.task(tool("t1", "Bash", "complete"));
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  await settled();
  assert.deepEqual(methods(slack), ["chat.startStream", "chat.stopStream"]); // the stream shows it all
});

test("a card running at the switch is updated not left as an error", async () => {
  // A card left in progress in a stopped stream is stored as an error until updated (M33).
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.text("Started a server.");
  await sink.task(
    tool("t1", "Bash", "in_progress", { task: true, details: "Running in background" }),
  );
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  await settled();
  const update = last(slack.callsTo("chat.update"));
  const card = only(list(update.blocks).filter((b) => b.type === "task_card"));
  assert.equal(card.status, "in_progress");
});

test("an answer that is text alone ends on a footer only message", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.text("**Bold** answer, with a [link](https://example.com).\n\nSecond paragraph.");
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  await sink.text(" More.");
  await sink.finish([]);
  assert.equal(await sink.closeOutFormatted("main · ctx 6%"), true);
  const closing = only(slack.callsTo("chat.postMessage"));
  assert.ok(closing.thread_ts === THREAD && closing.unfurl_links === false);
  assert.deepEqual(closing.blocks, [
    { type: "divider" },
    { type: "context", elements: [{ type: "mrkdwn", text: "main · ctx 6%" }] },
  ]);
  // Claude's own words, plain: never a line of the daemon's
  assert.equal(closing.text, "Bold answer, with a link.");
  assert.equal(slack.pushes(), 2); // the stop at 280 s, and this one
  // nothing is moved (the first message would keep nothing): the footer lives in the closing
  // message, and the body message carries none
  assert.ok(list(last(slack.callsTo("chat.update")).blocks).every((b) => b.type !== "divider"));
});

/** A reply past the stream's 280 seconds: words, a call, then the answer Claude ends on. */
async function longReply(slack: FakeSlack, options: ReplyOptions = {}): Promise<ReplySink> {
  const clock = new FakeClock();
  const sink = reply(slack, { ...options, clock });
  await sink.text("Let me check the build.");
  await sink.task(tool("t1", "Edit"));
  await sink.text("The build passed.\n\n- 214 tests\n- no failures");
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  await sink.finish([]);
  return sink;
}

const ENDING = { type: "markdown", text: "The build passed.\n\n- 214 tests\n- no failures" };

/** The kinds of block the reply's first message shows last. */
function kept(slack: FakeSlack): Json[] {
  const first = slack.messages.get(slack.streamTs[0] ?? "");
  assert.ok(first !== undefined);
  return first.blocks.map((b) => b.type ?? null);
}

test("the end after the switch posts the ending with the footer", async () => {
  const slack = new FakeSlack();
  const sink = await longReply(slack);
  assert.equal(await sink.closeOutFormatted("main · ctx 6%"), true);
  // The text Claude wrote after its last call, whole, and the footer, as the message that
  // notifies: its text says how the work ended (the stream's own stop said how it began).
  const ending = only(slack.callsTo("chat.postMessage"));
  assert.ok(ending.thread_ts === THREAD && ending.unfurl_links === false);
  assert.deepEqual(ending.blocks, [
    ENDING,
    { type: "divider" },
    sinks.contextBlock("main · ctx 6%"),
  ]);
  assert.equal(ending.text, "The build passed.");
  assert.equal(slack.pushes(), 2); // the stop at 280 s, and this one
  // The message it grew in keeps the rest, silently: nothing shows twice, and no footer there.
  assert.deepEqual(slack.messageTexts(), ["Let me check the build.", ENDING.text]);
  assert.deepEqual(kept(slack), ["markdown", "context"]); // its words, and the call as a line of counts
});

test("an ending takes what follows it", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.task(tool("t1", "Edit"));
  await sink.text("The edit is done.");
  await sink.text("_2 messages were not sent._", { notice: true });
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  await sink.finish([]);
  assert.equal(await sink.closeOutFormatted("footer"), true);
  const ending = only(slack.callsTo("chat.postMessage"));
  assert.deepEqual(
    list(ending.blocks).map((b) => b.type),
    ["markdown", "markdown", "divider", "context"],
  );
  assert.equal(get(ending, "blocks", 0, "text"), "The edit is done.");
  assert.equal(ending.text, "The edit is done."); // never a line of the daemon's
  assert.ok(!kept(slack).includes("markdown")); // the call stays, the words moved
});

test("an ending that slack did not take is posted once by the retry", async () => {
  const slack = new FakeSlack();
  const sink = await longReply(slack, { finalRetrySeconds: 0.02 });
  const answer = slack.responses["chat.postMessage"] as JsonObject;
  slack.responses["chat.postMessage"] = [rejected("ratelimited"), answer];
  assert.equal(await sink.closeOutFormatted("footer"), false);
  // Not posted: the answer stays where it was, never nowhere.
  assert.deepEqual(slack.messageTexts(), [`Let me check the build.\n\n${ENDING.text}`]);
  await passed(0.02);
  assert.equal(await soon(sink.waitLanded()), true);
  assert.deepEqual(slack.messageTexts(), ["Let me check the build.", ENDING.text]);
  assert.ok(slack.postedTs.length === 1 && slack.pushes() === 2);
});

test("an ending left twice by a failed edit has not landed until the retry", async () => {
  const slack = new FakeSlack();
  const sink = await longReply(slack, { finalRetrySeconds: 0.02 });
  slack.responses["chat.update"] = [rejected("ratelimited"), { ok: true }];
  assert.equal(await sink.closeOutFormatted("footer"), false); // posted, still shown in the first message
  assert.ok(slack.messageTexts()[1] === ENDING.text && kept(slack).slice(2).includes("markdown"));
  await passed(0.02);
  assert.equal(await soon(sink.waitLanded()), true);
  assert.deepEqual(slack.messageTexts(), ["Let me check the build.", ENDING.text]);
  assert.equal(slack.postedTs.length, 1);
});

/**
 * A reply whose `closeOut` caller is cancelled while Slack holds the end's write open (released
 * by setting the returned event).
 */
async function cancelledInsideTheEnd(
  slack: FakeSlack,
  options: ReplyOptions = {},
): Promise<[ReplySink, AsyncEvent]> {
  const sink = reply(slack, options);
  await sink.text("Hello.");
  await settled();
  await sink.finish([]);
  const gate = new AsyncEvent();
  slack.gate = gate;
  slack.gated.clear();
  const caller = new AbortController();
  const closing = sink.closeOutFormatted("footer", caller.signal);
  await slack.gated.wait();
  caller.abort(new Cancelled());
  await assert.rejects(closing, Cancelled);
  return [sink, gate];
}

test("a close out whose caller is cancelled still lands and resolves", async () => {
  const slack = new FakeSlack();
  const [sink, gate] = await cancelledInsideTheEnd(slack);
  slack.gate = null;
  gate.set();
  assert.equal(await soon(sink.waitLanded()), true);
  assert.equal(await sink.closeOutFormatted("footer"), true);
});

test("a close out cancelled over a write slack refused still gets its retry", async () => {
  const slack = new FakeSlack();
  slack.responses["chat.stopStream"] = [rejected("ratelimited"), { ok: true }];
  const [sink, gate] = await cancelledInsideTheEnd(slack, { finalRetrySeconds: 0.02 });
  slack.gate = null;
  gate.set();
  // The refused end schedules its retry; the pause before it passes.
  await inner(sink).ending?.settled();
  await passed(0.02);
  assert.equal(await soon(sink.waitLanded()), true);
  assert.equal(slack.callsTo("chat.stopStream").length, 2); // the refused write, then the retry
});

test("a settle behind a refused end leaves no retry alive", async () => {
  const slack = new FakeSlack();
  // Slack refuses the end and then the shutdown's own write: that shutdown has lost the reply
  // and nothing may write it afterwards.
  slack.responses["chat.stopStream"] = [rejected("ratelimited"), rejected("ratelimited")];
  const [sink, gate] = await cancelledInsideTheEnd(slack, { finalRetrySeconds: 0.02 });
  const settling = sink.settle(); // queues behind the end's write
  await tick();
  slack.gate = null;
  gate.set();
  assert.equal(await soon(settling), false);
  const writes = slack.apiCalls.length;
  await passed(0.1); // well past FINAL_RETRY_SECONDS
  assert.equal(slack.apiCalls.length, writes); // nothing goes out after `settle` returned
});

test("a settle behind an end cancelled itself does not raise", async () => {
  const slack = new FakeSlack();
  const [sink, gate] = await cancelledInsideTheEnd(slack);
  const settling = sink.settle(); // waits for the end in flight
  await tick();
  const ending = inner(sink).ending;
  assert.ok(ending !== null);
  ending.cancel(); // the end's own task is cancelled: `settle` itself was not
  slack.gate = null;
  gate.set();
  await soon(settling); // no cancellation reaches the caller of `settle`
  assert.equal(await soon(sink.waitLanded()), false);
});

test("an end that raises still resolves landed", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  await sink.text("Hello.");
  await settled();
  await sink.finish([]);
  inner(sink).flush = async () => {
    throw new RangeError("boom");
  };
  await assert.rejects(sink.closeOutFormatted("footer"), RangeError);
  assert.equal(await soon(sink.waitLanded()), false);
  assert.equal(await sink.closeOutFormatted("footer"), false);
});

test("an ending cut off while it is posted is posted before anything is shortened", async () => {
  const slack = new FakeSlack();
  const sink = await longReply(slack);
  // Slack holds the ending's post open, where Python's fake answered 50 ms late.
  const gate = new AsyncEvent();
  slack.gate = gate;
  slack.gateMethod = "chat.postMessage";
  slack.gated.clear();
  const caller = new AbortController();
  const closing = sink.closeOutFormatted("footer", caller.signal);
  await slack.gated.wait(); // the ending's post is out
  caller.abort(new Cancelled());
  await closing.catch(() => {});
  assert.deepEqual(slack.postedTs, []); // cut off before Slack took it
  const writes = slack.apiCalls.length;
  const settling = sink.settle(); // a shutdown's last pass
  slack.gate = null;
  gate.set();
  assert.equal(await soon(settling), true);
  const order = slack.apiCalls
    .slice(writes)
    .map((call) => call.method)
    .filter((m) => m === "chat.postMessage" || m === "chat.update");
  assert.equal(order[0], "chat.postMessage"); // posted first: never nowhere
  assert.deepEqual(slack.messageTexts(), ["Let me check the build.", ENDING.text]);
  assert.equal(slack.postedTs.length, 1);
});

test("the ending follows the running counts by edits", async () => {
  const slack = new FakeSlack();
  const sink = await longReply(slack);
  await sink.closeOutFormatted("footer");
  const ts = slack.postedTs[0];
  await sink.setRunning("⏳ 1 shell");
  await settled();
  const edit = last(slack.callsTo("chat.update"));
  assert.equal(edit.ts, ts);
  assert.deepEqual(get(edit, "blocks", -1), sinks.contextBlock("footer · ⏳ 1 shell"));
  await sink.setRunning("");
  await settled();
  assert.deepEqual(
    get(last(slack.callsTo("chat.update")), "blocks", -1),
    sinks.contextBlock("footer"),
  );
  assert.ok(slack.postedTs.length === 1 && slack.pushes() === 2); // an edit never pushes
});

test("a reply says its footer shows only once its end has landed", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack, { finalRetrySeconds: 0.02 });
  await sink.text("Done.");
  await sink.finish([]);
  assert.equal(sink.footerShown, false);
  slack.responses["chat.stopStream"] = [rejected("ratelimited"), { ok: true }];
  assert.equal(await sink.closeOutFormatted("footer"), false);
  assert.equal(sink.footerShown, false); // no footer on Slack yet: the status line still counts
  await passed(0.02);
  assert.equal(await soon(sink.waitLanded()), true);
  assert.equal(sink.footerShown, true);
});

test("the closing message text skips the daemon s own lines", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.text("_Compacted the conversation._\n\n", { notice: true });
  await sink.text("The real answer & more.");
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  await sink.finish([]);
  await sink.closeOutFormatted(null);
  const closing = only(slack.callsTo("chat.postMessage"));
  assert.equal(closing.text, "The real answer &amp; more.");
  assert.deepEqual(closing.blocks, [sinks.contextBlock(sinks.ZERO_WIDTH_SPACE)]); // no footer to show
});

const ERROR_LINE = "Claude Code reported an error: `the Claude Code process exited`";

test("a reply cut short after its stream stopped ends with the daemon s line", async () => {
  // Issue #165, seen live on 2026-10-08: the reply's last part was a tool call, the turn had no
  // footer, and the message that notified showed empty while the error sat in an edit.
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.task(tool("t1", "Bash"));
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  await sink.text(`\n\n${ERROR_LINE}`, { ending: true });
  await sink.finish([]);
  await sink.closeOutFormatted(null);
  const closing = only(slack.callsTo("chat.postMessage"));
  assert.deepEqual(closing.blocks, [{ type: "markdown", text: ERROR_LINE }]);
  assert.equal(closing.text, sinks.bannerText(ERROR_LINE, { limit: sinks.BANNER_LIMIT }));
  const stayed = list(last(slack.callsTo("chat.update")).blocks);
  // moved, not shown twice
  assert.ok(stayed.every((block) => !JSON.stringify(block).includes(ERROR_LINE)));
});

test("an answer of text alone cut short ends with the daemon s line", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.text("Half an answer");
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  await sink.text(`\n\n${ERROR_LINE}`, { ending: true });
  await sink.finish([]);
  await sink.closeOutFormatted(null);
  const closing = only(slack.callsTo("chat.postMessage"));
  assert.deepEqual(closing.blocks, [{ type: "markdown", text: ERROR_LINE }]);
  assert.deepEqual(last(slack.callsTo("chat.update")).blocks, [
    { type: "markdown", text: "Half an answer" },
  ]);
});

test("a reply cut short while a task still counts ends with the daemon s line", async () => {
  // The session shows the running list before it closes a reply and empties it once the lost
  // process's tasks are stopped: the list is no footer, and the closing message it made
  // ended up empty when the list did.
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.task(tool("t1", "Bash"));
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  await sink.text(`\n\n${ERROR_LINE}`, { ending: true });
  await sink.setRunning("⏳ 1 agent");
  await sink.finish([]);
  await sink.closeOutFormatted(null);
  const closing = only(slack.callsTo("chat.postMessage"));
  assert.deepEqual(closing.blocks, [
    { type: "markdown", text: ERROR_LINE },
    { type: "divider" },
    sinks.contextBlock("⏳ 1 agent"),
  ]);
  assert.equal(closing.text, sinks.bannerText(ERROR_LINE, { limit: sinks.BANNER_LIMIT }));
  await sink.setRunning("");
  await settled();
  assert.deepEqual(last(slack.messageBlocks()), [{ type: "markdown", text: ERROR_LINE }]);
});

test("an answer of text alone under a daemon line cut short ends with the error", async () => {
  // A compaction at the turn's start writes its line before the answer: the text is then no
  // ending (only a line of the daemon's would stay above it), and the error still is one.
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.text(`${texts.COMPACTED_PLAIN}\n\n`, { notice: true });
  await sink.text("Half an answer");
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  await sink.text(`\n\n${ERROR_LINE}`, { ending: true });
  await sink.finish([]);
  await sink.closeOutFormatted(null);
  const closing = only(slack.callsTo("chat.postMessage"));
  assert.deepEqual(closing.blocks, [{ type: "markdown", text: ERROR_LINE }]);
  assert.deepEqual(last(slack.callsTo("chat.update")).blocks, [
    { type: "markdown", text: texts.COMPACTED_PLAIN },
    { type: "markdown", text: "Half an answer" },
  ]);
});

test("only the line on how the reply ended moves not a notice written before it", async () => {
  // Two notices in a row were one part: the compaction line moved with the error and was the
  // notification's text.
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.task(tool("t1", "Bash"));
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  await sink.text(`\n\n${texts.COMPACTED_PLAIN}\n\n`, { notice: true });
  await sink.text(`\n\n${ERROR_LINE}`, { ending: true });
  await sink.finish([]);
  await sink.closeOutFormatted(null);
  const closing = only(slack.callsTo("chat.postMessage"));
  assert.deepEqual(closing.blocks, [{ type: "markdown", text: ERROR_LINE }]);
  assert.equal(closing.text, sinks.bannerText(ERROR_LINE, { limit: sinks.BANNER_LIMIT }));
  assert.ok(
    JSON.stringify(last(slack.callsTo("chat.update")).blocks).includes(texts.COMPACTED_PLAIN),
  );
});

test("a card that closes after the line on how the reply ended moves under it", async () => {
  // Two calls open when the turn is cut: closing them draws a card after the error line, which
  // was then no longer the reply's last part, and the closing message showed empty.
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.task(tool("t1", "Bash"));
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  await sink.text(`\n\n${ERROR_LINE}`, { ending: true });
  await sink.task(tool("t2", "Read"));
  await sink.finish([]);
  await sink.closeOutFormatted(null);
  const closing = only(slack.callsTo("chat.postMessage"));
  assert.deepEqual(get(closing, "blocks", 0), { type: "markdown", text: ERROR_LINE });
  // folded, as a run that ended
  assert.ok(JSON.stringify(list(closing.blocks).slice(1)).includes("Read 1 file"));
  // the error, not the card's title, is what the notification says
  assert.equal(closing.text, sinks.bannerText(ERROR_LINE, { limit: sinks.BANNER_LIMIT }));
});

test("a daemon line that does not say how the reply ended never moves", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.task(tool("t1", "Bash"));
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  await sink.text(`\n\n${texts.COMPACTED_PLAIN}`, { notice: true });
  await sink.finish([]);
  await sink.closeOutFormatted(null);
  const closing = only(slack.callsTo("chat.postMessage"));
  assert.deepEqual(closing.blocks, [sinks.contextBlock(sinks.ZERO_WIDTH_SPACE)]);
  assert.ok(
    JSON.stringify(last(slack.callsTo("chat.update")).blocks).includes(texts.COMPACTED_PLAIN),
  );
});

test("the line on how the reply ended moves from under a notice alone", async () => {
  // Nothing of the answer above it, only a line of the daemon's: that line stays, and the
  // message that notifies holds the error where it showed empty.
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.text(`${texts.COMPACTED_PLAIN}\n\n`, { notice: true });
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  await sink.text(`\n\n${ERROR_LINE}`, { ending: true });
  await sink.finish([]);
  await sink.closeOutFormatted(null);
  const closing = only(slack.callsTo("chat.postMessage"));
  assert.deepEqual(closing.blocks, [{ type: "markdown", text: ERROR_LINE }]);
  assert.deepEqual(last(slack.callsTo("chat.update")).blocks, [
    { type: "markdown", text: texts.COMPACTED_PLAIN },
  ]);
});

test("the line on how the reply ended moves with a footer an earlier turn left", async () => {
  // A report turn cut short renders into a reply that already has its first turn's footer:
  // the footer alone was the ending, and the error stayed in a silent edit.
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.task(tool("t1", "Bash"));
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  await sink.text(`\n\n${ERROR_LINE}`, { ending: true });
  await sink.finish([]);
  await sink.closeOutFormatted("footer");
  const closing = only(slack.callsTo("chat.postMessage"));
  assert.deepEqual(closing.blocks, [
    { type: "markdown", text: ERROR_LINE },
    { type: "divider" },
    sinks.contextBlock("footer"),
  ]);
  assert.equal(closing.text, sinks.bannerText(ERROR_LINE, { limit: sinks.BANNER_LIMIT }));
});

test("a last message that opens with the line on how the reply ended is the ending", async () => {
  // The first message is full to the character, so the whole error line goes on in a
  // continuation, which rings with it: a closing message after it showed empty and rang a
  // third time. With any room left the line is cut where the room ends, as any text is.
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.text("w".repeat(sinks.MESSAGE_LIMIT));
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  // The first message holds it all, in one stream, and no more.
  assert.ok(slack.streamTs.length === 1 && slack.postedTs.length === 0);
  await sink.text(`\n\n${ERROR_LINE}`, { ending: true });
  await sink.finish([]);
  await sink.closeOutFormatted(null);
  await settled();
  const continuation = only(slack.callsTo("chat.postMessage")); // and no closing message after it
  assert.deepEqual(continuation.blocks, [{ type: "markdown", text: ERROR_LINE }]);
  assert.equal(continuation.text, sinks.bannerText(ERROR_LINE, { limit: sinks.BANNER_LIMIT }));
  assert.deepEqual(last(slack.messageBlocks()), continuation.blocks);
});

test("a last message that opens after the line on how the reply ended is the ending", async () => {
  // The line fits the first message and the note under it does not: the continuation starts
  // in the note, past the line, and a closing message after it showed empty all the same.
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.text("w".repeat(sinks.MESSAGE_LIMIT - `\n\n${ERROR_LINE}`.length));
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  await sink.text(`\n\n${ERROR_LINE}`, { ending: true });
  await sink.text("\n\n_3 messages were not sent: Claude Code stopped._", { notice: true });
  await sink.finish([]);
  await sink.closeOutFormatted(null);
  await settled();
  const continuation = only(slack.callsTo("chat.postMessage")); // and no closing message after it
  assert.ok(JSON.stringify(continuation.blocks).includes("were not sent"));
  assert.ok(!JSON.stringify(slack.messageBlocks()).includes(sinks.ZERO_WIDTH_SPACE));
});

test("a daemon line stays in the reply when the footer is the ending", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.task(tool("t1", "Bash"));
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  await sink.text("\n\n_Stopped._", { notice: true });
  await sink.finish([]);
  await sink.closeOutFormatted("footer");
  const closing = only(slack.callsTo("chat.postMessage"));
  assert.deepEqual(closing.blocks, [{ type: "divider" }, sinks.contextBlock("footer")]);
});

test("a stream slack closed first falls back to updates", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  await sink.text("one");
  await settled();
  slack.expire(slack.streamTs[0] ?? ""); // Slack ended it, at 5 minutes, before the daemon's timer
  await sink.text(" two");
  await settled();
  assert.equal(get(last(slack.callsTo("chat.appendStream")), "chunks", 0, "text"), " two"); // refused
  const update = last(slack.callsTo("chat.update"));
  assert.deepEqual(update.blocks, [{ type: "markdown", text: "one two" }]);
  await sink.finish([]);
  await sink.closeOutFormatted("footer");
  assert.equal(slack.callsTo("chat.postMessage").length, 1); // the closing message, as after 280 s
});

test("a stop refused because the stream expired still ends the reply", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  await sink.text("one");
  await settled();
  slack.expire(slack.streamTs[0] ?? "");
  await sink.finish([]);
  assert.equal(await sink.closeOutFormatted("footer"), true);
  assert.equal(slack.callsTo("chat.postMessage").length, 1);
  assert.equal(slack.pushes(), 2); // Slack's own stop, then the closing message
});

// Limits: a message holds so much, and the reply goes on in the next.

test("text past the limit continues in a new stream", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  const body = "a line of text\n".repeat(1_000); // 15,000 characters
  await sink.text(body);
  await settled();
  await sink.finish([]);
  await sink.closeOutFormatted("footer");
  assert.ok(slack.streamTs.length === 2 && slack.postedTs.length === 0);
  const [first, second] = slack.messageTexts();
  assert.ok(first !== undefined && second !== undefined);
  assert.ok(first.length <= sinks.MESSAGE_LIMIT && second.length <= sinks.MESSAGE_LIMIT);
  assert.equal(`${first}\n${second}`, body.replace(/^\n+|\n+$/g, "")); // nothing lost at the cut
  const stops = slack.callsTo("chat.stopStream");
  assert.deepEqual(
    stops.map((s) => "blocks" in s),
    [false, true],
  ); // the footer only ends the reply
  assert.equal(slack.pushes(), 2); // each extra message pushes (accepted)
});

test("cards past the limit continue in a new stream", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  for (let i = 0; i < 60; i += 1) {
    await sink.task(tool(`t${i}`, "Agent", "complete", { task: true })); // each has a card of its own
  }
  await settled();
  await sink.finish([]);
  await sink.closeOutFormatted(null);
  assert.deepEqual(
    slack.messageCards().map((cards) => cards.length),
    [sinks.BLOCKS_LIMIT, 15],
  );
});

test("a reply past the limit after the window continues in a post", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.text("start\n");
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  await sink.text("a line of text\n".repeat(1_000));
  await settled();
  assert.ok(slack.streamTs.length === 1 && slack.postedTs.length === 1);
  const [first, second] = slack.messageTexts();
  assert.ok(first !== undefined && second !== undefined);
  assert.ok(first.length <= sinks.MESSAGE_LIMIT && second.length <= sinks.MESSAGE_LIMIT);
  await sink.finish([]);
  await sink.closeOutFormatted("footer");
  assert.equal(slack.postedTs.length, 2); // the closing message follows the continuation
});

test("a message never holds more blocks than slack allows", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.text("go\n");
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  for (let i = 0; i < 80; i += 1) {
    // text and cards alternating: every one is a block of its own
    await sink.text(`step ${i}`);
    await sink.task(tool(`t${i}`, "Read"));
  }
  await sink.finish([]);
  await sink.closeOutFormatted("footer");
  await settled();
  for (const message of slack.messageBlocks()) assert.ok(message.length <= 50);
  for (const call of slack.apiCalls) {
    if (call.method === "chat.update" || call.method === "chat.postMessage") {
      assert.ok(list(call.args.blocks).length <= 50);
    }
  }
});

test("a long reply keeps the text of an update short", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.text(`${"word ".repeat(60)}\n`);
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  await sink.text("tail ".repeat(2_000));
  await settled();
  for (const call of slack.apiCalls) {
    if (call.method === "chat.update" || call.method === "chat.postMessage") {
      assert.ok(str(call.args.text).length <= sinks.BANNER_LIMIT);
    }
  }
});

// What follows the end: a late change edits the stopped message, silently.

test("a task that ends after the reply edits the stopped message", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  await sink.text("Started it.");
  await sink.task(
    tool("t1", "Bash", "in_progress", { task: true, details: "Running in background" }),
  );
  await settled();
  await sink.finish([]);
  await sink.closeOutFormatted("footer");
  assert.equal(slack.pushes(), 1);
  await sink.task(tool("t1", "Bash", "complete", { task: true }));
  await settled();
  const update = last(slack.callsTo("chat.update"));
  assert.equal(update.ts, slack.streamTs[0]);
  const card = only(list(update.blocks).filter((b) => b.type === "task_card"));
  assert.equal(card.status, "complete");
  assert.equal(get(update, "blocks", -1, "elements", 0, "text"), "footer"); // the footer stays
  assert.equal(slack.pushes(), 1);
});

test("running counts join the footer of a stopped stream", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  await sink.text("Done.");
  await sink.finish([]);
  await sink.closeOutFormatted("main · ctx 6%");
  await sink.setRunning("⏳ 1 shell");
  await settled();
  const update = last(slack.callsTo("chat.update"));
  assert.equal(get(update, "blocks", -1, "elements", 0, "text"), "main · ctx 6% · ⏳ 1 shell");
  await sink.setRunning("");
  await settled();
  assert.equal(
    get(last(slack.callsTo("chat.update")), "blocks", -1, "elements", 0, "text"),
    "main · ctx 6%",
  );
});

test("unchanged running counts write nothing", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  await sink.text("Done.");
  await sink.finish([]);
  await sink.closeOutFormatted("footer");
  const before = slack.apiCalls.length;
  await sink.setRunning("");
  await sink.setRunning("");
  await settled();
  assert.equal(slack.apiCalls.length, before);
});

test("a reply that is no longer latest keeps its footer and drops the counts", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  await sink.text("Done.");
  await sink.finish([]);
  await sink.closeOutFormatted("footer");
  await sink.setRunning("⏳ 1 shell");
  await settled();
  assert.deepEqual(
    get(last(slack.callsTo("chat.update")), "blocks", -1),
    sinks.contextBlock("footer · ⏳ 1 shell"),
  );
  await sink.setLatest(false);
  await settled();
  // The footer is the record of how this turn ended; what still runs is said once, at the
  // bottom of the thread, by the latest reply.
  const update = last(slack.callsTo("chat.update"));
  assert.deepEqual(
    list(update.blocks).map((b) => b.type),
    ["markdown", "divider", "context"],
  );
  assert.deepEqual(get(update, "blocks", -1), sinks.contextBlock("footer"));
});

test("the closing message follows running counts and latest", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.text("Done.");
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  await sink.finish([]);
  await sink.closeOutFormatted("footer");
  const ts = slack.postedTs[0];
  await sink.setRunning("⏳ 1 shell");
  await settled();
  const edit = last(slack.callsTo("chat.update"));
  assert.equal(edit.ts, ts);
  assert.equal(get(edit, "blocks", -1, "elements", 0, "text"), "footer · ⏳ 1 shell");
  assert.equal(edit.text, "Done."); // an edit never pushes: the text stays Claude's words
  await sink.setLatest(false);
  await settled();
  // no longer the latest: the counts go, the footer stays
  assert.deepEqual(last(slack.callsTo("chat.update")).blocks, [
    { type: "divider" },
    sinks.contextBlock("footer"),
  ]);
  assert.ok(slack.postedTs.length === 1 && slack.callsTo("chat.delete").length === 0);
});

test("a second close out call is a no op", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  await sink.text("Done.");
  await sink.finish([]);
  await sink.closeOutFormatted("one");
  const before = slack.apiCalls.length;
  await sink.closeOutFormatted("two");
  assert.equal(slack.apiCalls.length, before);
});

// Failures never raise, and the end is tried once more.

test("a slack failure never raises", async () => {
  const slack = new FakeSlack();
  slack.responses["chat.startStream"] = networkDown();
  const sink = reply(slack);
  await sink.text("hello");
  await sink.task(tool("t1", "Bash"));
  await settled();
  await sink.finish([]);
  assert.equal(await sink.closeOutFormatted("footer"), false);
  await sink.settle();
});

test("a failed append is sent with the next flush", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  await sink.text("A");
  await settled();
  slack.responses["chat.appendStream"] = [networkDown(), { ok: true }];
  await sink.text("B");
  await settled();
  await sink.text("C");
  await settled();
  assert.deepEqual(slack.messageTexts(), ["ABC"]); // B was not lost, nor sent twice
});

test("a failed start is tried again with everything since", async () => {
  const slack = new FakeSlack();
  slack.responses["chat.startStream"] = [
    networkDown(),
    slack.responses["chat.startStream"] as JsonObject,
  ];
  const sink = reply(slack);
  await sink.text("one");
  await settled();
  assert.deepEqual(slack.streamTs, []);
  await sink.text(" two");
  await settled();
  assert.deepEqual(slack.messageTexts(), ["one two"]);
});

test("a final write lost to the network is tried again once", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack, { finalRetrySeconds: 0.01 });
  await sink.text("Done.");
  await settled();
  slack.responses["chat.stopStream"] = [networkDown(), { ok: true }];
  await sink.finish([]);
  assert.equal(await sink.closeOutFormatted("footer"), false);
  await passed(0.01);
  assert.equal(await soon(sink.waitLanded()), true);
  assert.equal(slack.callsTo("chat.stopStream").length, 2);
});

test("a final write that fails twice is reported lost", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack, { finalRetrySeconds: 0.01 });
  await sink.text("Done.");
  await settled();
  slack.responses["chat.stopStream"] = networkDown();
  await sink.finish([]);
  assert.equal(await sink.closeOutFormatted("footer"), false);
  await passed(0.01);
  assert.equal(await soon(sink.waitLanded()), false);
});

test("settle writes what is still debounced and retries the end", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  await sink.text("one");
  assert.equal(await sink.settle(), true); // nothing waits a debounce at a shutdown
  assert.deepEqual(slack.messageTexts(), ["one"]);
  slack.responses["chat.stopStream"] = [networkDown(), { ok: true }];
  await sink.finish([]);
  assert.equal(await sink.closeOutFormatted(null), false);
  assert.equal(await sink.settle(), true); // the retry still waiting is tried now
  assert.equal(await soon(sink.waitLanded()), true);
});

test("an error is logged without message content", async () => {
  const slack = new FakeSlack();
  const log = new Recorded();
  const sink = reply(slack, { logger: log });
  await sink.text("first");
  await settled();
  slack.responses["chat.appendStream"] = rejected("ratelimited");
  await sink.text(" the owner's secret content");
  await settled();
  assert.ok(log.text.includes("ratelimited"));
  assert.ok(!log.text.includes("secret content"));
});

test("a refused rewrite never replaces a message that shows its body", async () => {
  // A reply that ended inline already shows its whole body. Slack refusing a later rewrite of
  // it (the counts joining its footer, say) must leave that body as it is: no plain-text
  // fallback.
  const slack = new FakeSlack();
  const sink = reply(slack);
  await sink.text("line of text\n".repeat(400));
  await sink.task(tool("t1", "Bash"));
  await sink.finish([]);
  assert.ok(await sink.closeOutFormatted("footer"));
  slack.responses["chat.update"] = rejected("invalid_blocks");
  await sink.setRunning("⏳ 1 shell");
  await settled();
  const updates = slack.callsTo("chat.update");
  assert.ok(updates.length > 0 && updates.every((u) => list(u.blocks).length > 0));
  const tried = updates.length;
  await settled();
  assert.equal(slack.callsTo("chat.update").length, tried); // the change is dropped, not retried
  slack.responses["chat.update"] = { ok: true };
  await sink.setRunning("⏳ 2 shells"); // a later change is tried again
  await settled();
  assert.equal(slack.callsTo("chat.update").length, tried + 1);
});

test("a refused continuation post is posted as plain text", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.text("start\n");
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  slack.responses["chat.postMessage"] = [
    rejected("invalid_blocks"),
    rejected("invalid_blocks"),
    { ok: true, ts: "9.9" },
  ];
  await sink.text("a line of text\n".repeat(1_000));
  await sink.finish([]);
  await sink.closeOutFormatted(null);
  const plain = slack.callsTo("chat.postMessage").filter((p) => !("blocks" in p));
  assert.ok(plain.length > 0 && str(plain[0]?.text).startsWith("a line of text"));
});

test("a refused draft update is not retried as plain text", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.text("partial");
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  slack.responses["chat.update"] = rejected("invalid_blocks");
  await sink.text(" more");
  await settled();
  assert.ok(
    slack.callsTo("chat.update").every((a) => !Array.isArray(a.blocks) || a.blocks.length > 0),
  );
});

test("a rate limited update is never turned into plain text", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  await sink.text("one");
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  await sink.text(" two");
  slack.responses["chat.update"] = rejected("ratelimited");
  await sink.finish([]);
  await sink.closeOutFormatted(null);
  assert.ok(
    slack.callsTo("chat.update").every((a) => !Array.isArray(a.blocks) || a.blocks.length > 0),
  );
});

// Crash repair's bookkeeping: the message that a crash would leave unfinished.

test("the open reply is the stream until the end lands", async () => {
  const slack = new FakeSlack();
  const seen: [string | null, string | null][] = [];
  const sink = reply(slack, { onOpenReply: (old, fresh) => seen.push([old, fresh]) });
  await sink.text("x".repeat(sinks.MESSAGE_LIMIT + 10)); // two streams
  await settled();
  const [first, second] = slack.streamTs;
  assert.ok(first !== undefined && second !== undefined && slack.streamTs.length === 2);
  // the first is whole once the second starts (its cards are final): only the last stays open
  assert.deepEqual(seen, [
    [null, first],
    [null, second],
    [first, null],
  ]);
  await sink.finish([]);
  assert.deepEqual(last(seen), [first, null]); // still open: the reply has not ended
  await sink.closeOutFormatted("footer");
  assert.deepEqual(last(seen), [second, null]);
});

test("two sinks never step on each other s entry", async () => {
  const slack = new FakeSlack();
  const directory = tmpPath();
  const store = new StateStore(join(directory, "state.json"));
  store.bind(CHANNEL, directory);
  store.openThread(CHANNEL, THREAD);
  const track = (old: string | null, fresh: string | null): void => {
    store.replaceOpenReply(CHANNEL, THREAD, old, fresh);
  };
  const limiter = new UpdateLimiter({ clock: new SelfPacedClock() });
  const a = reply(slack, { limiter, onOpenReply: track });
  const b = reply(slack, { limiter, onOpenReply: track });
  await a.text("A");
  await a.settle();
  await b.text("B");
  await b.settle();
  assert.deepEqual(store.thread(CHANNEL, THREAD)?.openReplies, slack.streamTs);
  await a.finish([]);
  await a.closeOutFormatted(null);
  assert.deepEqual(store.thread(CHANNEL, THREAD)?.openReplies, [slack.streamTs[1]]);
});

test("a failed end keeps the reply tracked until the retry lands", async () => {
  const slack = new FakeSlack();
  const seen: [string | null, string | null][] = [];
  const sink = reply(slack, {
    finalRetrySeconds: 0.01,
    onOpenReply: (old, fresh) => seen.push([old, fresh]),
  });
  await sink.text("Done.");
  await settled();
  const ts = slack.streamTs[0];
  assert.ok(ts !== undefined);
  slack.responses["chat.stopStream"] = [networkDown(), { ok: true }];
  await sink.finish([]);
  await sink.closeOutFormatted(null);
  assert.deepEqual(seen, [[null, ts]]); // the stream is still open: repair must still find it
  await passed(0.01);
  await soon(sink.waitLanded());
  assert.deepEqual(last(seen), [ts, null]);
});

test("a failed tracking write never orphans the stored ts", async () => {
  const slack = new FakeSlack();
  const directory = tmpPath();
  const store = new StateStore(join(directory, "state.json"));
  store.bind(CHANNEL, directory);
  store.openThread(CHANNEL, THREAD);
  let failing = false;
  const track = (old: string | null, fresh: string | null): void => {
    if (failing) throw new Error("disk full");
    store.replaceOpenReply(CHANNEL, THREAD, old, fresh);
  };
  const sink = reply(slack, { onOpenReply: track });
  await sink.text("x");
  await settled();
  const first = slack.streamTs[0];
  failing = true;
  await sink.text("y".repeat(sinks.MESSAGE_LIMIT + 10));
  await settled();
  assert.deepEqual(store.thread(CHANNEL, THREAD)?.openReplies, [first]);
  failing = false;
  await sink.finish([]);
  await sink.closeOutFormatted(null);
  assert.deepEqual(store.thread(CHANNEL, THREAD)?.openReplies, []);
});

// Cards and previews.

test("a card says what the tool line said", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  await sink.task(
    tool("a", "Agent", "in_progress", { details: "Read: x\nBash: ls", calls: 3, task: true }),
  );
  await sink.task(tool("b", "Bash", "error", { output: "exit 1" }));
  await sink.task(tool("c", "Bash", "complete", { output: STOPPED }));
  await sink.task(tool("d", "Bash", "complete", { output: "fine" })); // a success shows no output
  await settled();
  assert.deepEqual(slack.messageCards()[0], [
    {
      id: "a",
      title: "Agent: a · 3 calls",
      status: "in_progress",
      details: "Read: x\nBash: ls",
    },
    // a call of a run of calls says why it failed in its title: its card is reused, and
    // Slack appends `output` to what a card already holds
    { id: "fold:b", title: "Bash: b · exit 1", status: "error" },
    { id: "c", title: "Bash: c", status: "complete", output: STOPPED },
    { id: "fold:d", title: "Bash: d", status: "complete" },
  ]);
});

test("an edit that ended well is one container with no card", async () => {
  // Issue #136: the call's line is the container's title, its sentence the subtitle.
  const slack = new FakeSlack();
  const sink = reply(slack);
  const view = preview("Update(a.txt)", "Added 1 line", "+🟩 1 x", "diff");
  await sink.task(tool("e", "Edit", "complete", { preview: view }));
  await settled();
  const start = only(slack.callsTo("chat.startStream"));
  const blocks = only(list(start.chunks));
  assert.equal(blocks.type, "blocks");
  const container = only(list(blocks.blocks));
  assert.ok(container.type === "container" && container.is_collapsible === true);
  assert.deepEqual(container.title, { type: "plain_text", text: "Update(a.txt)" });
  assert.deepEqual(container.subtitle, { type: "plain_text", text: "Added 1 line" });
  // The call's line in code style, as a tool line shows it; the plain title is the fallback.
  const section = only(list(get(container, "rich_text_title", "elements")));
  assert.deepEqual(section.elements, [
    { type: "text", text: "Update(a.txt)", style: { code: true } },
  ]);
});

test("a new file that was written is one container with no card", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  const view = preview("Write(new.txt)", "Wrote 2 lines to new.txt", "1 alpha\n2 beta");
  await sink.task(tool("w", "Write", "complete", { preview: view }));
  await settled();
  const start = only(slack.callsTo("chat.startStream"));
  const blocks = only(list(start.chunks));
  const container = only(list(blocks.blocks));
  assert.equal(get(container, "title", "text"), "Write(new.txt)");
  assert.equal(get(container, "subtitle", "text"), "Wrote 2 lines to new.txt");
  assert.equal(container.default_collapsed, true);
  const pre = only(list(get(container, "child_blocks", 0, "elements")));
  assert.ok(!("language" in pre)); // no highlighting asked for lines that are no diff
  assert.equal(sinks.blockText(container as unknown as sinks.Block), "1 alpha\n2 beta");
});

test("a diff under a card that already showed keeps the sentence as title", async () => {
  // A stream cannot take a card back: a call that showed one keeps it, and its container
  // does not repeat the call's line (issue #110).
  const slack = new FakeSlack();
  const sink = reply(slack);
  const view = preview("Update(a.txt)", "Added 1 line", "+x", "diff");
  await sink.task(tool("e", "Edit", "in_progress"));
  await settled();
  await sink.task(tool("e", "Edit", "complete", { preview: view }));
  await settled();
  const append = only(slack.callsTo("chat.appendStream"));
  const [card, blocks] = list(append.chunks);
  assert.ok(card?.type === "task_update" && card.title === "Update(a.txt)");
  const container = only(list(blocks?.blocks));
  assert.ok(get(container, "title", "text") === "Added 1 line" && !("subtitle" in container));
  assert.ok(!("rich_text_title" in container));
});

test("a preview with no lines keeps its card", async () => {
  // An empty new file has nothing to put in a container: the card says the sentence.
  const slack = new FakeSlack();
  const sink = reply(slack);
  await sink.task(
    tool("w", "Write", "complete", {
      preview: preview("Write(e.txt)", "Wrote 0 lines to e.txt", ""),
    }),
  );
  await settled();
  const start = only(slack.callsTo("chat.startStream"));
  const card = only(list(start.chunks));
  assert.ok(card.type === "task_update" && card.output === "Wrote 0 lines to e.txt");
});

test("a new file preview follows its card as a code block", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  const view = preview("Write(new.txt)", "Wrote 2 lines to new.txt", "1 alpha\n2 beta");
  await sink.task(tool("w", "Write", "in_progress"));
  await settled();
  await sink.task(tool("w", "Write", "complete", { preview: view }));
  await settled();
  const append = only(slack.callsTo("chat.appendStream"));
  assert.deepEqual(
    list(append.chunks).map((c) => c.type),
    ["task_update", "blocks"],
  );
  // a blocks chunk holding a markdown block: measured accepted 2026-09-29
  assert.deepEqual(get(append, "chunks", 1), {
    type: "blocks",
    blocks: [{ type: "markdown", text: "```\n1 alpha\n2 beta\n```" }],
  });
});

test("a preview is sent once however often the card changes", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  const view = preview("Update(a.txt)", "Added 1 line", "+x", "diff");
  await sink.task(tool("e", "Edit", "complete", { preview: view }));
  await settled();
  await sink.task(tool("e", "Edit", "complete", { preview: view, output: "again" }));
  await sink.text("after");
  await settled();
  assert.equal(allChunks(slack).filter((c) => c.type === "blocks").length, 1);
});

test("a failed call shows its error even if it carries a preview", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  const view = preview("Update(a.txt)", "Added 1 line", "+x", "diff");
  await sink.task(tool("e", "Edit", "error", { output: "File not found", preview: view }));
  await settled();
  const start = only(slack.callsTo("chat.startStream"));
  assert.deepEqual(start.chunks, [
    {
      type: "task_update",
      id: "fold:e",
      title: "Edit: e · File not found",
      status: "error",
    },
  ]);
});

// Not ported yet, in this place of the Python file: `an edit and a write show as the terminal
// shows them`, which feeds `edit-write.jsonl` through a `TurnRenderer` with a session folder.

test("a stopped message shows its previews as blocks", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  const diff = preview("Update(a.txt)", "Added 1 line", "+x", "diff");
  const fresh = preview("Write(b.txt)", "Wrote 1 line to b.txt", "1 hi");
  await sink.task(tool("e", "Edit", "complete", { preview: diff }));
  await sink.task(tool("w", "Write", "complete", { preview: fresh }));
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  await sink.text("more");
  await settled();
  const blocks = list(last(slack.callsTo("chat.update")).blocks);
  assert.deepEqual(
    blocks.map((b) => b.type),
    ["container", "container", "markdown"],
  );
  assert.deepEqual(
    blocks.slice(0, 2).map((b) => get(b, "title", "text")),
    ["Update(a.txt)", "Write(b.txt)"],
  );
  assert.equal(sinks.blockText(blocks[1] as unknown as sinks.Block), "1 hi");
});

// Not ported yet, in this place of the Python file: `an answered question shows in the reply
// where it was answered` and `answers to a call the reply has no line for are not kept`, which
// need `TurnRenderer.answered`.

test("a stopped message shows the answers under their card", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = reply(slack, { clock });
  const answered = preview(
    "User answered Claude's questions:",
    "",
    "· Colour? → <b> `x`",
    "",
    true,
  );
  await sink.task(tool("q", "AskUserQuestion", "complete", { preview: answered }));
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  await sink.text("more");
  await settled();
  const blocks = list(last(slack.callsTo("chat.update")).blocks);
  assert.deepEqual(
    blocks.map((b) => b.type),
    ["task_card", "context", "markdown"],
  );
  assert.ok(blocks[0] !== undefined && !("output" in blocks[0]));
  // Shown as written: no markup of the question's or the answer's is read as Slack's.
  const zero = sinks.ZERO_WIDTH_SPACE;
  assert.equal(
    get(blocks[1], "elements", 0, "text"),
    `${texts.NESTED}· Colour? → &lt;b&gt; \`${zero}x\`${zero}`,
  );
});

test("answers longer than a context block are cut", async () => {
  const slack = new FakeSlack();
  const sink = reply(slack);
  const body = Array.from({ length: 4 }, () => `· ${"q".repeat(900)} → ${"a".repeat(300)}`).join(
    "\n",
  );
  await sink.task(
    tool("q", "AskUserQuestion", "complete", {
      preview: preview("User answered", "", body, "", true),
    }),
  );
  await settled();
  const answers = only(allChunks(slack).filter((c) => c.type === "blocks"));
  const block = only(list(answers.blocks));
  const text = str(get(block, "elements", 0, "text"));
  assert.ok(len(text) === sinks.CONTEXT_LIMIT && text.endsWith("…"));
});

test("answers longer than a message still show and the reply goes on", async () => {
  // Counted for the 3,000 characters it shows, never for its whole body: a body past
  // MESSAGE_LIMIT would fit no message, and the reply would stop there.
  const slack = new FakeSlack();
  const sink = reply(slack);
  const body = `· ${"q".repeat(sinks.MESSAGE_LIMIT + 4_000)} → yes`;
  await sink.task(
    tool("q", "AskUserQuestion", "complete", {
      preview: preview("User answered", "", body, "", true),
    }),
  );
  await sink.text("And then.");
  await settled();
  assert.deepEqual(
    allChunks(slack).map((c) => c.type),
    ["task_update", "blocks", "markdown_text"],
  );
  assert.equal(slack.createdTs.length, 1);
});

test("a subagent card counts its calls from a recorded turn", async () => {
  // subagent-foreground.jsonl (CLI 2.1.286): the agent's task ends before the Agent call's
  // result. Python fed the recording through a `TurnRenderer`; until that is ported, the sink
  // is given the calls Python's renderer made for it (the reply golden), up to the turn's end.
  const slack = new FakeSlack();
  const sink = reply(slack);
  const whole = get(golden("reply", "subagent-foreground"), "whole", "calls");
  for (const call of list(whole)) {
    if (call.call === "text") {
      await sink.text(str(call.markdown), {
        notice: call.notice === true,
        ending: call.ending === true,
      });
    } else if (call.call === "task") {
      await sink.task(call.update as unknown as TaskUpdate);
    } else if (call.call === "finish") {
      await sink.finish(list(call.closing) as unknown as TaskUpdate[]);
      break;
    }
  }
  const cards = slack.messageCards()[0] ?? [];
  const card = only(cards.filter((c) => str(c.title).startsWith("Agent: ")));
  assert.ok(str(card.title).endsWith(" · 1 call") && card.status === "complete");
});

// The limiter: appends and updates spend its budget, a start, a stop and a post do not.

class CountingLimiter implements Limiter {
  acquired = 0;
  refunded = 0;

  async acquire(): Promise<void> {
    this.acquired += 1;
  }

  async refund(): Promise<void> {
    this.refunded += 1;
  }
}

test("appends and updates spend the budget and starts stops and posts do not", async () => {
  const slack = new FakeSlack();
  const limiter = new CountingLimiter();
  const clock = new FakeClock();
  const sink = reply(slack, { limiter, clock });
  await sink.text("one");
  await settled(); // a start
  await sink.text(" two");
  await settled(); // an append
  assert.equal(limiter.acquired, 1);
  await clock.advance(sinks.STREAM_SECONDS + 1); // a stop, then an update
  await sink.text(" three");
  await settled();
  assert.equal(limiter.acquired, 2);
  await sink.finish([]);
  await sink.closeOutFormatted("footer"); // a post
  assert.ok(limiter.acquired === 2 && limiter.refunded === 0);
});

/**
 * `acquire` blocks until `release`, as a busy shared limiter does while another reply spends its
 * only token.
 */
class StuckLimiter implements Limiter {
  acquired = 0;
  refunded = 0;
  private readonly gate = new AsyncEvent();

  async acquire(): Promise<void> {
    this.acquired += 1;
    await this.gate.wait();
  }

  async refund(): Promise<void> {
    this.refunded += 1;
  }

  release(): void {
    this.gate.set();
  }
}

test("a task update never blocks the caller", async () => {
  // `task` runs in the SDK reader loop: a limiter that makes a write wait must not hold it.
  const slack = new FakeSlack();
  const limiter = new StuckLimiter();
  const sink = reply(slack, { limiter });
  await sink.text("one");
  await settled();
  await soon(sink.text(" two"));
  await soon(sink.task(tool("t1", "Bash", "in_progress")));
  await settled();
  assert.equal(limiter.acquired, 1); // the append is waiting its turn, and the caller is not
  limiter.release();
  await settled();
});

test("a change during a limiter wait is not dropped", async () => {
  // The limiter refills on the reply's own clock: its wait is crossed, not slept through.
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const limiter = new UpdateLimiter({ limit: 1, window: 0.5, burst: 1, clock });
  const sink = reply(slack, { limiter, clock });
  await sink.text("one");
  await settled();
  await sink.text(" two"); // spends the only token; the next append waits for a refill
  await settled();
  await sink.text(" three");
  await sink.text(" four");
  await settled(); // the debounce is over: the append waits its turn
  assert.deepEqual(slack.messageTexts(), ["one two"]);
  await clock.advance(1.3);
  await settled();
  assert.deepEqual(slack.messageTexts(), ["one two three four"]);
});

/** A fake Slack that notes when each append arrives, on a clock, and may answer late on it. */
class TimedSlack extends FakeSlack {
  stamps: number[] = [];
  private readonly clock: FakeClock;
  private readonly roundTrip: number;

  constructor(clock: FakeClock, roundTrip = 0) {
    super();
    this.clock = clock;
    this.roundTrip = roundTrip;
  }

  override async apiCall(
    method: string,
    options: Record<string, unknown> = {},
  ): ReturnType<FakeSlack["apiCall"]> {
    if (method === "chat.appendStream") this.stamps.push(this.clock.time());
    if (this.roundTrip > 0) await this.clock.sleep(this.roundTrip);
    return super.apiCall(method, options);
  }
}

/** `seconds` pass one at a time, so that what a second wakes can wait and be woken in turn. */
async function elapse(clock: FakeClock, seconds: number): Promise<void> {
  for (let second = 0; second < seconds; second += 1) await clock.advance(1);
}

/** The clock moves, a second at a time, for as long as `promise` is pending. */
async function during<T>(clock: FakeClock, promise: Promise<T>): Promise<T> {
  let done = false;
  const guarded = promise.finally(() => {
    done = true;
  });
  guarded.catch(() => {});
  for (let second = 0; second < 10_000 && !done; second += 1) await clock.advance(1);
  if (!done) throw new Error("still pending after 10,000 seconds of the fake clock");
  return guarded;
}

function pairwise(values: readonly number[]): [number, number][] {
  return values.slice(1).map((value, i) => [values[i] as number, value]);
}

test("the debounce holds while streaming through a slow round trip", async () => {
  // Python's 10 ms debounce and 50 ms round trip, a hundred times longer and on the fake clock:
  // the debounce is DEBOUNCE_SECONDS itself, a round trip takes 5 seconds, a change every 2.
  const clock = new FakeClock();
  const slack = new TimedSlack(clock, 5);
  const sink = reply(slack, { clock, debounceSeconds: sinks.DEBOUNCE_SECONDS });
  await sink.text("go");
  await elapse(clock, 20);
  for (let i = 0; i < 20; i += 1) {
    await sink.text(` ${i}`);
    await elapse(clock, 2);
  }
  await elapse(clock, 40);
  assert.ok(slack.stamps.length >= 2);
  const gaps = pairwise(slack.stamps).map(([a, b]) => b - a);
  assert.ok(Math.min(...gaps) >= sinks.DEBOUNCE_SECONDS);
});

test("two busy sinks share one limiter and each reaches its end", async () => {
  // Python's 0.6 s window and 30 ms steps, a hundred times longer and on the fake clock.
  const [limit, window, burst] = [9, 60, 1];
  const clock = new FakeClock();
  const slack = new TimedSlack(clock);
  const limiter = new UpdateLimiter({ limit, window, burst, clock });
  const options = { limiter, clock, debounceSeconds: sinks.DEBOUNCE_SECONDS };
  const a = reply(slack, options);
  const b = reply(slack, options);
  for (let i = 0; i < 6; i += 1) {
    await a.text(`a${i} `);
    await b.text(`b${i} `);
    await elapse(clock, 3);
  }
  await during(clock, a.finish([]));
  await during(clock, a.closeOutFormatted("footer-a"));
  await during(clock, b.finish([]));
  await during(clock, b.closeOutFormatted("footer-b"));
  await elapse(clock, 150);
  for (const [i, start] of slack.stamps.entries()) {
    const inWindow = slack.stamps.slice(i).filter((t) => t < start + window).length;
    assert.ok(inWindow <= limit + burst);
  }
  assert.deepEqual(slack.messageTexts(), ["a0 a1 a2 a3 a4 a5", "b0 b1 b2 b3 b4 b5"]);
  assert.equal(slack.pushes(), 2);
});

// Helpers that build the blocks.

test("a notice fits one context element", () => {
  assert.equal(sinks.noticeText("short"), "short");
  const cut = sinks.noticeText("x".repeat(sinks.CONTEXT_LIMIT + 10));
  assert.ok(cut.length === sinks.CONTEXT_LIMIT && cut.endsWith("…"));
});

test("a banner is plain and escaped", () => {
  const text = "## Title\n\n- **bold** and `code` with [a link](https://x.example) & <tags>";
  assert.equal(sinks.bannerText(text), "Title\nbold and code with a link &amp; &lt;tags&gt;");
});

for (const [text, plain] of [
  ["see [1] and [a [b](u) then [c](v", "see [1] and a [b then [c](v"],
  ["intro\n \n\t\n- item\n  \nnot a marker", "intro\nitem\n  \nnot a marker"],
  ["snake_case a_`_`_b _lead_ trail_ *x* 2*3", "snake_case a___b lead trail x 2*3"],
] as const) {
  test(`stripping markdown keeps what only looks like a marker [${JSON.stringify(text)}]`, () => {
    // The texts a pattern reads and puts back. Each result is the one the patterns gave before
    // they were made linear (taken from them, 2026-10-04).
    assert.equal(sinks.stripMarkdown(text), plain);
  });
}

for (const [id, paragraph, banner] of [
  ["brackets", "[".repeat(100_000), "[".repeat(sinks.BANNER_LIMIT)],
  [
    "blank lines",
    `x${"\n ".repeat(50_000)}y`,
    `x${"\n ".repeat(150)}`.slice(0, sinks.BANNER_LIMIT),
  ],
  ["underscores", `a${"_`".repeat(50_000)}b`, `a${"_".repeat(sinks.BANNER_LIMIT - 1)}`],
] as const) {
  test(`a banner takes a time linear in its paragraph [${id}]`, () => {
    // A few milliseconds of CPU each. Patterns that start again from every `[`, blank line or
    // `_` took 15, 49 and 11 seconds on these in Python (measured 2026-10-04, Python 3.12).
    const started = process.cpuUsage();
    assert.equal(sinks.bannerText(paragraph, { limit: sinks.BANNER_LIMIT }), banner);
    const spent = process.cpuUsage(started);
    assert.ok((spent.user + spent.system) / 1e6 < 1);
  });
}

test("a task card block reads back as the card it was", () => {
  const update = tool("t1", "Bash", "error", {
    output: "exit 1",
    details: "ignored while it ended",
  });
  const block = sinks.cardBlock(update);
  assert.ok(block.type === "task_card" && block.task_id === "t1");
  assert.ok(block.status === "error" && block.title === "Bash: t1");
  assert.ok(block.output?.type === "rich_text" && !("details" in block));
});

test("a diff shows collapsed and full width", () => {
  const block = only(sinks.previewContainers("Added 1 line", "+🟩 1 x"));
  assert.ok(block.is_collapsible === true && block.default_collapsed === true);
  assert.equal(block.width, "full");
  const child = only(block.child_blocks);
  const pre = only(child.elements);
  assert.ok(pre.type === "rich_text_preformatted" && pre.language === "diff");
});

test("a diff is titled with its sentence alone", () => {
  // The call's line is the card above it (issue #110): the container must not say it again.
  const block = only(sinks.previewContainers("Added 1 line", "+x"));
  assert.deepEqual(block.title, { type: "plain_text", text: "Added 1 line" });
  assert.ok(!("rich_text_title" in block) && !("subtitle" in block));
});

test("a container with no card says the sentence under the calls line", () => {
  const block = only(sinks.previewContainers("Update(a.txt)", "+x", { subtitle: "s".repeat(200) }));
  assert.equal(block.title.text, "Update(a.txt)");
  assert.deepEqual(block.subtitle, { type: "plain_text", text: "s".repeat(150) });
});

test("a diff past a message continues in the next", () => {
  const body = Array.from({ length: 399 }, (_, i) => `+🟩 ${i + 1} ${"x".repeat(90)}`).join("\n");
  const blocks = sinks.previewContainers("Added 399 lines", body);
  assert.ok(blocks.length > 1);
  assert.ok(blocks.every((b) => len(sinks.blockText(b)) <= sinks.MESSAGE_LIMIT));
  assert.equal(blocks.map((b) => sinks.blockText(b)).join("\n"), body); // nothing lost at the cuts
});

test("a long title is cut to slacks limit", () => {
  const block = only(sinks.previewContainers("x".repeat(200), "+x", { asCode: true }));
  assert.equal(block.title.text.length, 150);
  const section = only(block.rich_text_title?.elements ?? []);
  assert.equal(section.elements[0]?.text.length, 150);
});

for (const fence of ["```", "````", "``````", "```x```"]) {
  test(`a fence inside a preview does not close its block [${fence}]`, () => {
    // Any run of three or more backticks would close the block (a Markdown file's ```` fence).
    const block = only(sinks.previewBlocks(`a\n${fence}\nb`));
    assert.equal(block.text.split("```").length - 1, 2);
  });
}

test("update limiter never exceeds the budget in any window", async () => {
  // On a clock that moves by what the limiter sleeps: the times are exact.
  const [limit, window, burst] = [4, 0.2, 1];
  const clock = new SelfPacedClock();
  const limiter = new UpdateLimiter({ limit, window, burst, clock });
  const times: number[] = [];
  for (let i = 0; i < limit * 3; i += 1) {
    await limiter.acquire();
    times.push(clock.time());
  }
  // a token bucket may spend its whole burst at once; past that, the standard bound holds:
  // tokens spent in any span <= burst + rate * span, i.e. at most limit+burst per window.
  for (let i = 0; i < times.length - (limit + burst); i += 1) {
    assert.ok((times[i + limit + burst] as number) - (times[i] as number) >= window - 0.03);
  }
});

test("update limiter paces evenly after its burst", async () => {
  // Even pacing, not a sliding window: past the burst, one token every window/limit seconds,
  // never every reply racing through the whole budget and then freezing together. An injected
  // clock, advanced by exactly what the limiter itself sleeps for, so the gaps are exact and
  // this cannot flake on a loaded runner.
  const [limit, window, burst] = [6, 0.6, 2];
  const clock = new SelfPacedClock();
  const limiter = new UpdateLimiter({ limit, window, burst, clock });
  const gaps: number[] = [];
  let previous: number | null = null;
  for (let i = 0; i < limit; i += 1) {
    await limiter.acquire();
    if (previous !== null) gaps.push(clock.time() - previous);
    previous = clock.time();
  }
  const steady = gaps.slice(burst - 1); // the gaps once the burst is spent
  const interval = window / limit;
  assert.ok(steady.length > 0);
  assert.ok(steady.every((gap) => Math.abs(gap - interval) <= interval * 1e-6));
});

test("update limiter serves waiters in arrival order", async () => {
  const limiter = new UpdateLimiter({
    limit: 1,
    window: 0.1,
    burst: 1,
    clock: new SelfPacedClock(),
  });
  const order: number[] = [];
  const take = async (n: number): Promise<void> => {
    await limiter.acquire();
    order.push(n);
  };
  await Promise.all([take(1), take(2), take(3)]);
  assert.deepEqual(order, [1, 2, 3]);
});

test("update limiter releases its place when a waiter is cancelled", async () => {
  const clock = new FakeClock();
  const limiter = new UpdateLimiter({ limit: 1, window: 0.2, burst: 1, clock });
  await limiter.acquire(); // spends the only token
  const caller = new AbortController();
  const waiter = limiter.acquire(caller.signal);
  await tick(); // the waiter is now sleeping, holding the limiter's own lock
  caller.abort(new Cancelled());
  await assert.rejects(waiter, Cancelled);
  // a cancelled waiter must not keep the lock: the next acquire is served once a token is back.
  const next = limiter.acquire();
  await clock.advance(0.2);
  await soon(next);
});

test("update limiter refund makes a token available at once", async () => {
  // too slow to refill on its own, on a clock that never moves
  const limiter = new UpdateLimiter({ limit: 1, window: 10.0, burst: 1, clock: new FakeClock() });
  await limiter.acquire(); // spends the only token
  await limiter.refund();
  await soon(limiter.acquire()); // available again, not after `window`
});

test("update limiter refund never exceeds burst", async () => {
  const limiter = new UpdateLimiter({ limit: 1, window: 10.0, burst: 2, clock: new FakeClock() });
  await limiter.refund();
  await limiter.refund();
  await limiter.refund(); // never more than a full burst, whatever was actually spent
  assert.equal((limiter as unknown as { tokens: number }).tokens, 2);
});

// The helpers of the tests that follow in `tests/test_sinks.py`, each under the section comment
// it has there, in the file's order. The tests themselves are ported from `an append refused for
// its content stops the stream and goes on by update` on, each under its section.

// A run of calls: two cards while the reply is written, a line of counts once its body ended.

export function context(text: string): sinks.ContextBlock {
  return { type: "context", elements: [{ type: "mrkdwn", text }] };
}

/** Text, then three calls one after the other (the last fails), then text. */
export async function runOfCalls(sink: ReplySink): Promise<void> {
  await sink.text("Let me look.\n\n");
  for (const [id, name, status, fields] of [
    ["a", "Bash", "complete", {}],
    ["b", "Read", "complete", {}],
    ["c", "Bash", "error", { output: "Exit code 1" }],
  ] as const) {
    await sink.task(tool(id, name, "in_progress"));
    await settled();
    await sink.task(tool(id, name, status, fields));
    await settled();
  }
  await sink.text("One test fails.");
  await settled();
}

// A reply that has ended never opens a message below its footer or its closing message.

/** The text of one line of `previewOf`: what tells that a preview is on the page. */
export function marker(line: number): string {
  return `${String(line).padStart(4)} ${"x".repeat(90)}`;
}

export function latePreview(lines = 40): Preview {
  const body = Array.from({ length: lines }, (_, i) => marker(i)).join("\n");
  return preview("Write(big.txt)", `Wrote ${lines} lines to big.txt`, body);
}

export function previewOf(lines: number): Preview {
  return latePreview(lines);
}

export function shows(blocks: readonly JsonObject[], needle: string): boolean {
  return blocks.some((b) => sinks.blockText(b as unknown as sinks.Block).includes(needle));
}

export function cardsOf(blocks: readonly JsonObject[]): Record<string, Json> {
  return Object.fromEntries(
    blocks.filter((b) => b.type === "task_card").map((b) => [str(b.task_id), b.status ?? null]),
  );
}

export const CUT_NOTE = sinks.contextBlock(sinks.PREVIEW_CUT);

// What a message written by `chat.update` holds: a collapsed container's text does not count
// toward MESSAGE_LIMIT there (measured 2026-10-06, slack-sdk 3.44.1: `chat.update` took 50
// containers of 10,000 characters and refused nothing; a stream and a post count the text).

/**
 * A diff of about `chars` characters: the size of the largest Edit previews, which a message of
 * MESSAGE_LIMIT cannot hold two of.
 */
export function largeDiff(i: number, chars = 9_000): Preview {
  const body = Array.from(
    { length: Math.floor(chars / 96) },
    (_, n) => `+${String(n).padStart(4)} ${"x".repeat(90)}`,
  ).join("\n");
  return preview(`Update(f${i}.txt)`, "Added lines", body, "diff");
}

export function containersOf(blocks: readonly JsonObject[]): JsonObject[] {
  return blocks.filter((b) => b.type === "container");
}

/** A stopped first message of 45 blocks, and a continuation that gets three large diffs. */
export async function continuationWithDiffs(
  slack: FakeSlack,
  clock: FakeClock,
): Promise<ReplySink> {
  const sink = reply(slack, { clock });
  await sink.text("start\n");
  await settled();
  await clock.advance(sinks.STREAM_SECONDS + 1);
  for (let i = 0; i < 50; i += 1) {
    await sink.task(tool(`c${i}`, "Agent", "complete", { task: true }));
  }
  for (let i = 0; i < 3; i += 1) {
    await sink.task(tool(`e${i}`, "Edit", "complete", { preview: largeDiff(i) }));
  }
  return sink;
}

/**
 * Slack refuses every `chat.update` that carries more than one container; the refused updates
 * are listed.
 */
export function refusingContainers(slack: FakeSlack, code = "msg_too_long"): JsonObject[] {
  const refused: JsonObject[] = [];
  slack.responses["chat.update"] = (args) => {
    if (containersOf(list(args.blocks)).length > 1) {
      refused.push(args);
      return rejected(code);
    }
    return { ok: true };
  };
  return refused;
}

// A stream's card: Slack adds the details and the output of every chunk to what the card holds
// (measured 2026-10-01 and 2026-10-08), so a stream is sent only what the card lacks.

/** Every chunk a reply's streams were sent for one card, in order. */
export function cardChunks(slack: FakeSlack, cardId: string): JsonObject[] {
  return ["chat.startStream", "chat.appendStream"].flatMap((method) =>
    slack.callsTo(method).flatMap((call) => list(call.chunks).filter((c) => c.id === cardId)),
  );
}

// The blocks Slack makes of markdown: a header per heading, a table per table, a divider per
// rule, rich text for each run between them. A post or an update whose message passes 50 of them
// is refused; a stream is not, and the update after its stop is (measured 2026-10-08).

/** `count` headings with a paragraph each: two blocks a section, as Slack stored them. */
export function sections(count: number, start = 1): string {
  return Array.from(
    { length: count },
    (_, i) => `## Heading ${start + i}\n\nparagraph ${start + i}`,
  ).join("\n\n");
}

/** The lines with words of some texts, in order: what a reply says, however it was cut. */
export function linesOf(...written: string[]): string[] {
  return written.flatMap((text) => text.split("\n").filter((line) => !blank(line)));
}

/** Slack's refusal of a message with more than 50 blocks, as recorded on 2026-10-08. */
export function tooMany(error = "invalid_blocks"): ReturnType<typeof rejected> {
  const notes = ["[ERROR] no more than 50 items allowed [json-pointer:/blocks]"];
  return rejected(error, { response_metadata: { messages: notes } });
}
