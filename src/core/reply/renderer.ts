/**
 * Turn an agent's session events into what the owner sees: text as it is written, and one line
 * per tool call or background task. Generic over the events: no branch names a tool, so a tool
 * the agent adds tomorrow renders with no change here. The few tools the terminal shows in words
 * of its own live in `previews`, which falls back to this generic view for any other.
 *
 * The events are the agent seam's (`SessionEvent`). What the SDK's stream repeats or leaves
 * unsaid is settled before they get here, by the back end: a text arrives once, as pieces or
 * whole, and a call's result arrives as its text and as what it did to a file.
 */
import type { Question, SessionEvent } from "../../agent/seam.ts";
import type { FooterFields, Preview, ReplySink, TaskStatus, TaskUpdate } from "../../chat/seam.ts";
import { formatTokens } from "../footer.ts";
import * as texts from "../texts.ts";
import { answered, PREVIEWED, preview } from "./previews.ts";
import { BACKGROUND, blank, oneLine, STOPPED, splitLines } from "./words.ts";

export const TITLE_LIMIT = 80;
export const OUTPUT_LIMIT = 200;
export const CHILD_LINES = 10;

/**
 * The reply the renderer writes: the chat seam's `ReplySink`, generic in the footer. The
 * renderer keeps the footer a turn's end decided and hands it back at the reply's end, without
 * ever reading it.
 */
export type Sink<Footer = FooterFields> = Omit<ReplySink, "closeOut"> & {
  closeOut(footer: Footer | null, signal?: AbortSignal): Promise<boolean>;
};

type EventOf<T extends SessionEvent["type"]> = Extract<SessionEvent, { type: T }>;
export type TaskStarted = EventOf<"task_started">;
export type TurnEnded = EventOf<"turn_ended">;
/** An event of a task's life: Python's `TaskFrame`. */
export type TaskEvent = EventOf<"task_started" | "task_progress" | "task_updated" | "task_ended">;

function line(
  id: string,
  title: string,
  status: TaskStatus,
  fields: Partial<TaskUpdate> = {},
): TaskUpdate {
  return {
    id,
    title,
    status,
    details: null,
    output: null,
    name: "",
    task: false,
    calls: 0,
    preview: null,
    folded: null,
    ...fields,
  };
}

/** `Name: first non-empty string argument`, whatever the tool. */
export function taskTitle(name: string, input: Readonly<Record<string, unknown>>): string {
  for (const value of Object.values(input)) {
    if (typeof value === "string" && !blank(value)) {
      return oneLine(`${name}: ${value}`, TITLE_LIMIT);
    }
  }
  return name;
}

/** A result's text as a line of its call: its first line that says something, or null. */
export function resultSummary(output: string | null): string | null {
  if (output === null) return null;
  const first = splitLines(output).find((text) => !blank(text)) ?? "";
  return oneLine(first, OUTPUT_LIMIT) || null;
}

export function formatDuration(seconds: number): string {
  const whole = Math.trunc(seconds);
  if (whole < 60) return `${whole}s`;
  if (whole < 3600) return `${Math.floor(whole / 60)}m ${whole % 60}s`;
  return `${Math.floor(whole / 3600)}h ${Math.floor((whole % 3600) / 60)}m`;
}

/**
 * How a task's end opens the agent's report of it: the notification's own summary, as the
 * terminal prints it (`Agent "..." finished · 10s`), with the duration when there is one.
 */
export function endedLine(summary: string, status: string, durationMs: number | null): string {
  const said = `${status === "failed" ? "✗" : "✓"} ${summary}`;
  return durationMs === null ? said : `${said} · ${formatDuration(durationMs / 1000)}`;
}

export function terminalStatus(status: string): [status: TaskStatus, output: string | null] {
  if (status === "failed") return ["error", null];
  if (status === "stopped" || status === "killed") return ["complete", STOPPED];
  return ["complete", null];
}

