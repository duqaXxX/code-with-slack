/**
 * Claude Code's wire records into the daemon's session events.
 *
 * A record is one object as the CLI wrote it, which the Agent SDK's `query()` yields unchanged.
 * Each is read as `unknown` and narrowed field by field. What is read here is what the Python
 * renderer and session read off the SDK's messages (`render.renderer.TurnRenderer.feed`,
 * `sessions.ThreadSession._dispatch`), parsed as the Python SDK's `parse_message` parsed them
 * (claude-agent-sdk 0.2.165), so the core decides from events what it decided from messages.
 *
 * Three things need memory across records, and `Translator` keeps them for one session: which
 * messages a stream announced, since Claude Code sends a streamed text again whole; which
 * prompts the daemon sent, since a prompt comes back as a user record; and whether a compaction
 * is under way, since the status record that ends one says so only to who knows it started.
 */
import type {
  DiffHunk,
  FileChange,
  ModelTokens,
  SessionEvent,
  TaskKind,
  TurnEnding,
} from "../seam.ts";
import {
  integer,
  isRecord,
  number,
  records,
  string,
  strip,
  type WireRecord,
  words,
} from "./wire.ts";

// Each task_type (measured on Claude Code 2.1.280: `local_bash` for a background command,
// `local_agent` for a subagent). A type not listed is a task of no kind the daemon names.
const TASK_KINDS: ReadonlyMap<string, TaskKind> = new Map([
  ["local_agent", "subagent"],
  ["local_bash", "command"],
]);
// The Python package's `TERMINAL_TASK_STATUSES` (claude-agent-sdk 0.2.165), which the TypeScript
// package does not export: a task can end with a `task_updated` of one of these and no
// notification.
const TERMINAL_TASK_STATUSES: ReadonlySet<string> = new Set([
  "completed",
  "failed",
  "stopped",
  "killed",
]);
const INTERRUPTED: ReadonlySet<string> = new Set(["aborted_streaming", "aborted_tools"]);
const AUTHENTICATION_FAILED = "authentication_failed";
// The signs a line of a `structuredPatch` hunk opens with; the backslash is
// `\ No newline at end of file`, about the line above.
const SIGNS: ReadonlySet<string> = new Set(["-", "+", " ", "\\"]);

/** What one session's translation remembers from one record to the next. */
interface Memory {
  /** The ids of the prompts the daemon sent. */
  readonly sent: Set<string>;
  /** The ids of the messages a `message_start` announced: their text is written from its deltas. */
  readonly streamed: Set<string>;
  /** The message each stream is writing, by the call it runs under (null: the top level). */
  readonly open: Map<string | null, string | null>;
  /** A top-level `message_start` carried no id: the ids tell nothing. */
  unnamed: boolean;
  compacting: boolean;
}

type Handler = (memory: Memory, record: WireRecord) => SessionEvent[];

/**
 * A record's kind: its `type`, with the `subtype` of a `system` record and the event type of a
 * `stream_event`. Null for what is no record.
 */
export function kindOf(record: unknown): string | null {
  if (!isRecord(record)) return null;
  const type = string(record.type);
  if (type === null) return null;
  if (type === "system") return `system:${string(record.subtype) ?? ""}`;
  if (type === "stream_event") {
    return `stream_event:${isRecord(record.event) ? (string(record.event.type) ?? "") : ""}`;
  }
  return type;
}

function parentOf(record: WireRecord): string | null {
  return words(record.parent_tool_use_id);
}

// Stream events: the Claude API's streaming events, passed through unparsed.

function messageStart(memory: Memory, record: WireRecord): SessionEvent[] {
  const event = isRecord(record.event) ? record.event : {};
  const messageId = isRecord(event.message) ? string(event.message.id) : null;
  const parentCallId = parentOf(record);
  memory.open.set(parentCallId, messageId);
  if (messageId !== null) memory.streamed.add(messageId);
  else if (parentCallId === null) memory.unnamed = true;
  return [{ type: "message_started", messageId, parentCallId }];
}

function messageStop(memory: Memory, record: WireRecord): SessionEvent[] {
  const parentCallId = parentOf(record);
  const messageId = memory.open.get(parentCallId) ?? null;
  memory.open.delete(parentCallId);
  return [{ type: "message_ended", messageId, parentCallId }];
}

function contentBlockStart(memory: Memory, record: WireRecord): SessionEvent[] {
  const event = isRecord(record.event) ? record.event : {};
  if (!isRecord(event.content_block) || event.content_block.type !== "text") return [];
  const parentCallId = parentOf(record);
  return [{ type: "text_started", messageId: memory.open.get(parentCallId) ?? null, parentCallId }];
}

