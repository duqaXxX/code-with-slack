/**
 * The same-folder hold: two busy sessions in one folder. Port of the section of
 * `tests/test_slack_app.py` that opens with `# --- D8: two busy sessions in one folder`, in its
 * order. The harness is `test/support/slack-app.ts`.
 *
 * Ported up to `a click for another channel is refused`. The next test of the Python file is
 * `test_a_drain_starting_right_after_continue_does_not_leave_a_stale_raised_hand`.
 */
import assert from "node:assert/strict";
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { HOLD_CANCEL, HOLD_CONTINUE } from "../../../../src/chat/slack/hold.ts";
import * as texts from "../../../../src/core/texts.ts";
import { fill } from "../../../../src/core/texts.ts";
import {
  CHANNEL,
  OTHER_CHANNEL,
  OTHER_TEAM,
  OTHER_THREAD,
  STRANGER,
  THREAD,
} from "../../../support/fake-slack.ts";
import { sdkMessages, splitTurns } from "../../../support/sessions.ts";
import {
  assertHeldUnsent,
  type Body,
  buttonValue,
  clickIn,
  holdQuestions,
  message,
  postedBlocks,
  reactionsOn,
  reply,
  startAHold,
  worldOf,
} from "../../../support/slack-app.ts";

// Python's `user0` and `user1`: a click by another user, and by the owner from another workspace.
const OTHER_USERS: readonly Body[] = [{ id: STRANGER }, { team_id: OTHER_TEAM }];

test("a busy session in the same folder holds the message", async (t) => {
  const world = worldOf(t);
  await startAHold(world);
  assertHeldUnsent(world); // held, not sent
  // mrkdwn's own `<url|label>` form (a `section` block, like approvals and the resume picker
  // use for their own buttons), not `threadLink`'s standard-Markdown form.
  const link = "<https://example.slack.com/archives/C000CHAN/p1780000000000001|Session>";
  const section = postedBlocks(world)[0] as Body;
  assert.equal(section.type, "section");
  assert.deepEqual(section.text, { type: "mrkdwn", text: fill(texts.HOLD_QUESTION, { link }) });
  // Crash repair (issue #19): a hold is a request like an approval or a question.
  assert.deepEqual(world.state.thread(CHANNEL, THREAD)?.requests, [world.slack.postedTs.at(-1)]);
});

test("a failed state write for a d8 hold still lets it proceed", async (t) => {
  const world = worldOf(t);
  // `state.addRequest` sits outside `askOwner`'s own try/finally on purpose, so a write failure
  // here can never skip `holdStart`/`holdEnd` and leave the hold itself undiscarded.
  world.state.addRequest = () => {
    throw new Error("disk full");
  };
  await startAHold(world);
  const questionTs = world.slack.postedTs.at(-1) as string;
  const holdId = buttonValue(postedBlocks(world), HOLD_CONTINUE);
  await world.dispatch(clickIn(HOLD_CONTINUE, holdId, CHANNEL, THREAD, { messageTs: questionTs }));
  assert.deepEqual(world.clients.at(-1)?.queries, ["hello"]);
});

test("continue sends the held message", async (t) => {
  const world = worldOf(t);
  await startAHold(world);
  const questionTs = world.slack.postedTs.at(-1) as string;
  const holdId = buttonValue(postedBlocks(world), HOLD_CONTINUE);
  await world.dispatch(clickIn(HOLD_CONTINUE, holdId, CHANNEL, THREAD, { messageTs: questionTs }));
  assert.deepEqual(world.clients.at(-1)?.queries, ["hello"]);
  const deleted = world.slack.callsTo("chat.delete").map((args) => args.ts);
  assert.deepEqual(deleted, [questionTs]); // the question's own message
  assert.deepEqual(world.state.thread(CHANNEL, THREAD)?.requests, []);
});

test("bypass typed after start while held switches the session", async (t) => {
  const world = worldOf(t);
  // Start was applied (the world's setup starts on its own), then the hold asks: the setup's
  // box is gone, so the word works as in a session that ran, and holds after Continue.
  await startAHold(world);
  const questionTs = world.slack.postedTs.at(-1) as string;
  const word = reply("!bypass on", THREAD);
  await world.dispatch(word);
  assert.deepEqual(world.ephemerals(), [texts.BYPASS_ON_THREAD]);
  assert.deepEqual(reactionsOn(world, word.event.ts), ["white_check_mark"]);
  const holdId = buttonValue(postedBlocks(world, -1), HOLD_CONTINUE);
  await world.dispatch(clickIn(HOLD_CONTINUE, holdId, CHANNEL, THREAD, { messageTs: questionTs }));
  const client = world.clients.at(-1);
  assert.deepEqual(client?.queries, ["hello"]);
  assert.deepEqual(client?.modes, ["bypassPermissions"]);
  assert.equal(world.state.thread(CHANNEL, THREAD)?.bypass, true);
});

