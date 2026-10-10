/**
 * The session setup (issue #74): model, effort and bypass, asked before a new session's first
 * prompt. Port of the section of `tests/test_slack_app.py` that opens with `# Session setup
 * (issue #74)`, in its order, through `a model change keeps a supported effort and the tick`. The
 * harness is `test/support/slack-app.ts`; the `manual` fixture is `manualWorld`.
 *
 * What stands for Python's patches and private members:
 * - `monkeypatch.setattr(FakeClaudeClient, "set_model" | "set_permission_mode", ...)` is
 *   `t.mock.method` on `FakeAgentSession.prototype`, which `t` restores.
 * - `native_bypass_folder` and `auto_mode_settings` are `world.permissionMode`.
 * - `_drop_client`, which a test calls to lose the client, and the `ThreadSession._drop_client`
 *   patch are the private `dropClient` (TypeScript `private` is compile-time only), reached by
 *   the cast in `Internals`.
 * - `session.update_limiter.acquire` is `world.limiter.acquire`, the same object the handlers
 *   and the session setup draw from.
 * - A `RuntimeError` or `OSError` raised by a patch is an `Error` of that name: a failure is
 *   told to the owner by its name.
 *
 * The fake back end declares `liveEffort: true`, as the real one does: an effort chosen at Start
 * is set on the live session. Python's back end started a fresh client for it, and the tests that
 * read that second client run twice: under the Python name with `liveEffort` switched off on the
 * world's back end (Python's assertions), and under a name of their own with it on, asserting
 * that `setEffort` reached the live session and that no second one started. The tests that hold
 * Start between the effort and the prompt held the reconnect's start; with the effort set live
 * there is no reconnect, so their live variants hold `setEffort` instead.
 */
import assert from "node:assert/strict";
import { join } from "node:path";
import { type TestContext, test } from "node:test";
import { HOLD_CANCEL, HOLD_CONTINUE } from "../../../../src/chat/slack/hold.ts";
import {
  SETUP_BYPASS,
  SETUP_EFFORT,
  SETUP_MODEL,
  SETUP_START,
} from "../../../../src/chat/slack/setup.ts";
import { ThreadSession } from "../../../../src/core/sessions/session.ts";
import type { Choice } from "../../../../src/core/setup.ts";
import * as texts from "../../../../src/core/texts.ts";
import {
  AsyncEvent,
  CHANNEL,
  OTHER_CHANNEL,
  OTHER_TEAM,
  OTHER_THREAD,
  STRANGER,
  THREAD,
} from "../../../support/fake-slack.ts";
import { FakeAgentSession } from "../../../support/sessions.ts";
import {
  answerInsidePost,
  type Body,
  buttonValue,
  clickIn,
  gateLimiter,
  manualWorld as manualWorldWithoutKeepAlive,
  message,
  postedBlocks,
  reactionsOn,
  reply,
  said,
  setupClick,
  setupControls,
  sharedFile,
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
  keepAlive(t);
  return worldWithoutKeepAlive(t);
}

/** The `manual` fixture, with the same timer. */
function manualWorld(t: TestContext): World {
  keepAlive(t);
  return manualWorldWithoutKeepAlive(t);
}

function keepAlive(t: TestContext): void {
  const guard = setInterval(() => {}, 1_000);
  t.after(() => clearInterval(guard));
}

/** The private member a test calls to lose a session's client, as a reader crash does. */
interface Internals {
  dropClient(): Promise<void>;
}

function internals(session: ThreadSession): Internals {
  return session as unknown as Internals;
}

/** Python's `RuntimeError` and `OSError` as the daemon names a failure to the owner. */
function failure(name: string, message: string): Error {
  return Object.assign(new Error(message), { name });
}

/** A world whose back end changes an effort only through a fresh client, as Python's did. */
function withoutLiveEffort(world: World): World {
  world.backend.capabilities = { ...world.backend.capabilities, liveEffort: false };
  return world;
}

/** A control of the setup's one `actions` block, by action_id. */
function control(found: Record<string, Body>, actionId: string): Body {
  const element = found[actionId];
  assert.ok(element !== undefined);
  return element;
}

function sessionOf(world: World): ThreadSession {
  const session = world.sessions.get(CHANNEL, THREAD);
  assert.ok(session !== null);
  return session;
}

function summaryOf(world: World): string {
  return String(world.slack.callsTo("chat.update").at(-1)?.text);
}

function posts(world: World): string[] {
  return world.slack.callsTo("chat.postMessage").map((args) => String(args.text));
}

// Session setup (issue #74): model, effort and bypass, asked before a new session's first prompt.

test("a plain prompt waits for start", async (t) => {
  const manual = manualWorld(t);
  await manual.dispatch(message("hello", { ts: THREAD }));
  assert.deepEqual(manual.queries(), []);
  const [waiting, ...others] = manual.waitingSetups();
  assert.equal(others.length, 0);
  assert.deepEqual(waiting?.slice(1, 3), [CHANNEL, THREAD]);
  const session = manual.sessions.get(CHANNEL, THREAD);
  assert.ok(session?.waitingForOwner); // ✋, idle timer paused
  assert.deepEqual(reactionsOn(manual, THREAD), ["raised_hand"]);
  assert.deepEqual(manual.state.thread(CHANNEL, THREAD)?.requests, [manual.slack.postedTs.at(-1)]);
});

