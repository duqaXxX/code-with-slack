/**
 * What the session tests share: the recordings cut into the scenes several files replay, and
 * the way into a session's private state where a Python test read an underscore attribute.
 * Python kept these in `tests/test_sessions.py`, which the other files imported.
 */
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { TestContext } from "node:test";
import type { AgentSession, PromptContent } from "../../../src/agent/seam.ts";
import type { Reply } from "../../../src/chat/seam.ts";
import type { TurnRenderer } from "../../../src/core/reply/renderer.ts";
import type { Approvals } from "../../../src/core/requests.ts";
import { logger, ThreadSession } from "../../../src/core/sessions/session.ts";
import type { Event, Task } from "../../../src/core/sessions/tasks.ts";
import type { ActiveTurn, Turn } from "../../../src/core/sessions/turn.ts";
import * as texts from "../../../src/core/texts.ts";
import { FIXTURES, type JsonObject } from "../../support/fixtures.ts";
import {
  type Harness,
  type Item,
  isRecord,
  isSystem,
  recordOf,
  sdkMessages,
  splitTurns,
} from "../../support/sessions.ts";

/** A session's private state, by the names the class gives it: Python's `session._name`. */
export interface Inside {
  client: AgentSession | null;
  active: ActiveTurn | null;
  latest: Reply | null;
  taken: Turn | null;
  sent: Turn[];
  held: unknown[];
  taskReplies: Map<string, TurnRenderer>;
  tasks: Map<string, readonly [string, string]>;
  ended: Array<readonly [string, string]>;
  unreported: Map<string, number>;
  stopped: Set<string>;
  unlanded: Set<Reply>;
  waiting: Set<string>;
  settled: Event;
  injectedExpected: boolean;
  notificationWaits: boolean;
  reportDue: boolean;
  errorStanding: boolean;
  interrupting: boolean;
  compacting: boolean;
  pendingSubmits: number;
  expiry: Task | null;
  idleExpiry: Task | null;
  worker: Task | null;
  reader: Task | null;
  expiring: Set<Task>;
  replyOf: WeakMap<TurnRenderer, Reply>;
  expectInjectedTurn(): void;
  refreshUsage(): void;
  expireInjectedTurn(signal: AbortSignal): Promise<void>;
  sweepClosedOut(signal?: AbortSignal): Promise<void>;
  stopTaskReplies(): Promise<void>;
  awaitLanding(reply: Reply): Promise<void>;
  sink(): Reply | Promise<Reply>;
  idleTimerCheck(): void;
  reactDoneIfIdle(signal?: AbortSignal): Promise<void>;
  threadLine(): readonly [string, string];
}

export function inside(session: ThreadSession): Inside {
  return session as unknown as Inside;
}

/** The ids of the requests waiting for the owner: Python's `h.approvals._pending`. */
export function pending(approvals: Approvals): string[] {
  return [...(approvals as unknown as { entries: Map<string, unknown> }).entries.keys()];
}

/** A turn's prompt text, for a test that reads what was queued. */
export function promptOf(turn: Turn): PromptContent {
  return turn.prompt;
}

// A report opens with Claude Code's notification summary (recorded: `Background command "..."
// completed (exit code 0)`, `Agent "..." finished`), never with a tool line.
export const REPORT = /^[✓✗] (Background command|Agent) "/m;

/** A reply of Claude Code's own turn about a background task (not the owner's). */
export function isReport(reply: string): boolean {
  return REPORT.test(reply) || reply.includes(texts.BACKGROUND_NOTICE);
}

function indexWhere(items: readonly Item[], test: (item: Item) => boolean, from = 0): number {
  const at = items.findIndex((item, index) => index >= from && test(item));
  if (at === -1) throw new Error("the recording holds no such record");
  return at;
}

function turnsOf(items: readonly Item[], count: number): Item[][] {
  const turns = splitTurns(items);
  if (turns.length < count) throw new Error(`the recording holds ${turns.length} turns`);
  return turns;
}

/**
 * The recorded background run: the owner's turn, the notification that arrives while idle, and
 * the turn the CLI injects to report it.
 */
