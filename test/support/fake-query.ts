/**
 * A stand-in for the Agent SDK's `query()`, at the boundary the testing rules allow: it takes the
 * options and the streaming input the SDK takes, plays scripted batches of the CLI's recorded
 * wire records, and records every control call made on it. Nothing below it is mocked.
 *
 * What it offers (the TypeScript counterpart of `FakeClaudeClient` of the Python tests):
 * - `turns`: one batch answers each prompt it is sent, in order, with the replay of the prompt
 *   after the turn's `init` as Claude Code sends it under `--replay-user-messages` (recorded
 *   2026-10-06, CLI 2.1.286, and 2026-10-09, 2.1.292);
 * - `answer` and `inject`: a batch delivered when the test chooses, as a turn nobody asked for;
 * - inside a batch, `ask` (a call of the permission callback), `hook` (a run of a registered
 *   hook), `END` (the process exits) and `fail` (the stream throws);
 * - `holdClose`: a close that ends the stream only when the test lets it, as a CLI takes real
 *   time to flush and exit after EOF.
 */
import type {
  CanUseTool,
  EffortLevel,
  Options,
  PermissionMode,
  PermissionResult,
  SDKUserMessage,
} from "@anthropic-ai/claude-agent-sdk";
import {
  AsyncQueue,
  type QueryFunction,
  type QueryHandle,
} from "../../src/agent/claude/session.ts";
import { type JsonObject, sdkJson } from "./fixtures.ts";

/** A call of the permission callback, at this point of a batch. */
export interface AskCall {
  readonly ask: {
    readonly toolName: string;
    readonly input: Record<string, unknown>;
    readonly toolUseID?: string;
    readonly requestId?: string;
    readonly title?: string;
    readonly description?: string;
  };
}

/** A run of the registered hooks of `event`, at this point of a batch. */
export interface HookRun {
  readonly hook: "Stop" | "PostToolUse";
  readonly input: Record<string, unknown>;
}

export interface EndOfStream {
  readonly end: true;
}

export interface StreamFailure {
  readonly fail: Error;
}

/** A wire record, or something the process does at that point. */
export type Scripted = JsonObject | AskCall | HookRun | EndOfStream | StreamFailure;
export type Batch = readonly Scripted[];

export const END: EndOfStream = { end: true };

export function ask(call: AskCall["ask"]): AskCall {
  return { ask: call };
}

export function hook(event: HookRun["hook"], input: Record<string, unknown>): HookRun {
  return { hook: event, input };
}

export function fail(error: Error): StreamFailure {
  return { fail: error };
}

/** A control call made on the query, with what it was given. */
export type ControlCall =
  | { readonly method: "interrupt" }
  | { readonly method: "stopTask"; readonly taskId: string }
  | { readonly method: "setPermissionMode"; readonly mode: PermissionMode }
  | { readonly method: "setModel"; readonly model: string | undefined }
  | { readonly method: "applyFlagSettings"; readonly effortLevel: EffortLevel | null }
  | { readonly method: "getContextUsage" }
  | { readonly method: "close" };

export interface FakeScript {
  /** One batch per prompt sent, in order. */
  readonly turns?: readonly Batch[];
  /** Delivered when the process starts, before any prompt. */
  readonly start?: Batch;
  /** The answer to the initialize request. */
  readonly init?: unknown;
  /** The initialize request fails with this, as when Claude Code refuses to resume. */
  readonly initError?: Error;
  readonly contextUsage?: unknown;
  /** A control call that rejects with this error. */
  readonly rejects?: Partial<Record<ControlCall["method"], Error>>;
  /** `close()` ends the stream only when `finishClose()` is called. */
  readonly holdClose?: boolean;
  /** `close()` throws this, after it has done its work, as a transport that fails to kill. */
  readonly closeThrows?: Error;
}

function isAsk(item: Scripted): item is AskCall {
  return "ask" in item && !("type" in item);
}

function isHook(item: Scripted): item is HookRun {
  return "hook" in item && !("type" in item);
}

function isEnd(item: Scripted): item is EndOfStream {
  return "end" in item && !("type" in item);
}

function isFailure(item: Scripted): item is StreamFailure {
  return "fail" in item && !("type" in item);
}

const WORDED = new Set(["stream_event", "assistant", "user", "result"]);

export class FakeQuery implements QueryHandle {
  readonly options: Options;
  /** Every user message the session pushed, as the SDK would have written it to stdin. */
  readonly sent: SDKUserMessage[] = [];
  readonly calls: ControlCall[] = [];
  /** What the permission callback answered at each `ask`. */
  readonly permissionResults: PermissionResult[] = [];
  /** The streaming input reached its end: the session closed stdin. */
  inputEnded = false;
  closed = false;
  readonly #script: FakeScript;
  readonly #turns: Batch[];
  readonly #feed = new AsyncQueue<Batch>();
  readonly #stop = new AbortController();

  constructor(
    params: { prompt: AsyncIterable<SDKUserMessage>; options: Options },
    script: FakeScript,
  ) {
    this.options = params.options;
    this.#script = script;
    this.#turns = [...(script.turns ?? [])];
    if (script.start !== undefined) this.#feed.push(script.start);
    void this.#consume(params.prompt);
  }