test("a message with files waits for start too", async (t) => {
  const manual = manualWorld(t);
  const body = sharedFile("snippet");
  manual.downloads.set(
    body.event.files[0].url_private_download,
    new TextEncoder().encode("hello\n"),
  );
  await manual.dispatch(body);
  await manual.idle();
  assert.equal(manual.waitingSetups().length, 1);
  assert.deepEqual(manual.queries(), []);
  await manual.dispatch(setupClick(manual));
  await manual.idle();
  const [query, ...rest] = manual.queries();
  assert.equal(rest.length, 0);
  assert.ok(String(query).startsWith(body.event.text));
});

test("a top level passthrough waits for start too", async (t) => {
  const manual = manualWorld(t);
  await manual.dispatch(message("!compact", { ts: THREAD }));
  assert.equal(manual.waitingSetups().length, 1);
  assert.deepEqual(manual.queries(), []);
  await manual.dispatch(setupClick(manual));
  const queries = manual.queries();
  assert.ok(
    (queries.length === 1 && queries[0] === "/compact") ||
      (queries.length === 1 && queries[0] === "!compact"),
  );
});

test("daemon words and thread replies show no setup", async (t) => {
  const manual = manualWorld(t);
  await manual.dispatch(message("!status"));
  await manual.dispatch(message("!help"));
  assert.deepEqual(manual.waitingSetups(), []);
  await manual.dispatch(message("hi", { ts: THREAD }));
  await manual.dispatch(setupClick(manual));
  await manual.dispatch(reply("second", THREAD));
  // The top-level prompt's, not the reply's.
  assert.equal(posts(manual).filter((text) => text === texts.SETUP_FALLBACK).length, 1);
});

test("start with defaults changes nothing", async (t) => {
  const manual = manualWorld(t);
  await manual.dispatch(message("hello", { ts: THREAD }));
  await manual.dispatch(setupClick(manual));
  const client = manual.clients.at(-1);
  assert.deepEqual(client?.queries, ["hello"]);
  assert.equal(manual.clients.length, 1);
  assert.deepEqual(client?.modelsSet, []);
  assert.deepEqual(client?.modes, []);
  assert.equal(client?.options.effort, null);
  assert.equal(summaryOf(manual), "Model: Default (recommended) · Effort: Default · Bypass: off");
  assert.deepEqual(manual.state.thread(CHANNEL, THREAD)?.requests, []); // kept, no longer a request
  assert.deepEqual(manual.slack.callsTo("chat.delete"), []);
  assert.equal(reactionsOn(manual, THREAD).at(-1), "hourglass_flowing_sand");
});

test("start applies model effort and bypass", async (t) => {
  const manual = withoutLiveEffort(manualWorld(t));
  await manual.dispatch(message("hello", { ts: THREAD }));
  await manual.dispatch(
    setupClick(manual, SETUP_START, { model: "opus", effort: "high", bypass: true }),
  );
  assert.equal(manual.clients.length, 2); // reconnected once, for the effort
  const client = manual.clients.at(-1);
  assert.equal(client?.options.effort, "high");
  assert.deepEqual(client?.modelsSet, ["opus"]);
  assert.deepEqual(client?.modes, ["bypassPermissions"]);
  assert.deepEqual(client?.queries, ["hello"]);
  assert.deepEqual(manual.clients[0]?.queries, []);
  assert.equal(manual.clients[0]?.connected, false); // the effort reconnect closed the first client
  const stored = manual.state.thread(CHANNEL, THREAD);
  assert.equal(stored?.bypass, true);
  assert.equal(stored?.effort, "high");
  assert.equal(summaryOf(manual), "Model: Opus 5.5 · Effort: high · Bypass: on");
});

test("start applies model effort and bypass with a live effort change", async (t) => {
  const manual = manualWorld(t);
  await manual.dispatch(message("hello", { ts: THREAD }));
  await manual.dispatch(
    setupClick(manual, SETUP_START, { model: "opus", effort: "high", bypass: true }),
  );
  assert.equal(manual.clients.length, 1); // the effort went to the live session
  const client = manual.clients[0];
  assert.deepEqual(client?.effortsSet, ["high"]);
  assert.equal(client?.options.effort, null); // started before the choice
  assert.deepEqual(client?.modelsSet, ["opus"]);
  assert.deepEqual(client?.modes, ["bypassPermissions"]);
  assert.deepEqual(client?.queries, ["hello"]);
  assert.equal(client?.connected, true);
  const stored = manual.state.thread(CHANNEL, THREAD);
  assert.equal(stored?.bypass, true);
  assert.equal(stored?.effort, "high");
  assert.equal(summaryOf(manual), "Model: Opus 5.5 · Effort: high · Bypass: on");
});

