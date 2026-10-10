/**
 * The sessions as a whole: what a thread's session does from a prompt to its reply, what ends
 * it, and what the manager does across threads. Port of `tests/test_sessions.py`, in its order.
 *
 * Python's fake stood in for the SDK's client; here the scripted agent stands at the agent seam
 * (`test/support/sessions.ts`). A test of what the back end does with a start (the SDK's own
 * options, the `--chrome` argument) is the agent module's, and is not here.
 */
import assert from "node:assert/strict";
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmdirSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { type TestContext, test } from "node:test";
import { agentInfo } from "../../../src/agent/claude/info.ts";
import { questionResult } from "../../../src/agent/claude/requests.ts";
import { ResumeRefused } from "../../../src/agent/seam.ts";
import type { Reply } from "../../../src/chat/seam.ts";
import { Status } from "../../../src/chat/slack/reply/status.ts";
import type { TurnRenderer } from "../../../src/core/reply/renderer.ts";
import { APPROVE, type Outcome } from "../../../src/core/requests.ts";
import {
  ASKED_LIMIT,
  INJECTED_TURN_WAIT,
  STOP_TAIL_WAIT,
} from "../../../src/core/sessions/constants.ts";
import { resolveDirectory } from "../../../src/core/sessions/directory.ts";
import { SessionManager } from "../../../src/core/sessions/manager.ts";
import { ThreadSession } from "../../../src/core/sessions/session.ts";
import { asked } from "../../../src/core/sessions/turn.ts";
import { StateStore } from "../../../src/core/state.ts";
import * as texts from "../../../src/core/texts.ts";
import { setLevel, setWriter } from "../../../src/log.ts";
import {
  AsyncEvent,
  CHANNEL,
  FakeSlack,
  networkDown,
  OTHER_THREAD,
  rejected,
  THREAD,
} from "../../support/fake-slack.ts";
import { type JsonObject, sdkJson } from "../../support/fixtures.ts";
import {
  canUseToolCall,
  END_OF_STREAM,
  type Harness,
  harnessFor,
  hookRun,
  type Item,
  isRecord,
  isResult,
  isSystem,
  recordOf,
  sdkMessages,
  splitTurns,
  WRITES,
} from "../../support/sessions.ts";
import {
  inside,
  isReport,
  logged,
  pending,
  renamedBackground,
  splitBackground,
  systemRecord,
  toolUseOf,
  withTaskStatus,
} from "./helpers.ts";

const NESTED = texts.NESTED;

function at<T>(list: readonly T[], index: number): T {
  const found = list.at(index);
  if (found === undefined) throw new Error(`nothing at ${index}`);
  return found;
}

function thread(h: Harness, threadTs: string = THREAD) {
  const stored = h.state.thread(CHANNEL, threadTs);
  assert.ok(stored !== null);
  return stored;
}

/** An object a record holds under `key`; empty when the item is no record or holds none. */
function part(item: Item | undefined, key: string): JsonObject {
  const value = recordOf(item)?.[key];
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value : {};
}

function blocksOf(args: JsonObject | undefined): JsonObject[] {
  return (args?.blocks ?? []) as JsonObject[];
}

/** Resolve the one request that waits, as the owner's click would. */
function resolve(h: Harness, outcome: Outcome): void {
  const approvalId = at(pending(h.approvals), 0);
  assert.notEqual(h.approvals.resolve(approvalId, CHANNEL, THREAD, outcome), null);
}

/** The recorded question the SDK asked permission for (`ask-can-use-tool.json`). */
function recordedQuestion(): { toolName: string; input: JsonObject; questions: JsonObject[] } {
  const recorded = sdkJson("ask-can-use-tool") as JsonObject;
  const input = recorded.input as JsonObject;
  return {
    toolName: String(recorded.tool_name),
    input,
    questions: input.questions as JsonObject[],
  };
}

/** Each question answered with its first option. */
function firstOptions(questions: readonly JsonObject[]): Record<string, string> {
  return Object.fromEntries(
    questions.map((q) => [String(q.question), String(at(q.options as JsonObject[], 0).label)]),
  );
}

/** What Python raised at connect for a session id Claude Code no longer has. */
function gone(): Error {
  return new ResumeRefused();
}

test("a turn replies in the thread and records the session", async (t) => {
  const turnMessages = sdkMessages("tools");
  const h = harnessFor(t)({ turns: [turnMessages] });
  const turn = await h.session().submit("list the files");
  await turn.done.wait();
  assert.deepEqual(h.clients[0]?.queries, ["list the files"]);
  assert.ok(h.slack.callsTo("chat.postMessage").every((args) => args.thread_ts === THREAD));
  const result = turnMessages.at(-1);
  assert.ok(isResult(result));
  assert.equal(thread(h).sessionId, recordOf(result)?.session_id);
  const [stop, ...more] = h.slack.callsTo("chat.stopStream");
  assert.equal(more.length, 0);
  assert.equal(blocksOf(stop).at(-1)?.type, "context"); // the footer closes the reply
});

test("the client is launched as the design says", async (t) => {
  // What the core asks of the seam. What the back end makes of it for the SDK (partial
  // messages, the extra arguments, the permission callback, the CLI's path) is the agent
  // module's to test.
  const h = harnessFor(t)({});
  await h.session().ensureConnected();
  assert.deepEqual(h.clients[0]?.options, {
    folder: h.tmpPath,
    resume: null,
    settingsSources: ["user", "project", "local"],
    model: null,
    effort: null,
    permissionMode: null,
  });
});

test("a restart resumes the stored session", async (t) => {
  const h = harnessFor(t)({});
  h.session(); // opens the thread
  h.state.setSession(CHANNEL, THREAD, "stored-session");
  await h.session().ensureConnected();
  assert.equal(h.clients[0]?.options.resume, "stored-session");
});

test("clear records the new session id", async (t) => {
  const [first, second] = splitTurns(sdkMessages("clear")) as [Item[], Item[]];
  const h = harnessFor(t)({ turns: [first, second] });
  const session = h.session();
  await (await session.submit("hi")).done.wait();
  await (await session.submit("/clear")).done.wait();
  assert.ok(isResult(second.at(-1)));
  assert.equal(thread(h).sessionId, recordOf(second.at(-1))?.session_id);
});

test("a gone session is removed and says so", async (t) => {
  // A stored session id that fails to resume no longer falls back to a fresh session.
  const h = harnessFor(t)({ startError: gone() });
  h.session(); // opens the thread
  h.state.setSession(CHANNEL, THREAD, "gone");
  const turn = await h.session().submit("hello");
  await turn.done.wait();
  assert.deepEqual(
    h.clients.map((client) => client.options.resume),
    ["gone"],
  );
  assert.deepEqual(h.bodies(), [texts.SESSION_GONE]);
  assert.equal(h.state.thread(CHANNEL, THREAD), null);
  assert.equal(h.manager.get(CHANNEL, THREAD), null);
  assert.equal(h.slack.pushes(), 1); // the reply's stream stops once
});

test("a gone session fails a queued turn and leaks no process", async (t) => {
  // Regression: the worker used to take the next queued turn after SessionGone and start a
  // second, unrecorded Claude Code session for it instead of refusing.
  const h = harnessFor(t)({ startError: gone() });
  const session = h.session(); // opens the thread
  h.state.setSession(CHANNEL, THREAD, "gone");
  const first = await session.submit("hello");
  const second = await session.submit("again");
  await Promise.all([first.done.wait(), second.done.wait()]);
  // Only the one failed attempt: no second client started for the queued turn.
  assert.deepEqual(
    h.clients.map((client) => client.options.resume),
    ["gone"],
  );
  assert.deepEqual(h.bodies(), [texts.SESSION_GONE, texts.SESSION_GONE]);
  // The worker must not be left spinning on an empty queue nobody will ever fill again. Python
  // looked for its task among the loop's; here the task says whether it has ended.
  await h.idle();
  assert.equal(inside(session).worker?.done, true);
  assert.equal(inside(session).idleExpiry, null);
  await h.manager.closeAll();
  assert.ok(h.clients.every((client) => !client.connected));
});

/** Hold each `submit` where its reply is made, one gate per call, as Python patched `_sink`. */
function gatedSinks(t: TestContext, gates: readonly AsyncEvent[]): { entered: number } {
  const state = { entered: 0 };
  const prototype = ThreadSession.prototype as unknown as { sink(): Reply | Promise<Reply> };
  const real = prototype.sink;
  t.mock.method(prototype, "sink", async function (this: ThreadSession) {
    const reply = await real.call(this);
    const gate = at(gates, state.entered);
    state.entered += 1;
    await gate.wait();
    return reply;
  });
  return state;
}

test("a turn that races sessiongone while its sink is made gets session gone", async (t) => {
  // `ensureConnected` finds the stored session gone, closes the session and drains the queue
  // (empty at that point). The second turn is queued only after that: nothing will ever take
  // it from an ended worker, so it must be resolved right there, as gone too.
  const h = harnessFor(t)({ startError: gone() });
  const session = h.session();
  h.state.setSession(CHANNEL, THREAD, "gone");
  const gates = [new AsyncEvent(), new AsyncEvent()];
  const sinks = gatedSinks(t, gates);
  const firstTask = session.submit("hello");
  await h.until(() => sinks.entered === 1); // "hello" is paused while its sink is made
  const secondTask = session.submit("again");
  await h.until(() => sinks.entered === 2); // "again" is paused there too
  at(gates, 0).set(); // let "hello" queue itself and start its worker
  const firstTurn = await firstTask;
  await firstTurn.done.wait();
  assert.ok(session.closed); // the SessionGone branch has already closed and drained the queue
  at(gates, 1).set(); // only now does "again" reach the point where it would enqueue itself
  const secondTurn = await secondTask;
  await secondTurn.done.wait();
  assert.deepEqual(h.bodies(), [texts.SESSION_GONE, texts.SESSION_GONE]);
  await h.idle();
  assert.equal(inside(session).worker?.done, true);
  await h.manager.closeAll();
});

test("a turn that races a plain close while its sink is made gets session closed", async (t) => {
  // Whatever closed the session while the sink was made, it must not always be answered as
  // SessionGone: a plain close (an idle close, a restart) leaves the thread's own entry in
  // state.json, unlike SessionGone's close, and that is the only thing telling the two apart
  // once the session is closed either way.
  const h = harnessFor(t)({});
  const session = h.session();
  const gate = new AsyncEvent();
  const sinks = gatedSinks(t, [gate]);
  const submitTask = session.submit("hello");
  await h.until(() => sinks.entered === 1); // paused while its sink is made
  await session.close(); // a plain close: the thread's entry stays in state.json
  assert.notEqual(h.state.thread(CHANNEL, THREAD), null);
  gate.set();
  const turn = await submitTask;
  await turn.done.wait();
  assert.deepEqual(h.bodies(), [texts.SESSION_CLOSED]);
});

test("a direct sessiongone call leaves no idle timer task", async (t) => {
  // `!status` (or `!bypass`) can hit SessionGone with no worker ever created: the idle-close
  // timer armed when the session was handed out must not be left running past the close.
  const h = harnessFor(t)({ startError: gone() });
  const session = h.session(); // touched on hand-out: its idle-close timer is armed already
  h.state.setSession(CHANNEL, THREAD, "gone");
  await session.status();
  assert.ok(session.closed);
  await h.idle();
  assert.equal(inside(session).idleExpiry, null);
  assert.equal(inside(session).worker, null);
});

