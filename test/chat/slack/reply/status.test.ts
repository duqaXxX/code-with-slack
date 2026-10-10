import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";
import {
  type Logger,
  resetStatusFlags,
  Status,
  StatusReaction,
  ThreadStatus,
} from "../../../../src/chat/slack/reply/status.ts";
import * as texts from "../../../../src/core/texts.ts";
import {
  AsyncEvent,
  CHANNEL,
  FakeClock,
  FakeSlack,
  rejected,
  THREAD,
} from "../../../support/fake-slack.ts";

// ported from tests/test_status.py

// `missingScope` and the status refusal are process-wide by design (D10): reset around each test
// so one test's `missing_scope` never leaks into the next.
beforeEach(resetStatusFlags);
afterEach(resetStatusFlags);

/** Every line the module logs, by level; `text` is the lines joined, as `caplog.text` was. */
class Lines implements Logger {
  readonly warnings: string[] = [];
  readonly others: string[] = [];

  debug = (message: string) => void this.others.push(message);
  info = (message: string) => void this.others.push(message);
  warning = (message: string) => void this.warnings.push(message);
  error = (message: string) => void this.others.push(message);

  get text(): string {
    return [...this.warnings, ...this.others].join("\n");
  }
}

function reaction(slack: FakeSlack, log?: Logger): StatusReaction {
  return new StatusReaction(slack, { channel: CHANNEL, rootTs: THREAD, logger: log });
}

/** The name of each call to `method`, in order. */
function names(slack: FakeSlack, method: string): unknown[] {
  return slack.callsTo(method).map((args) => args.name);
}

test("a sequence of changes adds the new reaction before removing the previous one", async () => {
  const slack = new FakeSlack();
  const sr = reaction(slack);
  await sr.show(Status.WORKING); // the fresh instance's own cleanup: not this test's concern
  slack.apiCalls.length = 0;
  await sr.show(Status.WAITING);
  await sr.show(Status.WORKING);
  await sr.show(Status.DONE);
  assert.deepEqual(slack.apiCalls, [
    { method: "reactions.add", args: { channel: CHANNEL, name: "raised_hand", timestamp: THREAD } },
    {
      method: "reactions.remove",
      args: { channel: CHANNEL, name: "hourglass_flowing_sand", timestamp: THREAD },
    },
    {
      method: "reactions.add",
      args: { channel: CHANNEL, name: "hourglass_flowing_sand", timestamp: THREAD },
    },
    {
      method: "reactions.remove",
      args: { channel: CHANNEL, name: "raised_hand", timestamp: THREAD },
    },
    {
      method: "reactions.add",
      args: { channel: CHANNEL, name: "white_check_mark", timestamp: THREAD },
    },
    {
      method: "reactions.remove",
      args: { channel: CHANNEL, name: "hourglass_flowing_sand", timestamp: THREAD },
    },
  ]);
});

test("showing the same state again makes no call", async () => {
  const slack = new FakeSlack();
  const sr = reaction(slack);
  await sr.show(Status.WORKING);
  const before = slack.apiCalls.length;
  await sr.show(Status.WORKING);
  assert.equal(slack.apiCalls.length, before);
});

test("already reacted on add counts as done", async () => {
  const slack = new FakeSlack();
  const log = new Lines();
  slack.responses["reactions.add"] = rejected("already_reacted");
  const sr = reaction(slack, log);
  await sr.show(Status.WORKING); // never rejects
  assert.ok(!log.text.includes("already_reacted"));
});

test("no reaction on remove counts as done", async () => {
  const slack = new FakeSlack();
  const log = new Lines();
  slack.responses["reactions.remove"] = rejected("no_reaction");
  const sr = reaction(slack, log);
  await sr.show(Status.WORKING);
  await sr.show(Status.WAITING); // the remove of the previous reaction never rejects
  assert.ok(!log.text.includes("no_reaction"));
});