test("changing the model rewrites the efforts", async (t) => {
  const manual = manualWorld(t);
  await manual.dispatch(message("hello", { ts: THREAD }));
  await manual.dispatch(setupClick(manual, SETUP_MODEL, { model: "haiku", effort: "high" }));
  const update = manual.slack.callsTo("chat.update").at(-1) as Body;
  const found = setupControls(update.blocks);
  assert.deepEqual(
    (control(found, SETUP_EFFORT).options as Body[]).map((option) => option.value),
    ["default"],
  );
  assert.equal(control(found, SETUP_MODEL).initial_option.value, "haiku");
  assert.ok(!("initial_options" in control(found, SETUP_BYPASS))); // the tick state is kept
  assert.deepEqual(manual.queries(), []); // still waiting
});

test("an effort or bypass change only acks", async (t) => {
  const manual = manualWorld(t);
  await manual.dispatch(message("hello", { ts: THREAD }));
  await manual.dispatch(setupClick(manual, SETUP_EFFORT, { effort: "low" }));
  await manual.dispatch(setupClick(manual, SETUP_BYPASS, { bypass: true }));
  assert.deepEqual(manual.slack.callsTo("chat.update"), []);
  assert.deepEqual(manual.queries(), []);
});

const OTHER_USERS: readonly Body[] = [{ id: STRANGER }, { team_id: OTHER_TEAM }];
for (const [index, user] of OTHER_USERS.entries()) {
  test(`a setup click from someone else is ignored [user${index}]`, async (t) => {
    const manual = manualWorld(t);
    await manual.dispatch(message("hello", { ts: THREAD }));
    await manual.dispatch(setupClick(manual, SETUP_START, { user }));
    assert.deepEqual(manual.queries(), []);
    assert.deepEqual(manual.ephemerals(), []);
  });
}

test("a setup click for another thread or channel is refused", async (t) => {
  const manual = manualWorld(t);
  // Sessions exist at both places, so the refusal is `Holds.resolve`'s own channel/thread check,
  // not merely a lookup that finds no session.
  manual.autoStart = true;
  manual.state.bind(OTHER_CHANNEL, join(manual.root, "app"));
  await manual.dispatch(message("elsewhere", { ts: OTHER_THREAD }));
  await manual.dispatch(message("elsewhere", { ts: THREAD, channel: OTHER_CHANNEL }));
  manual.autoStart = false;
  await manual.dispatch(message("hello", { ts: THREAD }));
  const before = manual.queries().length;
  await manual.dispatch(setupClick(manual, SETUP_START, { threadTs: OTHER_THREAD }));
  await manual.dispatch(setupClick(manual, SETUP_START, { channel: OTHER_CHANNEL }));
  assert.equal(manual.queries().length, before);
  assert.equal(manual.waitingSetups().length, 1); // still waiting
  assert.equal(manual.ephemerals().at(-1), texts.HOLD_GONE);
});

test("a second start is stale", async (t) => {
  const manual = manualWorld(t);
  await manual.dispatch(message("hello", { ts: THREAD }));
  const body = setupClick(manual);
  await manual.dispatch(body);
  await manual.dispatch(body);
  assert.deepEqual(manual.queries(), ["hello"]);
  assert.equal(manual.ephemerals().at(-1), texts.HOLD_GONE);
});

test("stop in the thread cancels a waiting setup", async (t) => {
  const manual = manualWorld(t);
  await manual.dispatch(message("hello", { ts: THREAD }));
  await manual.dispatch(reply("!stop", THREAD));
  assert.deepEqual(manual.queries(), []);
  assert.deepEqual(manual.ephemerals(), [texts.NOT_SENT]);
  assert.equal(manual.slack.callsTo("chat.delete").length, 1);
  assert.deepEqual(manual.state.thread(CHANNEL, THREAD)?.requests, []);
});

test("a top level stop cancels a waiting setup", async (t) => {
  const manual = manualWorld(t);
  await manual.dispatch(message("hello", { ts: THREAD }));
  await manual.dispatch(message("!stop"));
  assert.deepEqual(manual.queries(), []);
  assert.ok(manual.ephemerals().includes(texts.NOT_SENT));
  assert.equal(manual.slack.callsTo("chat.delete").length, 1);
});

test("a drain cancels a waiting setup", async (t) => {
  const manual = manualWorld(t);
  await manual.dispatch(message("hello", { ts: THREAD }));
  const cutShort = new AbortController();
  cutShort.abort();
  await manual.sessions.drain(cutShort.signal);
  await manual.idle();
  assert.deepEqual(manual.queries(), []);
  assert.ok(manual.ephemerals().includes(texts.NOT_SENT));
});

