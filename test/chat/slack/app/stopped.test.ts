/**
 * The app after the daemon stopped (`BuiltApp.close`, which the shutdown calls once the Socket
 * Mode connection is closed): a listener still in flight when the stop ends goes on at its next
 * await, and must do nothing the owner can see, call nothing in Slack and write nothing, since
 * the sessions are closed and the lock is released. Python had no such state: `asyncio.run`
 * cancelled every listener when `run()` returned.
 *
 * Not a port. The listeners are held where the reviewer held them: on the channel check
 * (`conversations.members`), which every inbound path runs after the owner check.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { DELETE_ACTION } from "../../../../src/chat/slack/home.ts";
import { AsyncEvent, CHANNEL, OWNER, THREAD } from "../../../support/fake-slack.ts";
import {
  type Body,
  CLICK_THREAD,
  click,
  clickIn,
  FORM_THREAD,
  homeAction,
  listed,
  message,
  QUESTIONS,
  reply,
  resumeClick,
  SESSION_A,
  sharedFile,
  type World,
  worldOf,
} from "../../../support/slack-app.ts";

/** A world with `THREAD` stored and the channel check held, a body dispatched and waiting on it. */
async function held(
  t: Parameters<typeof worldOf>[0],
  body: Body,
  arrange?: (world: World) => void,
) {
  const world = worldOf(t);
  world.state.openThread(CHANNEL, THREAD, SESSION_A);
  arrange?.(world);
  const gate = new AsyncEvent();
  world.slack.gate = gate;
  world.slack.gateMethod = "conversations.members";
  await world.dispatch(body);
  assert.ok(world.slack.gated.isSet(), "the listener never reached the channel check");
  return { world, gate };
}

/** The daemon's stop: the connection closed, the app told, the sessions closed. */
async function stop(world: World): Promise<void> {
  world.built.close();
  await world.sessions.closeAll();
}

/** What changed since the stop, once the held listener went on. */
async function release(world: World, gate: AsyncEvent): Promise<string[]> {
  const stateFile = join(world.tmpPath, "state.json");
  const before = readFileSync(stateFile, "utf8");
  const calls = world.slack.apiCalls.length;
  gate.set();
  await world.idle();
  assert.equal(readFileSync(stateFile, "utf8"), before, "state.json was written after the stop");
  assert.deepEqual(world.clients, [], "an agent session was started after the stop");
  // The held `conversations.members` is recorded once it is let go; nothing follows it.
  return world.slack.apiCalls.slice(calls).map((call) => call.method);
}

for (const text of ["!help", "!status", "!bypass on", "hello"]) {
  test(`a message in a thread whose channel check ends after the stop does nothing [${text}]`, async (t) => {
    const { world, gate } = await held(t, reply(text, THREAD));
    await stop(world);
    assert.deepEqual(await release(world, gate), ["conversations.members"]);
  });
}

test("a top-level message whose channel check ends after the stop does nothing", async (t) => {
  const { world, gate } = await held(t, message("hello", { ts: "1790000000.000044" }));
  await stop(world);
  assert.deepEqual(await release(world, gate), ["conversations.members"]);
  assert.equal(world.state.thread(CHANNEL, "1790000000.000044"), null);
});

test("a channel the guard refuses is not told about once the daemon stopped", async (t) => {
  const { world, gate } = await held(t, reply("hello", THREAD));
  world.slack.responses["conversations.members"] = {
    ok: true,
    members: [OWNER, "U000BOB"],
  };
  await stop(world);
  assert.deepEqual(await release(world, gate), ["conversations.members"]);
});

test("a message that arrives after the stop is not even checked", async (t) => {
  const world = worldOf(t);
  await stop(world);
  await world.dispatch(message("hello", { ts: "1790000000.000045" }));
  assert.deepEqual(world.slack.apiCalls, []);
  assert.deepEqual(world.clients, []);
});

test("a file download that ends after the stop sends nothing and says nothing", async (t) => {
  const world = worldOf(t);
  world.state.openThread(CHANNEL, THREAD, SESSION_A);
  const body = sharedFile("snippet");
  Object.assign(body.event, { thread_ts: THREAD, ts: "1790000000.000046" });
  world.downloads.set(body.event.files[0].url_private_download, new TextEncoder().encode("hi"));
  world.slowDownloads = 5;
  await world.dispatch(body);
  const calls = world.slack.apiCalls.length;
  await stop(world);
  await world.appClock.advance(5);
  await world.idle();
  assert.deepEqual(
    world.slack.apiCalls.slice(calls).map((call) => call.method),
    [],
  );
  assert.deepEqual(world.clients, []);
});

const CLICKS: ReadonlyArray<readonly [string, (world: World) => Body, (world: World) => boolean]> =
  [
    [
      "approval_allow",
      (world) => {
        const [approvalId] = world.approvals.open(CHANNEL, CLICK_THREAD, "Bash: ls");
        return click("approval_allow", approvalId);
      },
      (world) => world.slack.callsTo("chat.delete").length > 0,
    ],
    [
      "question_open",
      (world) => {
        const [approvalId] = world.approvals.open(CHANNEL, CLICK_THREAD, "Colour", QUESTIONS);
        const body = click("question_open", approvalId);
        body.trigger_id = "0000000000.0000000000.fake";
        return body;
      },
      (world) => world.slack.callsTo("views.open").length > 0,
    ],
    [
      "folder_bind",
      (world) => {
        world.state.removeChannel(CHANNEL);
        return click("folder_bind", "app");
      },
      (world) => world.state.channel(CHANNEL) !== null,
    ],
    [
      "session_resume",
      (world) => {
        world.storedSessions = [listed(SESSION_A, "footer", 0, 1)];
        return resumeClick(SESSION_A, FORM_THREAD);
      },
      (world) => world.state.thread(CHANNEL, FORM_THREAD) !== null,
    ],
  ];

for (const [name, arrange, acted] of CLICKS) {
  test(`a click on ${name} whose channel check ends after the stop does nothing`, async (t) => {
    const world = worldOf(t);
    const body = arrange(world);
    const gate = new AsyncEvent();
    world.slack.gate = gate;
    world.slack.gateMethod = "conversations.members";
    await world.dispatch(body);
    assert.ok(world.slack.gated.isSet(), "the listener never reached the channel check");
    await stop(world);
    const stateFile = join(world.tmpPath, "state.json");
    const before = readFileSync(stateFile, "utf8");
    const calls = world.slack.apiCalls.length;
    gate.set();
    await world.idle();
    assert.equal(readFileSync(stateFile, "utf8"), before);
    assert.equal(acted(world), false);
    assert.deepEqual(
      world.slack.apiCalls.slice(calls).map((call) => call.method),
      ["conversations.members"],
    );
  });
}

test("a Home control used after the stop does nothing", async (t) => {
  const world = worldOf(t);
  await world.home.edit(true);
  await stop(world);
  world.slack.apiCalls.length = 0;
  const value = `${CHANNEL}:${FORM_THREAD}`;
  await world.dispatch(homeAction({ type: "button", action_id: DELETE_ACTION, value }));
  assert.deepEqual(world.deleted, []);
  assert.deepEqual(world.slack.apiCalls, []);
});

test("closing the app twice is harmless", async (t) => {
  const world = worldOf(t);
  world.built.close();
  world.built.close();
  await world.dispatch(clickIn("approval_allow", "none", CHANNEL, THREAD));
  assert.deepEqual(world.slack.apiCalls, []);
});