test("another error is logged with the ids and swallowed", async () => {
  const slack = new FakeSlack();
  const log = new Lines();
  slack.responses["reactions.add"] = rejected("channel_not_found");
  const sr = reaction(slack, log);
  await sr.show(Status.WORKING); // never rejects
  assert.ok(log.text.includes(CHANNEL));
  assert.ok(log.text.includes(THREAD));
  assert.ok(log.text.includes("channel_not_found"));
});

test("concurrent show calls end on the last state", async () => {
  const slack = new FakeSlack();
  const sr = reaction(slack);
  await sr.show(Status.DONE); // past the fresh instance's own cleanup, not this test's concern
  slack.apiCalls.length = 0;
  // Both start before the first one's add has returned: the second meets the lock.
  const first = sr.show(Status.WORKING);
  const second = sr.show(Status.WAITING);
  await Promise.all([first, second]);
  assert.deepEqual(names(slack, "reactions.add"), ["hourglass_flowing_sand", "raised_hand"]);
  // The first call's own removal of the primed DONE, then the second's removal of WORKING.
  assert.deepEqual(names(slack, "reactions.remove"), [
    "white_check_mark",
    "hourglass_flowing_sand",
  ]);
});

/**
 * Holds the next `reactions.add` open, aborts `change` while it is out, and lets it return:
 * a caller cancelled in the middle of a change. `change` is the started `show`; its outcome is
 * returned, to be the abort's reason.
 */
async function abortWhileAddIsOut(
  slack: FakeSlack,
  start: (signal: AbortSignal) => Promise<void>,
): Promise<unknown> {
  const hold = new AsyncEvent();
  slack.gate = hold;
  slack.gateMethod = "reactions.add";
  const stop = new AbortController();
  const outcome = start(stop.signal).then(
    () => "returned",
    (error: unknown) => error,
  );
  await slack.gated.wait();
  stop.abort();
  hold.set();
  const result = await outcome;
  slack.gate = null;
  assert.equal(result, stop.signal.reason);
  return result;
}

test("settle finishes a change whose caller was cancelled between its two calls", async () => {
  // Issue #104: the new reaction is on the root, the previous one still is, and the caller
  // (a session's reader, cancelled by a close) is gone.
  const slack = new FakeSlack();
  const sr = reaction(slack);
  await sr.show(Status.WORKING);
  slack.apiCalls.length = 0;
  await abortWhileAddIsOut(slack, (signal) => sr.show(Status.DONE, signal));
  assert.deepEqual(
    slack.apiCalls.map((call) => [call.method, call.args.name]),
    [["reactions.add", "white_check_mark"]],
  );
  await sr.settle();
  assert.ok(
    slack.apiCalls.some(
      (call) =>
        call.method === "reactions.remove" &&
        call.args.name === "hourglass_flowing_sand" &&
        call.args.channel === CHANNEL &&
        call.args.timestamp === THREAD,
    ),
  );
  assert.equal(sr.current, Status.DONE);
});

test("a change after a cancelled one leaves neither of its two reactions", async () => {
  // The cancelled change left ⏳ and ✅ on the root, and the next state is neither: a close
  // that cuts a new prompt short shows ❌, which must stand alone.
  const slack = new FakeSlack();
  const sr = reaction(slack);
  await sr.show(Status.WORKING);
  await abortWhileAddIsOut(slack, (signal) => sr.show(Status.DONE, signal));
  slack.apiCalls.length = 0;
  await sr.show(Status.ERROR);
  const removed = new Set(names(slack, "reactions.remove"));
  assert.ok(removed.has("hourglass_flowing_sand") && removed.has("white_check_mark"));
  assert.ok(!removed.has("x"));
});

test("settle makes no call when the last change landed", async () => {
  const slack = new FakeSlack();
  const sr = reaction(slack);
  await sr.settle(); // nothing was ever asked for
  await sr.show(Status.WORKING);
  await sr.show(Status.DONE);
  const before = slack.apiCalls.length;
  await sr.settle();
  assert.equal(slack.apiCalls.length, before);
});

