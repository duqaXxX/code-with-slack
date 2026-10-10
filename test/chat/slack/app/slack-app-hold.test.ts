/**
 * The same-folder hold: two busy sessions in one folder. Port of the section of
 * `tests/test_slack_app.py` that opens with `# --- D8: two busy sessions in one folder`, in its
 * order. The harness is `test/support/slack-app.ts`.
 *
 * Ported through `cancel after a finished report turn shows done not a stale reaction`, the last
 * test of the section.
 *
 * What stands for Python's private fields. A session's `_status.current` is the reaction on the
 * root message, so it is read as the reactions Slack holds on it (`standing`: `reactions.add` and
 * `reactions.remove` replayed in order). `_drop_client` and `_closed`, which a test sets to put a
 * session where a race leaves it, are TypeScript `private` members, reached by a cast in
 * `Internals`: there is no public way to reach that state without the teardown the test stands in
 * for. The idle-close task, found by name in Python, is observed as a session the fake clock
 * cannot close.
 */
import assert from "node:assert/strict";
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { type TestContext, test } from "node:test";
import { HOLD_CANCEL, HOLD_CONTINUE } from "../../../../src/chat/slack/hold.ts";
import { Status } from "../../../../src/chat/slack/reply/status.ts";
import { IDLE_CLOSE_SECONDS } from "../../../../src/core/sessions/constants.ts";
import type { ThreadSession } from "../../../../src/core/sessions/session.ts";
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
import { slackPayload } from "../../../support/fixtures.ts";
import { sdkMessages, splitTurns } from "../../../support/sessions.ts";
import {
  assertHeldUnsent,
  type Body,
  buttonValue,
  clickIn,
  holdQuestions,
  idleMessage,
  lastHoldId,
  message,
  postedBlocks,
  reactionsOn,
  reply,
  said,
  startAHold,
  type World,
  worldOf as worldWithoutKeepAlive,
} from "../../../support/slack-app.ts";

/**
 * The `world` fixture, with a timer that keeps the event loop alive for the test's duration: on
 * Node 22 a test that awaits something which never comes, with only unref'd timers pending, lets
 * the process finish and cancels the rest of the file. With this it waits for the runner's own
 * timeout instead. (`test/support/sessions.ts` does the same for its harness; the world does not.)
 */
function worldOf(t: TestContext): World {
  const guard = setInterval(() => {}, 1_000);
  t.after(() => clearInterval(guard));
  return worldWithoutKeepAlive(t);
}

/** The private members a test sets to put a session where a race leaves it. */
interface Internals {
  dropClient(): Promise<void>;
  closedFlag: boolean;
}

function internals(session: ThreadSession): Internals {
  return session as unknown as Internals;
}

/** The reactions standing on the message at `ts`: what Slack holds after every add and remove. */
function standing(world: World, ts: string): string[] {
  const shown = new Set<string>();
  for (const call of world.slack.apiCalls) {
    if (call.args.timestamp !== ts) continue;
    if (call.method === "reactions.add") shown.add(String(call.args.name));
    if (call.method === "reactions.remove") shown.delete(String(call.args.name));
  }
  return [...shown];
}

/** `chat.postMessage` answers `answer` for a post whose text starts with `prefix`, else as by default. */
function postAnswers(
  world: World,
  prefix: string,
  answer: (args: Body) => Error | undefined,
): void {
  world.slack.responses["chat.postMessage"] = (args) => {
    const refused = String(args.text ?? "").startsWith(prefix) ? answer(args) : undefined;
    if (refused !== undefined) return refused;
    // A new ts for each message, as the fake gives one by default.
    const ts = `1790000000.${String(world.slack.createdTs.length + 1).padStart(6, "0")}`;
    return { ...slackPayload("api-chat-postMessage"), ts };
  };
}

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

// `_status.current is None` and the like: no reaction stands on the root.
test("a drain starting right after continue does not leave a stale raised hand", async (t) => {
  const world = worldOf(t);
  await startAHold(world);
  const holdId = buttonValue(postedBlocks(world), HOLD_CONTINUE);
  world.sessions.draining = true; // as if a drain's own cancellation pass had just run
  await world.dispatch(clickIn(HOLD_CONTINUE, holdId, CHANNEL, THREAD));
  assertHeldUnsent(world);
  const session = world.sessions.get(CHANNEL, THREAD);
  assert.ok(session !== null);
  assert.deepEqual(standing(world, THREAD), []); // restored, not left on ✋: this thread never ran
});