function contentBlockDelta(memory: Memory, record: WireRecord): SessionEvent[] {
  const event = isRecord(record.event) ? record.event : {};
  if (!isRecord(event.delta) || event.delta.type !== "text_delta") return [];
  const text = words(event.delta.text);
  if (text === null) return [];
  const parentCallId = parentOf(record);
  return [
    { type: "text_delta", messageId: memory.open.get(parentCallId) ?? null, text, parentCallId },
  ];
}

// Messages.

/** The text of a message's text blocks, joined and trimmed. */
function wordsOf(blocks: readonly WireRecord[]): string {
  return strip(
    blocks
      .filter((block) => block.type === "text")
      .map((block) => string(block.text) ?? "")
      .join(""),
  );
}

/**
 * Whether a message's text reached the core through no stream event. At the top level, never
 * while a streamed message is unfinished (its text may be this one's, sent again whole after a
 * failed stream, which no recording shows) or once a stream named no id.
 */
function unannounced(memory: Memory, messageId: string | null, parentCallId: string | null) {
  if (messageId === null || memory.streamed.has(messageId)) return false;
  return parentCallId !== null || (!memory.open.has(null) && !memory.unnamed);
}

function callStarted(block: WireRecord, parentCallId: string | null): SessionEvent[] {
  const callId = string(block.id);
  const toolName = string(block.name);
  if (callId === null || toolName === null) return [];
  const input = isRecord(block.input) ? block.input : {};
  return [{ type: "call_started", callId, toolName, input, parentCallId }];
}

/**
 * The text of a tool result, whatever its form: a string, the text parts of a list a line each,
 * or the type of a single object (a server tool's result). Null for anything else.
 */
function resultText(content: unknown): string | null {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return records(content)
      .map((part) => string(part.text) ?? "")
      .join("\n");
  }
  if (isRecord(content)) return string(content.type) ?? "";
  return null;
}

function callEnded(
  block: WireRecord,
  parentCallId: string | null,
  isError: boolean,
  change: FileChange | null,
): SessionEvent[] {
  const callId = string(block.tool_use_id);
  if (callId === null) return [];
  return [
    {
      type: "call_ended",
      callId,
      isError,
      output: resultText(block.content),
      fileChange: change,
      parentCallId,
    },
  ];
}

function assistant(memory: Memory, record: WireRecord): SessionEvent[] {
  const message = isRecord(record.message) ? record.message : {};
  if (!Array.isArray(message.content)) return [];
  const blocks = records(message.content);
  const parentCallId = parentOf(record);
  const text = wordsOf(blocks);
  if (record.error !== undefined && record.error !== null) {
    // Claude Code wrote this message itself, about a request of its own that failed: its words
    // name the failure and what to do next, and nothing else in it is a call or an answer. In
    // the recorded 529 they come in this message with no stream event of their own.
    const category = String(record.error);
    const kind = category === AUTHENTICATION_FAILED ? "authentication" : "other";
    return [{ type: "agent_error", kind, category, text, parentCallId }];
  }
  const events: SessionEvent[] = [];
  const messageId = string(message.id);
  if (text !== "" && messageId !== null && unannounced(memory, messageId, parentCallId)) {
    // Text no stream event announced is a command's own output (`Goal set: …` before a goal's
    // first inner turn, recorded: `goal.jsonl`), or a subagent's, which does not stream.
    events.push({ type: "text", messageId, text, parentCallId });
  }
  for (const block of blocks) {
    if (block.type === "tool_use" || block.type === "server_tool_use") {
      events.push(...callStarted(block, parentCallId));
    } else if (block.type === "tool_result") {
      events.push(...callEnded(block, parentCallId, block.is_error === true, null));
    } else if (block.type === "advisor_tool_result") {
      // The one server tool result the Python SDK's parser kept: it carries no error flag.
      events.push(...callEnded(block, parentCallId, false, null));
    }
  }
  return events;
}

function hunkOf(value: unknown): DiffHunk | null {
  if (!isRecord(value)) return null;
  const oldStart = integer(value.oldStart);
  const newStart = integer(value.newStart);
  if (oldStart === null || newStart === null || !Array.isArray(value.lines)) return null;
  const lines: string[] = [];
  for (const line of value.lines) {
    if (typeof line !== "string" || !SIGNS.has(line.charAt(0))) return null;
    lines.push(line);
  }
  return { oldStart, newStart, lines };
}

function hunksOf(patch: unknown): DiffHunk[] | null {
  if (!Array.isArray(patch)) return null;
  const hunks: DiffHunk[] = [];
  for (const entry of patch) {
    const hunk = hunkOf(entry);
    if (hunk === null) return null;
    hunks.push(hunk);
  }
  return hunks;
}

/**
 * What an `Edit` or a `Write` did to a file, from `tool_use_result`: a shape the SDK types as
 * `unknown` and does not document, measured on Claude Code 2.1.283 (2026-09-27,
 * `tests/fixtures/sdk/edit-write.jsonl`). Any other shape gives null, and the core shows the
 * call's generic line.
 */