test("a fresh instance strips a leftover reaction from a previous session", async () => {
  // An earlier session on the same root left ❌ standing (a restart); this one's own first
  // `show` must remove it too, not just the one name it happens to know about, so the root
  // never carries two.
  const slack = new FakeSlack();
  const sr = reaction(slack);
  await sr.show(Status.WORKING);
  const removed = names(slack, "reactions.remove");
  assert.deepEqual(removed.sort(), ["raised_hand", "white_check_mark", "x"].sort());
});

test("a fresh instance s second show removes only its own previous", async () => {
  const slack = new FakeSlack();
  const sr = reaction(slack);
  await sr.show(Status.WORKING);
  slack.apiCalls.length = 0;
  await sr.show(Status.WAITING);
  assert.deepEqual(names(slack, "reactions.remove"), ["hourglass_flowing_sand"]);
});

test("a failed add leaves current unchanged so the next show retries", async () => {
  const slack = new FakeSlack();
  slack.responses["reactions.add"] = rejected("channel_not_found");
  const sr = reaction(slack, new Lines());
  await sr.show(Status.WORKING);
  assert.equal(sr.current, null);
  assert.deepEqual(slack.callsTo("reactions.remove"), []); // no previous reaction to strip: add lost
  delete slack.responses["reactions.add"];
  await sr.show(Status.WORKING); // retried, not skipped as "no change"
  assert.equal(sr.current, Status.WORKING);
});

test("current reflects the last state shown successfully", async () => {
  const slack = new FakeSlack();
  const sr = reaction(slack);
  assert.equal(sr.current, null);
  await sr.show(Status.WORKING);
  assert.equal(sr.current, Status.WORKING);
});

test("missing scope is logged once and stops further reactions", async () => {
  const slack = new FakeSlack();
  const log = new Lines();
  slack.responses["reactions.add"] = rejected("missing_scope");
  const first = reaction(slack, log);
  const second = reaction(slack, log);
  await first.show(Status.WORKING);
  await second.show(Status.WAITING);
  assert.equal(log.text.split("missing_scope").length - 1, 1);
  assert.equal(first.current, null);
  assert.equal(second.current, null);
  const before = slack.apiCalls.length;
  delete slack.responses["reactions.add"];
  await second.show(Status.WAITING);
  assert.equal(second.current, null); // the process gave up on reactions for this run
  assert.equal(slack.apiCalls.length, before); // no further Slack call, even once add would pass again
});

// The thread's status line (issue #83).

const SHOWN = {
  channel_id: CHANNEL,
  thread_ts: THREAD,
  status: texts.THREAD_WORKING_STATUS,
  loading_messages: [texts.THREAD_WORKING],
};
const CLEARED = { channel_id: CHANNEL, thread_ts: THREAD, status: "" };

interface Timing {
  readonly refresh?: number;
  readonly afterWrite?: number;
}

function threadStatus(
  slack: FakeSlack,
  clock: FakeClock,
  timing: Timing = {},
  log?: Logger,
): ThreadStatus {
  return new ThreadStatus(slack, {
    channel: CHANNEL,
    threadTs: THREAD,
    clock,
    logger: log,
    ...timing,
  });
}

function statuses(slack: FakeSlack) {
  return slack.callsTo("assistant.threads.setStatus");
}

/**
 * Long enough for the status's own task to make a call that is due: no time passes on the clock,
 * and the task and the fake's calls run to their next wait.
 */
function beat(clock: FakeClock): Promise<void> {
  return clock.advance(0);
}

test("the thread status is set with a loading message then cleared", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const status = threadStatus(slack, clock);
  status.show(texts.THREAD_WORKING);
  await beat(clock);
  assert.deepEqual(statuses(slack), [SHOWN]);
  status.show("");
  await beat(clock);
  assert.deepEqual(statuses(slack), [SHOWN, CLEARED]);
});