  /** The user messages the session sent and the control calls it made, read as the SDK reads. */
  async #consume(prompt: AsyncIterable<SDKUserMessage>): Promise<void> {
    for await (const message of prompt) {
      this.sent.push(message);
      const batch = this.#turns.shift();
      if (batch !== undefined) this.#feed.push(this.#replayed(batch, message));
    }
    this.inputEnded = true;
  }

  /** `batch` with the replay of the prompt it answers, as Claude Code sends it. */
  #replayed(batch: Batch, message: SDKUserMessage): Batch {
    const content = message.message.content;
    const wanted =
      this.options.extraArgs !== undefined && "replay-user-messages" in this.options.extraArgs;
    if (!wanted || typeof content !== "string" || content.trimStart().startsWith("/")) return batch;
    if (message.uuid === undefined) return batch;
    const at = batch.findIndex((item) => "type" in item && WORDED.has(String(item.type)));
    const replay: JsonObject = {
      type: "user",
      message: { role: "user", content },
      session_id: "68da9311-0000-4000-8000-000000000001",
      parent_tool_use_id: null,
      uuid: message.uuid,
    };
    const index = at === -1 ? batch.length : at;
    return [...batch.slice(0, index), replay, ...batch.slice(index)];
  }

  /** Deliver the turn of the last prompt sent, with its replay, when the test holds that turn. */
  answer(batch: Batch): void {
    const last = this.sent.at(-1);
    this.#feed.push(last === undefined ? batch : this.#replayed(batch, last));
  }

  /** Deliver a turn nobody asked for, as the CLI does for a background-task notification. */
  inject(batch: Batch): void {
    this.#feed.push(batch);
  }

  /** Let a held `close()` end the stream. */
  finishClose(): void {
    this.#feed.end();
  }

  async *[Symbol.asyncIterator](): AsyncGenerator<unknown, void> {
    for await (const batch of this.#feed) {
      for (const item of batch) {
        if (isEnd(item)) return;
        if (isFailure(item)) throw item.fail;
        if (isAsk(item)) {
          await this.#ask(item);
        } else if (isHook(item)) {
          await this.#hook(item);
        } else {
          yield item;
        }
      }
    }
  }

  async #ask({ ask }: AskCall): Promise<void> {
    const canUseTool: CanUseTool | undefined = this.options.canUseTool;
    if (canUseTool === undefined) throw new Error("the session registered no permission callback");
    const context = {
      signal: this.#stop.signal,
      toolUseID: ask.toolUseID ?? "toolu_fake_1",
      requestId: ask.requestId ?? "request_fake_1",
      ...(ask.title !== undefined && { title: ask.title }),
      ...(ask.description !== undefined && { description: ask.description }),
    };
    const result = await canUseTool(ask.toolName, ask.input, context);
    if (result === null) throw new Error("the session answered the permission request out of band");
    this.permissionResults.push(result);
  }

  async #hook({ hook: event, input }: HookRun): Promise<void> {
    for (const matcher of this.options.hooks?.[event] ?? []) {
      for (const callback of matcher.hooks) {
        // The hook inputs are the recorded ones, which the SDK's union does not name field by field.
        await callback(input as never, undefined, { signal: this.#stop.signal });
      }
    }
  }

  #record(call: ControlCall): void {
    this.calls.push(call);
    const error = this.#script.rejects?.[call.method];
    if (error !== undefined) throw error;
  }

  async interrupt(): Promise<unknown> {
    this.#record({ method: "interrupt" });
    return undefined;
  }

  async stopTask(taskId: string): Promise<void> {
    this.#record({ method: "stopTask", taskId });
  }

  async setPermissionMode(mode: PermissionMode): Promise<void> {
    this.#record({ method: "setPermissionMode", mode });
  }

  async setModel(model?: string): Promise<void> {
    this.#record({ method: "setModel", model });
  }

  async applyFlagSettings(settings: { effortLevel: EffortLevel | null }): Promise<void> {
    this.#record({ method: "applyFlagSettings", effortLevel: settings.effortLevel });
  }

  async initializationResult(): Promise<unknown> {
    if (this.#script.initError !== undefined) throw this.#script.initError;
    return this.#script.init ?? sdkJson("server-info");
  }

  async getContextUsage(): Promise<unknown> {
    this.#record({ method: "getContextUsage" });
    return this.#script.contextUsage ?? sdkJson("context-usage");
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.calls.push({ method: "close" });
    this.#stop.abort();
    if (!this.#script.holdClose) this.#feed.end();
    if (this.#script.closeThrows !== undefined) throw this.#script.closeThrows;
  }
}

/** A `query` function that builds a `FakeQuery` per call, from the script of that call. */
export class FakeSdk {
  readonly queries: FakeQuery[] = [];
  readonly #scripts: (index: number) => FakeScript;

  constructor(script: FakeScript | ((index: number) => FakeScript) = {}) {
    this.#scripts = typeof script === "function" ? script : () => script;
  }

  readonly query: QueryFunction = (params) => {
    const query = new FakeQuery(params, this.#scripts(this.queries.length));
    this.queries.push(query);
    return query;
  };

  /** The query of the session under test, when there is exactly one. */
  get only(): FakeQuery {
    const [query] = this.queries;
    if (query === undefined || this.queries.length !== 1) {
      throw new Error(`expected one query, found ${this.queries.length}`);
    }
    return query;
  }
}