export function splitBackground(
  items: readonly Item[] = sdkMessages("background"),
): [first: Item[], notice: Item[], injected: Item[]] {
  const [first, later] = turnsOf(items, 2) as [Item[], Item[]];
  const start = indexWhere(later, (item) => isSystem(item, "init"));
  return [first, later.slice(0, start), later.slice(start)];
}

/**
 * The recorded subagent that runs two long commands: the owner's turn, what the main stream
 * carries while the agent works (each command's own task, then the agent's), the report turn,
 * and the index in the middle part where each notification ends.
 */
export function splitNestedCommand(): [
  first: Item[],
  work: Item[],
  report: Item[],
  ends: number[],
] {
  const [first, later] = turnsOf(sdkMessages("subagent-nested-command"), 2) as [Item[], Item[]];
  const start = indexWhere(later, (item) => isSystem(item, "init"));
  const work = later.slice(0, start);
  const ends = work.flatMap((item, index) => (isSystem(item, "task_notification") ? [index] : []));
  return [first, work, later.slice(start), ends];
}

/**
 * The recorded subagent whose command outlives it: the owner's turn; what the main stream
 * carries until the agent's first end; the report turn; the command's end and the agent's
 * second start and end; the second report turn.
 */
export function splitNestedBackground(): [
  first: Item[],
  work: Item[],
  report: Item[],
  later: Item[],
  second: Item[],
] {
  const [first, middle, third] = turnsOf(sdkMessages("subagent-nested-background"), 3) as [
    Item[],
    Item[],
    Item[],
  ];
  const start = indexWhere(middle, (item) => isSystem(item, "init"));
  const again = indexWhere(third, (item) => isSystem(item, "init"));
  return [
    first,
    middle.slice(0, start),
    middle.slice(start),
    third.slice(0, again),
    third.slice(again),
  ];
}

/**
 * The same recorded background run as `splitBackground`, with its task and tool ids changed so
 * a second one can run alongside the first with no id collision.
 */
export function renamedBackground(): [first: Item[], notice: Item[], injected: Item[]] {
  const raw = readFileSync(join(FIXTURES, "sdk", "background.jsonl"), "utf8")
    .replaceAll("bny2rux7d", "bc41other")
    .replaceAll("toolu_01LiZwhcy5g12fYSVLgAq5TS", "toolu_01OTHERxxxxxxxxxxxxxxxxx");
  const items = raw
    .split("\n")
    .filter((line) => line.trim() !== "")
    .map((line) => ({ record: JSON.parse(line) as JsonObject }))
    .filter((item) => !isRecord(item, "command_lifecycle") && !isRecord(item, "tool_progress"));
  return splitBackground(items);
}

/** Whether the item is a user message that carries a tool's result. */
export function isToolResult(item: Item): boolean {
  const record = recordOf(item);
  if (record === null || record.type !== "user") return false;
  const message = record.message as JsonObject | undefined;
  const content = message?.content;
  return (
    Array.isArray(content) &&
    content.some(
      (block) =>
        typeof block === "object" &&
        block !== null &&
        !Array.isArray(block) &&
        block.type === "tool_result",
    )
  );
}

/**
 * The recorded subagent whose command outlives it, reordered as when the owner's turn goes on
 * working meanwhile (issue #149): the turn up to the end of the agent's work; the end of the
 * command the agent left running; the turn's last text and its result. Only the order is
 * arranged by hand; every record is recorded.
 */
export function nestedBackgroundEndingMidTurn(): [head: Item[], commandEnd: Item[], tail: Item[]] {
  const [first, work, , later] = splitNestedBackground();
  const launched = indexWhere(first, isToolResult);
  const commandEnd = indexWhere(later, (item) => isSystem(item, "task_notification"));
  return [
    [...first.slice(0, launched + 1), ...work],
    later.slice(0, commandEnd + 1),
    first.slice(launched + 1),
  ];
}

/** The Slack methods whose blocks carried a reply's footer, in call order. */
export function footerWrites(h: Harness): string[] {
  return h.slack.apiCalls
    .filter(
      (call) =>
        Array.isArray(call.args.blocks) &&
        call.args.blocks.some(
          (block) =>
            typeof block === "object" &&
            block !== null &&
            !Array.isArray(block) &&
            block.type === "divider" &&
            Object.keys(block).length === 1,
        ),
    )
    .map((call) => call.method);
}