test("a direct gone call rescues the worker s taken turn", async (t) => {
  // A direct call (`!status`) found the session gone while the worker, having taken a turn,
  // waited on the same connect lock: cancelling the worker (to end it) lost that turn:
  // cancelled in its wait, it never reached its own catch, so the taken turn was never failed
  // and the reply stayed "writing" forever.
  const gate = new AsyncEvent();
  const h = harnessFor(t)({ startError: gone(), startGate: gate });
  const session = h.session();
  h.state.setSession(CHANNEL, THREAD, "gone");
  const statusTask = session.status(); // holds the connect lock, gated
  await h.until(() => h.clients.length === 1);
  const turn = await session.submit("hello");
  await h.until(() => inside(session).taken !== null); // the worker took it, now waits on the lock
  gate.set();
  await statusTask;
  assert.ok(session.closed);
  await turn.done.wait(); // must not hang
  assert.deepEqual(h.bodies(), [texts.SESSION_GONE]);
});

test("a restart rebuilds a stored thread with its bypass", async (t) => {
  const h = harnessFor(t)({}, {});
  const session = h.session();
  await session.setBypass(true);
  assert.deepEqual(h.clients[0]?.modes, ["bypassPermissions"]);
  assert.ok(session.bypass);
  const path = join(h.tmpPath, "state.json");
  const stored = JSON.parse(readFileSync(path, "utf8"));
  assert.ok(stored.channels[CHANNEL].threads[THREAD].bypass);
  // A new daemon: state.json read again, a new manager, a new Claude Code process.
  const reloaded = { ...h.deps, state: new StateStore(path) };
  const restarted = new SessionManager(reloaded).get(CHANNEL, THREAD);
  assert.ok(restarted?.bypass);
  await restarted.ensureConnected();
  assert.deepEqual(h.clients[1]?.modes, ["bypassPermissions"]);
  await restarted.setBypass(false);
  assert.equal(h.clients[1]?.modes.at(-1), "default");
  assert.ok(!new StateStore(path).thread(CHANNEL, THREAD)?.bypass);
  await restarted.close();
});

test("queue waits while approval pending", async (t) => {
  const ask = canUseToolCall("Bash", { command: "ls" });
  const h = harnessFor(t)({ turns: [[ask, ...sdkMessages("tools")], sdkMessages("tools")] });
  const session = h.session();
  const first = await session.submit("first");
  const second = await session.submit("second");
  await h.until(() => h.slack.callsTo("chat.postMessage").some((args) => "blocks" in args));
  await h.sleep(0.05);
  assert.deepEqual(h.clients[0]?.queries, ["first"]);
  assert.ok(session.busy);
  resolve(h, APPROVE);
  await second.done.wait();
  assert.ok(first.done.isSet);
  assert.deepEqual(h.clients[0]?.queries, ["first", "second"]);
  assert.deepEqual(h.clients[0]?.permissionResults[0], { allow: true });
});

test("restart hold says what a stop waits for in the thread", async (t) => {
  const h = harnessFor(t)({
    turns: [[canUseToolCall("Bash", { command: "rm -rf build" }), ...sdkMessages("interrupt")]],
  });
  const session = h.session();
  assert.equal(session.restartHold, ""); // nothing runs: the thread holds no stop
  const turn = await session.submit("clean");
  await h.until(() => pending(h.approvals).length > 0);
  assert.equal(session.restartHold, texts.RESTART_HOLD_OWNER);
  assert.deepEqual(h.manager.restartHolds(), []); // no stop under way
  await session.stop();
  await turn.done.wait();
  assert.equal(session.restartHold, "");
});

test("stop landed gives up when the stopped turn never ends", async (t) => {
  const h = harnessFor(t)({ turns: [[canUseToolCall("Bash", { command: "rm -rf build" })]] });
  const session = h.session();
  const turn = await session.submit("clean");
  await h.until(() => pending(h.approvals).length > 0);
  assert.equal(await session.stop(), true);
  // Python lowered STOP_TAIL_WAIT and waited it out; here the sessions' clock crosses it.
  let landed = false;
  const waiting = session.stopLanded().then(() => {
    landed = true;
  });
  await h.clock.advance(STOP_TAIL_WAIT - 1);
  assert.equal(landed, false);
  await h.clock.advance(1);
  await waiting;
  assert.ok(!turn.done.isSet); // it did not end: the wait is bounded
});

test("stop denies pending and interrupts", async (t) => {
  const h = harnessFor(t)({
    turns: [[canUseToolCall("Bash", { command: "rm -rf build" }), ...sdkMessages("interrupt")]],
  });
  const session = h.session();
  const turn = await session.submit("clean");
  await h.until(() => pending(h.approvals).length > 0);
  assert.equal(await session.stop(), true);
  // The answer to the stop waits for the reply it cut short: it sits under its footer.
  await session.stopLanded();
  assert.ok(turn.done.isSet);
  assert.equal(h.clients[0]?.interrupts, 1);
  const denied = at(h.clients[0]?.permissionResults ?? [], 0);
  assert.ok("allow" in denied && !denied.allow);
  assert.equal(h.slack.callsTo("chat.delete").length, 1);
  // Crash repair (issue #19): `deleteRequest` clears the state entry too.
  assert.deepEqual(thread(h).requests, []);
  assert.equal(await session.stop(), false);
});

test("ask user question returns answers", async (t) => {
  const recorded = recordedQuestion();
  const call = canUseToolCall(recorded.toolName, recorded.input);
  const h = harnessFor(t)({ turns: [[call, ...sdkMessages("tools")]] });
  const turn = await h.session().submit("ask me");
  await h.until(() => pending(h.approvals).length > 0);
  const answers = firstOptions(recorded.questions);
  resolve(h, { kind: "answer", answers });
  await turn.done.wait();
  const result = at(h.clients[0]?.permissionResults ?? [], 0);
  assert.ok("answered" in result && result.answered);
  assert.deepEqual(result.answers, answers);
  // As the back end hands it to the SDK: allowed, with the questions as asked and the answers.
  assert.deepEqual(questionResult(result, recorded.input), {
    behavior: "allow",
    updatedInput: { questions: recorded.questions, answers },
  });
});

/**
 * The recorded turn of an answered question (ask-answered.jsonl, CLI 2.1.286), with the
 * permission request where the CLI makes it: after the call, before its result. With it, the
 * call's id, which the request carries (measured), and the call's input.
 */
function answeredTurn(): [batch: Item[], callId: string, input: JsonObject] {
  const messages = sdkMessages("ask-answered");
  const calling = messages.find((item) => toolUseOf(item) !== null);
  const callId = toolUseOf(calling);
  assert.ok(callId !== null);
  const content = part(calling, "message").content as JsonObject[];
  const call = at(
    content.filter((block) => block.type === "tool_use"),
    0,
  );
  const where = messages.findIndex((item) => isRecord(item, "user"));
  const ask = canUseToolCall(String(call.name), call.input as JsonObject, callId);
  return [
    [...messages.slice(0, where), ask, ...messages.slice(where)],
    callId,
    call.input as JsonObject,
  ];
}

test("an answered question stays in the reply and its request goes", async (t) => {
  // Issue #82: the answers show under the call's line, where the question was asked, so what
  // Claude does next shows below them; the request message has done its job.
  const [batch, , askedInput] = answeredTurn();
  const questions = askedInput.questions as JsonObject[];
  const h = harnessFor(t)({ turns: [batch] });
  const turn = await h.session().submit("ask me");
  await h.until(() => pending(h.approvals).length > 0);
  const requestTs = at(h.slack.postedTs, -1);
  assert.deepEqual(thread(h).requests, [requestTs]);
  resolve(h, { kind: "answer", answers: firstOptions(questions) });
  await turn.done.wait();
  assert.deepEqual(
    h.slack.callsTo("chat.delete").map((args) => args.ts),
    [requestTs],
  );
  assert.ok(h.slack.callsTo("chat.update").every((args) => args.ts !== requestTs));
  assert.deepEqual(thread(h).requests, []);
  const cards = h.slack.messageCards().flat();
  const [card, ...more] = cards.filter((c) => c.title === texts.ANSWERED);
  assert.equal(more.length, 0);
  assert.equal(card?.status, "complete");
  const shown = h.slack.apiCalls
    .flatMap((call) => (call.args.chunks ?? []) as JsonObject[])
    .filter((chunk) => chunk.type === "blocks")
    .flatMap((chunk) => chunk.blocks as JsonObject[])
    .filter((block) => block.type === "context")
    .map((block) => String(at(block.elements as JsonObject[], 0).text));
  const first = at(questions, 0);
  const label = at(first.options as JsonObject[], 0).label;
  assert.ok(shown[0]?.includes(`· ${first.question} → ${label}`));
});

test("answers the reply cannot show stay in the request", async (t) => {
  // A question whose call the reply holds no line for (asked inside a subagent, say): the
  // request is rewritten into the record, with no buttons, as the terminal keeps it.
  const recorded = recordedQuestion();
  const call = canUseToolCall(recorded.toolName, recorded.input, "toolu_unseen");
  const h = harnessFor(t)({ turns: [[call, ...sdkMessages("tools")]] });
  const turn = await h.session().submit("ask me");
  await h.until(() => pending(h.approvals).length > 0);
  const requestTs = at(h.slack.postedTs, -1);
  resolve(h, { kind: "answer", answers: firstOptions(recorded.questions) });
  await turn.done.wait();
  assert.deepEqual(h.slack.callsTo("chat.delete"), []);
  const [update, ...more] = h.slack.callsTo("chat.update").filter((args) => args.ts === requestTs);
  assert.equal(more.length, 0);
  const text = String(at(at(blocksOf(update), 0).elements as JsonObject[], 0).text);
  const first = at(recorded.questions, 0);
  assert.ok(text.startsWith(`${texts.ANSWERED}\n${NESTED}· ${first.question} → `));
  assert.deepEqual(
    blocksOf(update).map((block) => block.type),
    ["context"],
  );
  assert.deepEqual(thread(h).requests, []);
});

test("a record slack refuses removes the request", async (t) => {
  const recorded = recordedQuestion();
  const call = canUseToolCall(recorded.toolName, recorded.input, "toolu_unseen");
  const h = harnessFor(t)({ turns: [[call, ...sdkMessages("tools")]] });
  const turn = await h.session().submit("ask me");
  await h.until(() => pending(h.approvals).length > 0);
  const requestTs = at(h.slack.postedTs, -1);
  h.slack.responses["chat.update"] = { ok: false, error: "msg_too_long" };
  resolve(h, { kind: "answer", answers: firstOptions(recorded.questions) });
  await h.until(() => h.slack.callsTo("chat.delete").length > 0);
  // Its buttons would no longer work: the request goes.
  assert.deepEqual(
    h.slack.callsTo("chat.delete").map((args) => args.ts),
    [requestTs],
  );
  delete h.slack.responses["chat.update"];
  await turn.done.wait();
});

test("an approval request is tracked in state while it is open", async (t) => {
  // Deleting the answered request's own message, which clears it from state too
  // (`ThreadSession.deleteRequest`), is the click handler's job: out of scope for a raw
  // `Approvals.resolve` call, as "queue waits while approval pending" already makes for the
  // approval itself.
  const ask = canUseToolCall("Bash", { command: "ls" });
  const h = harnessFor(t)({ turns: [[ask, ...sdkMessages("tools")]] });
  const session = h.session();
  await session.submit("list the files");
  await h.until(() => pending(h.approvals).length > 0);
  assert.deepEqual(thread(h).requests, [at(h.slack.postedTs, -1)]);
});

test("the status field tracks working then clears once done", async (t) => {
  const h = harnessFor(t)({ turns: [sdkMessages("tools")] });
  const session = h.session();
  const turn = await session.submit("list the files");
  assert.equal(thread(h).status, Status.WORKING);
  await turn.done.wait();
  await h.until(() => thread(h).status === null);
});

test("the ended field keeps the roots final reaction until work starts again", async (t) => {
  const h = harnessFor(t)({ turns: [sdkMessages("tools"), sdkMessages("tools")] });
  const session = h.session();
  const turn = await session.submit("list the files");
  assert.equal(thread(h).ended, null);
  await turn.done.wait();
  await h.until(() => thread(h).ended === Status.DONE);
  await session.submit("and again");
  const stored = thread(h);
  assert.deepEqual([stored.status, stored.ended], [Status.WORKING, null]);
});

