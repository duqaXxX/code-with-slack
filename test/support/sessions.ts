/**
 * The harness of the session tests: a real `SessionManager` over a scripted agent back end and
 * the real Slack provider on `FakeSlack`. Port of `FakeClaudeClient` (`tests/fakes.py`) and of
 * `Harness` and `harness_for` (`tests/test_sessions.py`).
 *
 * Python scripted the SDK's client with parsed messages. Here the fake stands at the agent seam,
 * and a script is made of the same recordings: each wire record goes through the session's own
 * `Translator` (the real one, as the live session does it), so a text streamed and then repeated
 * reaches the core once and a replayed prompt is `prompt_taken`. A script can also hold a
 * session event as it is, a request put to the handler the core passed to `start`, a hook's
 * run, and the end of the stream.
 *
 * `sdkMessages` gives a recording as Python's `sdk_messages` did, one item per message the
 * Python SDK parsed: it parsed no `command_lifecycle` and no `tool_progress` record (checked on
 * every recording, claude-agent-sdk 0.2.165), so an index into a Python test's list is the same
 * index here.
 *
 * Two clocks, both fake. `clock` is the sessions' own (`INJECTED_TURN_WAIT`, the idle close, the
 * drain's poll): Python's event loop, which its tests crossed by lowering the constants.
 * `slackClock` is the provider's (a reply's debounce and deadline, the status line's refresh).
 */
