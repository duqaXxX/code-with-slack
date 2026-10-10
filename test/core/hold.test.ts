// No Python test exercised `hold.Holds` on its own (`tests/test_slack_app.py` reached it through
// the handlers), so these are written for it directly, from `hold.py`'s docstrings.
import assert from "node:assert/strict";
import { test } from "node:test";
import { Holds } from "../../src/core/hold.ts";
import { DEFAULT_CHOICE } from "../../src/core/setup.ts";
import { CHANNEL, OTHER_CHANNEL, OTHER_THREAD, THREAD } from "../support/fake-slack.ts";

test("ids are unguessable and unique", () => {
  const holds = new Holds();
  const ids = new Set(Array.from({ length: 100 }, () => holds.open(CHANNEL, THREAD)[0]));
  assert.equal(ids.size, 100);
  assert.ok([...ids].every((id) => id.length >= 16));
});

test("a hold resolves once, only from the channel and thread it was posted in", async () => {
  const holds = new Holds();
  const [holdId, pending] = holds.open(CHANNEL, THREAD);
  assert.equal(holds.resolve(holdId, OTHER_CHANNEL, THREAD, true), null);
  assert.equal(holds.resolve(holdId, CHANNEL, OTHER_THREAD, true), null);
  assert.equal(pending.decided, false);
  assert.equal(holds.resolve(holdId, CHANNEL, THREAD, true), pending);
  assert.equal(await pending.answer, true);
  assert.equal(holds.resolve(holdId, CHANNEL, THREAD, null), null);
});

test("a cancelled hold leaves at once, an answered one stays until discarded", async () => {
  const holds = new Holds();
  const [cancelledId, cancelled] = holds.open(CHANNEL, THREAD);
  holds.resolve(cancelledId, CHANNEL, THREAD, null);
  assert.equal(await cancelled.answer, null);
  assert.equal(holds.get(cancelledId), null);

  const [answeredId, answered] = holds.open(CHANNEL, OTHER_THREAD);
  holds.resolve(answeredId, CHANNEL, OTHER_THREAD, DEFAULT_CHOICE);
  assert.equal(await answered.answer, DEFAULT_CHOICE);
  assert.equal(holds.get(answeredId), answered);
  holds.discard(answeredId);
  assert.equal(holds.get(answeredId), null);
});

test("the context given to open comes back with the pending", () => {
  const models = [
    {
      value: "m",
      displayName: "M",
      description: null,
      supportsEffort: false,
      supportedEffortLevels: [],
    },
  ];
  const [, pending] = new Holds().open(CHANNEL, THREAD, models);
  assert.equal(pending.context, models);
  assert.equal(new Holds().open(CHANNEL, THREAD)[1].context, null);
});

test("cancel releases the hold open in this thread and nothing else", async () => {
  const holds = new Holds();
  const [holdId, pending] = holds.open(CHANNEL, THREAD);
  const [, otherThread] = holds.open(CHANNEL, OTHER_THREAD);
  const [, otherChannel] = holds.open(OTHER_CHANNEL, THREAD);
  assert.equal(holds.cancel(CHANNEL, THREAD), pending);
  assert.equal(await pending.answer, null);
  assert.equal(holds.get(holdId), null);
  assert.equal(otherThread.decided, false);
  assert.equal(otherChannel.decided, false);
  assert.equal(holds.cancel(CHANNEL, THREAD), null);
});

test("cancel flags a hold already answered but still being applied, once", () => {
  const holds = new Holds();
  const [holdId, pending] = holds.open(CHANNEL, THREAD);
  holds.resolve(holdId, CHANNEL, THREAD, DEFAULT_CHOICE);
  assert.equal(holds.cancel(CHANNEL, THREAD), pending);
  assert.equal(pending.cancelled, true);
  assert.equal(holds.get(holdId), pending);
  // A second stop finds nothing more to cancel.
  assert.equal(holds.cancel(CHANNEL, THREAD), null);
});

test("a hold decided before its message is known says so", () => {
  const holds = new Holds();
  const [clickedId, clicked] = holds.open(CHANNEL, THREAD);
  holds.resolve(clickedId, CHANNEL, THREAD, true);
  assert.equal(holds.posted(clickedId, "1790000000.000001"), false);
  assert.equal(clicked.messageTs, "1790000000.000001");

  const [stoppedId] = holds.open(CHANNEL, OTHER_THREAD);
  holds.cancel(CHANNEL, OTHER_THREAD);
  assert.equal(holds.posted(stoppedId, "1790000000.000002"), false);

  const [liveId, live] = holds.open(OTHER_CHANNEL, THREAD);
  assert.equal(holds.posted(liveId, "1790000000.000003"), true);
  assert.equal(live.messageTs, "1790000000.000003");
});

test("an answer that arrives before the post returned is kept for the asker", async () => {
  const holds = new Holds();
  const [holdId, pending] = holds.open(CHANNEL, THREAD);
  holds.resolve(holdId, CHANNEL, THREAD, DEFAULT_CHOICE);
  assert.equal(holds.posted(holdId, "1790000000.000001"), false);
  assert.equal(await pending.answer, DEFAULT_CHOICE);
  assert.equal(holds.get(holdId), pending);
});

test("a control with no id of its own finds the open hold its message shows", () => {
  const holds = new Holds();
  const [holdId] = holds.open(CHANNEL, THREAD);
  holds.posted(holdId, "1790000000.000001");
  assert.equal(holds.atMessage(CHANNEL, THREAD, "1790000000.000001"), holdId);
  assert.equal(holds.atMessage(CHANNEL, THREAD, "1790000000.000009"), null);
  assert.equal(holds.atMessage(OTHER_CHANNEL, THREAD, "1790000000.000001"), null);
  assert.equal(holds.atMessage(CHANNEL, OTHER_THREAD, "1790000000.000001"), null);
  holds.resolve(holdId, CHANNEL, THREAD, DEFAULT_CHOICE);
  assert.equal(holds.atMessage(CHANNEL, THREAD, "1790000000.000001"), null);
});