/** A start decided before its post returns is applied; checked for either way of changing the effort. */
async function startDecidedBeforeThePost(manual: World): Promise<FakeAgentSession> {
  const choice: Choice = { model: "opus", effort: "high", bypass: true };
  answerInsidePost(manual, texts.SETUP_FALLBACK, choice, SETUP_START);
  await manual.dispatch(message("hello", { ts: THREAD }));
  await manual.settle();
  assert.deepEqual(manual.queries(), ["hello"]);
  const client = manual.clients.at(-1);
  assert.ok(client !== undefined);
  assert.deepEqual(client.modelsSet, ["opus"]);
  assert.deepEqual(client.modes, ["bypassPermissions"]);
  assert.deepEqual(manual.slack.callsTo("chat.delete"), []); // the record stays
  assert.equal(summaryOf(manual), "Model: Opus 5.5 · Effort: high · Bypass: on");
  assert.deepEqual(manual.state.thread(CHANNEL, THREAD)?.requests, []);
  return client;
}

test("a start decided before the post returns is still applied", async (t) => {
  const manual = withoutLiveEffort(manualWorld(t));
  const client = await startDecidedBeforeThePost(manual);
  assert.equal(client.options.effort, "high");
});

test("a start decided before the post returns is still applied with a live effort change", async (t) => {
  const manual = manualWorld(t);
  const client = await startDecidedBeforeThePost(manual);
  assert.equal(manual.clients.length, 1);
  assert.deepEqual(client.effortsSet, ["high"]);
});

test("a continue decided before the post returns still sends", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("busy elsewhere", { ts: OTHER_THREAD }));
  answerInsidePost(world, texts.HOLD_QUESTION.split("{")[0] as string, true, HOLD_CONTINUE);
  await world.dispatch(message("hello", { ts: THREAD }));
  await world.settle();
  assert.deepEqual(world.clients.at(-1)?.queries, ["hello"]);
});

/**
 * Start with an effort and the bypass box ticked, held in the window in which the answer is
 * applied but nothing is sent yet. Python held the reconnect's CLI start; with the effort set
 * live there is no reconnect, so `live` holds `setEffort` instead. Returns what opens the window.
 */
async function startWithAGatedReconnect(
  t: TestContext,
  manual: World,
  live: boolean,
): Promise<AsyncEvent> {
  const gate = new AsyncEvent();
  if (!live) withoutLiveEffort(manual);
  await manual.dispatch(message("hello", { ts: THREAD }));
  if (live) {
    t.mock.method(
      FakeAgentSession.prototype,
      "setEffort",
      async function (this: FakeAgentSession, level: string | null): Promise<void> {
        this.effortsSet.push(level);
        await gate.wait();
      },
    );
  } else {
    manual.connectGate = gate;
  }
  await manual.dispatch(setupClick(manual, SETUP_START, { effort: "high", bypass: true }));
  await manual.idle();
  return gate;
}

async function assertCancelledWhileSettling(manual: World, gate: AsyncEvent): Promise<void> {
  gate.set();
  await manual.settle();
  assert.deepEqual(manual.queries(), []);
  assert.ok(manual.ephemerals().includes(texts.NOT_SENT));
  assert.deepEqual(manual.state.thread(CHANNEL, THREAD)?.requests, []);
  assert.equal(manual.slack.callsTo("chat.delete").length, 1);
}

test("bypass typed while start is applied wins over the box", async (t) => {
  const manual = manualWorld(t);
  // The word came after the click: it waits for Start to finish, then switches the session, so
  // what its answer says is what runs. Start is held inside `setModel`, after the point where it
  // is marked as applied and before it compares the box with the live client.
  const gate = new AsyncEvent();
  t.mock.method(
    FakeAgentSession.prototype,
    "setModel",
    async function (this: FakeAgentSession, model: string | null): Promise<void> {
      this.modelsSet.push(model);
      await gate.wait();
    },
  );
  await manual.dispatch(message("hello", { ts: THREAD }));
  await manual.dispatch(setupClick(manual, SETUP_START, { model: "opus" })); // bypass unticked
  await manual.idle();
  const word = reply("!bypass on", THREAD);
  await manual.dispatch(word);
  await manual.idle();
  assert.ok(!manual.ephemerals().includes(texts.BYPASS_ON_THREAD)); // nothing is said before it holds
  gate.set();
  await manual.settle();
  assert.deepEqual(manual.clients.at(-1)?.modes, ["bypassPermissions"]);
  assert.equal(manual.state.thread(CHANNEL, THREAD)?.bypass, true);
  assert.ok(manual.ephemerals().includes(texts.BYPASS_ON_THREAD));
  assert.deepEqual(reactionsOn(manual, word.event.ts), ["white_check_mark"]);
  assert.deepEqual(manual.queries(), ["hello"]);
});