test("a close that cuts a turn short keeps x as the ended reaction", async (t) => {
  const ask = canUseToolCall("Bash", { command: "rm -rf build" });
  const h = harnessFor(t)({ turns: [[ask, ...sdkMessages("tools")]] });
  const session = h.session();
  await session.submit("clean");
  await h.until(() => pending(h.approvals).length > 0);
  await session.close();
  const stored = thread(h);
  assert.deepEqual([stored.status, stored.ended], [null, Status.ERROR]);
});

test("close leaves every repair field cleared", async (t) => {
  const ask = canUseToolCall("Bash", { command: "rm -rf build" });
  const h = harnessFor(t)({ turns: [[ask, ...sdkMessages("tools")]] });
  const session = h.session();
  await session.submit("clean");
  await h.until(() => pending(h.approvals).length > 0);
  const approvalId = at(pending(h.approvals), 0);
  const requestTs = h.approvals.get(approvalId)?.messageTs;
  await session.close();
  const stored = thread(h);
  assert.deepEqual(stored.openReplies, []);
  assert.deepEqual(stored.requests, []);
  assert.equal(stored.status, null);
  // Issue #19 fix round item 8: the pending approval's own message is actually deleted too.
  const deleted = h.slack.callsTo("chat.delete").map((args) => args.ts);
  assert.deepEqual(deleted, [requestTs]);
});

test("waiting for owner reflects an open approval", async (t) => {
  const ask = canUseToolCall("Bash", { command: "ls" });
  const h = harnessFor(t)({ turns: [[ask, ...sdkMessages("tools")]] });
  const session = h.session();
  await session.submit("list the files");
  await h.until(() => pending(h.approvals).length > 0);
  assert.equal(session.waitingForOwner, true);
  resolve(h, APPROVE);
  await h.until(() => session.waitingForOwner === false);
});

test("running kinds reflects a task that outlives its turn", async (t) => {
  const first = at(splitTurns(sdkMessages("background")), 0);
  const h = harnessFor(t)({ turns: [first] });
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  assert.equal(session.busy, false);
  assert.ok(session.runningKinds); // a task still runs, though the session itself is not busy
});

test("no reply ever carries a channel mention", async (t) => {
  // `<!channel>` was removed with the thread model: a full turn that ends with a closing
  // message, posts an approval request and a question request must never bring it back.
  const recorded = recordedQuestion();
  const ask = canUseToolCall("Bash", { command: "ls" });
  const question = canUseToolCall(recorded.toolName, recorded.input);
  const h = harnessFor(t)({ turns: [[ask, question, ...sdkMessages("tools")]] });
  const turn = await h.session().submit("do it");
  await h.until(() => pending(h.approvals).length > 0);
  const approvalId = at(pending(h.approvals), 0);
  resolve(h, APPROVE);
  await h.until(() => pending(h.approvals).some((id) => id !== approvalId));
  resolve(h, { kind: "answer", answers: firstOptions(recorded.questions) });
  await turn.done.wait();
  for (const call of h.slack.apiCalls) {
    assert.ok(!JSON.stringify(call.args).includes("<!channel>"));
  }
});

test("a turn with a task an approval and a question is one stream and two posts", async (t) => {
  // A turn that starts a background task, asks for an approval and a question, then the report
  // turn for that task, writes exactly the approval request, the question request, and one
  // stream for the reply, which stops once, when the task's report is in. Nothing else.
  const [first, notice, injected] = splitBackground();
  const recorded = recordedQuestion();
  const ask = canUseToolCall("Bash", { command: "ls" });
  const question = canUseToolCall(recorded.toolName, recorded.input);
  const h = harnessFor(t)({ turns: [[ask, question, ...first]] });
  const session = h.session();
  const turn = await session.submit("do it and start something");
  await h.until(() => pending(h.approvals).length > 0);
  const approvalId = at(pending(h.approvals), 0);
  resolve(h, APPROVE);
  await h.until(() => pending(h.approvals).some((id) => id !== approvalId));
  resolve(h, { kind: "answer", answers: firstOptions(recorded.questions) });
  await turn.done.wait();
  h.clients[0]?.inject([...notice, ...injected]);
  await h.until(() => h.slack.callsTo("chat.stopStream").length > 0);
  await h.sleep(0.05);
  const posts = h.slack.callsTo("chat.postMessage");
  // Exactly these, in this order, nothing else.
  assert.deepEqual(
    posts.map((post) => post.text),
    ["Bash: ls", "AskUserQuestion"],
  );
  assert.ok(h.slack.streamTs.length === 1 && h.slack.callsTo("chat.stopStream").length === 1);
  assert.equal(h.slack.pushes(), 3);
});

test("asked cuts a long prompt and names an image only prompt", () => {
  const longPrompt = "word ".repeat(30); // more characters than ASKED_LIMIT
  const cut = asked(longPrompt);
  assert.ok(Array.from(cut).length === ASKED_LIMIT && cut.endsWith("…"));
  const image = { type: "image", mediaType: "image/png", data: "x" } as const;
  assert.equal(asked([image]), texts.PROMPT_IMAGE);
  assert.equal(asked([{ type: "text", text: "look at this" }, image]), "look at this");
});

test("an injected turn edits the reply that started the task", async (t) => {
  const turns = splitTurns(sdkMessages("background"));
  const h = harnessFor(t)({ turns: [at(turns, 0)] });
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  for (const later of turns.slice(1)) h.clients[0]?.inject(later);
  await h.until(() => isReport(h.bodies()[0] ?? ""));
  await h.sleep(0.05);
  // The report is appended to the one reply there is: no separate message carries it.
  assert.ok(h.slack.streamTs.length === 1 && h.slack.postedTs.length === 0);
});

test("rate limit event invalidates usage", async (t) => {
  const events = sdkMessages("tools").filter((item) => isRecord(item, "rate_limit_event"));
  // the spec measured one RateLimitEvent on a client's first turn
  assert.ok(events.length > 0);
  const h = harnessFor(t)({ turns: [sdkMessages("tools")] });
  const session = h.session();
  await (await session.submit("a")).done.wait();
  await h.until(() => h.usageFetches === 1);
  await h.deps.usage.refreshIfStale();
  assert.equal(h.usageFetches, 1); // still fresh
  h.clients[0]?.inject([at(events, 0)]);
  await h.sleep(0.05);
  await h.deps.usage.refreshIfStale();
  assert.equal(h.usageFetches, 2);
});

test("logs hold no message content", async (t) => {
  const h = harnessFor(t)({ turns: [sdkMessages("tools")] });
  // Every line the daemon logs, down to DEBUG: the log's own writer, and the console the
  // modules with a logger of their own still write to.
  const lines: string[] = [];
  const writer = setWriter((line) => lines.push(line));
  setLevel("DEBUG");
  t.mock.method(console, "error", (...words: unknown[]) => lines.push(words.join(" ")));
  t.mock.method(console, "warn", (...words: unknown[]) => lines.push(words.join(" ")));
  try {
    const turn = await h.session().submit("SECRET-PROMPT-CONTENT");
    await turn.done.wait();
    await h.idle();
  } finally {
    setLevel("INFO");
    setWriter(writer);
  }
  assert.ok(!lines.join("\n").includes("SECRET-PROMPT-CONTENT"));
});

test("an api error is logged by category and thread never by its text", async (t) => {
  const messages = sdkMessages("server-error");
  const [words, ...more] = messages.filter(isResult).map((item) => String(recordOf(item)?.result));
  assert.ok(more.length === 0 && words);
  const h = harnessFor(t)({ turns: [messages] });
  const logs = [...(["info", "warning", "error"] as const)].map((level) => logged(t, level));
  const session = h.session();
  const turn = await session.submit("hello");
  await turn.done.wait();
  const lines = logs.flat();
  const [line, ...others] = lines.filter((m) => m.includes("reported an error"));
  assert.equal(others.length, 0);
  assert.ok(line?.endsWith(`${session.channelId}/${session.threadTs}: server_error`));
  assert.ok(!lines.some((m) => m.includes(words)));
});

test("bind is refused while a thread of the channel is busy", async (t) => {
  const ask = canUseToolCall("Bash", { command: "ls" });
  const h = harnessFor(t)({ turns: [[ask, ...sdkMessages("tools")]] });
  const session = h.session();
  await session.submit("list the files");
  await h.until(() => pending(h.approvals).length > 0);
  const other = join(h.tmpPath, "other");
  mkdirSync(other);
  assert.equal(await h.manager.bind(CHANNEL, other), false);
  assert.equal(h.state.channel(CHANNEL)?.directory, h.tmpPath);
  resolve(h, APPROVE);
  await h.until(() => session.idle);
});

test("bind accepted when every thread is idle keeps the old thread s folder", async (t) => {
  const h = harnessFor(t)({ turns: [sdkMessages("tools")] });
  const session = h.session();
  await (await session.submit("list the files")).done.wait();
  const other = join(h.tmpPath, "other");
  mkdirSync(other);
  assert.equal(await h.manager.bind(CHANNEL, other), true);
  assert.equal(h.state.channel(CHANNEL)?.directory, other);
  // The thread already open is untouched: same folder, same live session, no closing.
  assert.equal(thread(h).directory, h.tmpPath);
  assert.ok(session.directory === h.tmpPath && h.session() === session);
  assert.equal(h.clients[0]?.connected, true);
  // A thread opened after the bind gets the new folder.
  const fresh = h.manager.open(CHANNEL, OTHER_THREAD);
  assert.equal(fresh?.directory, other);
});

test("two threads of one channel are two sessions with separate clients", async (t) => {
  const h = harnessFor(t)({ turns: [sdkMessages("tools")] }, { turns: [sdkMessages("tools")] });
  const first = h.manager.open(CHANNEL, THREAD);
  const second = h.manager.open(CHANNEL, OTHER_THREAD);
  assert.ok(first !== null && second !== null && first !== second);
  assert.equal(h.manager.get(CHANNEL, THREAD), first); // the same live session, not rebuilt
  await (await first.submit("a")).done.wait();
  await (await second.submit("b")).done.wait();
  assert.equal(h.clients.length, 2);
  assert.deepEqual(h.clients[0]?.queries, ["a"]);
  assert.deepEqual(h.clients[1]?.queries, ["b"]);
  assert.deepEqual(new Set(h.manager.sessionsOf(CHANNEL)), new Set([first, second]));
  const starts = h.slack.callsTo("chat.startStream");
  assert.deepEqual(new Set(starts.map((p) => p.thread_ts)), new Set([THREAD, OTHER_THREAD]));
});

test("stop channel stops every busy session of the channel", async (t) => {
  const turn = [canUseToolCall("Bash", { command: "rm -rf build" }), ...sdkMessages("interrupt")];
  const h = harnessFor(t)({ turns: [turn] }, { turns: [turn] });
  const first = h.manager.open(CHANNEL, THREAD);
  const second = h.manager.open(CHANNEL, OTHER_THREAD);
  assert.ok(first !== null && second !== null);
  const a = await first.submit("a");
  const b = await second.submit("b");
  await h.until(() => pending(h.approvals).length === 2);
  assert.equal(await h.manager.stopChannel(CHANNEL), true);
  await Promise.all([a.done.wait(), b.done.wait()]);
  assert.ok(h.clients[0]?.interrupts === 1 && h.clients[1]?.interrupts === 1);
  assert.equal(h.reactions(THREAD).at(-1), Status.DONE); // a stop is not an error
  assert.equal(h.reactions(OTHER_THREAD).at(-1), Status.DONE);
  assert.equal(await h.manager.stopChannel(CHANNEL), false);
});