test("a directory gone unavailable after continue reacts error not a raised hand", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("busy elsewhere", { ts: OTHER_THREAD }));
  await world.dispatch(message("!compact", { ts: THREAD })); // a Passthrough: held, `ensureConnected`
  const holdId = buttonValue(postedBlocks(world), HOLD_CONTINUE);
  const session = world.sessions.get(CHANNEL, THREAD);
  assert.ok(session !== null);
  // The setup connected it already; a client the CLI lost since makes Continue connect again.
  await internals(session).dropClient();
  world.backend.trusted = async () => false;
  await world.dispatch(clickIn(HOLD_CONTINUE, holdId, CHANNEL, THREAD));
  assert.deepEqual(world.clients.at(-1)?.queries, []); // never sent: its own connect failed
  assert.deepEqual(standing(world, THREAD), [Status.ERROR]);
});

test("a generic connect error after continue reacts error not a raised hand", async (t) => {
  const world = worldOf(t);
  // `submitted`, not a fixed set of exception types: any failure before `submit()` (a
  // logged-out CLI raising something `ensureConnected` does not special-case, most likely)
  // must not leave the hold's ✋ standing forever either.
  await world.dispatch(message("busy elsewhere", { ts: OTHER_THREAD }));
  await world.dispatch(message("!compact", { ts: THREAD })); // a Passthrough: held, `ensureConnected`
  const holdId = buttonValue(postedBlocks(world), HOLD_CONTINUE);
  const session = world.sessions.get(CHANNEL, THREAD);
  assert.ok(session !== null);
  await internals(session).dropClient(); // the setup connected it; a lost client makes Continue reconnect
  world.connectError = new Error("logged out");
  await world.dispatch(clickIn(HOLD_CONTINUE, holdId, CHANNEL, THREAD));
  assert.deepEqual(world.clients.at(-1)?.queries, []); // its own connect failed: nothing was ever sent
  assert.deepEqual(world.clients[0]?.queries, ["busy elsewhere"]); // the other session, unaffected
  assert.deepEqual(standing(world, THREAD), [Status.ERROR]);
});

test("a session gone at continue time reacts error not a raised hand", async (t) => {
  const world = worldOf(t);
  // The held session itself closed (its thread's own entry gone too, as a SessionGone close
  // leaves it) in the gap between Continue and `submit()`: the retry's own fresh lookup finds
  // nothing, so `SessionGone` propagates instead of a plain `SessionClosed`.
  await startAHold(world);
  const holdId = buttonValue(postedBlocks(world), HOLD_CONTINUE);
  const session = world.sessions.get(CHANNEL, THREAD);
  assert.ok(session !== null);
  internals(session).closedFlag = true; // as if something else had closed it while the hold was open
  world.state.removeThread(CHANNEL, THREAD);
  try {
    await world.dispatch(clickIn(HOLD_CONTINUE, holdId, CHANNEL, THREAD));
    assert.deepEqual(world.clients.at(-1)?.queries, []); // never sent
    assert.deepEqual(standing(world, THREAD), [Status.ERROR]);
    assert.ok(said(world).includes(texts.SESSION_GONE));
  } finally {
    // `closedFlag` was set directly above, bypassing the real teardown: finished properly here
    // even when an assertion fails, or the fixture's own `closeAll` waits for ever on this
    // object's `doneClosing` and the failure is never reported.
    await session.close();
  }
});

