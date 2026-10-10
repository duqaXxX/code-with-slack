/**
 * A background subagent that hands its report back, replayed from `subagent-handback`: recorded
 * on 2026-10-10 with `@anthropic-ai/claude-agent-sdk` 0.3.296 and Claude Code 2.1.296, the
 * owner's setting sources loaded. Not a port: the Python daemon's recordings (Claude Code
 * 2.1.286) end a background subagent with its notification and one turn that reports it.
 *
 * Measured there, and on Claude Code 2.1.294 through the Python SDK 0.2.165 the same day: the
 * subagent's last call is `SubagentHandback`; its report reaches the main agent as a message
 * from a peer that names the task, and starts a turn; the task's own notification then gets a
 * turn that does nothing (no message, no call, no text). The Python daemon, given this
 * recording, posted `Background task update` and `Done. Claude Code returned no text.` as a
 * message of its own for that turn.
 *
 * The recording: a turn starts a subagent and a command, both in the background, and ends; the
 * subagent ends and hands back; the turn that reports it; the turn that does nothing; the
 * command ends; the turn that reports it.
 */
import assert from "node:assert/strict";
import { type TestContext, test } from "node:test";
import * as texts from "../../../src/core/texts.ts";
import {
  type Harness,
  harnessFor,
  type Item,
  isResult,
  isSystem,
  recordOf,
  sdkMessages,
} from "../../support/sessions.ts";
import { inside } from "./helpers.ts";

const NAME = "subagent-handback";
const AGENT_ENDED = '✓ Agent "Run echo subagent-ok" finished';
const SHELL_ENDED = '✓ Background command "Wait 15 seconds" completed (exit code 0)';
const HANDED_BACK = "The subagent finished: `echo subagent-ok` printed `subagent-ok`.";
const SHELL_REPORTED = "The command `sleep 15` ended with exit code 0, so both tasks are done.";

interface Recording {
  /** The owner's turn, up to its result: both tasks started, the subagent's one call made. */
  readonly turn: Item[];
  /** The subagent's hand-back call and the end of its task. */
  readonly agentEnds: Item[];
  /** The turn the hand-back starts, from its `init` to its result. */
  readonly report: Item[];
  /** The turn the task's notification gets, which does nothing: an `init` and a result. */
  readonly nothing: Item[];
  /** The command's end and the turn that reports it. */
  readonly shell: Item[];
}

function cut(): Recording {
  const items = sdkMessages(NAME);
  const results = items.flatMap((item, index) => (isResult(item) ? [index] : []));
  assert.equal(results.length, 4);
  const [first, second, third] = results as [number, number, number, number];
  const reportStarts = items.findIndex((item, index) => index > first && isSystem(item, "init"));
  return {
    turn: items.slice(0, first + 1),
    agentEnds: items.slice(first + 1, reportStarts),
    report: items.slice(reportStarts, second + 1),
    nothing: items.slice(second + 1, third + 1),
    shell: items.slice(third + 1),
  };
}

/** The harness once `turn` has run and its reply stays open for the tasks it started. */
async function started(t: TestContext, turn: Item[]): Promise<Harness> {
  const h = harnessFor(t)({ turns: [turn] });
  await (await h.session().submit("start both")).done.wait();
  return h;
}

async function settle(h: Harness): Promise<void> {
  for (let passes = 0; passes < 10; passes += 1) await h.sleep(0.5);
}

function noExtraMessage(h: Harness): void {
  const all = h.replies().join("\n");
  assert.equal(all.includes(texts.NO_OUTPUT), false, "the turn that did nothing wrote a reply");
  assert.equal(all.includes(texts.BACKGROUND_NOTICE), false, "a report opened under the notice");
  assert.equal(h.bodies().length, 1, "everything belongs to the reply that started the tasks");
}

test("the recording is the one the cut expects", () => {
  const { turn, agentEnds, report, nothing, shell } = cut();
  assert.ok(turn.some((item) => isSystem(item, "task_started")));
  assert.ok(agentEnds.some((item) => isSystem(item, "task_notification")));
  const peer = report.map(recordOf).find((record) => record?.type === "user");
  assert.equal((peer?.origin as { kind?: string } | undefined)?.kind, "peer");
  assert.deepEqual(
    nothing.map((item) => recordOf(item)?.type),
    ["system", "result"],
  );
  assert.equal(recordOf(nothing.at(-1))?.num_turns, 0);
  assert.ok(shell.some((item) => isSystem(item, "task_notification")));
});