test("resolve directory", (t) => {
  const tmpPath = realpathSync(mkdtempSync(join(tmpdir(), "awd-resolve-")));
  t.after(() => rmSync(tmpPath, { recursive: true, force: true }));
  const root = join(tmpPath, "root");
  mkdirSync(join(root, "app"), { recursive: true });
  writeFileSync(join(root, "file.txt"), "x");
  mkdirSync(join(tmpPath, "outside"));
  symlinkSync(join(tmpPath, "outside"), join(root, "escape"));
  assert.equal(resolveDirectory(join(root, "app"), root), join(root, "app"));
  assert.equal(resolveDirectory(root, root), root);
  assert.equal(resolveDirectory(`${root}/app/../../outside`, root), null);
  assert.equal(resolveDirectory(join(root, "escape"), root), null);
  assert.equal(resolveDirectory(join(root, "file.txt"), root), null);
  assert.equal(resolveDirectory(join(root, "missing"), root), null);
});

// A report opens with Claude Code's notification summary (recorded: `Background command "..."
// completed (exit code 0)`, `Agent "..." finished`), never with a tool line.

test("a report opens with claude code s summary and takes the footer", async (t) => {
  const [first, notice, injected] = splitBackground();
  const h = harnessFor(t)({ turns: [first] });
  await (await h.session().submit("start it")).done.wait();
  h.clients[0]?.inject([...notice, ...injected]);
  await h.until(() => isReport(h.bodies()[0] ?? ""));
  await h.sleep(0.05);
  const report = at(h.bodies(), 0); // appended to the one reply there is
  const summary = systemRecord(notice, "task_notification").summary;
  assert.ok(report.includes(`✓ ${summary}`)); // Claude Code's own words, as in the terminal
  assert.ok(!report.includes(texts.BACKGROUND_NOTICE));
  // The task has now fully ended (report in, nothing else owed): the closing message that
  // waited for it posts at last, with its footer.
  const closing = at(h.slack.messageBlocks(), -1);
  assert.ok(closing.some((block) => block.type === "divider"));
});

test("a report turn s writes debounce like any other reply", async (t) => {
  // The reply it renders into has already finished its body, which used to make every
  // streamed delta flush its own `chat.update` instead of debouncing.
  const [first, notice, injected] = splitBackground();
  const deltas = injected.filter((item) => {
    const event = recordOf(item)?.event as JsonObject | undefined;
    const delta = event?.delta as JsonObject | undefined;
    return (
      isRecord(item, "stream_event") &&
      event?.type === "content_block_delta" &&
      delta?.type === "text_delta"
    );
  }).length;
  assert.ok(deltas > 3); // the recording should have enough deltas to prove debouncing
  const h = harnessFor(t)({ turns: [first] });
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  const before = h.slack.callsTo("chat.update").length;
  h.clients[0]?.inject([...notice, ...injected]);
  await h.until(() => isReport(h.bodies()[0] ?? ""));
  await h.sleep(0.2);
  const updates = h.slack.callsTo("chat.update").length - before;
  assert.ok(updates <= 3, `${updates} chat.update calls for one report turn (no debounce)`);
});

test("an owner answer behind a wrong guess keeps its footer", async (t) => {
  const [first, notice] = splitBackground();
  const h = harnessFor(t)({ turns: [first] });
  await (await h.session().submit("start it")).done.wait();
  h.clients[0]?.inject(notice); // the session now expects Claude Code's own turn
  await h.sleep(0.05);
  h.clients[0]?.inject(sdkMessages("tools")); // but the result says a person asked for it
  await h.until(() => h.slack.callsTo("chat.stopStream").length > 0);
  // the answer joins the reply that waited for the task, which ends with the footer
  const stop = h.slack.callsTo("chat.stopStream").at(-1);
  assert.ok(blocksOf(stop).some((block) => block.type === "divider"));
});

test("a task type is forgotten when the task ends", async (t) => {
  const [first, notice, injected] = splitBackground();
  const h = harnessFor(t)({ turns: [first] });
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  assert.ok(inside(session).tasks.size > 0);
  h.clients[0]?.inject([...notice, ...injected]);
  await h.until(() => isReport(h.bodies()[0] ?? ""));
  assert.equal(inside(session).tasks.size, 0);
});

// A background agent's end, as the SDK delivered it (recorded 2026-09-24 by
// actions/scripts/2026-09-24-task-notification-summary-probe.py): its summary is the agent's
// result, not a status line.
function agentEnd(taskId: string, toolUseId: string): Item {
  return {
    record: {
      type: "system",
      subtype: "task_notification",
      task_id: taskId,
      tool_use_id: toolUseId,
      status: "completed",
      output_file: "/home/dev/tasks/out",
      summary: "| File | Lines |\n|---|---|\n| README.md | 3 |",
      usage: { total_tokens: 11181, tool_uses: 0, duration_ms: 10400 },
      session_id: "00000000-0000-0000-0000-000000000001",
      uuid: "00000000-0000-0000-0000-000000000002",
    },
  };
}

test("a background agent s end reads as in the terminal", async (t) => {
  const first = at(splitTurns(sdkMessages("subagent")), 0);
  const started = systemRecord(first, "task_started");
  const h = harnessFor(t)({ turns: [first] });
  await (await h.session().submit("start it")).done.wait();
  assert.ok(typeof started.tool_use_id === "string");
  h.clients[0]?.inject([
    agentEnd(String(started.task_id), started.tool_use_id),
    ...sdkMessages("tools"),
  ]);
  await h.until(() => isReport(h.bodies()[0] ?? ""));
  await h.sleep(0.05);
  // appended to the one reply there is, not a body of its own
  const line = at(h.bodies(), 0)
    .split("\n")
    .find((candidate) => candidate.startsWith('✓ Agent "'));
  assert.ok(line !== undefined);
  assert.ok(line.startsWith(`✓ Agent "${started.description}" finished · 10s`));
  assert.ok(!line.includes("README.md"));
});

test("the task record is bounded", async (t) => {
  const [first] = splitBackground();
  const renamed = first.map((item) => {
    const record = recordOf(item);
    return record !== null && isSystem(item, "task_started")
      ? { record: { ...record, task_id: "other" } }
      : item;
  });
  // Python lowered TASKS_KEPT to 1.
  const h = harnessFor(t, { tasksKept: 1 })({ turns: [first, renamed] });
  const session = h.session();
  await (await session.submit("one")).done.wait();
  await (await session.submit("two")).done.wait();
  assert.deepEqual([...inside(session).tasks.keys()], ["other"]);
});

test("a report line never outlives its chance", async (t) => {
  const [first, notice] = splitBackground();
  const h = harnessFor(t)({ turns: [first] });
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  h.clients[0]?.inject(notice); // and Claude Code starts no turn of its own
  await h.idle();
  await h.clock.advance(INJECTED_TURN_WAIT);
  await h.idle();
  assert.deepEqual(inside(session).ended, []);
});

test("the task bookkeeping goes with the process", async (t) => {
  const [first] = splitBackground();
  const h = harnessFor(t)({ turns: [first] });
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  assert.ok(inside(session).tasks.size > 0);
  h.clients[0]?.inject([END_OF_STREAM]);
  await h.until(() => inside(session).client === null);
  assert.ok(inside(session).tasks.size === 0 && inside(session).ended.length === 0);
});

function notNotification(item: Item): boolean {
  return !isSystem(item, "task_notification");
}

test("a suppressed notification s closing still posts eventually", async (t) => {
  // The CLI can suppress the notification altogether (SDK TaskUpdatedMessage docstring). Only
  // the terminal task_updated arrives; the closing message must still post once
  // INJECTED_TURN_WAIT passes, not wait on it forever.
  const [first, notice] = splitBackground();
  const h = harnessFor(t)({ turns: [first] });
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  const a = inside(session).taskReplies.get("bny2rux7d");
  assert.ok(a !== undefined);
  h.clients[0]?.inject(notice.filter(notNotification));
  await h.idle();
  await h.clock.advance(INJECTED_TURN_WAIT);
  await h.until(() => a.closedOut, 1.0);
});

test("a report whose target already closed out gets its own reply", async (t) => {
  // A notification arriving later than INJECTED_TURN_WAIT lets `expireUnreported` close the
  // reply out first; the report turn the CLI starts once that late notification finally comes
  // must not render into that already-closed reply (an edit, which never notifies, with any
  // overflow posting below the closing it can no longer touch): it gets a fresh reply.
  const [first, notice, injected] = splitBackground();
  const ended = notice.filter(notNotification);
  const notification = notice.filter((item) => !notNotification(item));
  const h = harnessFor(t)({ turns: [first] });
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  const a = inside(session).taskReplies.get("bny2rux7d");
  assert.ok(a !== undefined);
  h.clients[0]?.inject(ended);
  await h.idle();
  await h.clock.advance(INJECTED_TURN_WAIT);
  // closed out already, on the late-notification path
  await h.until(() => a.closedOut, 1.0);
  h.clients[0]?.inject([...notification, ...injected]);
  await h.until(() => h.replies().some(isReport));
  await h.sleep(0.05);
  assert.ok(!isReport(at(h.bodies(), 0))); // a's own body is untouched
  assert.ok(h.bodies().slice(1).some(isReport)); // the report got a reply of its own
});

test("the unreported expiry timer does not outlive close", async (t) => {
  // `expireUnreported`'s own task lives in a set of its own, which `close` must cancel along
  // with everything else, or it would try to post through a session that is already gone once
  // its wait finally elapses.
  const [first, notice] = splitBackground();
  const h = harnessFor(t)({ turns: [first] });
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  h.clients[0]?.inject(notice.filter(notNotification));
  await h.until(() => inside(session).unreported.size > 0);
  const timers = [...inside(session).expiring];
  assert.ok(timers.length > 0);
  await session.close(texts.ENDED_IDLE);
  assert.ok(timers.every((timer) => timer.done));
  assert.ok([...inside(session).expiring].every((timer) => timer.done));
});

test("closing a session does not cancel a pending usage refresh", async (t) => {
  // A usage refresh shares one probe, and its one client, across every session; a refresh cut
  // in the middle would leave that shared client answering the next lookup, of any session,
  // late. `close` cancels its own timers, never a refresh. Python read the refresh's task;
  // here the refresh is given no signal at all, and the test reads that it is still under way.
  const h = harnessFor(t)({});
  const session = h.session();
  const started = new AsyncEvent();
  const release = new AsyncEvent();
  let finished = false;
  t.mock.method(h.deps.usage, "refreshIfStale", async () => {
    started.set();
    await release.wait();
    finished = true;
  });
  inside(session).refreshUsage();
  await started.wait();
  await session.close(texts.ENDED_IDLE);
  assert.equal(finished, false);
  release.set(); // nothing else will, once the test is done with it
});

/** Hold the next `chat.stopStream` open: the end of a reply is where the expiry's work holds. */
function armGate(h: Harness): AsyncEvent {
  const gate = new AsyncEvent();
  h.slack.gate = gate;
  h.slack.gated.clear();
  return gate;
}

function openGate(h: Harness, gate: AsyncEvent): void {
  h.slack.gate = null;
  gate.set();
}

/**
 * The wait for a report turn runs out: Python lowered INJECTED_TURN_WAIT and let it pass, here
 * the sessions' clock crosses it once what was injected has been read.
 */
async function reportTurnNeverCame(h: Harness): Promise<void> {
  await h.idle();
  await h.clock.advance(INJECTED_TURN_WAIT);
}

function taskReply(session: ThreadSession, taskId: string): TurnRenderer {
  const reply = inside(session).taskReplies.get(taskId);
  assert.ok(reply !== undefined);
  return reply;
}