for (const live of [false, true]) {
  const how = live ? " with a live effort change" : "";

  test(`stop while start settles cancels${how}`, async (t) => {
    const manual = manualWorld(t);
    const gate = await startWithAGatedReconnect(t, manual, live);
    await manual.dispatch(reply("!stop", THREAD));
    await manual.idle();
    assert.ok(![...said(manual), ...manual.ephemerals()].includes(texts.NOTHING_TO_STOP));
    await assertCancelledWhileSettling(manual, gate);
  });

  test(`a channel stop while start settles cancels${how}`, async (t) => {
    const manual = manualWorld(t);
    const gate = await startWithAGatedReconnect(t, manual, live);
    await manual.dispatch(message("!stop"));
    await manual.idle();
    assert.ok(![...said(manual), ...manual.ephemerals()].includes(texts.NOTHING_TO_STOP));
    await assertCancelledWhileSettling(manual, gate);
  });

  test(`a drain while start settles cancels${how}`, async (t) => {
    const manual = manualWorld(t);
    const gate = await startWithAGatedReconnect(t, manual, live);
    const cutShort = new AbortController();
    cutShort.abort();
    await manual.sessions.drain(cutShort.signal);
    await assertCancelledWhileSettling(manual, gate);
  });
}

/**
 * The setup shown again: bypass unticked, effort Default, nothing left from the aborted Start in
 * `state.json` or on the client that will be used.
 */
function assertAFreshSetup(world: World): void {
  const [waiting, ...others] = world.waitingSetups();
  assert.ok(waiting !== undefined && others.length === 0);
  const found = setupControls(world.slack.messages.get(waiting[3])?.blocks as Body[]);
  assert.equal(control(found, SETUP_EFFORT).initial_option.value, "default");
  assert.ok(!("initial_options" in control(found, SETUP_BYPASS)));
  const stored = world.state.thread(CHANNEL, THREAD);
  assert.equal(stored?.bypass, null);
  assert.equal(stored?.effort, null);
  const client = world.clients.at(-1);
  assert.equal(client?.options.effort, null);
  assert.deepEqual(client?.modelsSet, []);
  assert.deepEqual(client?.modes, []);
}

for (const live of [false, true]) {
  test(`a reply after a stopped setup asks the setup again${live ? " with a live effort change" : ""}`, async (t) => {
    const manual = manualWorld(t);
    const gate = await startWithAGatedReconnect(t, manual, live);
    await manual.dispatch(reply("!stop", THREAD));
    await assertCancelledWhileSettling(manual, gate);
    manual.connectGate = null;
    await manual.dispatch(reply("again", THREAD));
    assert.deepEqual(manual.queries(), []); // held, not sent with what the aborted Start left
    assertAFreshSetup(manual);
    await manual.dispatch(setupClick(manual));
    assert.deepEqual(manual.clients.at(-1)?.queries, ["again"]);
  });
}

test("a reply after a cancelled d8 hold asks the setup again", async (t) => {
  const manual = manualWorld(t);
  manual.autoStart = true;
  await manual.dispatch(message("busy elsewhere", { ts: OTHER_THREAD }));
  manual.autoStart = false;
  await manual.dispatch(message("hello", { ts: THREAD }));
  await manual.dispatch(
    setupClick(manual, SETUP_START, { model: "opus", effort: "high", bypass: true }),
  );
  const holdId = buttonValue(postedBlocks(manual), HOLD_CANCEL);
  await manual.dispatch(clickIn(HOLD_CANCEL, holdId, CHANNEL, THREAD));
  assert.ok(manual.ephemerals().includes(texts.NOT_SENT));
  await manual.dispatch(reply("again", THREAD));
  assertAFreshSetup(manual);
});

test("a reply while the first turn runs is not asked again", async (t) => {
  const manual = manualWorld(t);
  await manual.dispatch(message("hello", { ts: THREAD }));
  await manual.dispatch(setupClick(manual));
  await manual.dispatch(reply("more", THREAD));
  await manual.settle();
  // The first prompt's, not the reply's.
  assert.equal(posts(manual).filter((text) => text === texts.SETUP_FALLBACK).length, 1);
  assert.deepEqual(manual.waitingSetups(), []);
});

test("a failing start leaves no controls and no leftovers", async (t) => {
  const manual = manualWorld(t);
  t.mock.method(FakeAgentSession.prototype, "setModel", async () => {
    throw failure("RuntimeError", "bad model");
  });
  await manual.dispatch(message("hello", { ts: THREAD }));
  await manual.dispatch(
    setupClick(manual, SETUP_START, { model: "opus", effort: "high", bypass: true }),
  );
  await manual.settle();
  assert.deepEqual(manual.queries(), []);
  assert.equal(manual.slack.callsTo("chat.delete").length, 1); // the controls are gone
  const stored = manual.state.thread(CHANNEL, THREAD);
  assert.deepEqual(stored?.requests, []);
  assert.equal(stored?.effort, null);
  assert.equal(stored?.bypass, null);
  assert.ok(
    manual.ephemerals().length > 0 || said(manual).some((text) => text.includes("RuntimeError")),
  );
});