test("a session closed at continue time retries and sends without reacting error", async (t) => {
  const world = worldOf(t);
  // The held session closed (its thread's own entry intact, unlike a `SessionGone` close) in
  // the gap between Continue and `submit()`: the retry's own fresh lookup rebuilds a live
  // session and sends normally, so the original object's ✋ must not be turned into ❌.
  await startAHold(world);
  const holdId = buttonValue(postedBlocks(world), HOLD_CONTINUE);
  const session = world.sessions.get(CHANNEL, THREAD);
  assert.ok(session !== null);
  internals(session).closedFlag = true; // an idle close, most likely; the thread's own entry survives
  session.doneClosing.set(); // what the real teardown this stands in for always fires
  try {
    await world.dispatch(clickIn(HOLD_CONTINUE, holdId, CHANNEL, THREAD));
    const fresh = world.sessions.get(CHANNEL, THREAD);
    assert.ok(fresh !== null && fresh !== session);
    assert.deepEqual(world.clients.at(-1)?.queries, ["hello"]); // sent, on a freshly rebuilt client
    assert.ok(!reactionsOn(world, THREAD).includes(Status.ERROR));
  } finally {
    // `closedFlag` was set directly above, bypassing the real teardown: finished properly here
    // even when an assertion fails, or the fixture's own `closeAll` hangs behind this object.
    await session.close();
  }
});

test("a stop answer that cannot be posted is not the word s failure", async (t) => {
  const world = worldOf(t);
  await idleMessage(world, "hi", THREAD);
  world.slack.responses["chat.postMessage"] = new Error("network down");
  await world.dispatch(reply("!stop", THREAD));
  // One attempt at the answer, and no `ERROR_REPLY` for a stop that did what it was asked.
  assert.equal(
    world.slack
      .callsTo("chat.postMessage")
      .map((post) => post.text)
      .at(-1),
    texts.NOTHING_TO_STOP_THREAD,
  );
  assert.deepEqual(world.ephemerals(), []);
});

test("stop in the held thread cancels it", async (t) => {
  const world = worldOf(t);
  await startAHold(world);
  await world.dispatch(reply("!stop", THREAD));
  assertHeldUnsent(world);
  // `!stop` cancelled the hold: `Not sent.` alone, from the waiter, since nothing Claude Code
  // itself was doing stopped (no separate "Nothing is running..." on top of it).
  assert.deepEqual(world.ephemerals(), [texts.NOT_SENT]);
  assert.ok(!said(world).includes(texts.NOT_SENT)); // for the owner alone: no push
});

test("a top level stop of the channel cancels the hold", async (t) => {
  const world = worldOf(t);
  await startAHold(world);
  await world.dispatch(message("!stop"));
  assertHeldUnsent(world);
  assert.ok(world.ephemerals().includes(texts.NOT_SENT));
});

test("a top level stop with only a cancelled hold says nothing else", async (t) => {
  const world = worldOf(t);
  // The busy session lives in ANOTHER channel here, so this channel's own `!stop` cancels the
  // hold and stops nothing else: `stopChannel`'s own `null` must not read as "nothing
  // stopped" and add a second, contradicting notice on top of `Not sent.`.
  await startAHold(world, OTHER_CHANNEL);
  await world.dispatch(message("!stop"));
  assert.ok(world.ephemerals().includes(texts.NOT_SENT));
  assert.ok(!said(world).includes(texts.NOTHING_TO_STOP));
  assert.ok(!said(world).includes(texts.STOPPED_CHANNEL));
});

test("a drain cancels the hold", async (t) => {
  const world = worldOf(t);
  await startAHold(world);
  const cutShort = new AbortController();
  cutShort.abort(); // returns as soon as the per-session cancellation pass is done
  await world.sessions.drain(cutShort.signal);
  await world.idle();
  assertHeldUnsent(world);
  assert.ok(world.ephemerals().includes(texts.NOT_SENT));
});

// Python looked for the session's `idle-close-<channel>-<thread>` task by name; the timer here
// sleeps on the sessions' clock, so a clock that passes the whole wait shows whether it is armed.
test("the idle close timer does not fire while held", async (t) => {
  const world = worldOf(t);
  await startAHold(world);
  const session = world.sessions.get(CHANNEL, THREAD);
  assert.ok(session !== null);
  assert.ok(session.waitingForOwner);
  await world.idle(); // a timer armed meanwhile has started its sleep
  await world.clock.advance(IDLE_CLOSE_SECONDS + 1); // never armed while held
  assert.equal(session.closed, false);
  assert.equal(world.sessions.get(CHANNEL, THREAD), session);
});

