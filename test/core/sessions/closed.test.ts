/**
 * What a session manager does once a stop has closed its sessions (`closeAll`): a listener that
 * was still in flight when the shutdown ended must not build a session nobody will close, nor
 * write `state.json` after the lock was released. Python had no such state: `asyncio.run`
 * cancelled every listener when `run()` returned.
 *
 * What each entry point of the manager does after the close:
 * - `get`: null, as for a thread that is not a session (no session is built).
 * - `open`, `resume`, `bind`: `SessionClosed`, before the state is read or written.
 * - `release`: false, as for a session in use (nothing is held, nothing is closed).
 * - `wrote`, `held`, `free`, `stopChannel`, `liveThreads`, `sessionsOf`, `restartHolds`,
 *   `workingIn`, `drain`, `repository`, `sessionsIn`, `dated`, `unavailable`, `foldersIn`: they
 *   start no process and write nothing to `state.json` (they read, or flip a flag of a map that
 *   `closeAll` emptied), so they are left as they are.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { SessionClosed } from "../../../src/core/sessions/errors.ts";
import { CHANNEL, THREAD } from "../../support/fake-slack.ts";
import { harnessFor } from "../../support/sessions.ts";

const STORED = "1780000000.000009";

/** A manager with one thread stored and one live, closed by `closeAll`. */
async function closed(t: Parameters<typeof harnessFor>[0]) {
  const h = harnessFor(t)();
  h.state.openThread(CHANNEL, STORED, "stored-id");
  h.session(THREAD);
  await h.manager.closeAll();
  const file = (): string => readFileSync(join(h.tmpPath, "state.json"), "utf8");
  return { h, file, before: file(), started: h.clients.length };
}

test("a closed manager hands out no session for a stored thread", async (t) => {
  const { h, started } = await closed(t);
  assert.equal(h.manager.get(CHANNEL, STORED), null);
  assert.equal(h.manager.get(CHANNEL, THREAD), null);
  assert.deepEqual(h.manager.liveThreads(), []);
  assert.equal(h.clients.length, started);
});

test("a closed manager opens no thread and writes nothing", async (t) => {
  const { h, file, before } = await closed(t);
  assert.throws(() => h.manager.open(CHANNEL, "1780000000.000010"), SessionClosed);
  assert.equal(h.state.thread(CHANNEL, "1780000000.000010"), null);
  assert.equal(file(), before);
});

test("a closed manager resumes no session and writes nothing", async (t) => {
  const { h, file, before } = await closed(t);
  await assert.rejects(h.manager.resume(CHANNEL, "1780000000.000011", "some-id"), SessionClosed);
  assert.equal(h.state.thread(CHANNEL, "1780000000.000011"), null);
  assert.equal(file(), before);
});

test("a closed manager binds no channel and writes nothing", async (t) => {
  const { h, file, before } = await closed(t);
  await assert.rejects(h.manager.bind(CHANNEL, join(h.tmpPath, "elsewhere")), SessionClosed);
  assert.equal(file(), before);
});

test("a closed manager holds no thread for a delete", async (t) => {
  const { h } = await closed(t);
  assert.equal(await h.manager.release(CHANNEL, STORED), false);
  assert.equal(h.manager.held(CHANNEL, STORED), false);
});

test("a close in flight already refuses what comes in while it waits", async (t) => {
  // The flag is set at the top of `closeAll`, not once it is done: the sessions close one after
  // another, and a listener can run between two of them.
  const h = harnessFor(t)();
  h.session(THREAD);
  const closing = h.manager.closeAll();
  assert.equal(h.manager.get(CHANNEL, STORED), null);
  assert.throws(() => h.manager.open(CHANNEL, "1780000000.000012"), SessionClosed);
  await closing;
});