test("a model change cannot overwrite the summary", async (t) => {
  const manual = manualWorld(t);
  await manual.dispatch(message("hello", { ts: THREAD }));
  const gate = new AsyncEvent();
  const acquire = manual.limiter.acquire.bind(manual.limiter);
  let slow = true;
  manual.limiter.acquire = async (signal?: AbortSignal) => {
    if (slow) {
      slow = false;
      await gate.wait();
    }
    await acquire(signal);
  };
  const body = setupClick(manual, SETUP_MODEL, { model: "haiku" });
  const start = setupClick(manual);
  await manual.dispatch(body); // waits on the limiter
  await manual.dispatch(start);
  await manual.settle();
  gate.set();
  await manual.settle();
  const last = manual.slack.callsTo("chat.update").at(-1) as Body;
  assert.ok(String(last.text).startsWith("Model:"));
});

test("stop while the summary waits on the limiter cancels", async (t) => {
  const manual = manualWorld(t);
  await manual.dispatch(message("hello", { ts: THREAD }));
  const gate = new AsyncEvent();
  gateLimiter(manual, gate);
  await manual.dispatch(setupClick(manual));
  await manual.idle();
  assert.deepEqual(manual.queries(), []); // Start is still being settled: the summary waits
  await manual.dispatch(reply("!stop", THREAD));
  await manual.idle();
  assert.ok(![...said(manual), ...manual.ephemerals()].includes(texts.NOTHING_TO_STOP));
  gate.set();
  await manual.settle();
  assert.deepEqual(manual.queries(), []);
  assert.ok(manual.ephemerals().includes(texts.NOT_SENT));
  // The cancel deleted the message; the summary edit is skipped rather than sent to it.
  assert.equal(manual.slack.callsTo("chat.delete").length, 1);
  assert.deepEqual(manual.slack.callsTo("chat.update"), []);
  assert.deepEqual(manual.state.thread(CHANNEL, THREAD)?.requests, []);
});

test("a restart during start leaves nothing for the next start", async (t) => {
  const manual = manualWorld(t);
  await manual.dispatch(message("hello", { ts: THREAD }));
  const gate = new AsyncEvent();
  gateLimiter(manual, gate);
  await manual.dispatch(setupClick(manual, SETUP_START, { effort: "high", bypass: true }));
  await manual.idle();
  manual.sessions.draining = true; // a restart begins before the prompt is sent
  gate.set();
  await manual.settle();
  assert.deepEqual(manual.queries(), []);
  assert.ok(manual.ephemerals().includes(texts.RESTARTING));
  let stored = manual.state.thread(CHANNEL, THREAD);
  assert.equal(stored?.bypass, true); // what the new process finds
  assert.equal(stored?.effort, "high");
  // The restart: a new ThreadSession rebuilt from state.json.
  manual.sessions.draining = false;
  const old = sessionOf(manual);
  await old.close();
  await manual.dispatch(reply("again", THREAD));
  await manual.idle();
  assert.equal(manual.waitingSetups().length, 1); // asked the setup again
  await manual.dispatch(setupClick(manual)); // defaults: effort Default, bypass off
  await manual.settle();
  const client = manual.clients.at(-1);
  assert.deepEqual(client?.queries, ["again"]);
  assert.equal(client?.options.effort, null);
  assert.ok(
    JSON.stringify(client?.modes) === "[]" || JSON.stringify(client?.modes) === '["default"]',
  );
  stored = manual.state.thread(CHANNEL, THREAD);
  // Start's unticked box is an explicit off.
  assert.equal(stored?.bypass, false);
  assert.equal(stored?.effort, null);
  assert.ok(summaryOf(manual).endsWith("Effort: Default · Bypass: off"));
});

for (const word of ["!bypass on", "!bypass off"]) {
  test(`bypass typed while the setup waits points at the setup [${word}]`, async (t) => {
    const manual = manualWorld(t);
    await manual.dispatch(message("hello", { ts: THREAD }));
    const typed = reply(word, THREAD);
    await manual.dispatch(typed);
    // Start alone sets bypass before the first prompt: the word changes nothing and says so.
    assert.deepEqual(manual.clients.at(-1)?.modes, []);
    assert.equal(manual.state.thread(CHANNEL, THREAD)?.bypass, null);
    assert.deepEqual(manual.ephemerals(), [texts.BYPASS_BEFORE_START]);
    assert.deepEqual(reactionsOn(manual, typed.event.ts), []); // nothing took effect: no ✅
    await manual.dispatch(setupClick(manual)); // bypass unticked
    await manual.settle();
    const client = manual.clients.at(-1);
    assert.deepEqual(client?.modes, []); // never left the folder's own mode
    assert.equal(manual.state.thread(CHANNEL, THREAD)?.bypass, false);
    assert.deepEqual(client?.queries, ["hello"]);
    assert.ok(summaryOf(manual).endsWith("Bypass: off"));
  });
}