test("cancel drops the message and says so", async (t) => {
  const world = worldOf(t);
  await startAHold(world);
  const questionTs = world.slack.postedTs.at(-1) as string;
  const holdId = buttonValue(postedBlocks(world), HOLD_CANCEL);
  await world.dispatch(clickIn(HOLD_CANCEL, holdId, CHANNEL, THREAD, { messageTs: questionTs }));
  assertHeldUnsent(world); // never sent
  assert.equal(world.ephemerals().at(-1), texts.NOT_SENT);
  const deleted = world.slack.callsTo("chat.delete").map((args) => args.ts);
  assert.deepEqual(deleted, [questionTs]); // the question, not the `Not sent.` notice
  assert.deepEqual(world.state.thread(CHANNEL, THREAD)?.requests, []);
});

test("no hold when the other session is idle", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("hi", { ts: OTHER_THREAD }));
  const session = world.sessions.get(CHANNEL, OTHER_THREAD);
  assert.ok(session !== null);
  world.clients[0]?.answer(sdkMessages("tools"));
  await world.until(() => session.idle);
  await world.dispatch(message("hello", { ts: THREAD }));
  assert.deepEqual(world.clients.at(-1)?.queries, ["hello"]); // sent at once, no question
});

test("no hold when the other session is in another folder", async (t) => {
  const world = worldOf(t);
  mkdirSync(join(world.root, "other"));
  world.state.bind(OTHER_CHANNEL, join(world.root, "other"));
  await world.dispatch(message("busy elsewhere", { ts: OTHER_THREAD, channel: OTHER_CHANNEL }));
  await world.dispatch(message("hello", { ts: THREAD }));
  assert.deepEqual(world.clients.at(-1)?.queries, ["hello"]);
});

test("no hold when the target itself is busy", async (t) => {
  const world = worldOf(t);
  await startAHold(world); // THREAD's own first message is held (it is not busy yet)
  const questionTs = world.slack.postedTs.at(-1) as string;
  const holdId = buttonValue(postedBlocks(world), HOLD_CONTINUE);
  await world.dispatch(clickIn(HOLD_CONTINUE, holdId, CHANNEL, THREAD, { messageTs: questionTs }));
  await world.dispatch(reply("again", THREAD)); // THREAD is busy now: queues, asks nothing
  assert.deepEqual(
    world.clients.map((client) => client.queries),
    [["busy elsewhere"], ["hello"]],
  );
  assert.equal(holdQuestions(world).length, 1); // only the first message was ever held
});

test("another channel bound to the same folder also holds", async (t) => {
  const world = worldOf(t);
  await startAHold(world, OTHER_CHANNEL);
  assertHeldUnsent(world);
  const holdId = buttonValue(postedBlocks(world), HOLD_CONTINUE);
  await world.dispatch(clickIn(HOLD_CONTINUE, holdId, CHANNEL, THREAD));
  assert.deepEqual(world.clients.at(-1)?.queries, ["hello"]);
});

test("a background only session counts as working", async (t) => {
  const world = worldOf(t);
  const first = splitTurns(sdkMessages("background"))[0];
  assert.ok(first !== undefined);
  await world.dispatch(message("start it", { ts: OTHER_THREAD }));
  world.clients[0]?.answer(first);
  const session = world.sessions.get(CHANNEL, OTHER_THREAD);
  assert.ok(session !== null);
  await world.until(() => !session.busy && session.runningKinds !== "");
  await world.dispatch(message("hello", { ts: THREAD }));
  assertHeldUnsent(world); // held: the background task still counts as working
});

test("a double click finds no hold", async (t) => {
  const world = worldOf(t);
  await startAHold(world);
  const holdId = buttonValue(postedBlocks(world), HOLD_CONTINUE);
  const body = clickIn(HOLD_CONTINUE, holdId, CHANNEL, THREAD);
  await world.dispatch(body);
  await world.dispatch(body);
  assert.equal(world.ephemerals().at(-1), texts.HOLD_GONE);
});

for (const [index, user] of OTHER_USERS.entries()) {
  test(`a click from someone else is ignored [user${index}]`, async (t) => {
    const world = worldOf(t);
    await startAHold(world);
    const holdId = buttonValue(postedBlocks(world), HOLD_CONTINUE);
    await world.dispatch(clickIn(HOLD_CONTINUE, holdId, CHANNEL, THREAD, { user }));
    assertHeldUnsent(world);
    assert.deepEqual(world.ephemerals(), []);
  });
}

test("a click for another thread is refused", async (t) => {
  const world = worldOf(t);
  await startAHold(world);
  const holdId = buttonValue(postedBlocks(world), HOLD_CONTINUE);
  await world.dispatch(clickIn(HOLD_CONTINUE, holdId, CHANNEL, OTHER_THREAD));
  assertHeldUnsent(world); // never sent: the click did not match the hold
  assert.equal(world.ephemerals().at(-1), texts.HOLD_GONE);
});

test("a click for another channel is refused", async (t) => {
  const world = worldOf(t);
  await startAHold(world);
  const holdId = buttonValue(postedBlocks(world), HOLD_CONTINUE);
  await world.dispatch(clickIn(HOLD_CONTINUE, holdId, OTHER_CHANNEL, THREAD));
  assertHeldUnsent(world); // never sent: the click did not match the hold
  assert.equal(world.ephemerals().at(-1), texts.HOLD_GONE);
});