test("the same thread status again makes no call", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const status = threadStatus(slack, clock);
  status.show(""); // never shown: nothing to clear
  await beat(clock);
  assert.deepEqual(statuses(slack), []);
  status.show(texts.THREAD_WORKING);
  status.show(texts.THREAD_WORKING);
  await beat(clock);
  assert.deepEqual(statuses(slack), [SHOWN]);
});

test("a thread status asked for and dropped at once makes no call", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const status = threadStatus(slack, clock);
  status.show(texts.THREAD_WORKING);
  status.show("");
  await beat(clock);
  assert.deepEqual(statuses(slack), []);
});

test("the thread status is set again before slack removes it", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const status = threadStatus(slack, clock, { refresh: 30 });
  status.show(texts.THREAD_WORKING);
  await beat(clock);
  await clock.advance(30);
  await clock.advance(30);
  assert.ok(statuses(slack).length >= 3);
  for (const call of statuses(slack)) assert.deepEqual(call, SHOWN);
  await status.close();
  const count = statuses(slack).length;
  assert.deepEqual(statuses(slack).at(-1), CLEARED);
  await clock.advance(100);
  assert.equal(statuses(slack).length, count); // closed: nothing is left running
});

test("a write sets the thread status again soon after", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const status = threadStatus(slack, clock, { afterWrite: 5 });
  status.wrote(); // not shown: a write changes nothing
  await beat(clock);
  assert.deepEqual(statuses(slack), []);
  status.show(texts.THREAD_WORKING);
  await beat(clock);
  status.wrote();
  await beat(clock);
  assert.deepEqual(statuses(slack), [SHOWN]); // not at once: one call covers a burst of writes
  await clock.advance(5);
  assert.deepEqual(statuses(slack), [SHOWN, SHOWN]);
  await status.close();
});

test("writes that keep coming do not put the thread status off", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const status = threadStatus(slack, clock, { afterWrite: 2 });
  status.show(texts.THREAD_WORKING);
  await beat(clock);
  for (let write = 0; write < 10; write += 1) {
    // a card updated again and again while its turn waits on it
    status.wrote();
    await clock.advance(1);
  }
  assert.ok(statuses(slack).length >= 3);
  for (const call of statuses(slack)) assert.deepEqual(call, SHOWN);
  await status.close();
});

test("a write right after the thread status is asked for does not delay it", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const status = threadStatus(slack, clock, { afterWrite: 10 });
  status.show(texts.THREAD_WORKING);
  status.wrote();
  await beat(clock);
  assert.deepEqual(statuses(slack), [SHOWN]);
  await status.close();
});

test("a close that cuts the clearing call short clears the thread status itself", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const status = threadStatus(slack, clock);
  status.show(texts.THREAD_WORKING);
  await beat(clock);
  // The clearing call is out when the session closes. A call cannot be cut short here, so the
  // close waits for it: the status ends cleared, by that call, and once.
  const hold = new AsyncEvent();
  slack.gate = hold;
  slack.gateMethod = "assistant.threads.setStatus";
  slack.gated.clear();
  status.show("");
  await slack.gated.wait();
  assert.deepEqual(statuses(slack), [SHOWN]); // still on its way
  const closing = status.close();
  await beat(clock);
  hold.set();
  await closing;
  assert.deepEqual(statuses(slack), [SHOWN, CLEARED]);
});

// Not in the Python file: the same close, with the clearing call it waits for failing.
test("a close that waits for a clearing call that fails clears the thread status itself", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const status = threadStatus(slack, clock, undefined, new Lines());
  status.show(texts.THREAD_WORKING);
  await beat(clock);
  const hold = new AsyncEvent();
  slack.gate = hold;
  slack.gateMethod = "assistant.threads.setStatus";
  slack.gated.clear();
  slack.responses["assistant.threads.setStatus"] = [rejected("ratelimited"), { ok: true }];
  status.show("");
  await slack.gated.wait();
  const closing = status.close();
  await beat(clock);
  hold.set();
  await closing;
  assert.deepEqual(statuses(slack), [SHOWN, CLEARED, CLEARED]);
});