test("the raised hand shows while held and clears on cancel", async (t) => {
  const world = worldOf(t);
  await startAHold(world);
  const session = world.sessions.get(CHANNEL, THREAD);
  assert.ok(session !== null);
  assert.deepEqual(standing(world, THREAD), [Status.WAITING]);
  const holdId = buttonValue(postedBlocks(world), HOLD_CANCEL);
  await world.dispatch(clickIn(HOLD_CANCEL, holdId, CHANNEL, THREAD));
  assert.deepEqual(standing(world, THREAD), []); // nothing to go back to: a session that never ran
});

test("the other session finishing does not skip the question", async (t) => {
  const world = worldOf(t);
  await startAHold(world);
  const holdId = buttonValue(postedBlocks(world), HOLD_CONTINUE);
  const other = world.sessions.get(CHANNEL, OTHER_THREAD);
  assert.ok(other !== null);
  world.clients[0]?.answer(sdkMessages("tools"));
  await world.until(() => other.idle);
  assertHeldUnsent(world); // the hold still waits: nobody re-checked on its own
  await world.dispatch(clickIn(HOLD_CONTINUE, holdId, CHANNEL, THREAD));
  assert.deepEqual(world.clients.at(-1)?.queries, ["hello"]);
});

test("an unpostable question fails closed", async (t) => {
  const world = worldOf(t);
  postAnswers(world, texts.HOLD_QUESTION.split("{")[0] as string, () => new Error("boom"));
  await world.dispatch(message("busy elsewhere", { ts: OTHER_THREAD }));
  await world.dispatch(message("hello", { ts: THREAD }));
  assertHeldUnsent(world);
  assert.equal(world.ephemerals().at(-1), texts.HOLD_UNPOSTED);
});

/** A stop under way: the drain runs until the test cuts it short. */
async function draining(world: World): Promise<() => Promise<void>> {
  const cutShort = new AbortController();
  const drain = world.sessions.drain(cutShort.signal);
  await world.idle();
  return async () => {
    cutShort.abort();
    await drain;
  };
}

test("a message during a drain is refused not held", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("busy elsewhere", { ts: OTHER_THREAD }));
  const cut = await draining(world); // the drain has set its flags before this message arrives
  await world.dispatch(message("hello", { ts: THREAD }));
  // The busy session of the other thread holds the stop: the refusal names it (issue #119).
  assert.ok(
    (world.ephemerals().at(-1) as string).startsWith(
      `${texts.RESTARTING}\n${texts.RESTART_WAITS_FOR}`,
    ),
  );
  assert.deepEqual(holdQuestions(world), []); // never held: a hold opened now would wait for ever
  await cut();
});

test("a message queued behind a drain cancelled hold is also refused", async (t) => {
  const world = worldOf(t);
  await startAHold(world); // THREAD's own "hello" is held
  // "again" now waits behind "hello" for the thread's arrival lock: the dispatch returns while
  // its handler waits.
  await world.dispatch(reply("again", THREAD));
  const cutShort = new AbortController();
  cutShort.abort();
  await world.sessions.drain(cutShort.signal); // cancels "hello"'s hold
  await world.idle();
  assertHeldUnsent(world); // neither "hello" nor "again" was ever sent
  assert.ok(world.ephemerals().includes(texts.NOT_SENT)); // "hello", cancelled by the drain
  // "again", refused once draining had begun; the session busy elsewhere still holds the stop.
  assert.ok((world.ephemerals().at(-1) as string).startsWith(texts.RESTARTING));
});

// Python cancelled the task awaiting the hold's future. No handler is cancelled in this daemon:
// the wait ends only through `Holds` (a click, `!stop`, a drain). So the same two outcomes are
// read after a wait that ends with no answer: the marker leaves `waitingForOwner`, and the
// question's id is gone from `holds`.
test("a cancelled wait does not leak the hold", async (t) => {
  const world = worldOf(t);
  await startAHold(world);
  const holdId = lastHoldId(world);
  const session = world.sessions.get(CHANNEL, THREAD);
  assert.ok(session !== null);
  assert.ok(session.waitingForOwner);
  assert.notEqual(world.holds.get(holdId), null);
  world.holds.cancel(CHANNEL, THREAD);
  await world.idle();
  assert.ok(!session.waitingForOwner); // `HOLD_MARKER` did not stay in `waiting`
  assert.equal(world.holds.get(holdId), null); // no leaked entry
});