test("a report turn starting over the expiry s write leaves the root on done", async (t) => {
  // The report turn starts after INJECTED_TURN_WAIT, while `expireInjectedTurn` already writes
  // the end of the reply that started the task: `startTurn` cancels that write, which must not
  // leave the reply's end unresolved (and ⏳ on the root for good).
  const [first, notice, injected] = splitBackground();
  const h = harnessFor(t)({ turns: [first] });
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  const a = taskReply(session, "bny2rux7d");
  const gate = armGate(h);
  h.clients[0]?.inject(notice);
  await reportTurnNeverCame(h);
  await h.slack.gated.wait(); // the expiry's end is inside its write
  h.clients[0]?.inject(injected); // the report turn starts over that write
  await h.until(() => inside(session).active !== null, 1.0);
  openGate(h, gate);
  await h.until(() => h.reactions().at(-1) === Status.DONE && session.idle, 3.0);
  assert.equal(inside(session).unlanded.size, 0);
  assert.equal(await a.sink.waitLanded(), true);
});

test("two notifications a moment apart end both replies and show done", async (t) => {
  const [first, notice, injected] = splitBackground();
  const [firstB, noticeB] = renamedBackground();
  const h = harnessFor(t)({ turns: [first, firstB] });
  const session = h.session();
  await (await session.submit("start A")).done.wait();
  await (await session.submit("start B")).done.wait();
  const a = taskReply(session, "bny2rux7d");
  const b = taskReply(session, "bc41other");
  const gate = armGate(h);
  h.clients[0]?.inject(notice);
  await reportTurnNeverCame(h);
  await h.slack.gated.wait(); // the expiry's end of A is in its write
  h.clients[0]?.inject([...noticeB, ...injected]); // the report turn starts over it
  await h.until(() => inside(session).active !== null, 1.0);
  openGate(h, gate);
  await h.until(() => a.closedOut && b.closedOut, 2.0);
  await h.until(() => h.reactions().at(-1) === Status.DONE && session.idle, 3.0);
  assert.ok(inside(session).unlanded.size === 0 && !inside(session).injectedExpected);
  assert.equal(await a.sink.waitLanded(), true);
  assert.equal(await b.sink.waitLanded(), true);
  assert.ok(![...h.slack.messages.values()].some((message) => message.streaming));
});

test("a report turn keeps the reply it renders into open while the expiry sweeps", async (t) => {
  // Guard against letting the expiry's work run beside a turn (`startTurn`'s cancel is the
  // exclusion). Replies A and B each hold a task. The first notification's wait passes and the
  // expiry's sweep stops on the end of that reply; the second notification arrives and the
  // report turn starts, rendering into the second reply. A sweep that resumed under the turn
  // would close that reply with its snapshot of the active turn (none). Python's sweep walked
  // a set ordered by hash and the test fixed that order; here the sweep walks the replies in
  // the order their tasks started, which is the order the test needs (A first).
  const [first, notice, injected] = splitBackground();
  const [firstB, noticeB] = renamedBackground();
  const h = harnessFor(t)({ turns: [first, firstB] });
  const session = h.session();
  await (await session.submit("start A")).done.wait();
  await (await session.submit("start B")).done.wait();
  const target = taskReply(session, "bc41other"); // the second notified: the turn renders there
  const gate = armGate(h);
  h.clients[0]?.inject(notice);
  await reportTurnNeverCame(h);
  await h.slack.gated.wait();
  h.clients[0]?.inject([...noticeB, ...injected.slice(0, 3)]); // the turn's first message starts it
  await h.until(() => inside(session).active !== null, 1.0);
  openGate(h, gate);
  await h.sleep(0.05); // whatever the sweep still does, it has done by now
  assert.ok(!target.closedOut);
  h.clients[0]?.inject(injected.slice(3));
  await h.until(() => target.closedOut, 3.0);
  await h.until(() => ![...h.slack.messages.values()].some((message) => message.streaming), 2.0);
  const shown = at(h.slack.messageBlocks(), 1);
  // the report turn's footer, not the first's
  assert.ok(String(at(at(shown, -1).elements as JsonObject[], 0).text).includes("49.2k"));
});

test("an owner prompt sent during the expiry s work runs after it", async (t) => {
  const [first, notice] = splitBackground();
  const h = harnessFor(t)({ turns: [first] });
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  const a = taskReply(session, "bny2rux7d");
  const gate = armGate(h);
  h.clients[0]?.inject(notice);
  await reportTurnNeverCame(h);
  await h.slack.gated.wait(); // the expiry's end is inside its write
  const turn = await session.submit("and now?");
  await h.until(() => h.clients[0]?.queries.at(-1) === "and now?", 1.0);
  h.clients[0]?.answer(sdkMessages("tools")); // its turn starts over that write
  await h.until(() => inside(session).active !== null, 1.0);
  openGate(h, gate);
  await turn.done.wait();
  await h.until(() => h.reactions().at(-1) === Status.DONE && session.idle, 2.0);
  assert.equal(inside(session).unlanded.size, 0);
  assert.equal(await a.sink.waitLanded(), true);
});

test("a close during the expiry s work ends promptly and leaks no task", async (t) => {
  const [first, notice] = splitBackground();
  const h = harnessFor(t)({ turns: [first] });
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  const a = taskReply(session, "bny2rux7d");
  // Python slowed its fake Slack by a tenth of a second and timed the close: `close` settles
  // every reply, which waits for the write in flight. Here that write is held at a gate and let
  // go once the close has reached it, and no clock is read.
  const gate = armGate(h);
  h.clients[0]?.inject(notice);
  await reportTurnNeverCame(h);
  await h.until(() => a.closedOut, 1.0); // the expiry is writing
  await h.slack.gated.wait();
  const working = inside(session).expiry;
  let closed = false;
  const closing = session.close().then(() => {
    closed = true;
  });
  await h.idle();
  assert.equal(closed, false); // the close waits for the write Slack still holds, and no longer
  openGate(h, gate);
  await closing;
  await h.sleep(0.2); // the write Slack still held answers, then nothing is left
  // Python looked for a live `_expire_injected_turn` or `_end_out` task.
  assert.ok(working?.done && inside(session).expiry === working);
  assert.equal(await a.sink.waitLanded(), true);
});

/**
 * Two replies, each holding a background task, and the Slack gate armed on the first
 * notification's expiry (the end of a reply is where it holds). Returns the records of the
 * first notification, those of the second, the report turn, the second reply and the gate.
 */
async function twoRepliesWithAGate(
  h: Harness,
  session: ThreadSession,
): Promise<[Item[], Item[], Item[], TurnRenderer, AsyncEvent]> {
  const [, notice, injected] = splitBackground();
  const [, noticeB] = renamedBackground();
  await (await session.submit("start A")).done.wait();
  await (await session.submit("start B")).done.wait();
  const gate = armGate(h);
  return [notice, noticeB, injected, taskReply(session, "bc41other"), gate];
}

function twoBackgrounds(t: TestContext): [Harness, ThreadSession] {
  const [first] = splitBackground();
  const [firstB] = renamedBackground();
  const h = harnessFor(t)({ turns: [first, firstB] });
  return [h, h.session()];
}

test("a notification during the expiry s standalone write waits for its own turn", async (t) => {
  // The expiry posts a background update (a task of no tracked reply) and a second notification
  // arrives while that write is out. Its wait starts when the write ends, not never: the
  // session keeps reading as waiting for the report turn, and the owner's prompt stays behind
  // it.
  const [h, session] = twoBackgrounds(t);
  const [notice, noticeB, injected, b, gate] = await twoRepliesWithAGate(h, session);
  inside(session).taskReplies.delete("bny2rux7d"); // A's reply is gone: its records are held
  h.clients[0]?.inject(notice);
  await reportTurnNeverCame(h);
  await h.slack.gated.wait(); // the expiry's standalone is in its write
  // The next wait outlives the test: the sessions' clock is not moved again.
  h.clients[0]?.inject(noticeB);
  await h.until(() => inside(session).injectedExpected);
  const working = inside(session).expiry;
  openGate(h, gate);
  await h.until(() => inside(session).expiry !== working); // the wait of the second notification
  assert.equal(h.slack.createdTs.length, 3); // the expiry posted the held update, a reply of its own
  assert.equal(inside(session).expiry?.done, false);
  assert.ok(inside(session).injectedExpected && !inside(session).settled.isSet && !session.idle);
  await session.submit("and now?");
  await h.sleep(0.1);
  assert.deepEqual(h.clients[0]?.queries, ["start A", "start B"]); // held behind the report turn
  assert.ok(!h.reactions().slice(2).includes(Status.DONE));
  h.clients[0]?.inject(injected);
  await h.until(() => b.closedOut && inside(session).settled.isSet, 3.0);
  assert.ok(!inside(session).injectedExpected);
});

test("a notification during the expiry s sweep gets its wait started after it", async (t) => {
  // The sweep ends A's reply while the second notification arrives: with nothing armed for it
  // the session would read as waiting for ever, holding every prompt behind a turn that may
  // never come. Its own wait runs out, and the session reads idle again.
  const [h, session] = twoBackgrounds(t);
  const [notice, noticeB, , b, gate] = await twoRepliesWithAGate(h, session);
  const a = taskReply(session, "bny2rux7d");
  h.clients[0]?.inject(notice);
  await reportTurnNeverCame(h);
  await h.slack.gated.wait(); // the sweep is in the end of A
  h.clients[0]?.inject(noticeB);
  await h.until(() => inside(session).injectedExpected);
  const working = inside(session).expiry;
  openGate(h, gate);
  await h.until(() => inside(session).expiry !== working);
  await reportTurnNeverCame(h); // the second notification's own wait runs out
  await h.until(() => a.closedOut && b.closedOut, 3.0);
  await h.until(() => h.reactions().at(-1) === Status.DONE && session.idle, 3.0);
  assert.ok(!inside(session).injectedExpected && inside(session).settled.isSet);
  assert.equal(inside(session).unlanded.size, 0);
});

test("a close while a notification waits after the expiry s work leaks no task", async (t) => {
  const [h, session] = twoBackgrounds(t);
  const [notice, noticeB, , , gate] = await twoRepliesWithAGate(h, session);
  h.clients[0]?.inject(notice);
  await reportTurnNeverCame(h);
  await h.slack.gated.wait();
  h.clients[0]?.inject(noticeB);
  await h.until(() => inside(session).injectedExpected);
  const working = inside(session).expiry;
  openGate(h, gate);
  await h.until(() => inside(session).expiry !== working);
  const waiting = inside(session).expiry;
  assert.ok(waiting !== null && !waiting.done);
  await session.close();
  assert.ok(waiting.done);
  // Python looked for a live `_expire_injected_turn` task: none was made after this one.
  assert.equal(inside(session).expiry, waiting);
});

test("a close during the expiry s write with a notification pending arms no timer", async (t) => {
  // The expiry task `close` is cancelling must not arm the wait of the notification that came
  // during its write: `close` cancels twice, which is all that would catch such a timer.
  // Python counted the timers made; here a timer is a task the session keeps, and the one the
  // session holds after the close is still the one that was writing.
  const [h, session] = twoBackgrounds(t);
  const [notice, noticeB, , , gate] = await twoRepliesWithAGate(h, session);
  inside(session).taskReplies.delete("bny2rux7d"); // A's records are held: the expiry posts them
  h.clients[0]?.inject(notice);
  await reportTurnNeverCame(h);
  await h.slack.gated.wait(); // the standalone is in its write
  h.clients[0]?.inject(noticeB);
  await h.until(() => inside(session).injectedExpected);
  const writing = inside(session).expiry;
  const closing = session.close();
  await h.until(() => session.closed);
  openGate(h, gate);
  await closing;
  const calls = h.slack.apiCalls.length;
  await h.sleep(0.2);
  await h.clock.advance(INJECTED_TURN_WAIT);
  assert.ok(writing?.done && inside(session).expiry === writing); // the one that was writing, and no other
  assert.equal(h.slack.apiCalls.length, calls); // nothing writes to Slack after the close
});