test("a failing forget setup does not hide the original error", async (t) => {
  const manual = manualWorld(t);
  t.mock.method(FakeAgentSession.prototype, "setModel", async () => {
    throw failure("RuntimeError", "bad model");
  });
  t.mock.method(ThreadSession.prototype as unknown as Internals, "dropClient", async () => {
    throw failure("OSError", "cannot drop");
  });
  await manual.dispatch(message("hello", { ts: THREAD }));
  await manual.dispatch(setupClick(manual, SETUP_START, { model: "opus" }));
  await manual.settle();
  assert.deepEqual(manual.queries(), []);
  assert.ok(
    [...said(manual), ...manual.ephemerals()].some((text) => text.includes("RuntimeError")),
  );
});

test("bang bypass off in an auto mode thread returns to auto mode", async (t) => {
  const world = worldOf(t);
  world.permissionMode = "auto"; // an owner whose own Claude Code settings start the process in auto mode
  await world.dispatch(message("hi", { ts: THREAD }));
  await world.dispatch(reply("!bypass on", THREAD));
  await world.dispatch(reply("!bypass off", THREAD));
  assert.deepEqual(world.clients[0]?.modes, ["bypassPermissions", "auto"]);
  assert.deepEqual(world.ephemerals(), [texts.BYPASS_ON_THREAD, texts.BYPASS_OFF_THREAD]);
});

function bypassBox(world: World): Body {
  const [waiting, ...others] = world.waitingSetups();
  assert.ok(waiting !== undefined && others.length === 0);
  const box = setupControls(world.slack.messages.get(waiting[3])?.blocks as Body[])[SETUP_BYPASS];
  assert.ok(box !== undefined);
  return box;
}

/** A folder whose own Claude Code settings start the process in bypassPermissions. */
function nativeBypassFolder(world: World): void {
  world.permissionMode = "bypassPermissions";
}

test("a native bypass folder starts the box ticked", async (t) => {
  const manual = manualWorld(t);
  nativeBypassFolder(manual);
  await manual.dispatch(message("hello", { ts: THREAD }));
  assert.deepEqual(
    (bypassBox(manual).initial_options as Body[]).map((option) => option.value),
    ["on"],
  );
  await manual.dispatch(setupClick(manual, SETUP_START, { bypass: true })); // the ticked default
  await manual.settle();
  const client = manual.clients.at(-1);
  const session = sessionOf(manual);
  assert.equal(session.nativeMode, "bypassPermissions");
  assert.deepEqual(client?.queries, ["hello"]);
  const last = client?.modes.slice(-1);
  assert.ok(last?.length === 0 || last?.[0] === "bypassPermissions");
  assert.ok(summaryOf(manual).endsWith("Bypass: on"));
});

test("unticking in a native bypass folder turns bypass off", async (t) => {
  const manual = manualWorld(t);
  nativeBypassFolder(manual);
  await manual.dispatch(message("hello", { ts: THREAD }));
  await manual.dispatch(setupClick(manual)); // unticked: an explicit off, as `!bypass off`
  await manual.settle();
  const client = manual.clients.at(-1);
  assert.equal(sessionOf(manual).bypass, false);
  assert.equal(client?.modes.at(-1), "default");
  assert.deepEqual(client?.queries, ["hello"]);
  assert.ok(summaryOf(manual).endsWith("Bypass: off"));
});

test("a typed bypass does not change what the summary says", async (t) => {
  const manual = manualWorld(t);
  nativeBypassFolder(manual);
  await manual.dispatch(message("hello", { ts: THREAD }));
  await manual.dispatch(reply("!bypass on", THREAD));
  await manual.dispatch(setupClick(manual)); // unticked
  await manual.settle();
  assert.equal(manual.clients.at(-1)?.modes.at(-1), "default");
  assert.ok(summaryOf(manual).endsWith("Bypass: off"));
});

test("a model change keeps the bypass tick", async (t) => {
  const manual = manualWorld(t);
  nativeBypassFolder(manual);
  await manual.dispatch(message("hello", { ts: THREAD }));
  await manual.dispatch(setupClick(manual, SETUP_MODEL, { model: "haiku" })); // unticked by the owner
  assert.ok(!("initial_options" in bypassBox(manual)));
  await manual.dispatch(setupClick(manual, SETUP_MODEL, { model: "haiku", bypass: true }));
  assert.ok(bypassBox(manual).initial_options);
});

test("a connect racing start does not record bypass as off", async (t) => {
  const manual = manualWorld(t);
  await manual.dispatch(message("hello", { ts: THREAD }));
  const session = sessionOf(manual);
  await internals(session).dropClient(); // the client is gone while the setup waits
  manual.state.setBypass(CHANNEL, THREAD, true); // what a restart left
  const gate = new AsyncEvent();
  const original = FakeAgentSession.prototype.setPermissionMode;
  t.mock.method(
    FakeAgentSession.prototype,
    "setPermissionMode",
    async function (this: FakeAgentSession, mode: string): Promise<void> {
      await original.call(this, mode);
      await gate.wait();
    },
  );
  const status = manual.dispatch(reply("!status", THREAD));
  await manual.idle();
  assert.deepEqual(manual.clients.at(-1)?.modes, ["bypassPermissions"]); // connect blocked mid-switch
  const click = manual.dispatch(setupClick(manual)); // bypass off
  await manual.idle();
  gate.set();
  await status;
  await click;
  await manual.settle();
  const client = manual.clients.at(-1);
  assert.deepEqual(client?.queries, ["hello"]);
  assert.equal(client?.modes.at(-1), "default"); // what Start said, not the client's first mode
});