export function fileChange(result: unknown): FileChange | null {
  if (!isRecord(result)) return null;
  const path = string(result.filePath);
  if (path === null) return null;
  const hunks = hunksOf(result.structuredPatch);
  if (result.type === "create") {
    const content = string(result.content);
    if (content === null) return null;
    return { path, kind: "created", hunks: hunks ?? [], content };
  }
  if (hunks === null || hunks.length === 0) return null;
  return { path, kind: "edited", hunks, content: null };
}

function user(memory: Memory, record: WireRecord): SessionEvent[] {
  const promptId = string(record.uuid);
  if (promptId !== null && memory.sent.has(promptId)) {
    // The replay of a prompt this session sent (`replay-user-messages`): it shows nothing. A
    // uuid that names no prompt of ours (a resumed session's, a command's output) is none.
    return [{ type: "prompt_taken", promptId }];
  }
  const message = isRecord(record.message) ? record.message : {};
  if (!Array.isArray(message.content)) return [];
  const blocks = records(message.content);
  const parentCallId = parentOf(record);
  // One `tool_use_result` per message: it belongs to a result only when it is alone.
  const results = blocks.filter((block) => block.type === "tool_result");
  const change = results.length === 1 ? fileChange(record.tool_use_result) : null;
  const events: SessionEvent[] = [];
  for (const block of blocks) {
    if (block.type === "tool_use") events.push(...callStarted(block, parentCallId));
    else if (block.type === "tool_result") {
      events.push(...callEnded(block, parentCallId, block.is_error === true, change));
    }
  }
  return events;
}

// System records.

function init(_memory: Memory, record: WireRecord): SessionEvent[] {
  return [
    {
      type: "session_started",
      sessionId: string(record.session_id),
      agentVersion: string(record.claude_code_version),
    },
  ];
}

function status(memory: Memory, record: WireRecord): SessionEvent[] {
  const events: SessionEvent[] = [];
  // Claude Code reports the permission mode after a change (measured after each
  // `set_permission_mode`).
  const mode = string(record.permissionMode);
  if (mode !== null) events.push({ type: "mode_changed", mode });
  // A compaction opens with `status` `compacting` and ends 14 to 37 seconds later in the
  // recordings (2026-10-08, CLI 2.1.292) with a `status` record that carries `compact_result`.
  // The record that reports a permission mode carries neither, so one that arrives meanwhile
  // ends nothing.
  const reported = record.status ?? null;
  const compacting =
    reported === "compacting" ||
    (memory.compacting && reported === null && !Object.hasOwn(record, "compact_result"));
  if (compacting !== memory.compacting) {
    memory.compacting = compacting;
    events.push(
      compacting
        ? { type: "compaction_started" }
        : { type: "compaction_ended", result: string(record.compact_result) },
    );
  }
  return events;
}

function compactBoundary(_memory: Memory, record: WireRecord): SessionEvent[] {
  const metadata = isRecord(record.compact_metadata) ? record.compact_metadata : {};
  return [
    {
      type: "compacted",
      tokensBefore: integer(metadata.pre_tokens),
      tokensAfter: integer(metadata.post_tokens),
    },
  ];
}

function taskStarted(_memory: Memory, record: WireRecord): SessionEvent[] {
  const taskId = string(record.task_id);
  const description = string(record.description);
  if (taskId === null || description === null) return [];
  const taskType = words(record.task_type);
  return [
    {
      type: "task_started",
      taskId,
      kind: TASK_KINDS.get(taskType ?? "") ?? "other",
      taskType,
      description,
      callId: words(record.tool_use_id),
    },
  ];
}

function taskProgress(_memory: Memory, record: WireRecord): SessionEvent[] {
  const taskId = string(record.task_id);
  const description = string(record.description);
  if (taskId === null || description === null) return [];
  return [{ type: "task_progress", taskId, description, callId: words(record.tool_use_id) }];
}

function taskUpdated(_memory: Memory, record: WireRecord): SessionEvent[] {
  // Terminal task completion sometimes arrives only as a `task_updated` patch, with no
  // notification (Python SDK, `parse_message`): the status is the patch's, when it has one.
  const patch = isRecord(record.patch) ? record.patch : {};
  const reported = string(patch.status);
  return [
    {
      type: "task_updated",
      taskId: string(record.task_id) ?? "",
      status: reported,
      terminal: reported !== null && TERMINAL_TASK_STATUSES.has(reported),
    },
  ];
}

function taskNotification(_memory: Memory, record: WireRecord): SessionEvent[] {
  const taskId = string(record.task_id);
  const reported = string(record.status);
  const summary = string(record.summary);
  if (taskId === null || reported === null || summary === null) return [];
  const usage = isRecord(record.usage) ? record.usage : {};
  return [
    {
      type: "task_ended",
      taskId,
      status: reported,
      summary,
      durationMs: number(usage.duration_ms),
      callId: words(record.tool_use_id),
    },
  ];
}