/** The first record of that `system` subtype in a script, as a plain object. */
export function systemRecord(items: readonly Item[], subtype: string): JsonObject {
  const record = recordOf(items[indexWhere(items, (item) => isSystem(item, subtype))]);
  if (record === null) throw new Error("not a record");
  return record;
}

function contentOf(item: Item | undefined): unknown {
  const message = recordOf(item)?.message;
  return typeof message === "object" && message !== null && !Array.isArray(message)
    ? message.content
    : undefined;
}

/** The id of the first tool call an assistant message makes, or null when it makes none. */
export function toolUseOf(item: Item | undefined): string | null {
  const content = contentOf(item);
  if (!isRecord(item, "assistant") || !Array.isArray(content)) return null;
  for (const block of content) {
    if (typeof block === "object" && block !== null && !Array.isArray(block)) {
      if (block.type === "tool_use" && typeof block.id === "string") return block.id;
    }
  }
  return null;
}

/** An assistant message of the conversation itself (no parent call) that makes a tool call. */
export function isTopLevelCall(item: Item | undefined): boolean {
  return toolUseOf(item) !== null && (recordOf(item)?.parent_tool_use_id ?? null) === null;
}

/** The replay of a prompt: a user message whose content is plain text. */
export function isReplay(item: Item | undefined): boolean {
  return isRecord(item, "user") && typeof contentOf(item) === "string";
}

const TASK_SUBTYPES = ["task_started", "task_progress", "task_notification", "task_updated"];

/** `isinstance(m, sessions.TASK_MESSAGES)`. */
export function isTaskRecord(item: Item | undefined): boolean {
  return TASK_SUBTYPES.some((subtype) => isSystem(item, subtype));
}

/** Until a turn is running and has shown the call. */
export async function commandRuns(
  h: Harness,
  session: ThreadSession,
  callId: string,
): Promise<void> {
  await h.until(() => inside(session).active?.renderer.owns(callId) ?? false);
}

/**
 * Until the session's worker has taken the turn and is waiting to send it, then a moment more:
 * a worker that sends it does so in the next few steps of the loop.
 */
export async function workerHolds(h: Harness, session: ThreadSession, turn: Turn): Promise<void> {
  await h.until(() => inside(session).taken === turn || inside(session).sent.includes(turn));
  await h.sleep(0.1);
}

export function assertNothingRuns(h: Harness, session: ThreadSession): void {
  assert.equal(session.busy, false);
  assert.equal(inside(session).sent.length, 0);
  assert.equal(session.restartReady, true);
  assert.equal(session.restartHold, "");
  assert.deepEqual(h.manager.restartHolds(), []);
  assert.equal(inside(session).interrupting, false);
}

export async function assertStopHasNothingToStop(
  h: Harness,
  session: ThreadSession,
): Promise<void> {
  const interrupts = h.clients[0]?.interrupts;
  assert.equal(await session.stop(), false);
  assert.equal(h.clients[0]?.interrupts, interrupts);
  assertNothingRuns(h, session);
}

/** What the thread's status line said, in order; an empty string where it was cleared. */
export function statusLines(h: Harness): string[] {
  return h.slack.callsTo("assistant.threads.setStatus").map((args) => {
    const messages = args.loading_messages;
    return Array.isArray(messages) && messages.length > 0 ? String(messages[0]) : "";
  });
}

/**
 * `items` with the replay of the prompt (the user message of plain text) carrying `uuid`. The
 * daemon makes its own id for each prompt it sends, so the recorded one cannot match.
 */
export function acknowledged(items: readonly Item[], uuid: string): Item[] {
  return items.map((item) => {
    const record = recordOf(item);
    return record !== null && isReplay(item) ? { record: { ...record, uuid } } : item;
  });
}

/** Submit a prompt and wait until the session has sent it to the agent. */
export async function send(h: Harness, session: ThreadSession, text: PromptContent): Promise<Turn> {
  const sent = h.clients[0]?.sent.length ?? 0;
  const turn = await session.submit(text);
  await h.until(() => h.clients[0]?.sent.length === sent + 1);
  return turn;
}