test("a sweep that fails still arms the wait of a notification that came during it", async (t) => {
  const [h, session] = twoBackgrounds(t);
  const [notice, noticeB] = await twoRepliesWithAGate(h, session);
  h.slack.gate = null;
  let sweeps = 0;
  t.mock.method(inside(session), "sweepClosedOut", async (signal?: AbortSignal) => {
    // `expireUnreported` sweeps too: only the expiry's own sweep fails. Python asked which
    // task was running; here the sweep is handed the signal of the task that calls it.
    if (signal === undefined || signal !== inside(session).expiry?.signal) return;
    sweeps += 1;
    if (sweeps === 1) {
      h.clients[0]?.inject(noticeB); // a notification arrives during the sweep's writes
      await h.until(() => inside(session).injectedExpected);
      throw new Error("the sweep failed");
    }
  });
  h.clients[0]?.inject(notice);
  await h.idle();
  // The timer of the first notification, which does the sweep: read before its wait runs out,
  // since here the failed sweep is over by the time the clock has been moved.
  const working = inside(session).expiry;
  assert.ok(working !== null);
  await h.clock.advance(INJECTED_TURN_WAIT);
  await h.until(() => sweeps === 1);
  await h.until(() => inside(session).expiry !== working);
  assert.equal(inside(session).expiry?.done, false);
  assert.ok(inside(session).injectedExpected && !inside(session).settled.isSet);
});

test("owner query waits for an expected background turn", async (t) => {
  const [first, notice, injected] = splitBackground();
  assert.ok(notice.some((item) => isSystem(item, "task_notification")));
  const h = harnessFor(t)({ turns: [first, sdkMessages("tools")] });
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  h.clients[0]?.inject(notice);
  await h.sleep(0.05);
  const second = await session.submit("next");
  await h.sleep(0.05);
  assert.deepEqual(h.clients[0]?.queries, ["start it"]);
  h.clients[0]?.inject(injected);
  await second.done.wait();
  assert.deepEqual(h.clients[0]?.queries, ["start it", "next"]);
  const background = h.bodies().map(isReport);
  // The report is appended to the reply that started the task, not a new one of its own.
  assert.deepEqual(background, [true, false]);
});

/** The running counts a message's footer shows, if any. */
function runningBlock(blocks: readonly JsonObject[]): string | null {
  const shown = blocks.filter((block) => !String(block.block_id ?? "").startsWith("spacer"));
  const last = shown.at(-1);
  const footer = last?.type === "context" ? last : null;
  const text = footer === null ? "" : String(at(footer.elements as JsonObject[], 0).text);
  return text.includes("⏳") ? text.slice(text.indexOf("⏳")) : null;
}

function noStreamOpen(h: Harness): boolean {
  return ![...h.slack.messages.values()].some((message) => message.streaming);
}

test("running counts follow the latest reply", async (t) => {
  const [first, notice, injected] = splitBackground();
  const h = harnessFor(t)({ turns: [first, sdkMessages("tools")] });
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  // The running count lives in the footer, and that one waits for the task; the body never
  // carries it either, so nothing shows it yet.
  assert.ok(h.slack.messageBlocks().every((blocks) => runningBlock(blocks) === null));
  await (await session.submit("next")).done.wait();
  // the first reply's own stop is still deferred; the new, unrelated reply stops at once and
  // carries the count in its own footer.
  assert.deepEqual(h.slack.callsTo("chat.delete"), []);
  assert.equal(runningBlock(at(h.slack.messageBlocks(), -1)), "⏳ 1 shell");
  h.clients[0]?.inject([...notice, ...injected]);
  await h.until(() => isReport(h.bodies()[0] ?? ""));
  // the task has ended: no reply's footer shows a running count any more. The second reply
  // already stopped, so this update of it debounces like any other change to a finished reply:
  // `until` gives it the room to land.
  await h.until(() => h.slack.messageBlocks().every((blocks) => runningBlock(blocks) === null));
});

test("two prompts in a row each reply ends with its own stop", async (t) => {
  const [first, notice, injected] = splitBackground();
  const h = harnessFor(t)({ turns: [first, sdkMessages("tools")] });
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  // The first reply's stream stays open for its background task: nothing has stopped yet.
  assert.deepEqual([h.slack.streamTs.length, h.slack.callsTo("chat.stopStream").length], [1, 0]);
  await (await session.submit("next")).done.wait();
  // the second reply has nothing of its own pending: its stream stops right away, the footer
  // on the stop.
  assert.deepEqual([h.slack.streamTs.length, h.slack.callsTo("chat.stopStream").length], [2, 1]);
  assert.ok(at(h.slack.messageBlocks(), -1).some((block) => block.type === "divider"));
  h.clients[0]?.inject([...notice, ...injected]);
  await h.until(() => h.slack.callsTo("chat.stopStream").length === 2);
  // the first reply stops now, once its task has fully ended, with a footer of its own: no
  // longer the latest, it still says how its last turn ended. The second's stop is untouched.
  const last = h.slack.callsTo("chat.stopStream").at(-1);
  assert.ok(blocksOf(last).some((block) => block.type === "divider"));
  assert.equal(h.slack.pushes(), 2);
});

test("two prompts tasks ending together each reply ends", async (t) => {
  // The CLI's one report turn opens with the end of every task that finished together but
  // renders into only the first one's reply; the second must still end, not wait forever for a
  // report turn that was never coming for it specifically.
  const [first, notice, injected] = splitBackground();
  const [firstB, noticeB] = renamedBackground();
  const h = harnessFor(t)({ turns: [first, firstB] });
  const session = h.session();
  await (await session.submit("start A")).done.wait();
  await (await session.submit("start B")).done.wait();
  const a = taskReply(session, "bny2rux7d");
  const b = taskReply(session, "bc41other");
  assert.notEqual(a, b);
  // Both tasks end while idle, then Claude Code's one report turn, which renders into A's
  // reply (the first of the two ended tasks).
  h.clients[0]?.inject([...notice, ...noticeB, ...injected]);
  await h.until(() => a.closedOut);
  // Python's lowered wait could pass while it polled: the sessions' clock crosses it here.
  await h.clock.advance(INJECTED_TURN_WAIT);
  await h.until(() => b.closedOut);
  await h.until(() => noStreamOpen(h));
  assert.equal(h.slack.callsTo("chat.stopStream").length, 2);
});

test("a background task frame stays out of other replies", async (t) => {
  const [first, notice, injected] = splitBackground();
  const h = harnessFor(t)({ turns: [first] });
  await (await h.session().submit("start it")).done.wait();
  h.clients[0]?.inject([...notice, ...injected]);
  await h.until(() => h.replies().some(isReport));
  await h.sleep(0.05);
  const report = h.replies().find(isReport);
  const taskId = String(systemRecord(notice, "task_notification").task_id);
  assert.ok(report !== undefined && !report.includes(taskId));
});

function hasParent(item: Item): boolean {
  return (recordOf(item)?.parent_tool_use_id ?? null) !== null;
}

test("a background agent s calls update its card and open no reply", async (t) => {
  const recorded = sdkMessages("subagent");
  const turn = at(splitTurns(recorded), 0).filter((item) => !hasParent(item));
  const children = recorded.filter(hasParent);
  const h = harnessFor(t)({ turns: [turn] });
  await (await h.session().submit("start it")).done.wait();
  const posted = h.slack.postedTs.length;
  h.clients[0]?.inject(children);
  // A task that outlived its own turn updating its card afterward debounces like any other
  // change: `until` gives it the room to land.
  await h.until(() => h.cards().some((card) => String(card.details ?? "").includes("Bash")));
  assert.ok(h.slack.postedTs.length === posted && h.slack.streamTs.length === 1);
  const [agent, ...more] = h.cards().filter((card) => String(card.title).startsWith("Agent"));
  assert.equal(more.length, 0);
  assert.ok(String(agent?.details).includes("Bash") && String(agent?.title).includes(" call"));
});

test("ended tasks are forgotten past the limit", async (t) => {
  const [first, notice, injected] = splitBackground();
  const renamed = first.map((item) => {
    const record = recordOf(item);
    const task = ["task_started", "task_notification", "task_updated"].some((subtype) =>
      isSystem(item, subtype),
    );
    return record !== null && task ? { record: { ...record, task_id: "other" } } : item;
  });
  // Python lowered TASK_REPLIES_KEPT to 1.
  const h = harnessFor(t, { taskRepliesKept: 1 })({ turns: [first, renamed] });
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  h.clients[0]?.inject([...notice, ...injected]);
  await h.until(() => isReport(h.bodies()[0] ?? ""));
  await (await session.submit("again")).done.wait();
  assert.deepEqual([...inside(session).taskReplies.keys()], ["other"]);
});

test("a failed background task shows why in the reply that started it", async (t) => {
  const [first, notice, injected] = splitBackground();
  // The recorded order: a terminal task_updated (no summary), then task_notification (summary).
  const failed = notice.map((item) => withTaskStatus(item, "failed"));
  const summary = String(systemRecord(notice, "task_notification").summary);
  const h = harnessFor(t)({ turns: [first] });
  await (await h.session().submit("start it")).done.wait();
  h.clients[0]?.inject([...failed, ...injected]);
  await h.until(() => h.replies().some(isReport));
  const started = at(h.replies(), 0);
  const words = summary.split(/\s+/).filter(Boolean).join(" ").slice(0, 40);
  assert.ok(started.includes("✗") && started.includes(words));
});

test("closing the session stops the cards of running tasks", async (t) => {
  const [first] = splitBackground();
  const h = harnessFor(t)({ turns: [first] });
  await (await h.session().submit("start it")).done.wait();
  await h.manager.closeAll();
  assert.equal(runningBlock(at(h.slack.messageBlocks(), 0)), null);
  assert.deepEqual(
    h.cards().map((card) => [card.status, card.output]),
    [["complete", "Stopped"]],
  );
  assert.ok(noStreamOpen(h));
});

test("a shutdown during an active turn with a background task ends both replies", async (t) => {
  // A restart or shutdown while a turn is active, with an earlier background task still
  // running: `closeReply` with `force` runs before `stopTaskReplies` has actually stopped the
  // task, and both replies still end with their streams stopped, nothing posted.
  const [first] = splitBackground();
  const partial = sdkMessages("tools")
    .filter((item) => !isResult(item))
    .slice(0, 22);
  const h = harnessFor(t)({ turns: [first, partial] }); // the second query gets no scripted result
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  await session.submit("second");
  await h.until(() => inside(session).active !== null);
  await session.close(); // a restart or a shutdown: ends everything
  assert.deepEqual(h.slack.postedTs, []);
  assert.ok(noStreamOpen(h));
  assert.equal(h.slack.streamTs.length, 2);
});

test("an idle close ends a reply that still waited for its task", async (t) => {
  // An idle close (like a restart or SessionGone) stops the still-open stream at once, with its
  // footer.
  const [first] = splitBackground();
  const h = harnessFor(t)({ turns: [first] });
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  assert.deepEqual(h.slack.callsTo("chat.stopStream"), []);
  await session.close(texts.ENDED_IDLE);
  const [stop, ...more] = h.slack.callsTo("chat.stopStream");
  assert.equal(more.length, 0);
  assert.ok(blocksOf(stop).some((block) => block.type === "divider"));
  assert.deepEqual(h.slack.postedTs, []);
});

test("a notification with no turn updates its line and releases the queue", async (t) => {
  const [first, notice] = splitBackground();
  const h = harnessFor(t)({ turns: [first, sdkMessages("tools")] });
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  h.clients[0]?.inject(notice);
  await h.sleep(0.05);
  const second = await session.submit("next");
  await reportTurnNeverCame(h); // Python lowered the wait, which passed while it waited
  await second.done.wait();
  // The task is known: its end shows on the line where it started, not in a post of its own.
  // That line updates a reply whose own turn has already finished, which debounces like any
  // other change to a finished reply: `until` gives it the room to land.
  await h.until(
    () =>
      h
        .cards()
        .map((card) => card.status)
        .join() === "complete",
  );
  assert.ok(!h.replies().some(isReport));
  assert.deepEqual(h.clients[0]?.queries, ["start it", "next"]);
});