// The rest.

function rateLimit(): SessionEvent[] {
  return [{ type: "limits_changed" }];
}

function tokensOf(usage: unknown): Record<string, ModelTokens> {
  const tokens: Record<string, ModelTokens> = {};
  if (!isRecord(usage)) return tokens;
  for (const [model, counts] of Object.entries(usage)) {
    if (!isRecord(counts)) continue;
    // A count a model does not report (no cache, say) counts as none.
    tokens[model] = {
      input: number(counts.inputTokens) ?? 0,
      output: number(counts.outputTokens) ?? 0,
      cacheRead: number(counts.cacheReadInputTokens) ?? 0,
      cacheCreation: number(counts.cacheCreationInputTokens) ?? 0,
    };
  }
  return tokens;
}

function endingOf(record: WireRecord): TurnEnding {
  const reason = string(record.terminal_reason);
  if (reason !== null && INTERRUPTED.has(reason)) return "interrupted";
  return record.is_error === true ? "error" : "done";
}

function result(memory: Memory, record: WireRecord): SessionEvent[] {
  // The stream's memory is a turn's: an interrupted stream has no `message_stop`, and the next
  // turn must not read as still inside it. Python kept this per reply, which a turn's end closed.
  memory.streamed.clear();
  memory.open.clear();
  memory.unnamed = false;
  memory.compacting = false;
  // Claude Code started the turn itself (a task notification, say) when the origin names
  // anything but the owner: none or `human` is the owner's own prompt.
  const origin = isRecord(record.origin) ? string(record.origin.kind) : null;
  return [
    {
      type: "turn_ended",
      sessionId: words(record.session_id),
      startedBy: origin === null || origin === "human" ? "owner" : "agent",
      finalText: string(record.result),
      ending: endingOf(record),
      tokens: tokensOf(record.modelUsage),
    },
  ];
}

const HANDLERS = {
  assistant,
  rate_limit_event: rateLimit,
  result,
  "stream_event:content_block_delta": contentBlockDelta,
  "stream_event:content_block_start": contentBlockStart,
  "stream_event:message_start": messageStart,
  "stream_event:message_stop": messageStop,
  "system:compact_boundary": compactBoundary,
  "system:init": init,
  "system:status": status,
  "system:task_notification": taskNotification,
  "system:task_progress": taskProgress,
  "system:task_started": taskStarted,
  "system:task_updated": taskUpdated,
  user,
} as const satisfies Record<string, Handler>;

/** The kinds of record that can give an event. */
export const TRANSLATED_KINDS: readonly string[] = Object.keys(HANDLERS);

/**
 * The recorded kinds the daemon does not read, as the Python daemon did not: nothing in the
 * reply or in the session depends on them. A kind in neither list is new, and
 * `test/agent/claude/translate.test.ts` fails on the recording that carries it.
 */
export const UNREAD_KINDS: readonly string[] = [
  // The queue of a prompt sent while a turn runs; absent from the SDK's union of messages.
  "command_lifecycle",
  // `/clear`: the new session id comes with the `init` that follows.
  "conversation_reset",
  // The end of a content block and a message's stop reason: neither shows anything.
  "stream_event:content_block_stop",
  "stream_event:message_delta",
  // A request being retried: the message about the failure follows when the retries run out.
  "system:api_retry",
  // The list of background tasks: the task records say the same, one task at a time.
  "system:background_tasks_changed",
  "system:thinking_tokens",
  // A subagent's heartbeat while its call runs.
  "tool_progress",
];

function handlerOf(kind: string | null): Handler | null {
  return kind !== null && Object.hasOwn(HANDLERS, kind)
    ? HANDLERS[kind as keyof typeof HANDLERS]
    : null;
}

/**
 * One session's translator, as a class: it has two entry points over one memory (`translate`
 * for what arrives, `promptSent` for what leaves), and the session wrapper holds exactly one
 * for as long as its process lives.
 */
export class Translator {
  readonly #memory: Memory = {
    sent: new Set(),
    streamed: new Set(),
    open: new Map(),
    unnamed: false,
    compacting: false,
  };

  /** The daemon is sending a prompt under `promptId`: its replay will be `prompt_taken`. */
  promptSent(promptId: string): void {
    this.#memory.sent.add(promptId);
  }

  /**
   * The events one record gives, in the order the reply acts on them. A record of a kind the
   * daemon does not read, or of none it knows, gives none.
   */
  translate(record: unknown): SessionEvent[] {
    const handler = handlerOf(kindOf(record));
    return handler !== null && isRecord(record) ? handler(this.#memory, record) : [];
  }
}