import { mkdtempSync, realpathSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { TestContext } from "node:test";
import { CAPABILITIES } from "../../src/agent/claude/capabilities.ts";
import { postToolUseHookEvents, stopHookEvents } from "../../src/agent/claude/hooks.ts";
import { agentInfo, contextUsage } from "../../src/agent/claude/info.ts";
import { toRequest } from "../../src/agent/claude/requests.ts";
import { Translator } from "../../src/agent/claude/translate.ts";
import { locate, Unkeyed } from "../../src/agent/claude/trust.ts";
import type {
  AgentBackend,
  AgentInfo,
  AgentSession,
  Capabilities,
  ContextUsage,
  ListedSession,
  PermissionAnswer,
  PermissionRequest,
  Prompt,
  PromptContent,
  QuestionAnswer,
  QuestionRequest,
  Repository,
  RequestHandler,
  SessionEvent,
  StartOptions,
} from "../../src/agent/seam.ts";
import { UpdateLimiter } from "../../src/chat/slack/reply/limiter.ts";
import { resetStatusFlags } from "../../src/chat/slack/reply/status.ts";
import { SlackChat } from "../../src/chat/slack/thread.ts";
import { UsageCache } from "../../src/core/footer.ts";
import { Holds } from "../../src/core/hold.ts";
import { Approvals } from "../../src/core/requests.ts";
import { PROCESS_EXITED } from "../../src/core/sessions/constants.ts";
import type { SessionDeps } from "../../src/core/sessions/deps.ts";
import { SessionManager } from "../../src/core/sessions/manager.ts";
import type { ThreadSession } from "../../src/core/sessions/session.ts";
import { StateStore } from "../../src/core/state.ts";
import { setWriter } from "../../src/log.ts";
import {
  type AsyncEvent,
  BOT,
  CHANNEL,
  FakeClock,
  FakeSlack,
  OWNER,
  TEAM,
  THREAD,
} from "./fake-slack.ts";
import { type JsonObject, sdkJson, sdkRecords } from "./fixtures.ts";

/** A point in a scripted turn where the CLI would ask the host for a permission decision. */
export interface CanUseToolCall {
  readonly toolName: string;
  readonly input: JsonObject;
  readonly callId: string;
}

/** One step of a script. */
export type Item =
  /** A wire record of a recording, read by the session's translator. */
  | { readonly record: JsonObject }
  /** A session event as it is. */
  | { readonly event: SessionEvent }
  /** The CLI asks for a permission decision, and waits for it before it goes on. */
  | { readonly ask: CanUseToolCall }
  /** The CLI runs the host's hooks: Stop before the result, PostToolUse after a tool. */
  | { readonly hook: "Stop" | "PostToolUse"; readonly input: JsonObject }
  /** The CLI process exits and its stream ends. */
  | { readonly end: true };

export type Batch = readonly Item[];

// The records the Python SDK's parser gave no message for.
const UNPARSED = new Set(["command_lifecycle", "tool_progress"]);

/** A recording as Python's `sdk_messages` gave it: one item per message its SDK parsed. */
export function sdkMessages(name: string): Item[] {
  return sdkRecords(name)
    .filter((record) => !UNPARSED.has(String(record.type)))
    .map((record) => ({ record }));
}

/** The record of an item, or null for an item that is none. */
export function recordOf(item: Item | undefined): JsonObject | null {
  return item !== undefined && "record" in item ? item.record : null;
}

/** Whether the item is a record of that type (`isinstance(m, ResultMessage)` is `"result"`). */
export function isRecord(item: Item | undefined, type: string, subtype?: string): boolean {
  const record = recordOf(item);
  return (
    record !== null && record.type === type && (subtype === undefined || record.subtype === subtype)
  );
}

/** `isinstance(m, ResultMessage)`. */
export function isResult(item: Item | undefined): boolean {
  return isRecord(item, "result");
}

/** `isinstance(m, TaskStartedMessage)` and its siblings: a `system` record of that subtype. */
export function isSystem(item: Item | undefined, subtype: string): boolean {
  return isRecord(item, "system", subtype);
}

/** The turns of a script: cut after each result, as Python's `split_turns` cut messages. */
export function splitTurns(items: readonly Item[]): Item[][] {
  const turns: Item[][] = [[]];
  for (const item of items) {
    turns.at(-1)?.push(item);
    if (isResult(item)) turns.push([]);
  }
  return turns.filter((turn) => turn.length > 0);
}

export function canUseToolCall(toolName: string, input: JsonObject, callId = "toolu_fake_1"): Item {
  return { ask: { toolName, input, callId } };
}

export function hookRun(input: JsonObject, hook: "Stop" | "PostToolUse" = "Stop"): Item {
  return { hook, input };
}

export const END_OF_STREAM: Item = { end: true };

export function event(sessionEvent: SessionEvent): Item {
  return { event: sessionEvent };
}

// The items Python's fake put a prompt's replay before: the first stream event, assistant, user
// or result message of the turn.
const WORDED_RECORDS = new Set(["stream_event", "assistant", "user", "result"]);
const WORDED_EVENTS = new Set<SessionEvent["type"]>([
  "message_started",
  "message_ended",
  "text_started",
  "text_delta",
  "text",
  "call_started",
  "call_ended",
  "agent_error",
  "turn_ended",
]);

function worded(item: Item): boolean {
  if ("record" in item) return WORDED_RECORDS.has(String(item.record.type));
  return "event" in item && WORDED_EVENTS.has(item.event.type);
}

/** What a scripted session is given: Python's keyword arguments of `FakeClaudeClient`. */
export interface Script {
  readonly turns?: readonly Batch[];
  /** The answer to `info()`; the recorded `server-info.json`, started in `default`, when absent. */
  readonly info?: AgentInfo;
  readonly contextUsage?: ContextUsage;
  /** `start` rejects with it (Python's `connect_error`). */
  readonly startError?: Error;
  /** A start that waits for the test, as a CLI does while it starts. */
  readonly startGate?: AsyncEvent;
  readonly infoError?: Error;
  readonly contextUsageError?: Error;
  /** `close` rejects with it (Python's `disconnect_error`). */
  readonly closeError?: Error;
  /** A close that waits for the test, as a CLI takes real time to flush and exit after EOF. */
  readonly closeGate?: AsyncEvent;
}

const CLOSED = Symbol("closed");

/**
 * A feed of batches for one reader: `asyncio.Queue`. A batch put while the reader waits reaches
 * it on the next turn of the event loop, as asyncio woke a waiting reader: what the test, or the
 * session that just sent a prompt, does next in the same step comes first.
 */
class Feed {
  readonly #items: Array<Batch | typeof CLOSED> = [];
  #wake: (() => void) | null = null;

  put(item: Batch | typeof CLOSED): void {
    this.#items.push(item);
    const wake = this.#wake;
    this.#wake = null;
    if (wake !== null) setImmediate(wake);
  }

  async get(): Promise<Batch | typeof CLOSED> {
    while (this.#items.length === 0) {
      await new Promise<void>((resolve) => {
        this.#wake = resolve;
      });
    }
    return this.#items.shift() as Batch | typeof CLOSED;
  }
}

function defaultInfo(): AgentInfo {
  const recorded = sdkJson("server-info") as JsonObject;
  return agentInfo({
    commands: recorded.commands,
    models: recorded.models,
    current_permission_mode: "default",
  });
}

/** Stands in for a live agent session at the seam and plays scripted turns. */
export class FakeAgentSession implements AgentSession {
  readonly options: StartOptions;
  readonly events: AsyncIterable<SessionEvent>;
  readonly #requests: RequestHandler;
  readonly #script: Script;
  readonly #turns: Batch[];
  readonly #feed = new Feed();
  readonly #translator = new Translator();
  connected = false;
  /** Each prompt as sent: its text, or the parts of a prompt with an image. */
  queries: PromptContent[] = [];
  /** Every prompt as sent, id included. */
  sent: Prompt[] = [];
  modes: string[] = [];
  modelsSet: Array<string | null> = [];
  effortsSet: Array<string | null> = [];
  interrupts = 0;
  stoppedTasks: string[] = [];
  /** What the core answered each request with, in order. */
  permissionResults: Array<PermissionAnswer | QuestionAnswer> = [];

  constructor(options: StartOptions, requests: RequestHandler, script: Script = {}) {
    this.options = options;
    this.#requests = requests;
    this.#script = script;
    this.#turns = [...(script.turns ?? [])];
    this.events = this.#stream();
  }

  async send(prompt: Prompt): Promise<void> {
    this.sent.push(prompt);
    this.queries.push(prompt.content);
    this.#translator.promptSent(prompt.id);
    const batch = this.#turns.shift();
    if (batch !== undefined) this.#feed.put(this.#replayed(batch, prompt));
  }

  /**
   * `batch` with the replay of the prompt it answers, as Claude Code sends it with
   * `--replay-user-messages`: a `user` record of the prompt's text under the id it was sent
   * with, after the turn's `init` and before its first words (recorded 2026-10-06, CLI 2.1.286,
   * and 2026-10-09, 2.1.292). A command is not replayed so (`compact.jsonl`), and a prompt with
   * an image was never recorded: those turns are played as scripted.
   */
  #replayed(batch: Batch, prompt: Prompt | undefined): Batch {
    if (prompt === undefined) return batch;
    const { content } = prompt;
    if (typeof content !== "string" || content.trimStart().startsWith("/")) return batch;
    const found = batch.findIndex(worded);
    const at = found === -1 ? batch.length : found;
    const replay: Item = {
      record: {
        type: "user",
        message: { role: "user", content },
        parent_tool_use_id: null,
        uuid: prompt.id,
      },
    };
    return [...batch.slice(0, at), replay, ...batch.slice(at)];
  }

  /**
   * Deliver the turn of the last prompt sent, with its replay, when the test rather than
   * `turns` holds that turn.
   */
  answer(batch: Batch): void {
    this.#feed.put(this.#replayed(batch, this.sent.at(-1)));
  }

  /** Deliver a turn nobody asked for, as the CLI does for a background-task notification. */
  inject(batch: Batch): void {
    this.#feed.put(batch);
  }

  async *#stream(): AsyncGenerator<SessionEvent> {
    for (;;) {
      const batch = await this.#feed.get();
      if (batch === CLOSED) return;
      for (const item of batch) {
        if ("end" in item) {
          yield { type: "process_lost", reason: PROCESS_EXITED };
          return;
        }
        if ("ask" in item) {
          this.permissionResults.push(await this.#ask(item.ask));
        } else if ("hook" in item) {
          yield* item.hook === "Stop"
            ? stopHookEvents(item.input)
            : postToolUseHookEvents(item.input);
        } else if ("record" in item) {
          yield* this.#translator.translate(item.record);
        } else {
          yield item.event;
        }
      }
    }
  }

  /** The request the back end makes of a permission callback, put to the core's handler. */
  #ask(call: CanUseToolCall): Promise<PermissionAnswer | QuestionAnswer> {
    const mapped = toRequest(`request-${call.callId}`, call.toolName, call.input, {
      toolUseID: call.callId,
    });
    if (mapped.type === "question") {
      const request: QuestionRequest = { ...mapped, toolName: call.toolName, title: null };
      return this.#requests.question(request);
    }
    const request: PermissionRequest = mapped;
    return this.#requests.permission(request);
  }

  async setPermissionMode(mode: string): Promise<void> {
    this.modes.push(mode);
  }

  async setModel(model: string | null): Promise<void> {
    this.modelsSet.push(model);
  }

  async setEffort(level: string | null): Promise<void> {
    this.effortsSet.push(level);
  }

  async interrupt(): Promise<void> {
    this.interrupts += 1;
  }

  async stopTask(taskId: string): Promise<void> {
    this.stoppedTasks.push(taskId);
  }

  async info(): Promise<AgentInfo> {
    if (this.#script.infoError !== undefined) throw this.#script.infoError;
    return this.#script.info ?? defaultInfo();
  }

  async contextUsage(): Promise<ContextUsage> {
    if (this.#script.contextUsageError !== undefined) throw this.#script.contextUsageError;
    return this.#script.contextUsage ?? contextUsage(sdkJson("context-usage"));
  }

  async close(): Promise<void> {
    if (this.#script.closeGate !== undefined) await this.#script.closeGate.wait();
    this.connected = false;
    this.#feed.put(CLOSED);
    if (this.#script.closeError !== undefined) throw this.#script.closeError;
  }
}

/** `trust.trusted_repository` for a test about something else: every repository counts as trusted. */
export async function anyRepository(directory: string): Promise<Repository | null> {
  try {
    return await locate(directory);
  } catch (error) {
    if (error instanceof Unkeyed) return null;
    throw error;
  }
}

/** The back end of a harness: each `start` plays the next script. */
export class FakeAgentBackend implements AgentBackend {
  capabilities: Capabilities = CAPABILITIES_FOR_TESTS;
  /** Every session started, in order (Python's `h.clients`); one whose start failed is there too. */
  readonly sessions: FakeAgentSession[] = [];
  /** What `listSessions` answers for a folder. */
  listed: ListedSession[] = [];
  trusted: (folder: string) => Promise<boolean> = async () => true;
  repository: (folder: string, sessionFolder: string) => Promise<Repository | null> = (folder) =>
    anyRepository(folder);
  readonly #scripts: Script[];

  constructor(scripts: readonly Script[]) {
    this.#scripts = [...scripts];
  }

  async start(options: StartOptions, requests: RequestHandler): Promise<AgentSession> {
    const script = this.#scripts.shift() ?? {};
    const session = new FakeAgentSession(options, requests, script);
    this.sessions.push(session);
    if (script.startGate !== undefined) await script.startGate.wait();
    if (script.startError !== undefined) throw script.startError;
    session.connected = true;
    return session;
  }

  async listSessions(_folder: string): Promise<readonly ListedSession[]> {
    return this.listed;
  }

  async datedSessions(
    _folder: string,
    sessions: readonly ListedSession[],
  ): Promise<ListedSession[]> {
    return [...sessions].sort((a, b) => b.lastModified - a.lastModified);
  }

  async aliveSessions(_folder: string): Promise<ReadonlySet<string> | null> {
    return new Set(this.listed.map((session) => session.id));
  }

  folderTrusted(folder: string): Promise<boolean> {
    return this.trusted(folder);
  }

  trustedRepository(folder: string, sessionFolder: string): Promise<Repository | null> {
    return this.repository(folder, sessionFolder);
  }
}

// The Claude back end's capabilities, with the effort levels the seam asks a back end to name
// (`EffortLevel` of the SDK, as `src/agent/claude/session.ts` lists them).
const CAPABILITIES_FOR_TESTS: Capabilities = {
  ...CAPABILITIES,
  effortLevels: ["low", "medium", "high", "xhigh", "max"],
};

export const WRITES = [
  "chat.postMessage",
  "chat.startStream",
  "chat.appendStream",
  "chat.stopStream",
  "chat.update",
] as const;

export interface HarnessOptions {
  /** The reply's debounce, in seconds on `slackClock` (Python's tests lowered `DEBOUNCE_SECONDS`). */
  readonly debounceSeconds?: number;
  readonly finalRetrySeconds?: number;
  readonly tasksKept?: number;
  readonly taskRepliesKept?: number;
  /** The client replies are written with, when it is not the one everything else goes through. */
  readonly replies?: FakeSlack;
}

export class Harness {
  readonly slack: FakeSlack;
  /** A scratch folder, bound to `CHANNEL`: Python's `tmp_path`, with its symlinks resolved. */
  readonly tmpPath: string;
  readonly state: StateStore;
  readonly approvals = new Approvals();
  readonly holds = new Holds();
  readonly backend: FakeAgentBackend;
  /** The sessions' own clock. */
  readonly clock = new FakeClock();
  /** The Slack provider's clock. */
  readonly slackClock = new FakeClock();
  readonly chat: SlackChat;
  readonly deps: SessionDeps;
  manager: SessionManager;
  usageFetches = 0;

  constructor(
    slack: FakeSlack,
    tmpPath: string,
    scripts: readonly Script[],
    options: HarnessOptions,
  ) {
    this.slack = slack;
    this.tmpPath = tmpPath;
    this.state = new StateStore(join(tmpPath, "state.json"));
    this.state.bind(CHANNEL, tmpPath);
    this.backend = new FakeAgentBackend(scripts);
    this.chat = new SlackChat({
      slack,
      replies: options.replies ?? slack,
      identity: { teamId: TEAM, ownerUserId: OWNER, botUserId: BOT },
      // A generous burst: these tests are about session orchestration, not the shared
      // limiter's own pacing (that lives in the sink's tests).
      limiter: new UpdateLimiter({ burst: 1_000, clock: this.slackClock }),
      clock: this.slackClock,
      ...(options.debounceSeconds !== undefined && { debounceSeconds: options.debounceSeconds }),
      ...(options.finalRetrySeconds !== undefined && {
        finalRetrySeconds: options.finalRetrySeconds,
      }),
    });
    this.deps = {
      chat: this.chat,
      agent: this.backend,
      state: this.state,
      approvals: this.approvals,
      usage: new UsageCache(async () => {
        this.usageFetches += 1;
        return { session: { percent: 5, resetsAt: null }, week: null };
      }),
      holds: this.holds,
      clock: this.clock,
      ...(options.tasksKept !== undefined && { tasksKept: options.tasksKept }),
      ...(options.taskRepliesKept !== undefined && { taskRepliesKept: options.taskRepliesKept }),
    };
    this.manager = new SessionManager(this.deps);
  }

  /** A restart of the daemon: every session closed, then a new manager on the same state. */
  async restart(): Promise<void> {
    await this.manager.closeAll();
    this.manager = new SessionManager(this.deps);
  }

  /** Every agent session started, in order: Python's `h.clients`. */
  get clients(): FakeAgentSession[] {
    return this.backend.sessions;
  }

  /** The live session of `thread`, opened as a top-level owner message would. */
  session(thread: string = THREAD): ThreadSession {
    const session = this.manager.open(CHANNEL, thread);
    if (session === null) throw new Error("the channel is not bound");
    return session;
  }

  /** The text every posted message ends up showing, in the order they were posted. */
  replies(): string[] {
    return this.slack.messageTexts();
  }

  /**
   * Each reply's own text: `replies()` with the empty entries a silent closing message leaves
   * (it carries no markdown or tool line) filtered out.
   */
  bodies(): string[] {
    return this.replies().filter((reply) => reply !== "");
  }

  /**
   * Every reaction shown on `thread`'s root message, in order: `reactions.add` alone, since
   * `StatusReaction` always adds the new one before removing the previous.
   */
  reactions(thread: string = THREAD): string[] {
    return this.slack
      .callsTo("reactions.add")
      .filter((args) => args.timestamp === thread)
      .map((args) => String(args.name));
  }

  /** Every markdown a write carried: blocks of a post or an update, chunks of a stream. */
  writtenText(): string {
    const texts = (methods: readonly string[], field: string, type: string): string =>
      methods
        .flatMap((method) => this.slack.callsTo(method))
        .flatMap((args) => (Array.isArray(args[field]) ? args[field] : []))
        .filter(
          (part): part is JsonObject =>
            typeof part === "object" && part !== null && !Array.isArray(part) && part.type === type,
        )
        .map((part) => String(part.text))
        .join("\n");
    const blocks = texts(["chat.postMessage", "chat.update"], "blocks", "markdown");
    const chunks = texts(
      ["chat.startStream", "chat.appendStream", "chat.stopStream"],
      "chunks",
      "markdown_text",
    );
    return `${blocks}\n${chunks}`;
  }

  /** The task cards of the first reply's message, as it shows them now. */
  cards(): JsonObject[] {
    return this.slack.createdTs.length > 0 ? (this.slack.messageCards()[0] ?? []) : [];
  }

  /** The methods of the writes made so far, in order. */
  writes(): string[] {
    const writes: readonly string[] = WRITES;
    return this.slack.apiCalls.map((call) => call.method).filter((m) => writes.includes(m));
  }

  /**
   * Let what is ready run to its next wait: every promise callback, and every task that starts
   * on a later turn of the event loop. Where a Python test slept a moment for the daemon to act.
   */
  async idle(): Promise<void> {
    for (let round = 0; round < 20; round += 1) {
      await new Promise<void>((resolve) => setImmediate(resolve));
    }
  }

  /**
   * What a Python test slept through: what is ready runs, and Slack's clock moves on by
   * `seconds`, a hundredth at a time. The sessions' clock stays put.
   */
  async sleep(seconds: number): Promise<void> {
    await this.idle();
    for (let waited = 0; waited < seconds - 1e-9; waited += 0.01) {
      await this.slackClock.advance(0.01);
    }
  }

  /**
   * Wait for a condition on the fakes, as Python's `until` polled it: what is ready runs, and
   * Slack's clock moves on a hundredth of a second at a time, for two seconds at most, so a
   * reply's debounce passes as it did while Python polled. The sessions' clock stays put.
   * Each step also yields a moment of real time: some of what a test waits for ends in real
   * file reads and a `git` subprocess, and a loaded machine must not use up the steps first.
   */
  async until(condition: () => boolean, limit = 2.0): Promise<void> {
    await this.idle();
    for (let waited = 0; !condition(); waited += 0.01) {
      if (waited >= limit) throw new Error("the condition never held");
      await this.slackClock.advance(0.01);
      if (!condition()) await new Promise<void>((resolve) => setTimeout(resolve, 2));
    }
  }
}

/**
 * A harness maker for one test, as the `harness_for` fixture was: each harness it made has its
 * manager closed and its folder removed when the test ends.
 */
export function harnessFor(
  t: TestContext,
  options: HarnessOptions = {},
): ((...scripts: Script[]) => Harness) & { readonly tmpPath: string } {
  const made: Harness[] = [];
  const slack = new FakeSlack();
  const tmpPath = realpathSync(mkdtempSync(join(tmpdir(), "awd-sessions-")));
  // The two "stop for the rest of the run" flags are the process's: each test starts clear.
  resetStatusFlags();
  // The daemon's log stays out of the test report, as pytest kept it: a test that reads a line
  // replaces the logger's method (`mock.method`), which this does not touch.
  const writer = setWriter(() => {});
  // A timer that keeps the loop alive for the test's duration: on Node 22 a test that awaits
  // something which never comes, with only unref'd timers pending, lets the process finish and
  // cancels the rest of the file. With this it waits for the runner's own timeout instead.
  const guard = setInterval(() => {}, 1_000);
  t.after(async () => {
    clearInterval(guard);
    for (const harness of made) await harness.manager.closeAll();
    rmSync(tmpPath, { recursive: true, force: true });
    setWriter(writer);
  });
  // `tmpPath` is there before any harness, for a test that arranges the folder first (Python's
  // `tmp_path` fixture, which `repo` and the harness shared).
  return Object.assign(
    (...scripts: Script[]) => {
      const harness = new Harness(slack, tmpPath, scripts, options);
      made.push(harness);
      return harness;
    },
    { tmpPath },
  );
}