test("a query crossing a notification never hangs", async (t) => {
  const [first, notice, injected] = splitBackground();
  const h = harnessFor(t)({ turns: [first, sdkMessages("tools"), sdkMessages("tools")] });
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  h.clients[0]?.inject(notice);
  const second = await session.submit("next");
  await second.done.wait();
  h.clients[0]?.inject(injected);
  const third = await session.submit("after");
  await third.done.wait();
  assert.deepEqual(h.clients[0]?.queries, ["start it", "next", "after"]);
});

test("a notification after the owner query was sent leaves the turn to the owner", async (t) => {
  // Measured 2026-09-24: the owner's prompt reached the CLI queue first, then the task ended;
  // the CLI reported the task inside the owner's turn and started no turn of its own.
  const [first, notice] = splitBackground();
  const h = harnessFor(t)({ turns: [first] });
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  const second = await session.submit("next");
  await h.until(() => h.clients[0]?.queries.join() === "start it,next");
  h.clients[0]?.inject(notice);
  await h.sleep(0.05);
  h.clients[0]?.answer(sdkMessages("tools"));
  await second.done.wait();
  assert.ok(!h.replies().some(isReport));
});

function loseEveryWrite(h: Harness): void {
  for (const method of WRITES) h.slack.responses[method] = networkDown();
}

test("a slack network error does not stop the session", async (t) => {
  const h = harnessFor(t)({ turns: [sdkMessages("tools"), sdkMessages("tools")] });
  loseEveryWrite(h);
  const session = h.session();
  await (await session.submit("while offline")).done.wait();
  assert.ok(h.clients[0]?.connected);
  h.slack.responses = new FakeSlack().responses;
  await (await session.submit("back online")).done.wait();
  assert.deepEqual(h.clients[0]?.queries, ["while offline", "back online"]);
  assert.equal(h.clients.length, 1);
});

test("a cli that exits ends the turn and the next message reconnects", async (t) => {
  const h = harnessFor(t)(
    { turns: [[...sdkMessages("tools").slice(0, 3), END_OF_STREAM]] },
    { turns: [sdkMessages("tools")] },
  );
  const session = h.session();
  await (await session.submit("first")).done.wait();
  await (await session.submit("second")).done.wait();
  assert.deepEqual(
    h.clients.map((client) => client.queries),
    [["first"], ["second"]],
  );
});

test("a cli that exits mid turn ends its reply once", async (t) => {
  // Cut right after the reply's first message_start: where that falls among the init, status
  // and rate-limit messages changes between CLI releases.
  const tools = sdkMessages("tools");
  const started = tools.findIndex(
    (item) => isRecord(item, "stream_event") && part(item, "event").type === "message_start",
  );
  assert.notEqual(started, -1);
  const h = harnessFor(t)({ turns: [[...tools.slice(0, started + 1), END_OF_STREAM]] });
  const session = h.session();
  await (await session.submit("first")).done.wait();
  assert.ok(h.slack.streamTs.length === 1 && h.slack.pushes() === 1);
  assert.ok(noStreamOpen(h));
});

test("a failed background post still releases the queue", async (t) => {
  const [first, notice] = splitBackground();
  const h = harnessFor(t)({ turns: [first, sdkMessages("tools")] });
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  loseEveryWrite(h);
  h.clients[0]?.inject(notice);
  await h.sleep(0.02);
  const second = await session.submit("next");
  await reportTurnNeverCame(h); // Python lowered the wait, which passed while it waited
  await second.done.wait();
  assert.deepEqual(h.clients[0]?.queries, ["start it", "next"]);
});

test("a missing directory starts nothing and says so", async (t) => {
  const h = harnessFor(t)({});
  const missing = join(h.tmpPath, "gone");
  mkdirSync(missing);
  h.state.bind(CHANNEL, missing);
  rmdirSync(missing);
  const turn = await h.session().submit("hello");
  await turn.done.wait();
  assert.deepEqual(h.clients, []);
  assert.deepEqual(h.bodies(), [texts.fill(texts.DIRECTORY_MISSING, { directory: missing })]);
  assert.equal(h.slack.pushes(), 1); // the reply's one stop: an error the owner has to act on
});

test("an unreadable directory says how to grant access", async (t) => {
  const h = harnessFor(t)({});
  const locked = join(h.tmpPath, "locked");
  mkdirSync(locked);
  h.state.bind(CHANNEL, locked);
  chmodSync(locked, 0);
  try {
    const turn = await h.session().submit("hello");
    await turn.done.wait();
  } finally {
    chmodSync(locked, 0o755);
  }
  assert.deepEqual(h.clients, []);
  assert.deepEqual(h.bodies(), [texts.fill(texts.DIRECTORY_UNREADABLE, { directory: locked })]);
  assert.equal(h.slack.pushes(), 1);
});

/** The footer (the last context block) of every write that carried one, in order. */
function statuses(h: Harness): string[] {
  const lasts: string[] = [];
  for (const call of h.slack.apiCalls) {
    const last = blocksOf(call.args).at(-1);
    if (
      ["chat.postMessage", "chat.update", "chat.stopStream"].includes(call.method) &&
      last?.type === "context"
    ) {
      lasts.push(String(at(last.elements as JsonObject[], 0).text));
    }
  }
  return lasts;
}

/** A recorded turn that ends with another result text, as a command's output would. */
function withResult(turn: readonly Item[], result: string): Item[] {
  const last = recordOf(turn.at(-1));
  assert.ok(last !== null && isResult(turn.at(-1)));
  return [...turn.slice(0, -1), { record: { ...last, result } }];
}

test("the footer follows an effort set from slack", async (t) => {
  const effortTurn = withResult(
    sdkMessages("usage"),
    "Set effort level to high (this session only): Comprehensive",
  );
  const h = harnessFor(t)({ turns: [effortTurn, sdkMessages("tools")] });
  const session = h.session();
  await (await session.submit("/effort high")).done.wait();
  await (await session.submit("next")).done.wait();
  assert.ok(statuses(h).at(-1)?.includes("*effort* high"));
});

/** A recorded turn with the CLI's Stop hook call where the CLI makes it: before the result. */
function withStopHook(turn: readonly Item[], hookInput: JsonObject): Item[] {
  return [...turn.slice(0, -1), hookRun(hookInput), at(turn, -1)];
}

test("the footer shows the effort claude code reports", async (t) => {
  // A top-level effortLevel in the settings does not decide the level (Opus 5.5 ignores the
  // user's): the level Claude Code runs at is the one its Stop hook reports.
  const hookInput = sdkJson("stop-hook") as JsonObject;
  const h = harnessFor(t)({ turns: [withStopHook(sdkMessages("tools"), hookInput)] });
  mkdirSync(join(h.tmpPath, ".claude"));
  writeFileSync(join(h.tmpPath, ".claude", "settings.json"), '{"effortLevel": "high"}');
  await (await h.session().submit("list the files")).done.wait();
  assert.ok(statuses(h).at(-1)?.includes("*effort* medium"));
});

test("the footer leaves out the effort until claude code reports it", async (t) => {
  // A local command's turn (`/usage`) runs no Stop hook: the level is not known yet.
  const h = harnessFor(t)({ turns: [sdkMessages("usage")] });
  await (await h.session().submit("/usage")).done.wait();
  assert.ok(statuses(h).length > 0 && !statuses(h).at(-1)?.includes("effort"));
});

test("the footer says default when claude code reports no effort", async (t) => {
  // Claude Code leaves the field out when the model takes no effort parameter.
  const { effort: _effort, ...hookInput } = sdkJson("stop-hook") as JsonObject;
  const h = harnessFor(t)({ turns: [withStopHook(sdkMessages("tools"), hookInput)] });
  await (await h.session().submit("list the files")).done.wait();
  assert.ok(statuses(h).at(-1)?.includes("*effort* default"));
});

test("an effort set before a restart is not carried over", async (t) => {
  // Measured 2026-09-25: a resumed session runs at the settings' level, not the `/effort` one.
  const effortTurn = withResult(
    sdkMessages("usage"),
    "Set effort level to low (this session only): Quick",
  );
  const h = harnessFor(t)(
    { turns: [effortTurn] },
    { turns: [withStopHook(sdkMessages("tools"), sdkJson("stop-hook") as JsonObject)] },
  );
  await (await h.session().submit("/effort low")).done.wait();
  assert.ok(statuses(h).at(-1)?.includes("*effort* low"));
  await h.manager.closeAll();
  await (await h.session().submit("next")).done.wait();
  assert.ok(statuses(h).at(-1)?.includes("*effort* medium"));
});

test("an approval slack refuses to show is denied and logged", async (t) => {
  // The approval request is the only post: Slack refuses it; the reply's stream is unaffected.
  const h = harnessFor(t)({
    turns: [[canUseToolCall("Bash", { command: "ls" }), ...sdkMessages("tools")]],
  });
  const errors = logged(t, "error");
  h.slack.responses["chat.postMessage"] = rejected("ratelimited");
  const turn = await h.session().submit("list the files");
  await turn.done.wait();
  assert.deepEqual(at(h.clients[0]?.permissionResults ?? [], 0), {
    allow: false,
    message: texts.APPROVAL_UNPOSTED,
  });
  const line = errors.find((said) => said.includes("could not post an approval request"));
  assert.ok(line?.includes("ratelimited"));
});

test("a footer that fails to build still ends the reply", async (t) => {
  const h = harnessFor(t)({ turns: [sdkMessages("tools")] });
  // Python replaced `git_state` with one that raised. Here the lookup `gitState` makes of the
  // back end fails, which is the one part of it the session supplies, and `gitState` lets any
  // failure but its own timeout through.
  h.backend.repository = async () => {
    throw new TypeError("invalid start byte");
  };
  const turn = await h.session().submit("list the files");
  await turn.done.wait();
  const [stop, ...more] = h.slack.callsTo("chat.stopStream");
  assert.equal(more.length, 0);
  assert.ok(stop !== undefined && !("blocks" in stop)); // no footer to show, and the stream still stopped
  assert.ok(noStreamOpen(h));
});

test("a usage entry without a token count still gets a footer", async (t) => {
  const messages = sdkMessages("tools");
  const result = recordOf(messages.at(-1));
  assert.ok(result !== null && isResult(messages.at(-1)));
  const usage = result.modelUsage as JsonObject;
  const model = at(Object.keys(usage), 0);
  const trimmed = {
    record: { ...result, modelUsage: { [model]: { inputTokens: 1000, outputTokens: 500 } } },
  };
  const h = harnessFor(t)({ turns: [[...messages.slice(0, -1), trimmed]] });
  const turn = await h.session().submit("list the files");
  await turn.done.wait();
  assert.ok(statuses(h).at(-1)?.includes("1.5k *tok*"));
});

test("a context usage failure is logged", async (t) => {
  const h = harnessFor(t)({ turns: [sdkMessages("tools")], contextUsageError: new Error("x") });
  const warnings = logged(t, "warning");
  const turn = await h.session().submit("list the files");
  await turn.done.wait();
  assert.ok(warnings.some((line) => line.includes("could not read the context usage")));
});

/** An error as a library would raise it: what a log line says of it is its name. */
function named(name: string): Error {
  return Object.assign(new Error("x"), { name });
}

test("a failed disconnect is logged", async (t) => {
  const h = harnessFor(t)({ closeError: named("BrokenPipeError") });
  const warnings = logged(t, "warning");
  await h.session().ensureConnected();
  await h.session().close();
  const line = warnings.find((said) => said.includes("could not close Claude Code"));
  assert.ok(line?.includes("BrokenPipeError"));
});