export class TurnRenderer<Footer = FooterFields> {
  readonly #sink: Sink<Footer>;
  /** The session's folder, which previews name paths from. */
  readonly #cwd: string | null;
  readonly #lines = new Map<string, TaskUpdate>();
  readonly #rootOf = new Map<string, string>();
  readonly #children = new Map<string, string[]>();
  readonly #lineOfTask = new Map<string, string>();
  // Tasks started and not yet ended (task id -> line id). Only the lifecycle events say this
  // reliably: a subagent can move to the background without a second `task_started`.
  readonly #running = new Map<string, string>();
  /** Lines of tasks no call started (a command's). */
  readonly #commands = new Set<string>();
  /** Tasks shown on a command's line, not on their own. */
  readonly #nested = new Set<string>();
  // Tasks a call inside another call started (a long command a subagent runs), held aside
  // while that call is open: the root's work, shown on its card through `#child`. A task
  // that ends before its call's result stays here, dropped; one still running at the result
  // outlives its call and leaves (`#outlived`). Kept after the end, so a later event of a
  // dropped one (a `task_updated` after its notification) still finds it.
  readonly #inside = new Set<string>();
  /** The held-aside tasks not yet ended. */
  readonly #aside = new Map<string, TaskStarted>();
  /** Nested calls whose result arrived. */
  readonly #closedCalls = new Set<string>();
  /** `takePromoted`'s, not yet taken. */
  #promoted: TaskStarted[] = [];
  /** A question's answers, until its call ends. */
  readonly #answers = new Map<string, Preview>();
  #wroteText = false;
  /** The agent's text is the last thing in the reply, no card since. */
  #afterText = false;
  /** The text that just started follows text directly. */
  #breakDue = false;
  /** The end of the last turn fed here, or null: how it ended and its last text. */
  result: TurnEnded | null = null;
  authFailed = false;
  /** The category of the last message the agent wrote itself about a failure, if any. */
  error: string | null = null;
  // The reply's footer, as decided by the turn(s) that have closed it so far (a report turn
  // can close it again): `closeOut` ends the reply with it once nothing is owed.
  #footer: Footer | null = null;
  #closedOut = false;

  constructor(sink: Sink<Footer>, cwd: string | null = null) {
    this.#sink = sink;
    this.#cwd = cwd;
  }

  /** One event of the session into the reply. An event that shows nothing is left out. */
  async feed(event: SessionEvent): Promise<void> {
    switch (event.type) {
      case "text_started":
        // Two texts with no card between them (a goal's inner turns, a Stop hook that
        // continues the turn) would run together: the break waits for the first piece, so a
        // text that stays empty adds none. A subagent's text never reaches the reply.
        if (event.parentCallId === null) this.#breakDue = this.#afterText;
        break;
      case "text_delta":
        if (event.parentCallId === null) {
          const broken = this.#breakDue;
          this.#breakDue = false;
          await this.#text(broken ? `\n\n${event.text}` : event.text);
        }
        break;
      case "text":
        // Text that came whole is a command's own output (`Goal set: …` before a goal's first
        // inner turn): like a text that follows another, it needs a break only when text is
        // the last thing written.
        if (event.parentCallId === null) {
          await this.#text((this.#afterText ? "\n\n" : "") + event.text);
        }
        break;
      case "agent_error":
        await this.#failed(event);
        break;
      case "call_started":
        await this.#callStarted(event);
        break;
      case "call_ended":
        await this.#callEnded(event);
        break;
      case "task_started":
        await this.#taskStarted(event);
        break;
      case "task_progress": {
        const entry = this.#lines.get(this.#lineOfTask.get(event.taskId) ?? "");
        if (entry !== undefined) {
          await this.#set({ ...entry, details: oneLine(event.description, OUTPUT_LIMIT) });
        }
        break;
      }
      case "task_ended":
        await this.#taskEnded(event.taskId, event.status, event.summary);
        break;
      case "task_updated":
        if (event.terminal && event.status !== null) {
          await this.#taskEnded(event.taskId, event.status, null);
        }
        break;
      case "compacted":
        await this.#compacted(event.tokensBefore, event.tokensAfter);
        break;
      case "turn_ended":
        this.result = event;
        if (!this.#wroteText && event.finalText) {
          await this.#text(event.finalText);
        } else if (!this.#wroteText && this.error !== null) {
          await this.#text(texts.fill(texts.ERROR_REPLY, { error: this.error }), { notice: true });
        }
        break;
      default:
        break;
    }
  }

