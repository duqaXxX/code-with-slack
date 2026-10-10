/**
 * A compaction as the session shows it (issue #169), replayed from recordings made on
 * 2026-10-08 with claude-agent-sdk 0.2.164 and its bundled CLI 2.1.292, with the daemon's own
 * CLI arguments (`--replay-user-messages`): `compact` is a whole session; `auto-compact` is the
 * one turn of a longer session in which Claude Code compacted on its own;
 * `auto-compact-report-turn` is the end of a session just past its threshold, from the turn
 * that starts a background command to the turn Claude Code starts to report it, which compacts
 * first. Port of `tests/test_sessions_compaction.py`.
 *
 * Measured there: a compaction opens with a `status` record that says `compacting` and ends
 * with one that carries `compact_result`, and `compact_boundary` follows. They come before
 * every record of their turn that could show anything. On `/compact` only `status` and `init`
 * records come before, and the prompt is never replayed; on an automatic compaction at the start
 * of a turn the replay of the prompt comes before the boundary as well.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import { INJECTED_TURN_WAIT } from "../../../src/core/sessions/constants.ts";
import {
  END_OF_STREAM,
  harnessFor,
  type Item,
  isSystem,
  recordOf,
  sdkMessages,
  splitTurns,
} from "../../support/sessions.ts";
import { play, send, statusLines } from "./helpers.ts";

const COMPACTING = "Compacting conversation…";

function at<T>(list: readonly T[], index: number): T {
  const found = list.at(index);
  if (found === undefined) throw new Error(`nothing at ${index}`);
  return found;
}

function boundary(items: readonly Item[]): Item {
  const found = items.find((item) => isSystem(item, "compact_boundary"));
  if (found === undefined) throw new Error("no boundary");
  return found;
}

/** Where the `status` record that says `compacting` sits in `items`. */
function compacting(items: readonly Item[]): number {
  const found = items.findIndex(
    (item) => isSystem(item, "status") && recordOf(item)?.status === "compacting",
  );
  if (found === -1) throw new Error("no compaction");
  return found;
}

test("a compact command says how many tokens it saved and the session goes on", async (t) => {
  const turns = splitTurns(sdkMessages("compact"));
  const h = harnessFor(t)({ turns });
  for (const prompt of ["remember a word", "read the ledger", "/compact", "which word"]) {
    await (await h.session().submit(prompt)).done.wait();
  }
  assert.deepEqual(
    h.slack.streamTexts().map((text) => text.trim()),
    ["ok", "read", "Compacted the conversation: 20.7k → 5.0k tokens.", "MARK-ALPHA"],
  );
});

test("a compaction before a turns first words shows above them", async (t) => {
  const h = harnessFor(t)({ turns: [] });
  const running = await send(h, h.session(), "read the ledgers again");
  play(h, sdkMessages("auto-compact"));
  await running.done.wait();
  const [text, ...more] = h.slack.streamTexts();
  assert.equal(more.length, 0);
  assert.deepEqual(text?.split("\n\n"), [
    "Compacted the conversation: 67.9k → 10.6k tokens.",
    "read",
  ]);
});

test("a boundary with no turn due starts no reply", async (t) => {
  const turns = splitTurns(sdkMessages("compact"));
  const h = harnessFor(t)({ turns: turns.slice(0, 1) });
  const session = h.session();
  await (await session.submit("remember a word")).done.wait();
  h.clients[0]?.inject([boundary(at(turns, 2))]);
  await h.sleep(0.05);
  assert.ok(h.slack.streamTs.length === 1 && session.idle);
});

test("a compaction with no turn due starts no reply and shows no line", async (t) => {
  const turns = splitTurns(sdkMessages("compact"));
  const h = harnessFor(t)({ turns: turns.slice(0, 1) });
  const session = h.session();
  await (await session.submit("remember a word")).done.wait();
  await h.until(() => statusLines(h).at(-1) === "");
  h.clients[0]?.inject([at(at(turns, 2), compacting(at(turns, 2)))]);
  await h.sleep(0.05);
  assert.ok(h.slack.streamTs.length === 1 && session.idle);
  assert.ok(!statusLines(h).includes(COMPACTING));
});