test("a thread status slack refuses is logged once and never raises", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const log = new Lines();
  slack.responses["assistant.threads.setStatus"] = rejected("ratelimited");
  const status = threadStatus(slack, clock, { refresh: 10 }, log);
  status.show(texts.THREAD_WORKING);
  await beat(clock);
  await clock.advance(10);
  await clock.advance(10);
  await status.close();
  assert.ok(statuses(slack).length >= 3);
  const lines = log.warnings.filter((line) => line.includes("setStatus"));
  assert.deepEqual(lines, [
    `assistant.threads.setStatus failed on ${CHANNEL}/${THREAD}: ratelimited`,
  ]);
});

test("closing a thread status that never showed makes no call", async () => {
  const slack = new FakeSlack();
  const status = threadStatus(slack, new FakeClock());
  await status.close();
  assert.deepEqual(statuses(slack), []);
});

test("a token that cannot set a thread status stops every instance", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  slack.responses["assistant.threads.setStatus"] = rejected("missing_scope");
  const first = threadStatus(slack, clock, { refresh: 20 }, new Lines());
  first.show(texts.THREAD_WORKING);
  await beat(clock);
  await clock.advance(100);
  assert.equal(ThreadStatus.refused(), true);
  const other = new ThreadStatus(slack, { channel: CHANNEL, threadTs: "1790000000.000002", clock });
  other.show(texts.THREAD_WORKING);
  await beat(clock);
  assert.equal(statuses(slack).length, 1); // asked once, by the first: no retry can change the answer
  await first.close();
  await other.close();
});

test("a thread status that changes its words says the new ones at once", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const status = threadStatus(slack, clock);
  status.show(texts.THREAD_WORKING);
  await beat(clock);
  status.show("2 shells still running");
  await beat(clock);
  status.show("1 shell still running");
  await beat(clock);
  assert.deepEqual(
    statuses(slack).map((call) => call.loading_messages),
    [[texts.THREAD_WORKING], ["2 shells still running"], ["1 shell still running"]],
  );
  await status.close();
  assert.deepEqual(statuses(slack).at(-1), CLEARED);
});

test("a clearing call that fails is tried once more", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const status = threadStatus(slack, clock, { afterWrite: 3 }, new Lines());
  status.show(texts.THREAD_WORKING);
  await beat(clock);
  slack.responses["assistant.threads.setStatus"] = [rejected("ratelimited"), { ok: true }];
  status.show("");
  await beat(clock);
  assert.deepEqual(statuses(slack), [SHOWN, CLEARED]); // refused: the status still stands
  await clock.advance(3);
  assert.deepEqual(statuses(slack), [SHOWN, CLEARED, CLEARED]);
  await status.close();
  assert.equal(statuses(slack).length, 3); // it went through: nothing is left to clear
});

test("a status that could not be cleared is cleared when it closes", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const status = threadStatus(slack, clock, { afterWrite: 1 }, new Lines());
  status.show(texts.THREAD_WORKING);
  await beat(clock);
  slack.responses["assistant.threads.setStatus"] = rejected("ratelimited");
  status.show("");
  await beat(clock);
  await clock.advance(2); // the call and its one retry both fail
  assert.deepEqual(statuses(slack), [SHOWN, CLEARED, CLEARED]);
  slack.responses["assistant.threads.setStatus"] = { ok: true };
  await status.close();
  assert.deepEqual(statuses(slack), [SHOWN, CLEARED, CLEARED, CLEARED]);
});

test("the fallback status says the same as the line", async () => {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const status = threadStatus(slack, clock);
  status.show("1 shell still running", "has 1 shell still running");
  await beat(clock);
  const calls = statuses(slack);
  assert.equal(calls.length, 1);
  assert.equal(calls[0]?.status, "has 1 shell still running");
  assert.deepEqual(calls[0]?.loading_messages, ["1 shell still running"]);
  await status.close();
});
