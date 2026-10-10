// Not in the Python suite: asyncio's own semaphore, lock, timeout and shield had no tests there.
import assert from "node:assert/strict";
import { test } from "node:test";
import { Gate, TIMED_OUT, waitFor, within } from "../../../../src/chat/slack/openfile/waiting.ts";
import { AsyncEvent, FakeClock } from "../../../support/fake-slack.ts";

test("a gate lets its limit of holders in and the next one in when a turn is given back", async () => {
  const gate = new Gate(2);
  const order: string[] = [];
  const hold = new AsyncEvent();
  const job = (name: string) =>
    gate.run(async () => {
      order.push(`${name} in`);
      await hold.wait();
      order.push(`${name} out`);
    });
  const jobs = [job("a"), job("b"), job("c")];
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(order, ["a in", "b in"]);
  hold.set();
  await Promise.all(jobs);
  assert.deepEqual(order, ["a in", "b in", "a out", "b out", "c in", "c out"]);
});

test("a request that gives up while waiting for a turn leaves the queue", async () => {
  const gate = new Gate(1);
  await gate.acquire();
  const giving = new AbortController();
  const waiting = gate.acquire(giving.signal);
  const behind = gate.acquire();
  giving.abort(new Error("gave up"));
  await assert.rejects(waiting, /gave up/);
  gate.release();
  await behind; // the turn went to the one behind, not to the one that left
});

test("a time limit ends the work's wait when the clock passes it", async () => {
  const clock = new FakeClock();
  let sawAbort = false;
  const pending = within(clock, 5, (signal) => {
    return new Promise<string>((_resolve, reject) => {
      signal.addEventListener("abort", () => {
        sawAbort = true;
        reject(signal.reason);
      });
    });
  });
  await clock.advance(4);
  assert.ok(!sawAbort);
  await clock.advance(1);
  assert.equal(await pending, TIMED_OUT);
  assert.ok(sawAbort);
});

test("work that ends in time is returned and the clock's sleep is dropped", async () => {
  const clock = new FakeClock();
  assert.equal(await within(clock, 5, async () => "done"), "done");
  await clock.advance(10); // nothing left to wake
});

test("work that ignores its signal is still given up on at the limit", async () => {
  const clock = new FakeClock();
  const never = new AsyncEvent();
  const pending = within(clock, 1, () => never.wait().then(() => "late"));
  await clock.advance(1);
  assert.equal(await pending, TIMED_OUT);
  never.set();
});

test("an out of time limit is out of time at once and the work is not started", async () => {
  let started = false;
  assert.equal(
    await within(new FakeClock(), 0, async () => {
      started = true;
    }),
    TIMED_OUT,
  );
  assert.ok(!started);
});

test("work that fails for its own reason is not taken for a timeout", async () => {
  await assert.rejects(
    within(new FakeClock(), 5, async () => {
      throw new Error("its own");
    }),
    /its own/,
  );
});

test("a wait that gives up leaves the promise it waited for running", async () => {
  const done = new AsyncEvent();
  const work = done.wait().then(() => "finished");
  const giving = new AbortController();
  const waiting = waitFor(work, giving.signal);
  giving.abort(new Error("gave up"));
  await assert.rejects(waiting, /gave up/);
  done.set();
  assert.equal(await work, "finished");
});