test("a hold decided before its message ts is known does not flicker", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("busy elsewhere", { ts: OTHER_THREAD }));
  // The question is posted, but not yet recorded in `holds`: Python gated the post's answer,
  // here the stop (`holds.cancel`, exactly what `!stop` does) lands inside the post, once Slack
  // has the message and before the daemon has its ts, as `answerInsidePost` does for a click.
  postAnswers(world, texts.HOLD_QUESTION.split("{")[0] as string, () => {
    world.holds.cancel(CHANNEL, THREAD);
    return undefined;
  });
  await world.dispatch(message("hello", { ts: THREAD })); // Start on the setup, then the question
  await world.idle();
  assert.ok(world.ephemerals().includes(texts.NOT_SENT));
  const added = world.slack.callsTo("reactions.add").map((args) => args.name);
  // Only each setup's own ✋ (two threads opened): `holdStart`/`holdEnd` never ran for the
  // question, so it added none.
  assert.equal(added.filter((name) => name === Status.WAITING).length, 2);
});

test("a report turn during a hold keeps the raised hand", async (t) => {
  const world = worldOf(t);
  const turns = splitTurns(sdkMessages("background"));
  await world.dispatch(message("start it", { ts: THREAD }));
  const target = world.sessions.get(CHANNEL, THREAD);
  assert.ok(target !== null);
  world.clients[0]?.answer(turns[0] ?? []);
  await world.until(() => !target.busy && target.runningKinds !== "");
  // THREAD already has a running task, so OTHER_THREAD's own first message is held too:
  // Continue lets it become genuinely busy (never completes) before the real case below.
  await world.dispatch(message("busy elsewhere", { ts: OTHER_THREAD }));
  const holdId = buttonValue(postedBlocks(world), HOLD_CONTINUE);
  await world.dispatch(clickIn(HOLD_CONTINUE, holdId, CHANNEL, OTHER_THREAD));
  assert.equal(world.clients.length, 2);
  await world.dispatch(reply("more", THREAD)); // held: OTHER_THREAD busy, THREAD is not
  assert.ok(target.waitingForOwner);
  for (const turn of turns.slice(1)) world.clients[0]?.inject(turn);
  await world.idle(); // the background task's own report turn runs during the hold
  assert.deepEqual(standing(world, THREAD).at(-1), Status.WAITING); // the report turn did not show ⏳ over it
});

test("cancel after a finished report turn shows done not a stale reaction", async (t) => {
  const world = worldOf(t);
  const turns = splitTurns(sdkMessages("background"));
  await world.dispatch(message("start it", { ts: THREAD }));
  const target = world.sessions.get(CHANNEL, THREAD);
  assert.ok(target !== null);
  world.clients[0]?.answer(turns[0] ?? []);
  await world.until(() => !target.busy && target.runningKinds !== "");
  await world.dispatch(message("busy elsewhere", { ts: OTHER_THREAD })); // held too: see test above
  const holdId = buttonValue(postedBlocks(world), HOLD_CONTINUE);
  await world.dispatch(clickIn(HOLD_CONTINUE, holdId, CHANNEL, OTHER_THREAD));
  assert.equal(world.clients.length, 2);
  await world.dispatch(reply("more", THREAD)); // held: OTHER_THREAD busy, THREAD is not
  for (const turn of turns.slice(1)) world.clients[0]?.inject(turn);
  await world.until(() => target.runningKinds === ""); // the background task finishes reporting
  const cancelId = buttonValue(holdQuestions(world).at(-1)?.blocks as Body[], HOLD_CANCEL);
  await world.dispatch(clickIn(HOLD_CANCEL, cancelId, CHANNEL, THREAD));
  await world.idle();
  // Not the stale snapshot from `holdStart` (WAITING, its own ✋): the session actually
  // finished its report turn during the hold, so cancelling now shows done.
  assert.deepEqual(standing(world, THREAD), [Status.DONE]);
});