test("a hand-back is reported in the reply that started the subagent, and the turn that does nothing opens none", async (t) => {
  const { turn, agentEnds, report, nothing, shell } = cut();
  const h = await started(t, turn);
  h.clients[0]?.inject([...agentEnds, ...report, ...nothing]);
  await settle(h);
  noExtraMessage(h);
  const body = h.bodies()[0] ?? "";
  assert.ok(body.indexOf(AGENT_ENDED) < body.indexOf(HANDED_BACK) && body.includes(AGENT_ENDED));
  h.clients[0]?.inject(shell);
  await h.until(() => (h.bodies()[0] ?? "").includes(SHELL_REPORTED));
  await settle(h);
  noExtraMessage(h);
  const whole = h.bodies()[0] ?? "";
  assert.ok(whole.indexOf(HANDED_BACK) < whole.indexOf(SHELL_ENDED));
  assert.ok(whole.indexOf(SHELL_ENDED) < whole.indexOf(SHELL_REPORTED));
  assert.equal(h.reactions().at(-1), "white_check_mark");
});

// The subagent can end while the turn that started it still runs (seen live the same day, with
// a one-command subagent): its notification then comes inside that turn, no end line is kept
// for a report, and only the hand-back says whose report the next turn is.
test("a hand-back after a subagent that ended inside its turn is reported in the same reply", async (t) => {
  const { turn, agentEnds, report, nothing, shell } = cut();
  const result = turn.at(-1) as Item;
  const h = await started(t, [...turn.slice(0, -1), ...agentEnds, result]);
  h.clients[0]?.inject([...report, ...nothing]);
  await h.until(() => (h.bodies()[0] ?? "").includes(HANDED_BACK));
  await settle(h);
  noExtraMessage(h);
  h.clients[0]?.inject(shell);
  await h.until(() => (h.bodies()[0] ?? "").includes(SHELL_REPORTED));
  await settle(h);
  noExtraMessage(h);
});

// The hand-back's turn can also begin before the task's own end is told.
test("a hand-back that comes before its task's end is reported in the same reply, once", async (t) => {
  const { turn, agentEnds, report, nothing } = cut();
  const h = await started(t, turn);
  h.clients[0]?.inject([...report, ...agentEnds, ...nothing]);
  await h.until(() => (h.bodies()[0] ?? "").includes(HANDED_BACK));
  await settle(h);
  noExtraMessage(h);
  const body = h.bodies()[0] ?? "";
  assert.equal(body.split(HANDED_BACK).length, 2);
  // The end told after the report is not waited on for a turn of its own.
  assert.equal(inside(h.session()).injectedExpected, false);
});

test("a turn of the agent's that does nothing leaves an idle session as it was", async (t) => {
  const { turn, nothing } = cut();
  const plain = sdkMessages("tools");
  const h = harnessFor(t)({ turns: [plain] });
  await (await h.session().submit("read the notes")).done.wait();
  await settle(h);
  const writes = h.writes().length;
  const reactions = h.reactions().length;
  void turn;
  h.clients[0]?.inject(nothing);
  await settle(h);
  assert.equal(h.writes().length, writes);
  assert.equal(h.reactions().length, reactions);
  assert.equal(h.bodies().length, 1);
  assert.equal(inside(h.session()).active, null);
});

// A task's end with no turn running is waited on for the turn that reports it
// (`INJECTED_TURN_WAIT`). The turn that does nothing is that turn: the wait ends with it.
test("a turn that does nothing ends the wait for a task's report", async (t) => {
  const { turn, agentEnds, nothing } = cut();
  const h = await started(t, turn);
  h.clients[0]?.inject(agentEnds);
  await h.until(() => inside(h.session()).injectedExpected);
  h.clients[0]?.inject(nothing);
  await h.until(() => !inside(h.session()).injectedExpected);
  await settle(h);
  assert.equal(h.replies().join("\n").includes(texts.NO_OUTPUT), false);
  assert.equal(h.bodies().length, 1);
});