test("a native bypass folder asks again with the box ticked after a stop", async (t) => {
  const manual = manualWorld(t);
  nativeBypassFolder(manual);
  await manual.dispatch(message("hello", { ts: THREAD }));
  const gate = new AsyncEvent();
  gateLimiter(manual, gate);
  await manual.dispatch(setupClick(manual)); // unticked: native mode moves to default
  await manual.idle();
  await manual.dispatch(reply("!stop", THREAD));
  gate.set();
  await manual.settle();
  await manual.dispatch(reply("again", THREAD));
  assert.deepEqual(
    (bypassBox(manual).initial_options as Body[]).map((option) => option.value),
    ["on"],
  );
});

/** A native-bypass folder's thread that started with an explicit off and ran a turn. */
async function ranInANativeBypassFolder(manual: World, unticked = true): Promise<ThreadSession> {
  nativeBypassFolder(manual);
  await manual.dispatch(message("hello", { ts: THREAD }));
  await manual.dispatch(setupClick(manual, SETUP_START, { bypass: !unticked }));
  await manual.settle();
  manual.state.setSession(CHANNEL, THREAD, "sess-ran"); // a thread that ran
  return sessionOf(manual);
}

test("an unticked off survives an idle close", async (t) => {
  const manual = manualWorld(t);
  const session = await ranInANativeBypassFolder(manual);
  await session.close();
  await manual.dispatch(reply("second", THREAD));
  await manual.settle();
  const fresh = manual.sessions.get(CHANNEL, THREAD);
  assert.ok(fresh !== null && fresh !== session);
  assert.deepEqual(manual.clients.at(-1)?.queries, ["second"]);
  assert.equal(manual.clients.at(-1)?.modes.at(-1), "default"); // not back in the folder's own bypass
  assert.equal(fresh.bypass, false);
});

test("an unticked off survives a lost client", async (t) => {
  const manual = manualWorld(t);
  const session = await ranInANativeBypassFolder(manual);
  await internals(session).dropClient(); // the reader-crash path
  await manual.dispatch(reply("second", THREAD));
  await manual.settle();
  assert.equal(manual.clients.at(-1)?.modes.at(-1), "default");
  assert.equal(session.bypass, false);
});

test("bang bypass off in a native folder survives a restart", async (t) => {
  const manual = manualWorld(t);
  const session = await ranInANativeBypassFolder(manual, false);
  assert.equal(session.bypass, true);
  await manual.dispatch(reply("!bypass off", THREAD));
  assert.equal(manual.clients.at(-1)?.modes.at(-1), "default");
  await session.close(); // the restart: a new object from state.json
  await manual.dispatch(reply("second", THREAD));
  await manual.settle();
  assert.equal(manual.clients.at(-1)?.modes.at(-1), "default");
});

test("a thread that never chose keeps the folders own bypass", async (t) => {
  const world = worldOf(t);
  nativeBypassFolder(world);
  await world.dispatch(message("hello", { ts: THREAD })); // the harness presses Start, ticked
  await world.dispatch(reply("!bypass off", THREAD));
  world.state.setBypass(CHANNEL, THREAD, null); // never chosen
  const session = sessionOf(world);
  await internals(session).dropClient();
  await session.ensureConnected();
  assert.deepEqual(world.clients.at(-1)?.modes, []); // left as Claude Code started it
  assert.equal(session.bypass, true);
});

test("the channel status row reads the effective bypass", async (t) => {
  const manual = manualWorld(t);
  await ranInANativeBypassFolder(manual);
  await manual.dispatch(message("!status"));
  assert.ok(!(said(manual).at(-1) as string).includes(texts.STATUS_CHANNEL_BYPASS));
  manual.state.setBypass(CHANNEL, THREAD, null); // never chosen: the folder's bypass runs
  await manual.dispatch(message("!status"));
  assert.ok((said(manual).at(-1) as string).includes(texts.STATUS_CHANNEL_BYPASS));
});

test("a model change keeps a supported effort and the tick", async (t) => {
  const manual = manualWorld(t);
  await manual.dispatch(message("hello", { ts: THREAD }));
  await manual.dispatch(
    setupClick(manual, SETUP_MODEL, { model: "opus", effort: "low", bypass: true }),
  );
  const update = manual.slack.callsTo("chat.update").at(-1) as Body;
  const found = setupControls(update.blocks);
  assert.equal(control(found, SETUP_MODEL).initial_option.value, "opus");
  assert.equal(control(found, SETUP_EFFORT).initial_option.value, "low");
  assert.deepEqual(
    (control(found, SETUP_BYPASS).initial_options as Body[]).map((option) => option.value),
    ["on"],
  );
});