  /** Ids of the tasks that outlive the turn; a later task event fed here updates them. */
  get runningTasks(): string[] {
    return [...this.#running.keys()];
  }

  /** The line title of a task this reply shows, running or ended. */
  taskTitle(taskId: string): string | null {
    return this.#lines.get(this.#lineOfTask.get(taskId) ?? "")?.title ?? null;
  }

  /**
   * Whether this task event is of a task held aside or dropped as the work of a call inside
   * another call, a call whose root this reply holds: the agent reports such a task to the
   * subagent, not to the conversation. False for a call this reply never saw, for a task that
   * started after its call's result, and for one that outlived its call.
   */
  nests(event: TaskEvent): boolean {
    if (event.type === "task_started") {
      const call = event.callId;
      return call !== null && this.#rootOf.has(call) && !this.#closedCalls.has(call);
    }
    return this.#inside.has(event.taskId);
  }

  /**
   * The tasks that outlived the nested call that started them since the last call: each is now
   * an ordinary task of this reply (a line, a running task), which the session records as it
   * does any task it saw start.
   */
  takePromoted(): TaskStarted[] {
    const promoted = this.#promoted;
    this.#promoted = [];
    return promoted;
  }

  /**
   * Keep a question's answers for the line of its call, which shows them once the call ends.
   * False when this reply has no line of its own for that call (one asked inside a subagent
   * shows on the subagent's line) or there is no answer to show: the caller keeps the answers
   * elsewhere.
   */
  answered(
    callId: string,
    questions: readonly Pick<Question, "text">[],
    answers: Readonly<Record<string, string | readonly string[]>>,
  ): boolean {
    const shown = answered(questions, answers);
    if (shown === null || !this.#lines.has(callId)) return false;
    this.#answers.set(callId, shown);
    return true;
  }

  /** Whether this reply holds the line of that call, or of the subagent it runs in. */
  owns(callId: string): boolean {
    return this.#lines.has(callId) || this.#rootOf.has(callId);
  }

  /**
   * End the turn's own part of the reply: every open line is closed, except a task still
   * running, whose line stays open until its own end arrives through `feed` or `stopRunning`.
   * Does not end the reply; the caller follows with `closeOut` once it knows nothing more is
   * coming, which can be after more than one call here (a background task's own report turn
   * closes the same reply again).
   */
  async close(footer: Footer | null, signal?: AbortSignal): Promise<void> {
    const interrupted = this.result !== null && this.result.ending === "interrupted";
    if (!this.#wroteText && this.#lines.size === 0) {
      // A command that prints nothing (a local one, say) still gets a visible answer.
      await this.#text(interrupted ? texts.STOPPED : texts.NO_OUTPUT, { notice: true });
    }
    const running = new Set(this.#running.values());
    const closing: TaskUpdate[] = [];
    for (const entry of this.#lines.values()) {
      if (running.has(entry.id)) {
        closing.push({ ...entry, status: "in_progress", details: BACKGROUND, task: true });
      } else if (entry.status === "pending" || entry.status === "in_progress") {
        closing.push({
          ...entry,
          status: "complete",
          output: interrupted ? STOPPED : entry.output,
        });
      }
    }
    for (const entry of closing) this.#lines.set(entry.id, entry);
    // Only ever moves forward: a report turn passes none, and must not erase what an earlier
    // close already decided for a still-deferred end.
    this.#footer = footer ?? this.#footer;
    await this.#sink.finish(closing, signal);
  }

  /** Whether this reply has already ended. */
  get closedOut(): boolean {
    return this.#closedOut;
  }

  /**
   * End the reply, with the footer `close` last decided; call after `close`. True when it ended
   * in the chat. A second call is a no-op of the sink's.
   */
  async closeOut(signal?: AbortSignal): Promise<boolean> {
    this.#closedOut = true;
    return this.#sink.closeOut(this.#footer, signal);
  }

  get sink(): Sink<Footer> {
    return this.#sink;
  }

  /** The agent's process is gone and its tasks with it: close their lines as stopped. */
  async stopRunning(): Promise<void> {
    for (const taskId of [...this.#running.keys()]) {
      await this.#taskEnded(taskId, "stopped", null);
    }
  }

  /**
   * A note before what follows it: usually the daemon's own line, before the agent's reply even
   * starts. It is not the agent's text, so a local command's result still shows after it. A
   * report turn feeds one into an already-written reply instead (the one that started the task
   * it reports): a blank line still separates it from what is there, as a paragraph break would.
   */
  async feedNotice(text: string): Promise<void> {
    const prefix = this.#wroteText || this.#lines.size > 0 ? "\n\n" : "";
    this.#afterText = false;
    await this.#sink.text(`${prefix}${text}\n\n`, { notice: true, ending: false });
  }

  /** A note of the daemon's after what the reply holds: what was not sent, what a restart dropped. */
  async feedError(text: string): Promise<void> {
    await this.#text(`\n\n${text}`, { notice: true });
  }

  /**
   * The turn was cut short outside the agent's stream (the process died, the session closed):
   * say so in the reply, as the line on how it ended.
   */
  async feedEnding(text: string): Promise<void> {
    await this.#text(`\n\n${text}`, { notice: true, ending: true });
  }

  /** The agent compacted the conversation, on request or on its own: say by how much. */
  async #compacted(before: number | null, after: number | null): Promise<void> {
    const said =
      before !== null && after !== null
        ? texts.fill(texts.COMPACTED, { before: formatTokens(before), after: formatTokens(after) })
        : texts.COMPACTED_PLAIN;
    await this.#text(`${this.#wroteText ? "\n\n" : ""}${said}\n\n`, { notice: true });
  }

  async #failed(event: EventOf<"agent_error">): Promise<void> {
    if (event.parentCallId !== null) {
      // A subagent's own failed request (recorded: `subagent-api-error.jsonl`): like every
      // other text of a subagent it stays out of the reply, whose turn may well go on and
      // succeed. Its words go under the card of the call that started the subagent, the
      // outermost one when subagents nest, so the failure shows whether or not a task event
      // of that subagent ever reaches this reply.
      await this.#childNote(event.parentCallId, event.text);
      return;
    }
    if (event.kind === "authentication") {
      this.authFailed = true;
      await this.#text(texts.AUTH_FAILED, { notice: true });
      return;
    }
    // The agent wrote this message itself: its words name the failure and what to do next, as
    // the terminal shows them. With no words here the turn's end speaks: it repeats them, and
    // `feed` falls back to the category.
    this.error = event.category;
    if (event.text !== "") {
      await this.#text((this.#wroteText ? "\n\n" : "") + event.text, { notice: true });
    }
  }

  async #callStarted(event: EventOf<"call_started">): Promise<void> {
    const title = taskTitle(event.toolName, event.input);
    const parent = event.parentCallId;
    const root = parent === null ? null : (this.#rootOf.get(parent) ?? parent);
    if (root !== null && this.#lines.has(root)) {
      this.#rootOf.set(event.callId, root);
      await this.#child(root, title);
    } else if (PREVIEWED.has(event.toolName)) {
      // Kept from the sink until it ends: what shows it then is its preview with no card, or
      // a card that says why it failed. An approval has a message of its own.
      this.#lines.set(
        event.callId,
        line(event.callId, title, "in_progress", { name: event.toolName }),
      );
      this.#afterText = false;
    } else {
      await this.#set(line(event.callId, title, "in_progress", { name: event.toolName }));
    }
  }

  async #callEnded(event: EventOf<"call_ended">): Promise<void> {
    await this.#outlived(event.callId);
    const entry = this.#lines.get(event.callId);
    if (entry === undefined) return;
    if ([...this.#running.values()].includes(entry.id)) {
      // The call only launched a task, which is still running: it outlives the call, so its
      // line becomes a task's line and stays open. A task that ends before its call's result
      // (a long command in the foreground) never gets here.
      await this.#set({
        ...entry,
        details: BACKGROUND,
        task: true,
        output: resultSummary(event.output),
      });
    } else if (entry.output === STOPPED) {
      // Its task already ended as stopped; the result that follows only reports the rejection
      // (recorded: `interrupt.jsonl`, CLI 2.1.283), and must not turn a stopped call into a
      // failed one.
    } else {
      const kept = this.#answers.get(event.callId) ?? null;
      this.#answers.delete(event.callId);
      const shown = event.isError
        ? null
        : (kept ?? preview(entry.name, event.fileChange, this.#cwd));
      await this.#set({
        ...entry,
        status: event.isError ? "error" : "complete",
        output: resultSummary(event.output),
        preview: shown,
      });
    }
  }

  async #taskStarted(event: TaskStarted): Promise<void> {
    if (this.nests(event)) {
      // The agent starts a task for a long command a subagent runs in the foreground of its
      // own context (recorded: `subagent-nested-command.jsonl`, CLI 2.1.286). The call already
      // shows on its root's card (`#child`): one work, one line, unless the task outlives the
      // call (`#outlived`).
      this.#inside.add(event.taskId);
      this.#aside.set(event.taskId, event);
      return;
    }
    // An id held aside before, starting again after its call closed, is an ordinary task now:
    // its later events must reach it, or its end would never close its line.
    this.#inside.delete(event.taskId);
    const call = event.callId;
    if (call !== null && this.#lines.has(call)) {
      // A call's task: the agent starts one for a long command in the foreground too
      // (recorded: `interrupt.jsonl`, CLI 2.1.283). The line stays the call's until the call's
      // result arrives with the task still running (`#callEnded`).
      this.#lineOfTask.set(event.taskId, call);
      this.#running.set(event.taskId, call);
      return;
    }
    const command = [...this.#running.values()].reverse().find((id) => this.#commands.has(id));
    if (call !== null && command !== undefined && !this.#rootOf.has(call)) {
      // Started by a call this reply never saw while a command's task runs: an agent inside
      // that command (a forked skill typed as a command streams none of its own calls,
      // recorded: `skill-fork-command.jsonl`, CLI 2.1.283). It shows on the command's line, as
      // a subagent's calls show on the subagent's: one work, one line.
      this.#nested.add(event.taskId);
      await this.#child(command, oneLine(event.description, TITLE_LIMIT));
      return;
    }
    await this.#openLine(event);
  }

  async #openLine(event: TaskStarted): Promise<void> {
    const lineId = `task-${event.taskId}`;
    this.#lineOfTask.set(event.taskId, lineId);
    this.#running.set(event.taskId, lineId);
    if (event.callId === null) this.#commands.add(lineId);
    const title = oneLine(event.description, TITLE_LIMIT);
    const name = event.taskType ?? "task";
    await this.#set(line(lineId, title, "in_progress", { name, task: true }));
  }

  /**
   * A nested call's result: a task it started that is still running outlives it (recorded:
   * `subagent-nested-background.jsonl`, CLI 2.1.286, where the result comes first), and from
   * here is an ordinary background task, as for a call at the top level (`#callEnded`).
   */
  async #outlived(call: string): Promise<void> {
    if (!this.#rootOf.has(call)) return;
    this.#closedCalls.add(call);
    for (const [taskId, started] of [...this.#aside]) {
      if (started.callId === call) {
        this.#aside.delete(taskId);
        this.#inside.delete(taskId);
        // Queued before the line is written: a write that fails must not leave a running
        // task the session never adopts.
        this.#promoted.push(started);
        await this.#openLine(started);
      }
    }
  }

  async #taskEnded(taskId: string, status: string, summary: string | null): Promise<void> {
    if (this.#inside.has(taskId)) {
      this.#aside.delete(taskId); // dropped: it ended before its call's result
      return;
    }
    if (this.#nested.has(taskId)) {
      this.#nested.delete(taskId); // the command's own end closes its line
      return;
    }
    this.#running.delete(taskId);
    const lineId = this.#lineOfTask.get(taskId) ?? `task-${taskId}`;
    const entry =
      this.#lines.get(lineId) ??
      line(lineId, oneLine(summary || taskId, TITLE_LIMIT), "in_progress", {
        name: "task",
        task: true,
      });
    const [final, stopped] = terminalStatus(status);
    await this.#set({
      ...entry,
      status: final,
      details: null,
      output: stopped ?? (summary ? oneLine(summary, OUTPUT_LIMIT) : null),
    });
  }

  async #child(root: string, said: string): Promise<void> {
    const entry = this.#lines.get(root);
    if (entry === undefined) return;
    // A call that runs calls of its own (a subagent) keeps its line once it ends, whether it
    // ran in the foreground or not: the nested calls are its work, not one more call.
    await this.#set({
      ...entry,
      details: this.#under(root, said),
      task: true,
      calls: entry.calls + 1,
    });
  }

  /**
   * What a subagent's error message says, as a nested line of its card: not one more call, so
   * the count stays.
   */
  async #childNote(parent: string, words: string): Promise<void> {
    const root = this.#rootOf.get(parent) ?? parent;
    const entry = this.#lines.get(root);
    if (entry === undefined || words === "") return;
    await this.#set({ ...entry, details: this.#under(root, oneLine(words, 200)), task: true });
  }

  /** The nested lines of a card with `said` added: its last `CHILD_LINES`. */
  #under(root: string, said: string): string {
    const lines = [...(this.#children.get(root) ?? []), said].slice(-CHILD_LINES);
    this.#children.set(root, lines);
    return lines.join("\n");
  }

  async #set(update: TaskUpdate): Promise<void> {
    if (!this.#lines.has(update.id)) {
      // Only a new line puts a card after the text. A line that changes does so where its card
      // sits, and the text stays the last thing in the reply.
      this.#afterText = false;
    }
    this.#lines.set(update.id, update);
    await this.#sink.task(update);
  }

  async #text(
    markdown: string,
    options: { notice?: boolean; ending?: boolean } = {},
  ): Promise<void> {
    if (markdown === "") return;
    const { notice = false, ending = false } = options;
    this.#wroteText = true;
    this.#afterText = !notice; // a notice ends with its own break
    await this.#sink.text(markdown, { notice, ending });
  }
}