/** Deliver `items` as the stream does, the replay of the last prompt sent in them. */
export function play(h: Harness, items: readonly Item[]): void {
  const last = h.clients[0]?.sent.at(-1);
  if (last === undefined) throw new Error("no prompt was sent");
  h.clients[0]?.inject(acknowledged(items, last.id));
}

/** The words of every reply, one after the other. */
export function said(h: Harness): string {
  return h.replies().join("\n");
}

/**
 * The lines the sessions log at `level` from here on: Python's `caplog`. Restored when the test
 * ends.
 */
export function logged(t: TestContext, level: "info" | "warning" | "error"): string[] {
  const lines: string[] = [];
  t.mock.method(logger, level, (line: string) => {
    lines.push(line);
  });
  return lines;
}

// What the tests of `tests/test_sessions.py` not ported yet share, in its order.

/**
 * A task's end with another status: Python's `dataclasses.replace(m, status=...)`. The status of
 * a `task_updated` record is in its patch, a notification's is its own; any other item is left
 * as it is.
 */
export function withTaskStatus(item: Item, status: string): Item {
  const record = recordOf(item);
  if (record === null) return item;
  if (isSystem(item, "task_updated")) {
    const patch = (record.patch ?? {}) as JsonObject;
    return { record: { ...record, patch: { ...patch, status } } };
  }
  return isSystem(item, "task_notification") ? { record: { ...record, status } } : item;
}

/** The first task a recording starts, as its `task_started` record. */
export function startedOf(name: string): Item {
  const messages = sdkMessages(name);
  return messages[indexWhere(messages, (item) => isSystem(item, "task_started"))] as Item;
}

/** A recorded turn cut where its background task starts: what came before, and the rest. */
export function splitAtTaskStart(turn: readonly Item[]): [before: Item[], after: Item[]] {
  const at = indexWhere(turn, (item) => isSystem(item, "task_started"));
  return [turn.slice(0, at), turn.slice(at)];
}

/**
 * The recorded end of a background task, as `stop_task` makes it: a `killed` task_updated and a
 * `stopped` notification (SDK 0.2.160 `stop_task` docstring).
 */
export function stoppedEnd(notice: readonly Item[]): Item[] {
  return notice.map((item) =>
    withTaskStatus(item, isSystem(item, "task_updated") ? "killed" : "stopped"),
  );
}

/**
 * Counts how often a session starts waiting for a report turn: Python's `ExpectedTurns`, which
 * wrapped `ThreadSession._expect_injected_turn` for every session of the test.
 */
export function expectedTurns(t: TestContext): { entered: number } {
  const counted = { entered: 0 };
  const prototype = ThreadSession.prototype as unknown as { expectInjectedTurn(): void };
  const original = prototype.expectInjectedTurn;
  t.mock.method(prototype, "expectInjectedTurn", function (this: ThreadSession) {
    counted.entered += 1;
    original.call(this);
  });
  return counted;
}

/**
 * Plays the recorded subagent up to its first report turn's end, with its command still
 * running. Returns that command's task id and the records still to come.
 */
export async function nestedBackgroundRunning(
  h: Harness,
  session: ThreadSession,
): Promise<[command: string, tail: Item[], reportTwo: Item[]]> {
  const [, work, report, tail, reportTwo] = splitNestedBackground();
  await (await session.submit("start it")).done.wait();
  h.clients[0]?.inject(work);
  h.clients[0]?.inject(report);
  return [String(systemRecord(work, "task_started").task_id), tail, reportTwo];
}

/** Every task card Slack was sent, in call order. */
export function taskCards(h: Harness): JsonObject[] {
  return h.slack.apiCalls
    .flatMap((call) => (Array.isArray(call.args.chunks) ? call.args.chunks : []))
    .filter(
      (chunk): chunk is JsonObject =>
        typeof chunk === "object" &&
        chunk !== null &&
        !Array.isArray(chunk) &&
        chunk.type === "task_update",
    );
}

/**
 * A repository one level down the channel's folder, which is no repository itself, on the
 * branch `feature-x`: Python's `repo` fixture.
 */
export function repoUnder(tmpPath: string): string {
  const repo = join(tmpPath, "app");
  execFileSync("git", ["init", "-q", "-b", "feature-x", repo]);
  return repo;
}