test("the thread says it is compacting until the compaction ends", async (t) => {
  const turn = at(splitTurns(sdkMessages("compact")), 2);
  const cut = compacting(turn) + 1;
  const h = harnessFor(t)({ turns: [] });
  const running = await send(h, h.session(), "/compact");
  assert.equal(statusLines(h).at(-1), "Working…");
  h.clients[0]?.inject(turn.slice(0, cut));
  await h.until(() => statusLines(h).at(-1) === COMPACTING);
  const shown = h.slack.callsTo("assistant.threads.setStatus").at(-1);
  assert.equal(shown?.status, "is compacting the conversation…");
  h.clients[0]?.inject(turn.slice(cut));
  await running.done.wait();
  await h.until(() => statusLines(h).at(-1) === "");
});

test("a turn that goes on after a compaction says it is working again", async (t) => {
  const turn = sdkMessages("auto-compact");
  const began = compacting(turn) + 1;
  const h = harnessFor(t)({ turns: [] });
  const running = await send(h, h.session(), "read the ledgers again");
  play(h, turn.slice(0, began));
  await h.until(() => statusLines(h).at(-1) === COMPACTING);
  // The record that carries `compact_result` ends it, before the boundary arrives.
  assert.ok("compact_result" in (recordOf(turn[began]) ?? {}));
  play(h, turn.slice(began, began + 1));
  await h.until(() => statusLines(h).at(-1) === "Working…");
  play(h, turn.slice(began + 1));
  await running.done.wait();
});

test("a permission mode report does not end the compacting line", async (t) => {
  const turn = at(splitTurns(sdkMessages("compact")), 2);
  const cut = compacting(turn) + 1;
  const h = harnessFor(t)({ turns: [] });
  const running = await send(h, h.session(), "/compact");
  h.clients[0]?.inject(turn.slice(0, cut));
  await h.until(() => statusLines(h).at(-1) === COMPACTING);
  h.clients[0]?.inject(sdkMessages("permission-mode-status"));
  await h.sleep(0.05);
  assert.equal(statusLines(h).at(-1), COMPACTING);
  h.clients[0]?.inject(turn.slice(cut));
  await running.done.wait();
});

test("a process that exits while compacting leaves no compacting line behind", async (t) => {
  const turns = splitTurns(sdkMessages("compact"));
  const cut = compacting(at(turns, 2)) + 1;
  const h = harnessFor(t)(
    { turns: [[...at(turns, 2).slice(0, cut), END_OF_STREAM]] },
    { turns: [at(turns, 3)] },
  );
  const session = h.session();
  await (await session.submit("/compact")).done.wait();
  const before = statusLines(h).length;
  await (await session.submit("which word")).done.wait();
  const after = statusLines(h).slice(before);
  assert.ok(after.includes("Working…") && !after.includes(COMPACTING));
});

test("a report turn that compacts first is waited for", async (t) => {
  const [first, report] = splitTurns(sdkMessages("auto-compact-report-turn")) as [Item[], Item[]];
  const began = compacting(report) + 1;
  const h = harnessFor(t)({ turns: [first] });
  await (await h.session().submit("start it")).done.wait();
  h.clients[0]?.inject(report.slice(0, began));
  await h.idle();
  // Several times the wait for a report turn: Python lowered the wait and slept through six.
  await h.clock.advance(INJECTED_TURN_WAIT * 6);
  assert.deepEqual(h.slack.callsTo("chat.stopStream"), []);
  assert.equal(statusLines(h).at(-1), COMPACTING);
  h.clients[0]?.inject(report.slice(began));
  await h.until(() => h.slack.callsTo("chat.stopStream").length > 0);
  const [text, ...more] = h.slack.streamTexts();
  assert.equal(more.length, 0);
  assert.ok(text?.includes("Compacted the conversation: 68.2k → 10.7k tokens."));
});