test("a turn taken while claude code starts is ended on close", async (t) => {
  const gate = new AsyncEvent();
  const h = harnessFor(t)({ startGate: gate });
  const session = h.session();
  const turn = await session.submit("hello");
  await h.sleep(0.05); // the worker has taken the turn and waits for the CLI
  await session.close();
  assert.ok(turn.done.isSet);
  // no reply had started: the taken message is named in one message of its own
  const [note, ...more] = h.slack.callsTo("chat.postMessage");
  assert.equal(more.length, 0);
  const said = texts.fill(texts.NOT_SENT_ONE, { count: 1, because: texts.BECAUSE_SHUTDOWN });
  assert.ok(String(note?.text).includes(said));
});

test("a setup failure after connect closes the client", async (t) => {
  const h = harnessFor(t)({ infoError: named("RuntimeError") }, {});
  await assert.rejects(h.session().ensureConnected(), { name: "RuntimeError" });
  assert.equal(h.clients[0]?.connected, false);
  await h.session().ensureConnected(); // the next attempt starts one process, not two
  assert.ok(h.clients.length === 2 && h.clients[1]?.connected);
});

test("a bind after a turn never touches that thread s own record", async (t) => {
  const messages = sdkMessages("tools");
  const h = harnessFor(t)({ turns: [messages] });
  const result = recordOf(messages.at(-1));
  assert.ok(result !== null && isResult(messages.at(-1)));
  await (await h.session().submit("list the files")).done.wait();
  assert.equal(thread(h).sessionId, result.session_id);
  const other = join(h.tmpPath, "other");
  mkdirSync(other);
  assert.equal(await h.manager.bind(CHANNEL, other), true); // affects only the channel's next thread
  assert.equal(h.state.channel(CHANNEL)?.directory, other);
  assert.equal(thread(h).directory, h.tmpPath);
  assert.equal(thread(h).sessionId, result.session_id);
});

test("a relative bind path is read under the allowed root", (t) => {
  const tmpPath = realpathSync(mkdtempSync(join(tmpdir(), "awd-resolve-")));
  t.after(() => rmSync(tmpPath, { recursive: true, force: true }));
  mkdirSync(join(tmpPath, "app"));
  assert.equal(resolveDirectory("app", tmpPath), join(tmpPath, "app"));
  assert.equal(resolveDirectory("../", join(tmpPath, "app")), null);
});

test("a folder claude code does not trust is not started", async (t) => {
  const h = harnessFor(t)({ turns: [sdkMessages("tools")] });
  h.backend.trusted = async () => false;
  const turn = await h.session().submit("list the files");
  await turn.done.wait();
  assert.deepEqual(h.clients, []); // no Claude Code process, so no hook of the folder's ran
  assert.ok(h.writtenText().includes("trust"));
});

/** What a session answers `info()` with, from the keys the CLI's answer holds. */
function infoOf(raw: JsonObject) {
  return agentInfo(raw);
}

test("bypass from the folder s own settings shows and turns off", async (t) => {
  const info = infoOf({ commands: [], current_permission_mode: "bypassPermissions" });
  const h = harnessFor(t)({ turns: [sdkMessages("tools"), sdkMessages("tools")], info });
  const session = h.session();
  await (await session.submit("list the files")).done.wait();
  assert.ok(statuses(h).at(-1)?.startsWith("⚡ bypass"));
  await session.setBypass(false);
  assert.equal(h.clients[0]?.modes.at(-1), "default");
  assert.ok((await session.status()).includes("Mode: `default`"));
  await (await session.submit("again")).done.wait();
  assert.ok(!statuses(h).at(-1)?.startsWith("⚡ bypass"));
});

const AUTO_INFO = infoOf({ commands: [], current_permission_mode: "auto" });

test("a thread claude code started in auto mode says so in status only", async (t) => {
  // Bypass never chosen: the mode is the one the owner's own settings start Claude Code in.
  const h = harnessFor(t)({ turns: [sdkMessages("tools")], info: AUTO_INFO });
  const session = h.session();
  await (await session.submit("list the files")).done.wait();
  assert.deepEqual(h.clients[0]?.modes, []); // the daemon set no mode of its own
  // The footer marks bypass alone: with it off, the mode is on `!status`.
  assert.ok(!statuses(h).at(-1)?.includes("auto") && !statuses(h).at(-1)?.includes("⚡"));
  assert.ok((await session.status()).includes("Mode: `auto`"));
});

test("bypass off returns an auto thread to auto", async (t) => {
  const h = harnessFor(t)({ turns: [sdkMessages("tools"), sdkMessages("tools")], info: AUTO_INFO });
  const session = h.session();
  await session.setBypass(true);
  await (await session.submit("list the files")).done.wait();
  assert.ok(statuses(h).at(-1)?.startsWith("⚡ bypass"));
  await session.setBypass(false);
  assert.deepEqual(h.clients[0]?.modes, ["bypassPermissions", "auto"]);
  assert.ok((await session.status()).includes("Mode: `auto`"));
  await (await session.submit("again")).done.wait();
  assert.ok(!statuses(h).at(-1)?.startsWith("⚡ bypass"));
});

test("bypass off lands on default when claude code refuses auto", async (t) => {
  // The refusal as the CLI words it for a model with no auto mode (measured 2026-10-08, SDK
  // 0.2.164): it left the session in bypass, so off has to land somewhere that is not bypass.
  const h = harnessFor(t)({ turns: [sdkMessages("tools")], info: AUTO_INFO });
  const session = h.session();
  await session.ensureConnected();
  const client = at(h.clients, 0);
  await session.setBypass(true);
  const accepted = client.setPermissionMode.bind(client);
  client.setPermissionMode = async (mode) => {
    if (mode === "auto") {
      throw new Error("Cannot set permission mode to auto: auto mode unavailable");
    }
    await accepted(mode);
  };
  await session.setBypass(false);
  assert.deepEqual(client.modes, ["bypassPermissions", "default"]);
  assert.equal(thread(h).bypass, false);
  assert.ok((await session.status()).includes("Mode: `default`"));
  await (await session.submit("list the files")).done.wait();
  assert.ok(!statuses(h).at(-1)?.startsWith("⚡"));
});

test("bypass off that no mode accepts is not recorded", async (t) => {
  const h = harnessFor(t)({ info: AUTO_INFO });
  const session = h.session();
  await session.ensureConnected();
  const client = at(h.clients, 0);
  await session.setBypass(true);
  client.setPermissionMode = async () => {
    throw named("ConnectionError");
  };
  await assert.rejects(session.setBypass(false), { name: "ConnectionError" });
  assert.equal(thread(h).bypass, true);
  assert.ok(session.bypass);
});

test("the mode claude code reports is the one shown", async (t) => {
  // Both reports as recorded after `set_permission_mode` (2026-10-08, SDK 0.2.164): the mode
  // shown follows them, whatever the session started in.
  const [bypassReported, autoReported] = sdkMessages("permission-mode-status") as [Item, Item];
  const turns = [
    [autoReported, ...sdkMessages("tools")],
    [bypassReported, ...sdkMessages("tools")],
  ];
  const h = harnessFor(t)({ turns });
  const session = h.session();
  await (await session.submit("list the files")).done.wait();
  assert.ok((await session.status()).includes("Mode: `auto`"));
  await (await session.submit("again")).done.wait();
  assert.ok((await session.status()).includes("Mode: `bypassPermissions`"));
});

test("bypass off does not trade another refused mode for default", async (t) => {
  // Only auto mode's refusal was measured: any other mode that fails stays an error.
  const info = infoOf({ commands: [], current_permission_mode: "acceptEdits" });
  const h = harnessFor(t)({ info });
  const session = h.session();
  await session.ensureConnected();
  const client = at(h.clients, 0);
  await session.setBypass(true);
  const accepted = client.setPermissionMode.bind(client);
  client.setPermissionMode = async (mode) => {
    if (mode === "acceptEdits") throw new Error("Cannot set permission mode to acceptEdits");
    await accepted(mode);
  };
  await assert.rejects(session.setBypass(false), /acceptEdits/);
  assert.deepEqual(client.modes, ["bypassPermissions"]);
  assert.ok(session.bypass);
});

test("a listed model without a value is left out", async (t) => {
  // The setup builds one option per entry from `value`: an entry a future CLI lists without
  // one must not break every first prompt, so it is dropped where the list is read. That is
  // the back end's reading of the CLI's answer now (`agentInfo`); the session keeps what it
  // is given.
  const info = infoOf({
    commands: [],
    current_permission_mode: "default",
    models: [
      { value: "default", displayName: "Default (recommended)" },
      { displayName: "No value" },
      { value: "" },
      "not an entry",
      { value: "haiku", displayName: "Haiku 4.5" },
    ],
  });
  const h = harnessFor(t)({ info });
  const session = h.session();
  await session.ensureConnected();
  assert.deepEqual(
    session.models.map((model) => model.value),
    ["default", "haiku"],
  );
});

test("every message is posted without link previews", async (t) => {
  const ask = canUseToolCall("Bash", { command: "ls" });
  const h = harnessFor(t)({ turns: [[ask, ...sdkMessages("tools")]] });
  const turn = await h.session().submit("list the files");
  await h.until(() => pending(h.approvals).length === 1);
  resolve(h, APPROVE);
  await turn.done.wait();
  const posts = h.slack.callsTo("chat.postMessage");
  assert.ok(posts.length > 0); // the approval request
  assert.ok(posts.every((post) => post.unfurl_links === false && post.unfurl_media === false));
});

test("resume creates a new thread already on the given session", async (t) => {
  const h = harnessFor(t)({ turns: [sdkMessages("tools")] }, {});
  await (await h.session().submit("list the files")).done.wait();
  const other = "68da9311-0000-4000-8000-00000000abcd";
  const resumed = await h.manager.resume(CHANNEL, OTHER_THREAD, other);
  assert.ok(resumed !== null);
  assert.equal(thread(h, OTHER_THREAD).sessionId, other);
  assert.notEqual(thread(h).sessionId, other); // the original thread is untouched
  await resumed.ensureConnected(); // the next message on the new thread starts on that session
  assert.equal(h.clients.at(-1)?.options.resume, other);
});

test("resume creates a thread with bypass off regardless of others", async (t) => {
  const h = harnessFor(t)({}, {});
  await h.session().setBypass(true); // bypass is per thread: only the original thread's
  const resumed = await h.manager.resume(
    CHANNEL,
    OTHER_THREAD,
    "68da9311-0000-4000-8000-00000000abcd",
  );
  assert.ok(resumed !== null && !resumed.bypass);
  await resumed.ensureConnected();
  assert.deepEqual(h.clients.at(-1)?.modes, []);
});

test("resume creates an independent thread even while another is busy", async (t) => {
  const ask = canUseToolCall("Bash", { command: "ls" });
  const h = harnessFor(t)({ turns: [[ask, ...sdkMessages("tools")]] });
  await h.session().submit("list the files");
  await h.until(() => pending(h.approvals).length === 1);
  const resumed = await h.manager.resume(
    CHANNEL,
    OTHER_THREAD,
    "68da9311-0000-4000-8000-00000000abcd",
  );
  assert.ok(resumed !== null);
  assert.notEqual(h.state.thread(CHANNEL, OTHER_THREAD), null);
  assert.ok(h.session().busy); // the original thread's turn is unaffected
});

test("resume returns none when the channel is unbound", async (t) => {
  const h = harnessFor(t)();
  assert.equal(await h.manager.resume("C000OTHER", THREAD, "some-session"), null);
});

test("open returns none when the channel is unbound", (t) => {
  const h = harnessFor(t)();
  assert.equal(h.manager.open("C000OTHER", THREAD), null);
});

test("the footer names the bound folder", async (t) => {
  const h = harnessFor(t)({ turns: [sdkMessages("tools")] });
  await (await h.session().submit("list the files")).done.wait();
  const folder = h.tmpPath.split("/").at(-1);
  assert.ok(statuses(h).at(-1)?.startsWith("claude-haiku-4-5-20251001 · "));
  assert.ok(statuses(h).at(-1)?.includes(` · ${folder} · `));
});
