/**
 * One agent session per chat thread: its agent process, its queue of turns, and the reader that
 * turns the agent's events into replies. Port of `ThreadSession` in `sessions.py`.
 *
 * The session knows neither library. It starts its agent through the agent seam
 * (`AgentBackend.start`) and reads `SessionEvent`s; it writes its thread through the chat seam
 * (`ThreadChat`). What it keeps in `state.json` for crash repair it writes itself through
 * `StateStore`: the root's status when it asks for one, a request once the chat has named its
 * message, and a reply's open message when the reply says it changed.
 *
 * How asyncio is said here. One translation, used everywhere in this folder:
 *
 * 1. A Python task is a `Task` (`tasks.ts`): the worker that sends one prompt at a time, the
 *    reader that follows the agent's events, and the timers (`expireInjectedTurn`,
 *    `expireUnreported`, `closeWhenIdle`). A `Task` starts on the next turn of the event loop,
 *    never inside the call that made it, and the waiters of an `Event` wake on the next turn
 *    too: asyncio ran both only after the code that made them ready had reached a real wait,
 *    and this class sets state right after creating a task and right after releasing a turn
 *    (`Turn.done` is set before the turn stops being the active one). `cancel()` aborts the
 *    task's `AbortSignal` with `Cancelled`.
 * 2. Every wait a cancel could interrupt takes that signal: `Event.wait`, `Mutex.acquire`,
 *    `Queue.get`, `Clock.sleep` (every pause goes through the injected clock), the wait for
 *    the agent's next event, and, through `cancellable`, the wait for a call that cannot be
 *    cut. The wait then rejects with `Cancelled`, which unwinds through the same `try` and
 *    `finally` blocks Python's `CancelledError` did, so a Python `finally` is still a
 *    `finally`. A catch-all rethrows `Cancelled` first, as `except Exception` let it pass.
 * 3. A call that has begun is never cut: a write to the chat (the rule of the Slack sink), the
 *    agent's start, a git call. The signal only releases whoever waited for it. Where the
 *    result of such a call would be lost, an explicit cleanup picks it up: a session whose
 *    start was given up is closed when the start returns (`connect`).
 * 4. asyncio delivered a cancel once, and the awaits of a `finally` that ran after it were not
 *    interrupted again; an aborted signal stays aborted. So a `finally` hands on `live(signal)`,
 *    which is no signal at all once it has aborted.
 * 5. A cancelled Python task whose `finally` did work is explicit state plus an explicit call:
 *    the request handlers of a closing session are resolved by `Approvals.denyAll` and awaited
 *    (`asking`), where Python's reader was cancelled inside them.
 * 6. Where Python relied on there being no `await` between a check and an act, the stretch is
 *    synchronous here too and marked `(sync)` with the reason. JavaScript yields at every
 *    `await`, also on a call that never waits, where Python ran such a call straight through:
 *    so what Python did around a call that only looked asynchronous (making a reply, moving
 *    the running counts) is done here with no `await` at all, and a turn is made the active
 *    one before its first write.
 * 7. A reaction is asked for at once and not awaited (`react`): Python made a task for it, and
 *    the chat makes the changes in the order they were asked.
 */
import { basename } from "node:path";
import type {
  AgentSession,
  CommandInfo,
  ContextUsage,
  ModelInfo,
  PermissionAnswer,
  PermissionRequest,
  PromptContent,
  Question,
  QuestionAnswer,
  QuestionRequest,
  Repository,
  SessionEvent,
} from "../../agent/seam.ts";
import { ResumeRefused } from "../../agent/seam.ts";
import type { FooterFields, MessageId, Reply, SessionStatus, ThreadChat } from "../../chat/seam.ts";
import { getLogger } from "../../log.ts";
import {
  effortChange,
  footerFields,
  formatStatusFields,
  gitState,
  sessionTokens,
} from "../footer.ts";
import {
  endedLine,
  type TaskEvent,
  type TaskStarted,
  type TurnEnded,
  TurnRenderer,
  taskTitle,
} from "../reply/renderer.ts";
import { oneLine } from "../reply/words.ts";
import type { Decision } from "../requests.ts";
import { type Choice, DEFAULT } from "../setup.ts";
import * as texts from "../texts.ts";
import {
  HOLD_MARKER,
  IDLE_CLOSE_SECONDS,
  INJECTED_TURN_WAIT,
  PROCESS_EXITED,
  STOP_TAIL_WAIT,
  STORED_STATUS,
  SUMMARY_IS_END_LINE,
  TASK_EVENTS,
  TASK_KINDS,
  TASK_REPLIES_KEPT,
  TASKS_KEPT,
  TURN_EVENTS,
  UNKNOWN_KIND,
} from "./constants.ts";
import type { SessionDeps } from "./deps.ts";
import { checkDirectory, resolvePath, terminalLine } from "./directory.ts";
import { DirectoryUnavailable, SessionClosed, SessionGone } from "./errors.ts";
import { Cancelled, cancellable, Event, Mutex, Queue, Task } from "./tasks.ts";
import { type ActiveTurn, notSent, Turn, takenNote } from "./turn.ts";

export const logger = getLogger("awaydesk.core.sessions");

type TaskEnded = Extract<SessionEvent, { type: "task_ended" }>;

/** What a log line may say of a failure: its name (the chat's code for a `ChatError`), never its words. */
export function describe(error: unknown): string {
  return error instanceof Error ? error.name : typeof error;
}

/** The signal for a `finally`: none once it has aborted, since asyncio cancelled a task once. */
function live(signal: AbortSignal | undefined): AbortSignal | undefined {
  return signal?.aborted ? undefined : signal;
}

function isTaskEvent(event: SessionEvent): event is TaskEvent {
  return TASK_EVENTS.has(event.type);
}

/**
 * The call an event belongs to: the one a subagent's event runs under, or the one that started
 * a task. Either leads to the reply holding that call when a background subagent works on after
 * its turn.
 */
function callOf(event: SessionEvent): string | null {
  if ("parentCallId" in event) return event.parentCallId;
  if ("callId" in event && isTaskEvent(event)) return event.callId;
  return null;
}

export class ThreadSession {
  readonly channelId: string;
  readonly threadTs: string;
  readonly directory: string;
  // Resolved once here, not by `SessionManager.workingIn` on every live session for every
  // message (`directory` never changes after this object is built: a rebind only takes effect
  // for a thread not open yet).
  private readonly resolved: string;
  private readonly deps: SessionDeps;
  // One status on the session's root message, which `threadTs` always is, and the line under
  // the thread's last message (issues #83 and #95): both are the chat's to draw.
  private readonly thread: ThreadChat;
  // Set by `stop()` while it denies pending approvals, so `askOwner`'s own finally does not
  // race its closing ✅ back to working; `finish` and `abandon` clear it, once that turn's own
  // tail ends, whichever way.
  private interrupting = false;
  /** Set once the turn `stop()` interrupted has closed its reply (`stopLanded`). */
  private stopTail: Event | null = null;
  // True from `reactError` until new work starts (`submit`, `startTurn`), so a standing ❌ is
  // never mistaken for idle-and-done. What the chat shows cannot serve this alone: it only
  // changes once the chat has answered, which a quick turn can easily outrun.
  private errorStanding = false;
  private client: AgentSession | null = null;
  private reader: Task | null = null;
  private worker: Task | null = null;
  private readonly queue = new Queue<Turn>();
  /** Out of the queue, not sent yet. */
  private taken: Turn | null = null;
  private sent: Turn[] = [];
  private active: ActiveTurn | null = null;
  private readonly connectLock = new Mutex();
  /** Set once close starts: no client starts after it, and no word stores a setting. */
  private closedFlag = false;
  // The predecessor this object replaced at its key (the manager evicts a closed session as
  // soon as closing starts, not once it finishes). Awaited once, inside the connect lock,
  // before this session's own first connect: the agent's process needs real time to flush
  // after EOF, and a resume of the same session id must not start while that is in flight.
  private predecessor: Event | null;
  // Set once close() (or the SessionGone branch) has fully finished: the predecessor signal
  // above, for whoever replaces this session next, and what `closeAll` waits on.
  readonly doneClosing = new Event();
  // Set by the manager that created this session; called once, right when `doneClosing` is
  // set, so a session nobody ever looks up again does not sit in the manager's map forever.
  onClosed: (() => void) | null = null;
  /** A line of the daemon's own that opens the next reply. */
  private notice: string | null = null;
  // `expireUnreported`'s own timers only: this session's alone, so `close` can cancel them.
  // What the session only starts and never waits on (a usage refresh, a reaction) is not kept:
  // a usage refresh shares one probe across every session, and is never cancelled.
  private readonly expiring = new Set<Task>();
  // Approval ids open right now, waiting on the owner's decision (`waitingForOwner`); a hold
  // adds `HOLD_MARKER` here too.
  private readonly waiting = new Set<string>();
  // The request handlers in flight (`askOwner`). A close resolves them and waits for each:
  // none may write the root's status after the close has settled it.
  private readonly asking = new Set<Promise<unknown>>();
  /** Task events that arrived between turns, shown in the next turn's reply. */
  private held: TaskEvent[] = [];
  // Tasks that outlived their turn, and the reply whose line each one keeps up to date, for
  // as long as the agent's process lives: an agent can report more than once.
  private readonly taskReplies = new Map<string, TurnRenderer>();
  /** The reply each renderer writes, as the session holds it. */
  private readonly replyOf = new WeakMap<TurnRenderer, Reply>();
  /** The thread's newest reply: the only one that shows what is still running. */
  private latest: Reply | null = null;
  // Replies whose end did not land: no checkmark until the one retry does, a cross if it
  // fails too. Each is settled once more when the session closes.
  private readonly unlanded = new Set<Reply>();
  // What the running turn's reply says when it ends: how many queued messages a restart
  // dropped, since they get no reply of their own.
  private endNotes: string[] = [];
  // Each task's type and description, for the footer's counts and its end line.
  private readonly tasks = new Map<string, readonly [type: string, description: string]>();
  // Ended tasks not yet opened into a report turn's reply: (task id, its formatted end line).
  // The task id finds the reply that started it (the report renders there).
  private ended: Array<readonly [taskId: string, line: string]> = [];
  // Tasks whose end arrived without their notification yet, by the clock time it arrived: the
  // notification starts the turn that reports them. The CLI can suppress it (SDK
  // TaskUpdatedMessage docstring), so a stop waits for it only INJECTED_TURN_WAIT.
  private readonly unreported = new Map<string, number>();
  // Tasks `!stop` ended. Claude Code starts no turn to report such a task: its notification
  // stays queued (measured twice on 2026-09-27, Claude Code 2.1.283), so none is awaited.
  private readonly stopped = new Set<string>();
  // Clear while a background notification's own turn is expected or running: the owner's
  // next prompt waits, so the two replies never share a thread.
  private readonly settled = new Event();
  private injectedExpected = false;
  // The prompt whose replay arrived with no turn running: the turn that starts next is its
  // own (`acknowledge`, `whoseTurn`).
  private announced: Turn | null = null;
  // A report turn was awaited when a prompt's own turn started in its place: it is awaited
  // again once that turn ends (`settle`).
  private reportDue = false;
  // A task's notification arrived with no turn running and no report turn has started since:
  // the agent has a report to make (`whoseTurn`).
  private notificationWaits = false;
  private expiry: Task | null = null;
  /** The agent said it is compacting the conversation and has not said it ended. */
  private compacting = false;
  // Armed while idle with nothing pending; any turn, task event, approval or question cancels
  // it, and being idle with nothing pending again (re)starts it.
  private idleExpiry: Task | null = null;
  // A count, not a flag, since two `submit` calls can overlap. Nonzero makes `idle` false for
  // the span between a `submit` call starting and its turn actually being queued, so the timer
  // is genuinely cancelled there, not reset to fire again mid-await.
  private pendingSubmits = 0;
  commands: CommandInfo[] = [];
  /** The agent's own model list, for the setup. */
  models: ModelInfo[] = [];
  // A prompt was ever queued on this object; with no stored session id it tells a thread that
  // never ran a turn from one whose first turn is still starting.
  private submitted = false;
  /** `applySetup` changed something that `forgetSetup` has not undone. */
  private setupApplied = false;
  /** Held while Start is applied, so a `!bypass` typed meanwhile acts after it. */
  private readonly setupLock = new Mutex();
  // What the live client was started with or switched to: the effort, and whether it
  // effectively runs in bypass (the switch, or the folder's own settings starting it so).
  // `applySetup` compares the owner's choice with these, not with what was stored, which a
  // restart or a `!bypass on` can have moved.
  private clientEffort: string | null = null;
  private clientBypass = false;
  nativeMode: string;
  // The permission mode the client runs in now, for `!status`: what the connect left it in,
  // then whatever the agent reports (`dispatch`).
  mode: string;
  cliVersion: string | null = null;
  // The effort level the agent last reported: its Stop hook, or the output of `/effort` and
  // `/model`, which run no hook. Null for a model without effort.
  effort: string | null = null;
  // Whether the agent has reported the level since the client started: until then it is not
  // known, since the settings do not decide it (measured 2026-09-25).
  effortReported = false;
  // Where the session works as its hooks last reported (`cwd` follows a `cd` and a worktree;
  // Stop and PostToolUse measured 2026-09-27 on 2.1.283); null until then: `directory`.
  workingDirectory: string | null = null;
  /** The session's token count as the client's last result reported it, for `!status`. */
  sessionTokens: number | null = null;
  /** Set when the daemon stops: the turns already sent finish, no other one starts. */
  draining = false;
  // Whether this stop has said, in a message, which background tasks it waits for: only where
  // the thread's activity line cannot (`showRestartWait`).
  private toldWaiting = false;
  // Set when a stop begins while this session has a turn running. The signal names no sender,
  // and the session that sent it has its turn running then; a background task it starts from
  // that point is often its own wait for the new process, which cannot end before this one
  // exits (issue #87). Those tasks are not waited for: the shutdown ends them with the agent's
  // process. "After" means read after: a task the turn started just before the signal, whose
  // start event the reader had not reached yet, counts as started after it. A single
  // background command that sends the signal and then waits started before it, and is still
  // waited for.
  mayHaveOrderedRestart = false;
  private readonly afterRestart = new Set<string>();

  constructor(
    channelId: string,
    threadTs: string,
    directory: string,
    deps: SessionDeps,
    options: { readonly predecessor?: Event | null } = {},
  ) {
    this.channelId = channelId;
    this.threadTs = threadTs;
    this.directory = directory;
    this.resolved = resolvePath(directory);
    this.deps = deps;
    this.thread = deps.chat.thread(channelId, threadTs);
    this.predecessor = options.predecessor ?? null;
    this.settled.set();
    this.nativeMode = deps.agent.capabilities.permissionModes.default;
    this.mode = this.nativeMode;
  }

  private get where(): string {
    return `${this.channelId}/${this.threadTs}`;
  }

  /** The agent's own name for the mode that skips permission requests; null when it has none. */
  private get bypassMode(): string | null {
    return this.deps.agent.capabilities.permissionModes.bypass;
  }

  /**
   * The owner's last word on bypass in this thread, as state.json holds it (a restart keeps
   * it): true on, false off, null never chosen.
   */
  get bypassChoice(): boolean | null {
    return this.deps.state.thread(this.channelId, this.threadTs)?.bypass ?? null;
  }

  /**
   * Whether a client runs in bypass given `choice`: what the owner chose, else what the agent
   * itself started in (`nativeMode`, as it reported it).
   */
  private bypassRuns(choice: boolean | null): boolean {
    return choice ?? (this.bypassMode !== null && this.nativeMode === this.bypassMode);
  }

  /**
   * Whether this thread runs in bypass: the one answer the footer's ⚡, `!status`, the channel
   * list and the setup's checkbox read.
   */
  get bypass(): boolean {
    return this.bypassRuns(this.bypassChoice);
  }

  /**
   * The permission mode that means `on`; off is the native mode, or the default one when the
   * process itself started in bypass.
   */
  private modeFor(on: boolean): string {
    const modes = this.deps.agent.capabilities.permissionModes;
    if (on) {
      if (modes.bypass === null) throw new RangeError("the agent has no bypass mode");
      return modes.bypass;
    }
    return this.nativeMode === modes.bypass ? modes.default : this.nativeMode;
  }

  /**
   * `directory`, resolved once at construction and kept (`SessionManager.workingIn` reads this
   * across sessions, instead of resolving every live session's folder on every message).
   */
  get resolvedDirectory(): string {
    return this.resolved;
  }

  get busy(): boolean {
    return this.active !== null || this.sent.length > 0;
  }

  /** An approval or a question is open in this thread, waiting on the owner's answer. */
  get waitingForOwner(): boolean {
    return this.waiting.size > 0;
  }

  /**
   * What the thread's activity line says, empty for nothing, and the same after the app's
   * name. `Working…` while a prompt is on its way to the agent or a turn runs (issue #83),
   * `Compacting conversation…` while that turn compacts, in the terminal's words (issue #169).
   * Once the turn has ended, what it left running (`1 shell still running`, issue #95): a count
   * that changes cannot live in a reply, whose stream only grows. Nothing while an approval, a
   * question or a hold waits on the owner, while `!stop` winds a turn down, and once the
   * session is closed.
   */
  private threadLine(): readonly [text: string, afterName: string] {
    if (this.closedFlag || this.waitingForOwner || this.interrupting) return ["", ""];
    if (this.busy || this.taken !== null || !this.queue.empty) {
      if (this.compacting) return [texts.THREAD_COMPACTING, texts.THREAD_COMPACTING_STATUS];
      return [texts.THREAD_WORKING, texts.THREAD_WORKING_STATUS];
    }
    const held = this.draining ? this.runningKindsOf({ awaitedOnly: true }) : "";
    if (held) {
      // A stop that only background tasks hold: said here whatever the footer shows, since
      // `!stop` is what ends the wait.
      const them = held.startsWith("1 ") && !held.includes(" · ") ? "it" : "them";
      return [
        texts.fill(texts.RESTART_WAITS, { counts: held, them }),
        texts.fill(texts.RESTART_WAITS_STATUS, { counts: held }),
      ];
    }
    const kinds = this.runningKindsOf();
    if (!kinds || (this.latest?.footerShown ?? false)) {
      // Nothing runs, or the end of the thread's last reply is in the chat: its footer counts
      // them, and a second line under a footer would say the same twice.
      return ["", ""];
    }
    return [
      texts.fill(texts.STILL_RUNNING, { counts: kinds }),
      texts.fill(texts.STILL_RUNNING_STATUS, { counts: kinds }),
    ];
  }

  /**
   * Bring the thread's activity line to `threadLine`, after anything that can change it: it
   * never waits on the chat.
   */
  private showThreadStatus(): void {
    this.thread.showActivity(...this.threadLine());
  }

  /**
   * `1 shell · 2 agents`, the tasks that outlived their turn and still run; empty when none
   * does. The channel-level `!status` shows this beside a session's own state.
   */
  get runningKinds(): string {
    return this.runningKindsOf();
  }

  /**
   * Nothing running, sent, taken or queued, no `submit` call still on its way to queuing one,
   * and no background task still working: the session can be closed without a loss (closing it
   * ends its agent process). `bind` waits for every thread of a channel to be idle before it
   * stores a new folder.
   */
  get idle(): boolean {
    return this.quiet({ awaitedOnly: false });
  }

  /**
   * `idle` as a stop reads it: a background task started after the stop began, in a session
   * that may have sent the signal (`mayHaveOrderedRestart`), does not count.
   */
  get restartReady(): boolean {
    return this.quiet({ awaitedOnly: true });
  }

  /**
   * What a stop still waits for in this thread, in words the owner can act on; empty when the
   * thread does not hold it (`SessionManager.drain` reads the same two flags).
   */
  get restartHold(): string {
    if (this.restartReady && !this.reporting) return "";
    if (this.waitingForOwner) return texts.RESTART_HOLD_OWNER;
    const held = this.runningKindsOf({ awaitedOnly: true });
    if (held) {
      if (!this.busy) return texts.fill(texts.RESTART_HOLD_TASKS, { counts: held });
    } else if (!this.busy && this.reporting) {
      return texts.RESTART_HOLD_REPORT;
    }
    return texts.RESTART_HOLD_TURN;
  }

  private quiet(options: { readonly awaitedOnly: boolean }): boolean {
    return (
      !this.busy &&
      this.pendingSubmits === 0 &&
      !this.runningKindsOf(options) &&
      this.taken === null &&
      this.queue.empty &&
      this.settled.isSet
    );
  }

  /**
   * Closing has started (`close()`'s first line, a shutdown, the idle close, or the SessionGone
   * branch of `ensureConnected`); the teardown itself may still be running. The manager
   * discards this object rather than hand it out again, from its next lookup; `doneClosing`
   * says when the teardown is over.
   */
  get closed(): boolean {
    return this.closedFlag;
  }

  /**
   * A task ended less than INJECTED_TURN_WAIT ago and the notification that makes the agent
   * report it has not come yet.
   */
  get reporting(): boolean {
    const now = this.deps.clock.time();
    return [...this.unreported.values()].some((ended) => now - ended < INJECTED_TURN_WAIT);
  }

  /**
   * Cancel or (re)arm the idle-close timer for the state right now. The manager calls this
   * wherever a session is handed out (`get`/`open`), synchronously, before any await a caller
   * might do on the way to its own `submit`: otherwise the timer could still fire, and close
   * the session, in the gap between the lookup and the turn actually being queued.
   */
  touch(): void {
    this.idleTimerCheck();
  }

  /**
   * Mark this thread's "send anyway?" question as waiting on the owner, the same way an
   * approval does (`waitingForOwner` covers both, so the idle-close timer stays off and the
   * root shows ✋, including through any report turn a background task starts while held:
   * `startTurn` reacts waiting, not working, whenever `waitingForOwner` is true). At most one
   * hold is ever open on a thread at once, so a caller of this always pairs it with one
   * `holdEnd`. `react` would otherwise read waiting as "no error stands": a hold is not new
   * work, so a standing ❌ must outlive it (Cancel restores it; Continue's own `submit` clears
   * it, same as any new work).
   */
  holdStart(): void {
    const standing = this.errorStanding;
    this.waiting.add(HOLD_MARKER);
    this.idleTimerCheck();
    this.react("waiting");
    this.errorStanding = standing;
  }

  /**
   * Undo `holdStart`. `continued`: Continue was chosen, so the status is left alone (the
   * `submit()` right after this returns reacts ⏳ on its own); otherwise (Cancel, `!stop`, a
   * drain) the root reflects the session's live state now, not a snapshot from when the hold
   * started (a report turn during the hold can have changed it in the meantime).
   */
  async holdEnd(options: { readonly continued: boolean }): Promise<void> {
    this.waiting.delete(HOLD_MARKER);
    this.idleTimerCheck();
    if (!options.continued) await this.reactAfterHold();
  }

  /**
   * The status a hold not continued restores: none for a thread that never ran a turn (read
   * from the stored session id: an idle close or a restart hands the thread a fresh object
   * with its history intact, so "no client" alone is not "never ran"); otherwise ❌ when an
   * error stands (`holdStart` kept it standing through the hold; this is what restores it,
   * since `holdStart`'s own waiting status already overwrote whatever ❌ was showing), ✅
   * through `reactDoneIfIdle` when idle and no error stands, ⏳ or ✋ (whichever this thread
   * still holds) otherwise.
   */
  private async reactAfterHold(): Promise<void> {
    const stored = this.deps.state.thread(this.channelId, this.threadTs);
    if (stored === null || stored.sessionId === null) {
      this.deps.state.setStatusPending(this.channelId, this.threadTs, null);
      await this.thread.clearStatus();
      return;
    }
    if (this.errorStanding) this.reactError();
    else if (this.idle) await this.reactDoneIfIdle();
    else this.reactWaitingOrWorking();
  }

  /**
   * A Continue whose `submit()` never ran: `holdEnd({ continued: true })` already dropped the
   * hold and left the ✋ standing, betting on that `submit()` to show ⏳ right after; a caller
   * that instead exits without ever calling it (the manager draining, or `ensureConnected`
   * rejecting) must restore the status itself, or the ✋ stands forever. `error`: ❌, the same
   * a turn that reached the queue and then failed this way gets; otherwise whatever a Cancel
   * would show.
   */
  async reactHoldAbandoned(options: { readonly error: boolean }): Promise<void> {
    if (options.error) {
      this.reactError();
      return;
    }
    await this.reactAfterHold();
  }

  /**
   * Cancel a hold open in this thread, as the owner's own Cancel would: `stop()` and
   * `SessionManager.drain` both call this (a drain, unlike an approval or a question, never
   * leaves a hold open: nobody could still be typing a reply to a question that names a session
   * about to close). The button message is removed here, silently; whoever waits on the hold
   * tells the owner `Not sent.` once it wakes.
   */
  async cancelHold(): Promise<boolean> {
    const pending = this.deps.holds.cancel(this.channelId, this.threadTs);
    if (pending === null) return false;
    await this.deleteRequest(pending.messageTs);
    return true;
  }

  /**
   * End every queued turn's reply with `line`, and release whoever waits on them: the turns a
   * thread whose session is gone could not start.
   */
  async failQueued(line: string): Promise<void> {
    while (!this.queue.empty) {
      try {
        await this.fail(this.queue.getNowait(), line);
      } catch {
        // one reply that cannot be ended must not keep the others waiting
      }
    }
  }

  /**
   * A restart will never send the queued turns: release whoever waits on them. They get no
   * reply of their own; one note names them, at the end of the running turn's reply, or, with
   * nothing running, as a message of its own. `error` reacts ❌ (a restart drain dropping a
   * queued turn): a plain `close()` never needs it here, since its own cut-short check already
   * reacts once for the whole close.
   */
  async dropQueued(options: { readonly error?: boolean } = {}): Promise<void> {
    const queued: Turn[] = [];
    while (!this.queue.empty) queued.push(this.queue.getNowait());
    await this.drop(queued, texts.BECAUSE_RESTARTED, options);
  }

  /** Release `turns` that will never be sent, and say so once: see `dropQueued`. */
  private async drop(
    turns: readonly Turn[],
    because: string,
    options: { readonly error?: boolean } = {},
  ): Promise<void> {
    if (turns.length === 0) return;
    try {
      if (options.error) this.reactError();
      const note = notSent(turns, because);
      if (this.busy) this.endNotes.push(note);
      else await this.post(note);
    } finally {
      for (const turn of turns) turn.done.set();
      this.showThreadStatus();
    }
  }

  /** Say, at the end of the reply that just ran, what a restart dropped meanwhile. */
  private async feedEndNotes(renderer: TurnRenderer): Promise<void> {
    const notes = this.endNotes;
    this.endNotes = [];
    if (notes.length > 0) {
      // The dropped turns' replies are never written, and the newest was the latest: the
      // reply that says so is the one that has the footer.
      this.handLatestTo(this.reply(renderer));
    }
    for (const note of notes) await renderer.feedError(note);
  }

  /**
   * Queue a prompt; its reply starts when the agent has something to show, not before.
   * Rejects with `SessionClosed` if the session closed (an idle close, most likely) between
   * the caller's lookup and this call; the caller retries once, against a freshly looked-up
   * session.
   */
  async submit(prompt: PromptContent): Promise<Turn> {
    if (this.closedFlag) throw new SessionClosed();
    // Counted, not just checked-and-cancelled: `idle` reads false for as long as this stays
    // above zero, so the timer is genuinely cancelled here, not reset only to fire again
    // during the awaits below.
    this.pendingSubmits += 1;
    this.submitted = true;
    let turn: Turn;
    try {
      this.idleTimerCheck();
      // (sync) Python awaited `_sink`, which never waited: the prompt was queued in the same
      // step it arrived in, ahead of whatever the reader had not read yet. So the reply is
      // awaited here only when making it does wait, which is what the check below is for.
      const made = this.sink();
      turn = new Turn(prompt, made instanceof Promise ? await made : made);
      if (this.closedFlag) {
        // The session closed (an idle close or a restart, or `ensureConnected`'s SessionGone
        // branch, which already drained the queue) while the reply was made: no worker will
        // ever take this turn from the queue, so it is resolved right here instead of left
        // waiting for one that will not come. Only a SessionGone close also removes the
        // thread's own entry: that alone is what tells the two apart, since both leave the
        // session closed the same.
        const gone = this.deps.state.thread(this.channelId, this.threadTs) === null;
        await this.fail(turn, gone ? texts.SESSION_GONE : texts.SESSION_CLOSED, { error: true });
        return turn;
      }
      // (sync) from here to the end: the turn is queued, the worker made and the idle timer
      // read with nothing in between, as Python did after its last await.
      this.queue.putNowait(turn);
      // A submitted turn is working, unless an approval or question already open in this
      // thread still holds it (that state stands until it is answered). New work, so any
      // standing ❌ ends here.
      if (!this.waitingForOwner) {
        this.errorStanding = false;
        this.react("working");
      }
    } finally {
      this.pendingSubmits -= 1;
    }
    if (this.worker === null || this.worker.done) {
      this.worker = new Task((signal) => this.work(signal));
    }
    this.idleTimerCheck(); // a queued turn is not idle
    return turn;
  }

  /**
   * The live client, started when there is none. `signal` is the calling task's: a cancel
   * leaves the wait, and a start it gave up is closed when it returns.
   */
  async ensureConnected(signal?: AbortSignal): Promise<AgentSession> {
    const release = await this.connectLock.acquire(signal);
    try {
      if (this.closedFlag) throw new SessionClosed();
      if (this.client !== null) return this.client;
      if (this.predecessor !== null) {
        // The agent process this thread had before may still be exiting: it needs time to
        // flush the session file after EOF, and starting with the same id to resume before that
        // is done would race it. The Python SDK needed up to about 20 seconds; with the
        // TypeScript SDK 0.3.296 `close()` resolved in 282 ms and a resume of the same id right
        // after it worked (measured 2026-10-10). The wait stays, since it costs nothing then.
        await this.predecessor.wait(signal);
        this.predecessor = null;
      }
      await cancellable(
        checkDirectory(this.directory, (folder) => this.deps.agent.folderTrusted(folder)),
        signal,
      );
      const stored = this.deps.state.thread(this.channelId, this.threadTs);
      const sessionId = stored?.sessionId ?? null;
      const effort = stored?.effort ?? null;
      let client: AgentSession;
      try {
        client = await this.connect(sessionId, effort, signal);
      } catch (error) {
        if (!(error instanceof ResumeRefused) || sessionId === null) throw error;
        // The stored session is gone (its transcript was deleted): the thread cannot continue.
        // Its entry is dropped and the session ends itself here, so no later turn (already
        // queued, or the next one taken by the worker) starts a second, unrecorded session in
        // its place: it fails the same way instead.
        logger.warning(`could not resume the stored session in ${this.where}`);
        this.deps.state.removeThread(this.channelId, this.threadTs);
        this.closedFlag = true;
        // No client was ever connected in this branch (`connect` rejected): nothing to wait
        // on, so the "fully closed" signal fires at once, same as `close()`'s tail.
        this.doneClosing.set();
        this.onClosed?.();
        this.idleTimerCheck(); // cancels any armed timer; closed now, so none rearms
        const worker = this.worker;
        if (worker !== null && worker.signal !== signal) {
          // Called directly (`!status`, `!bypass`), not from this session's own worker: an
          // idle worker left blocked on the queue (most likely on this very connect lock,
          // still held here) would never learn to stop otherwise. It is cancelled and awaited
          // first, and its taken turn (if any) rescued only once it is confirmed stopped:
          // cancelling it while it still held a turn, with nothing rescuing that turn, left
          // that reply saying "writing" forever. When the worker IS the caller (the common
          // case: this branch runs inside the worker's own call), its own catch up the stack
          // already fails the taken turn itself; failing it again here too would double the
          // reply's text.
          worker.cancel();
          await worker.settled();
          const taken = this.taken;
          this.taken = null;
          if (taken !== null) {
            try {
              await this.fail(taken, texts.SESSION_GONE, { error: true });
            } catch {
              // the queued ones below are still told
            }
          }
        }
        await this.failQueued(texts.SESSION_GONE);
        throw new SessionGone({ cause: error });
      }
      let choice: boolean | null;
      let mode: string;
      try {
        const info = await cancellable(client.info(), signal);
        this.commands = [...info.commands];
        this.models = [...info.models];
        const modes = this.deps.agent.capabilities.permissionModes;
        this.nativeMode = info.permissionMode || modes.default;
        // Read once: a Start writing the switch meanwhile must not make the client's mode and
        // what is recorded for it disagree.
        choice = this.bypassChoice;
        mode = this.nativeMode;
        if (choice === true) {
          mode = this.modeFor(true);
          await cancellable(client.setPermissionMode(mode), signal);
        } else if (choice === false && this.nativeMode === modes.bypass) {
          // An explicit off outlives a rebuild: the folder's own bypass would return.
          mode = modes.default;
          await cancellable(client.setPermissionMode(mode), signal);
        }
      } catch (error) {
        // A client nobody holds would leave its agent process running.
        await this.disconnect(client);
        throw error;
      }
      this.client = client;
      this.clientEffort = effort;
      this.clientBypass = this.bypassRuns(choice);
      this.mode = mode;
      // A resumed session runs at the settings' level (measured), unknown until reported,
      // unless the daemon itself just asked for a stored level: that request is shown at once,
      // until the agent's own report (every turn ends with one) corrects it.
      const requested = this.validEffort(effort);
      this.effort = requested;
      this.effortReported = requested !== null;
      this.workingDirectory = null; // the new process starts in the bound folder
      this.sessionTokens = null; // counted by the client process, which starts at zero
      this.reader = new Task((reading) => this.read(client, reading));
      this.idleTimerCheck(); // a daemon word (`!status`, say) connects with no turn
      return client;
    } finally {
      release();
    }
  }

  /** `effort` when the agent takes it at a start; null for none, and for a level it does not know. */
  private validEffort(effort: string | null): string | null {
    return effort !== null && this.deps.agent.capabilities.effortLevels.includes(effort)
      ? effort
      : null;
  }

  async setBypass(on: boolean): Promise<void> {
    const client = await this.ensureConnected();
    const modes = this.deps.agent.capabilities.permissionModes;
    let mode = this.modeFor(on);
    try {
      try {
        await client.setPermissionMode(mode);
      } catch (error) {
        if (mode !== modes.auto || this.closedFlag) throw error;
        // Claude Code refuses auto mode to a session that does not meet its requirements (a
        // model without it, measured), and the refusal leaves bypass running: off then lands
        // on the mode the agent itself starts in when auto mode is unavailable.
        logger.warning(
          `could not return ${this.where} to auto mode, setting default: ${describe(error)}`,
        );
        mode = modes.default;
        await client.setPermissionMode(mode);
      }
    } catch (error) {
      if (this.closedFlag) throw new SessionClosed(); // the close disconnected the client under the call
      throw error;
    }
    this.clientBypass = on;
    this.mode = mode;
    // The session closed while the mode was being set: the thread this would write to may
    // already be gone, so the switch is never recorded after the fact.
    if (this.closedFlag) throw new SessionClosed();
    this.deps.state.setBypass(this.channelId, this.threadTs, on);
  }

  /**
   * `!bypass on` or `off` typed in the thread: switch and store it, true; or false, changing
   * nothing, before Start (`beforeStart`). A Start being applied is waited for, so the word,
   * typed after the click, is what holds; one that failed has undone itself by then, and the
   * word is refused.
   */
  async switchBypass(on: boolean): Promise<boolean> {
    const release = await this.setupLock.acquire();
    try {
      if (this.beforeStart) return false;
      await this.setBypass(on);
      return true;
    } finally {
      release();
    }
  }

  /**
   * No turn was ever queued in this thread: its next message is a first prompt, which asks for
   * the setup again (after a cancelled setup or a hold's Cancel, nothing was sent).
   */
  get neverRan(): boolean {
    const stored = this.deps.state.thread(this.channelId, this.threadTs);
    return !this.submitted && (stored === null || stored.sessionId === null);
  }

  /**
   * No first prompt was sent and no Start applied: the setup's Bypass box is what sets bypass,
   * and `applySetup` writes it over anything set before.
   */
  get beforeStart(): boolean {
    return this.neverRan && !this.setupApplied;
  }

  /**
   * Apply the owner's session setup before the first prompt. Start is authoritative: the
   * effort and the bypass switch are written from the choice whatever state.json held (a
   * restart can have left either; `!bypass` is refused before Start), so what runs is what the
   * summary line says. An effort the live client does not run at is set on it when the back end
   * can (`liveEffort`), and goes through a fresh client when it cannot (no prompt has been
   * sent, so no session is lost); then the model on the live client, then the permission mode:
   * when the choice differs from what the client effectively runs (the switch, or a folder
   * whose own settings start it in bypass), `setBypass` moves it. Unticking in such a folder is
   * an explicit off with the semantics of `!bypass off`: the mode it returns to is the default
   * one. Measured 2026-09-30 (CLI 2.1.285): a model set on the live session survives a resume
   * and leaves the owner's own default alone, so the daemon does not store the model. A failure
   * undoes what was applied (`forgetSetup`) and goes on. All of it under the setup lock, which
   * a word's `switchBypass` waits on.
   */
  async applySetup(choice: Choice): Promise<void> {
    const release = await this.setupLock.acquire();
    try {
      this.setupApplied = true;
      const effort = choice.effort === DEFAULT ? null : choice.effort;
      try {
        this.deps.state.setEffort(this.channelId, this.threadTs, effort);
        this.deps.state.setBypass(this.channelId, this.threadTs, choice.bypass);
        let client = await this.ensureConnected();
        if (effort !== this.clientEffort) {
          if (this.deps.agent.capabilities.liveEffort) await this.changeEffort(client, effort);
          else client = await this.reconnect();
        }
        if (choice.model !== DEFAULT) await client.setModel(choice.model);
        if (choice.bypass !== this.clientBypass) {
          // A client connected above applies the stored switch itself: this only moves one that
          // was already live, off meaning the mode it would have had without bypass.
          await this.setBypass(choice.bypass);
        }
      } catch (error) {
        try {
          await this.forgetSetup();
        } catch (undo) {
          logger.warning(`could not undo a failed setup in ${this.where}: ${describe(undo)}`);
        }
        throw error;
      }
    } finally {
      release();
    }
  }

  /**
   * Set the stored effort on the live client, where Python started a fresh one: what the
   * session knows of the level is left as a connect with that stored level leaves it.
   */
  private async changeEffort(client: AgentSession, effort: string | null): Promise<void> {
    const level = this.validEffort(effort);
    if (effort !== null && level === null) {
      logger.warning(`dropped an unrecognized stored effort level in ${this.where}`);
    }
    await client.setEffort(level);
    this.clientEffort = effort;
    this.effort = level;
    this.effortReported = level !== null;
  }

  /**
   * Undo a Start whose message was not sent (a failure, a stop, a hold's Cancel, or a restart
   * that kept what it wrote): the stored effort and bypass go back to never chosen, and the
   * client is dropped, since it carries the effort, the model and the mode, which the next
   * connect rebuilds. A no-op for a thread that ran a turn (its choices are the owner's) and
   * when there is nothing to undo.
   */
  async forgetSetup(): Promise<void> {
    const stored = this.deps.state.thread(this.channelId, this.threadTs);
    const left = stored !== null && (stored.effort !== null || stored.bypass !== null);
    if (!this.neverRan || !(this.setupApplied || left)) return;
    this.setupApplied = false;
    this.deps.state.setEffort(this.channelId, this.threadTs, null);
    this.deps.state.setBypass(this.channelId, this.threadTs, null);
    await this.dropClient();
  }

  /**
   * Close the live client and connect again, so `ensureConnected` starts it from state as it
   * is now: the way to a new effort for a back end that cannot change it live.
   */
  private async reconnect(): Promise<AgentSession> {
    await this.dropClient();
    return this.ensureConnected();
  }

  private async dropClient(): Promise<void> {
    const release = await this.connectLock.acquire();
    try {
      const reader = this.reader;
      this.reader = null;
      const client = this.client;
      this.client = null;
      if (reader !== null) {
        reader.cancel();
        await reader.settled();
      }
      if (client !== null) await this.disconnect(client);
    } finally {
      release();
    }
  }

  /**
   * During a stop: bring the thread's activity line up to date. Once only background tasks are
   * left it says which ones the restart waits for (`threadLine`), since only the owner knows
   * whether a task (a dev server, a watcher) ever ends. A line and not a message: a message
   * would notify, and would stay in the thread after the restart. Where the chat refuses this
   * app an activity line, a message says it, once: a restart that waits without a word is worse
   * than one notification.
   */
  async showRestartWait(): Promise<void> {
    this.showThreadStatus();
    if (!this.thread.activityRefused || this.toldWaiting || this.busy) return;
    const held = this.runningKindsOf({ awaitedOnly: true });
    if (held) {
      this.toldWaiting = true;
      await this.post(texts.fill(texts.RESTART_WAITS_MESSAGE, { counts: held }));
    }
  }

  /**
   * A message of the app's was posted in this thread outside the session: the chat clears a
   * thread's activity line when the app replies, so it is set again.
   */
  threadWritten(): void {
    this.thread.written();
  }

  /**
   * Interrupt the running turn, deny its pending approvals and stop this thread's background
   * tasks; queued turns stay queued. A hold open in this thread is always cancelled too,
   * silently (whoever waits on it tells the owner `Not sent.` once it wakes). Null when that
   * hold was the only thing here: the caller adds no further notice of its own then, since a
   * hold with nothing else running stops nothing the agent itself was doing, and `Not sent.`
   * alone already answers `!stop`.
   */
  async stop(): Promise<boolean | null> {
    // (sync) With no hold open, nothing waits between the call and the checks below: Python's
    // `cancel_hold` returned at once then.
    const hold = this.deps.holds.cancel(this.channelId, this.threadTs);
    const held = hold !== null;
    if (hold !== null) await this.deleteRequest(hold.messageTs);
    const client = this.client;
    if (client === null) return held ? null : false;
    const tasks = this.runningTaskIds();
    if (!this.busy && tasks.length === 0) return held ? null : false;
    if (this.busy) {
      // A denial `denyAll` triggers below resolves `askOwner`'s own wait, whose `finally`
      // would otherwise race this method's own closing ✅ back to working; this flag makes it
      // skip that instead. `finish` or `abandon` clears it once this turn's own tail ends.
      this.interrupting = true;
      this.stopTail = this.stopTail ?? new Event();
      for (const pending of this.deps.approvals.denyAll(this.channelId, this.threadTs)) {
        await this.deleteRequest(pending.messageTs);
      }
      await client.interrupt();
    }
    for (const taskId of tasks) {
      this.stopped.add(taskId);
      try {
        await client.stopTask(taskId);
      } catch (error) {
        // it may have ended meanwhile; the others still stop
        logger.warning(`could not stop a task in ${this.where}: ${describe(error)}`);
      }
    }
    // A stop the owner gave is not an error. `react` clears `errorStanding`, so a later idle
    // sweep shows the same ✅ and `noteStatus` drops the persisted ⏳.
    this.react("done");
    return true;
  }

  /**
   * Return once the reply `stop()` cut short has ended, so what is posted next sits under its
   * ending and its footer. Waits at most STOP_TAIL_WAIT, and not at all when the stop
   * interrupted no turn (it stopped background tasks only).
   */
  async stopLanded(): Promise<void> {
    const tail = this.stopTail;
    if (tail === null || tail.isSet) return;
    const waiting = new AbortController();
    try {
      await Promise.race([
        tail.wait(waiting.signal),
        this.deps.clock.sleep(STOP_TAIL_WAIT, waiting.signal),
      ]);
    } finally {
      waiting.abort(new Cancelled()); // whichever of the two still waits leaves
    }
  }

  private stopTailEnded(): void {
    const tail = this.stopTail;
    this.stopTail = null;
    tail?.set();
  }

  /**
   * The channel's directory, session and mode, then the footer's values one per line, or why
   * the agent cannot start. Starts the client like `!help` does, since model and context come
   * from it.
   */
  async status(): Promise<string> {
    let data: FooterFields | null = null;
    let unavailable: string[] = [];
    let gone = false;
    let usable = true;
    try {
      await this.ensureConnected();
      this.refreshUsage();
      data = await this.footerData(this.sessionTokens);
    } catch (error) {
      if (error instanceof DirectoryUnavailable) {
        unavailable = [error.message];
        usable = false;
      } else if (error instanceof SessionGone) {
        // This call is the one that found it gone: `ensureConnected` already closed the
        // session over it, which is not the race the check below guards against.
        unavailable = [error.message];
        gone = true;
      } else {
        // the status still answers, with what a prompt would get
        logger.warning(
          `could not read the footer's values for the status in ${this.where}: ${describe(error)}`,
        );
        unavailable = [texts.fill(texts.ERROR_REPLY, { error: describe(error) })];
      }
    }
    // The daemon closed the session (shutdown, an idle close) while this call was reading it.
    if (this.closedFlag && !gone) throw new SessionClosed();
    const fields = data === null ? [] : formatStatusFields(data, this.now());
    const here = this.workingDirectory;
    if (data !== null && here && here !== this.directory) {
      // The branch and the changes below describe this folder, not the thread's own.
      fields.unshift(texts.fill(texts.STATUS_WORKING, { directory: here }));
    }
    const running = this.runningKindsOf();
    if (running) fields.push(texts.fill(texts.STATUS_BACKGROUND, { counts: running }));
    const stored = this.deps.state.thread(this.channelId, this.threadTs);
    const activity = this.busy
      ? texts.fill(texts.ACTIVITY_BUSY, { queued: this.queue.size })
      : texts.ACTIVITY_IDLE;
    const sessionId = stored?.sessionId ?? null;
    const head = [
      texts.fill(texts.STATUS, { directory: this.directory, session: sessionId || "new" }),
    ];
    // Left out with a folder the line above cannot use: its `cd` would fail there.
    if (sessionId && usable) head.push(terminalLine(this.directory, sessionId));
    const state = texts.fill(texts.STATUS_STATE, {
      mode: this.bypass ? (this.bypassMode ?? this.mode) : this.mode,
      // Only a turn's first event carries the version, never the start (measured).
      version: this.cliVersion || (this.client !== null ? texts.VERSION_PENDING : "not started"),
      activity,
    });
    return [...head, state, ...fields, ...unavailable].join("\n");
  }

  private now(): number {
    return (this.deps.now ?? Date.now)();
  }

  /**
   * Refresh the usage limits in the background when stale; the next footer shows them. A task
   * of its own, started after what is already due (the reader of a client just started reads
   * its first turn's limits event before the refresh asks), and never cancelled: a refresh
   * shares one probe across every session.
   */
  private refreshUsage(): void {
    new Task(() => this.deps.usage.refreshIfStale());
  }

  /**
   * Stop the session. The running reply ends with a line saying why, and one note names the
   * messages that were sent or queued and will never be answered, so no stream is left open.
   *
   * `doneClosing` always fires, from a `finally`: a step below failing must not leave a
   * rebuilt session's `ensureConnected` waiting on it forever, or `closeAll` hanging at
   * shutdown, or the manager's map holding a session nothing can ever evict.
   */
  async close(reason: string = texts.ENDED_SHUTDOWN): Promise<void> {
    // (sync) Read before anything below settles it back to idle. A close that cuts anything
    // short (a shutdown or a restart's drain, most likely: `busy` alone misses a queued or
    // taken turn and a task that outlived its own turn, which `idle` already accounts for)
    // gets ❌; the idle close, always called on an idle session, never does, and only finishes
    // a change of the status still on its way (`ThreadChat.settleStatus`).
    const cutShort = !this.idle;
    // Issue #87: at the end of a stop, a session whose only unfinished work is a background
    // task the stop does not wait for (`mayHaveOrderedRestart`) ended its turn well; the task
    // is most likely its own wait for the new process. ✅, as `!stop` would show.
    const onlySkipped = cutShort && this.draining && this.restartReady;
    this.closedFlag = true;
    try {
      // A snapshot taken before any cancellation below, not `denyAll`'s own return list read
      // afterward: a request whose handler ends first has left `Approvals` by then, and its
      // message must go either way. Deleting each message here does not depend on the handler
      // ever seeing a decision.
      const requestsToDelete = this.deps.approvals
        .pendingIn(this.channelId, this.threadTs)
        .map((pending) => pending.messageTs);
      // The worker may hold the connect lock while the agent starts: cancelled first.
      await this.cancelTasks();
      // A daemon word may be starting the client on its own. Its start finishes, then the
      // reader it started is cancelled and its client closed below: no process outlives the
      // session, and none resumes the session id the next one will resume.
      const release = await this.connectLock.acquire();
      try {
        await this.cancelTasks();
      } finally {
        release();
      }
      this.deps.approvals.denyAll(this.channelId, this.threadTs);
      // Python's reader was cancelled inside a request it waited on, whose `finally` then ran:
      // here the denial above resolves each handler, and each is awaited to its end.
      await Promise.allSettled([...this.asking]);
      for (const messageTs of requestsToDelete) await this.deleteRequest(messageTs);
      // A turn the worker took but had not sent yet is in no queue.
      const taken = this.taken;
      this.taken = null;
      const unsent = taken === null ? [] : [taken];
      while (!this.queue.empty) unsent.push(this.queue.getNowait());
      await this.abandon(texts.fill(texts.ENDED, { reason }), {
        more: unsent,
        because: texts.BECAUSE_SHUTDOWN,
      });
      if (this.client !== null) {
        const client = this.client;
        this.client = null;
        await this.disconnect(client);
      }
    } finally {
      if (this.predecessor !== null) {
        // This object's own predecessor may still be mid-teardown: `ensureConnected` never ran
        // here (this session closed with no turn ever submitted), so nothing else has waited
        // on it yet. `doneClosing` must never fire before it does, or a session built after
        // this one could resume the same id while an earlier one is still exiting (the chain,
        // not just the direct predecessor, must be honoured).
        await this.predecessor.wait();
        this.predecessor = null;
      }
      // Only now, with every task cancelled: a round trip to the chat must not hold those up.
      await this.thread.close();
      // The last chance a change still only debounced gets, and the last chance of a reply
      // whose end failed and whose retry is still waiting: each reply once.
      let lost = false;
      const replies = new Set(this.unlanded);
      if (this.latest !== null) replies.add(this.latest);
      for (const reply of replies) {
        try {
          lost = !(await reply.settle()) || lost;
        } catch {
          lost = true;
        }
      }
      if (onlySkipped && !lost) {
        this.noteStatus("done");
        await this.thread.showStatus("done");
      } else if (cutShort || lost) {
        // Awaited, not `react`. Before `doneClosing`/`onClosed`, not after: those let the
        // manager evict this session and hand the same root to a freshly built one right away,
        // whose own first status strips every other one: this ❌ must already be on the root
        // before that race can even start. An answer that never reached the chat keeps the
        // persisted status (below), so the next start's repair still says so.
        if (!lost) this.noteStatus("error");
        await this.thread.showStatus("error");
      } else {
        // Issue #104: a session reads idle as soon as its turn ends, while its reader may
        // still be between the two calls that move ⏳ to ✅, and `cancelTasks` above stops it
        // there. No call when the root already reads as asked.
        await this.thread.settleStatus();
      }
      // Crash repair (issue #19): a graceful close is not what repair is for, whatever is
      // still live in the chat when it ends, so this thread's fields end up empty either way.
      // Best-effort: a failed write here must never skip `doneClosing`/`onClosed`.
      try {
        this.deps.state.clearRepair(this.channelId, this.threadTs, { keepOpen: lost });
        if (lost) {
          this.deps.state.setStatusPending(
            this.channelId,
            this.threadTs,
            STORED_STATUS.working,
            STORED_STATUS.error,
          );
        }
      } catch (error) {
        logger.warning(
          `could not clear state.json's crash-repair fields for ${this.where}: ${describe(error)}`,
        );
      }
      // Only now: the agent's process (if any) has had its chance to flush and exit, or
      // closing failed partway through and there is nothing left worth waiting for either way.
      this.doneClosing.set();
      this.onClosed?.();
    }
  }

  private async cancelTasks(): Promise<void> {
    // Every task is cancelled before any is awaited: a reader left running while the worker
    // stops could still end a turn and record its session after a rebind. `expiring`'s own
    // timers go too: none should outlive the session, and a stray one firing after would try
    // to post a closing through one that is already gone.
    const tasks = [this.worker, this.reader, this.expiry, this.idleExpiry, ...this.expiring].filter(
      (task): task is Task => task !== null,
    );
    for (const task of tasks) task.cancel();
    for (const task of tasks) await task.settled();
  }

  private async disconnect(client: AgentSession): Promise<void> {
    try {
      await client.close();
    } catch (error) {
      logger.warning(`could not close Claude Code in ${this.where}: ${describe(error)}`);
    }
  }

  private async connect(
    sessionId: string | null,
    effort: string | null,
    signal?: AbortSignal,
  ): Promise<AgentSession> {
    const valid = this.validEffort(effort);
    if (effort !== null && valid === null) {
      logger.warning(`dropped an unrecognized stored effort level in ${this.where}`);
    }
    const starting = this.deps.agent.start(
      {
        folder: this.directory,
        resume: sessionId,
        settingsSources: ["user", "project", "local"],
        model: null,
        // The model set with `/model` survives a resume by itself (measured); the effort
        // `/effort` set does not, so the daemon stores it per thread and passes it back here.
        effort: valid,
        permissionMode: null,
      },
      {
        permission: (request) => this.answering(request) as Promise<PermissionAnswer>,
        question: (request) => this.answering(request) as Promise<QuestionAnswer>,
      },
    );
    try {
      return await cancellable(starting, signal);
    } catch (error) {
      if (signal?.aborted) {
        // The start goes on by itself: a process nobody holds is closed when it is there.
        starting.then(
          (client) => this.disconnect(client),
          () => {},
        );
      }
      throw error;
    }
  }

  /** A request handler in flight, tracked so that a close can wait for it. */
  private answering(request: PermissionRequest | QuestionRequest): Promise<Decision> {
    const answer = this.askOwner(request);
    this.asking.add(answer);
    const forget = () => this.asking.delete(answer);
    answer.then(forget, forget);
    return answer;
  }

  private async work(signal: AbortSignal): Promise<void> {
    // Closed (the SessionGone branch of `ensureConnected`, most likely) ends the loop instead
    // of looping back to an empty queue nothing will ever fill again.
    while (!this.closedFlag) {
      const turn = await this.queue.get(signal);
      this.taken = turn;
      try {
        const client = await this.ensureConnected(signal);
        this.refreshUsage();
        if (!this.settled.isSet) await this.settled.wait(signal);
        // (sync) from the check to the send: a drain raises every flag before its first
        // await, so no turn is sent in between.
        if (this.draining) {
          this.taken = null;
          // A prompt genuinely dropped by the drain, reacting ❌; like the queued ones, it
          // gets no reply of its own, only a note.
          await this.drop([turn], texts.BECAUSE_RESTARTED, { error: true });
          this.idleTimerCheck(); // only cancels: draining itself blocks it from arming
          continue;
        }
        this.sent.push(turn);
        this.taken = null;
        await cancellable(client.send({ id: turn.uuid, content: turn.prompt }), signal);
        await turn.done.wait(signal);
      } catch (error) {
        if (error instanceof Cancelled) throw error;
        this.taken = null;
        if (
          error instanceof DirectoryUnavailable ||
          error instanceof SessionGone ||
          error instanceof SessionClosed
        ) {
          await this.fail(turn, error.message, { error: true });
          this.idleTimerCheck(); // a no-op if this also closed the session
        } else {
          // a failed turn must not stop this thread's queue
          logger.error(`turn failed in ${this.where}: ${describe(error)}`);
          const at = this.sent.indexOf(turn);
          if (at !== -1) this.sent.splice(at, 1);
          await this.fail(turn, texts.fill(texts.ERROR_REPLY, { error: describe(error) }), {
            error: true,
          });
          this.idleTimerCheck(); // the session stays alive; may need arming again
        }
      }
    }
  }

  /** Follow the client's events until they end; whatever ends them, release the thread. */
  private async read(client: AgentSession, signal: AbortSignal): Promise<void> {
    let reason = PROCESS_EXITED;
    const events = client.events[Symbol.asyncIterator]();
    try {
      for (;;) {
        const next = await cancellable(events.next(), signal);
        if (next.done) break;
        const event = next.value;
        if (event.type === "process_lost") {
          reason = event.reason;
          break;
        }
        try {
          await this.dispatch(event, signal);
        } catch (error) {
          if (error instanceof Cancelled) throw error;
          // one event that fails to render must not end it all
          logger.error(`could not render a message in ${this.where}: ${describe(error)}`);
        }
        // A task event or a turn's own end (through `dispatch`) may leave the session idle
        // again, or may end it: re-armed or cancelled here either way.
        this.idleTimerCheck();
      }
    } catch (error) {
      if (error instanceof Cancelled) {
        // Whoever cancelled the reader closes the client: the events it still held are dropped.
        events.return?.().catch(() => {});
        throw error;
      }
      reason = describe(error);
    }
    logger.error(`session reader stopped in ${this.where}: ${reason}`);
    if (this.client === client) this.client = null;
    try {
      await this.abandon(texts.fill(texts.ERROR_REPLY, { error: reason }), {
        error: true,
        because: texts.BECAUSE_STOPPED,
      });
    } finally {
      await client.close().catch(() => {});
    }
    // The session stays alive (not closed): the next turn reconnects. `abandon` settles it
    // from outside this loop's own per-event check above, so it needs its own call.
    this.idleTimerCheck();
  }

  /**
   * Whether `promptId` names a prompt this session sent and the agent took: it renders nothing
   * and starts no turn. One taken with no turn running comes right before the events of the
   * turn that prompt gets of its own. One that arrives inside a running turn means the agent
   * took the prompt into that turn, which then ends with one result for both (measured
   * 2026-10-06, CLI 2.1.286): `finish` releases it.
   */
  private acknowledge(promptId: string): boolean {
    const active = this.active;
    const sent = [...this.sent, ...(active?.turn ? [active.turn] : [])];
    const turn = sent.find((candidate) => candidate.uuid === promptId);
    if (turn === undefined) return false;
    if (active === null) this.announced = turn;
    else if (!active.taken.includes(turn)) active.taken.push(turn);
    return true;
  }

  private async dispatch(event: SessionEvent, signal?: AbortSignal): Promise<void> {
    switch (event.type) {
      case "limits_changed":
        this.deps.usage.invalidate();
        return;
      case "prompt_taken":
        this.acknowledge(event.promptId);
        return;
      case "effort_observed":
        // The Stop hook's input carries the level, absent when the model takes none.
        this.effort = event.level;
        this.effortReported = true;
        return;
      case "folder_changed":
        this.workingDirectory = event.folder;
        return;
      case "session_started":
        this.cliVersion = event.agentVersion;
        // Recorded as soon as the agent reports it, not only at the turn's end, so a
        // still-running first turn is already this session id's holder and `!resume` cannot
        // put a second process on the same transcript.
        if (event.sessionId !== null) {
          this.deps.state.setSession(this.channelId, this.threadTs, event.sessionId);
        }
        break;
      case "mode_changed":
        // Claude Code reports the permission mode after a change (measured after each
        // `set_permission_mode`): the mode shown follows what it says.
        this.mode = event.mode;
        break;
      case "compaction_started":
      case "compaction_ended": {
        // A compaction opens with a status the agent repeats while it lasts, and ends 14 to 37
        // seconds later in the recordings (2026-10-08, CLI 2.1.292). The back end says each
        // once.
        const compacting = event.type === "compaction_started";
        if (compacting !== this.compacting) {
          this.compacting = compacting;
          this.showThreadStatus();
        }
        break;
      }
      default:
        break;
    }
    const task = isTaskEvent(event) ? event : null;
    if (task !== null) {
      const inner = this.nestingReply(task);
      if (inner !== null) {
        // A task of a call inside another call (a long command a subagent runs) is that call's
        // root's work: the agent reports it to the subagent, so no turn follows it. None of
        // what a task of the conversation sets up (its record, its end line, a wait for its
        // report, a held event) applies; the reply keeps the event off its cards.
        await inner.feed(task);
        return;
      }
    }
    if (event.type === "task_started") this.recordTask(event);
    const stopped = task !== null && this.stopped.has(task.taskId);
    if (event.type === "task_ended" && this.active === null && this.sent.length === 0 && !stopped) {
      this.ended.push([event.taskId, this.endedLine(event)]);
    }
    if (
      event.type === "task_updated" &&
      event.terminal &&
      !this.unreported.has(event.taskId) &&
      !stopped
    ) {
      this.unreported.set(event.taskId, this.deps.clock.time());
      // The CLI can suppress the notification altogether (SDK TaskUpdatedMessage docstring);
      // nothing else rechecks this reply once the wait that holds it passes, so this
      // schedules that recheck itself.
      const taskId = event.taskId;
      const expiry: Task = new Task((waiting) => this.expireUnreported(taskId, waiting));
      this.expiring.add(expiry);
      expiry.settled().then(() => this.expiring.delete(expiry));
    }
    if (event.type === "task_ended") {
      if (this.active === null && !stopped) this.notificationWaits = true;
      this.unreported.delete(event.taskId);
      this.stopped.delete(event.taskId);
      // Read above for the end line, which comes after the terminal task_updated (recorded
      // order); a task whose notification never comes goes with the process.
      this.tasks.delete(event.taskId);
    }
    const holder = task === null ? undefined : this.taskReplies.get(task.taskId);
    if (task !== null && holder !== undefined) {
      // A task that outlived its turn shows only on its own line, wherever it started.
      await holder.feed(task);
      this.showRunning();
      if (this.active === null && event.type === "task_ended" && !stopped) this.notified();
      // The task that just ended may have been the last thing keeping this reply's stream
      // open; `notified` above, if it fired, already re-armed the wait.
      const owed = this.stillOwed(holder) || this.injectedExpected || !this.settled.isSet;
      // A task promoted out of a subagent's call sits here while its turn still runs: that
      // turn's own `closeReply` ends the reply, with the footer, as `sweepClosedOut` also
      // leaves it to.
      const writing = this.active?.renderer ?? null;
      if (!owed && holder !== writing) {
        this.trackLanding(await holder.closeOut(signal), this.reply(holder));
        this.showThreadStatus(); // its footer may be what counts the tasks now
        // `!stop` already reacted ✅ itself; skipped so a stopped task's end does not show it
        // a second time.
        if (!stopped) await this.reactDoneIfIdle(signal);
      }
      return;
    }
    const call = callOf(event);
    const origin = call ? this.originOf(call) : null;
    if (origin !== null) {
      if (event.type === "agent_error") {
        // A subagent's failed request: the category only, as for the turn's own below.
        logger.warning(
          `Claude Code reported a subagent's error in ${this.where}: ${event.category}`,
        );
      }
      await origin.feed(event);
      this.adoptPromoted(origin);
      if (event.type === "task_started") {
        this.taskReplies.set(event.taskId, origin);
        this.showRunning();
      }
      return;
    }
    if (this.active === null) {
      // A task started by no call, while an owner prompt is sent and no report turn is
      // expected, is that prompt's: a skill with `context: fork` typed as a command (`!review`
      // → `/review`) runs its agent before the turn's first message (recorded:
      // `skill-fork-command.jsonl`, CLI 2.1.283, the only recording with no call id). Its
      // turn starts now, so its line shows while it works.
      const startsOwnerTurn =
        event.type === "task_started" &&
        event.callId === null &&
        this.sent.length > 0 &&
        !this.injectedExpected;
      // Whether the turn opens with its own words: the case in which a prompt's replay is
      // known to come first (`whoseTurn`).
      let worded = false;
      if (!startsOwnerTurn) {
        if (task !== null) {
          this.held.push(task);
          if (event.type === "task_ended" && !stopped) this.notified();
          return;
        }
        // A compaction comes before every event of its turn that shows something, on
        // `/compact` and on an automatic compaction at a turn's start (recorded:
        // `compact.jsonl`, `auto-compact.jsonl`, CLI 2.1.292). The turn starts when the
        // compaction does, so a report turn that compacts first is not given up on after
        // `INJECTED_TURN_WAIT`, and at the latest with the boundary, whose line the reply
        // shows. Only a turn that is due starts so: with no prompt sent and no report awaited,
        // either event would open a reply that no result is known to end.
        const compaction =
          (event.type === "compacted" || event.type === "compaction_started") &&
          (this.sent.length > 0 || this.injectedExpected);
        if (!TURN_EVENTS.has(event.type) && !compaction) return;
        worded = !compaction;
        if (call) {
          // A subagent's own event, whose call no reply holds: an agent continued with
          // `SendMessage` in a daemon that never saw the call that first started it (recorded:
          // `report-turn-agent-resume.jsonl`, CLI 2.1.286, whose events name that first call,
          // not the `SendMessage` one). It is no turn of the conversation's, so it starts none
          // and takes no owner prompt's.
          return;
        }
      }
      await this.startTurn(worded);
      // A report turn has no prompt behind it: only now does `threadLine` read it.
      this.showThreadStatus();
    }
    const active = this.active;
    if (active === null) return; // abandoned while the turn's opening was written
    if (event.type === "agent_error") {
      // The category only: the words Claude Code wrote about it go to the thread.
      logger.warning(`Claude Code reported an error in ${this.where}: ${event.category}`);
    }
    await active.renderer.feed(event);
    this.adoptPromoted(active.renderer);
    if (event.type === "turn_ended") {
      // The turn stays active until its reply is closed: the footer is read first, and a
      // drain that saw this thread idle meanwhile would exit before the reply's end, leaving
      // its stream open (#25).
      let turnStopped = false;
      try {
        turnStopped = await this.finish(active, event, signal);
      } finally {
        this.active = null;
        this.compacting = false;
        this.showThreadStatus(); // whichever way it ended, `!stop` included
      }
      // Checked only now, with the turn no longer active: `finish` alone still reads busy.
      if (!turnStopped) await this.reactDoneIfIdle(signal);
    }
  }

  private recordTask(event: TaskStarted): void {
    this.tasks.set(event.taskId, [event.taskType ?? "", event.description]);
    if (this.mayHaveOrderedRestart) this.afterRestart.add(event.taskId);
    const kept = this.deps.tasksKept ?? TASKS_KEPT;
    for (const taskId of this.tasks.keys()) {
      if (this.tasks.size <= kept) break;
      this.tasks.delete(taskId);
    }
  }

  /**
   * Tasks that just outlived the nested call that started them are ordinary background tasks
   * from now on: recorded and counted as one that outlived its turn.
   */
  private adoptPromoted(renderer: TurnRenderer): void {
    const promoted = renderer.takePromoted();
    for (const started of promoted) {
      this.recordTask(started);
      this.taskReplies.set(started.taskId, renderer);
    }
    if (promoted.length > 0) this.showRunning();
  }

  /**
   * The terminal's line for a task's end: a command's own summary, or `Agent "..." finished`
   * from the task's description, plus the duration when the task reports one.
   */
  private endedLine(event: TaskEnded): string {
    const [taskType, description] = this.tasks.get(event.taskId) ?? ["", ""];
    let text: string;
    if (SUMMARY_IS_END_LINE.has(taskType) && event.summary) {
      text = oneLine(event.summary, 200);
    } else {
      const origin = this.taskReplies.get(event.taskId);
      const label = description || (origin?.taskTitle(event.taskId) ?? null);
      const name = (TASK_KINDS[taskType] ?? UNKNOWN_KIND)[1];
      const outcome = event.status === "completed" ? "finished" : event.status;
      text = `${name} "${oneLine(label || event.taskId, 120)}" ${outcome}`;
    }
    return endedLine(text, event.status, event.durationMs);
  }

  /**
   * What opens a report of the agent's own turn (the end of each task it reports), and the
   * reply that started the first of those tasks (the report renders there), if it is still
   * tracked. Null when it is not (a restart or an idle close dropped it, or it already ended: a
   * notification later than INJECTED_TURN_WAIT let `expireUnreported` end it first): the report
   * then gets a reply of its own, as it always has, rather than an edit of a reply that ended
   * and will not notify again.
   */
  private openingTarget(): readonly [text: string, target: TurnRenderer | null] {
    const lines = this.ended;
    this.ended = [];
    const first = lines[0];
    let target = first === undefined ? null : (this.taskReplies.get(first[0]) ?? null);
    if (target?.closedOut) target = null;
    const text = lines.map(([, line]) => line).join("\n") || texts.BACKGROUND_NOTICE;
    return [text, target];
  }

  /**
   * A task ended while no turn runs. The agent starts a turn to report it, unless an owner
   * prompt already sits in its queue: that turn comes first and carries the report (measured
   * 2026-09-24, Claude Code 2.1.280), and no turn of its own follows.
   */
  private notified(): void {
    if (this.sent.length === 0) this.expectInjectedTurn();
  }

  private expectInjectedTurn(): void {
    this.injectedExpected = true;
    this.settled.clear();
    if (this.expiry === null || this.expiry.done) {
      this.expiry = new Task((signal) => this.expireInjectedTurn(signal));
    }
  }

  private async expireInjectedTurn(signal: AbortSignal): Promise<void> {
    await this.deps.clock.sleep(INJECTED_TURN_WAIT, signal);
    if (!this.injectedExpected || this.active !== null) return;
    logger.warning(`no turn followed a task notification in ${this.where}`);
    this.injectedExpected = false;
    const held = this.held;
    this.held = [];
    const [text, target] = this.openingTarget();
    try {
      if (held.length > 0) {
        const renderer = target ?? this.newRenderer(this.newReply());
        await this.standalone(held, text, renderer, signal);
      }
    } catch (error) {
      if (error instanceof Cancelled) throw error;
      logger.warning(`could not post a background update in ${this.where}: ${describe(error)}`);
    } finally {
      // A notification that arrived while this wrote is expected on a wait of its own, armed
      // below: `expectInjectedTurn` found this task still running and started none, and it
      // cleared `settled`, which stays so. (`startTurn` cancelling this task leaves
      // `injectedExpected` false.)
      if (!this.injectedExpected) this.settled.set();
      try {
        // Catches `target` (nothing more coming for it either, when `held` was empty) and any
        // other reply a joint report turn left stranded, since a report only ever renders into
        // the first of the tasks it covers.
        await this.sweepClosedOut(live(signal));
        await this.reactDoneIfIdle(live(signal));
      } finally {
        // (sync) Whatever the sweep did: a notification that arrived during its own writes is
        // waiting on a timer this task alone can arm, since nothing awaits between here and its
        // end. None once the session is closed: `close` has cancelled this task and a timer
        // made now would outlive `cancelTasks`.
        if (this.injectedExpected && !this.closedFlag) {
          this.expiry = new Task((waiting) => this.expireInjectedTurn(waiting));
        }
        // Runs outside `read`'s loop (its own INJECTED_TURN_WAIT timer), so nothing else
        // re-checks idleness for this transition: it may need to arm the timer itself.
        this.idleTimerCheck();
      }
    }
  }

  /**
   * Show `state` on the thread's root, asked for at once and never awaited: a status must
   * never delay a turn, and the chat makes the changes in the order asked and swallows their
   * failures. `errorStanding` follows the state asked for last, set here synchronously, since
   * what the chat shows changes only once it has answered.
   */
  private react(state: SessionStatus): void {
    this.errorStanding = state === "error";
    this.noteStatus(state);
    this.showThreadStatus();
    this.thread.showStatus(state).catch(() => {});
  }

  /**
   * Crash repair (issue #19): the root's status while it is ⏳ or ✋, cleared once ✅ or ❌ is
   * requested; that one is kept as the thread's ended status, which the session index shows.
   * Called from `react` and from the two other places that ask the chat for a status directly
   * (`close`'s ❌, `reactDoneIfIdle`'s ✅).
   */
  private noteStatus(state: SessionStatus): void {
    const pending = state === "working" || state === "waiting";
    this.deps.state.setStatusPending(
      this.channelId,
      this.threadTs,
      pending ? STORED_STATUS[state] : null,
      pending ? null : STORED_STATUS[state],
    );
  }

  /**
   * ❌ is shown while the persisted ⏳ or ✋ stays for crash repair (an answer never reached the
   * chat): kept as the ended status, which is what the session index shows.
   */
  private noteCrossOverStatus(): void {
    const stored = this.deps.state.thread(this.channelId, this.threadTs);
    if (stored !== null) {
      this.deps.state.setStatusPending(
        this.channelId,
        this.threadTs,
        stored.status,
        STORED_STATUS.error,
      );
    }
  }

  /**
   * ❌. It stands until another state is asked for: `react` records it in `errorStanding` at
   * once, before the chat answers, so a quick turn's own idle sweep cannot mistake this session
   * for done.
   */
  private reactError(): void {
    this.react("error");
  }

  /**
   * Back to waiting, or working, right after an approval or question settles: whichever the
   * thread still holds, since a parallel tool call can leave another one open.
   */
  private reactWaitingOrWorking(): void {
    this.react(this.waitingForOwner ? "waiting" : "working");
  }

  /**
   * ✅, once the reply that just ended turns out to have been the last thing the session owed:
   * awaited, unlike `react`, so the checkmark never shows before the reply's end has landed. A
   * no-op while another prompt, a running task or an unreported one still keeps the session
   * going, or while `errorStanding` says a ❌ from `abandon` already stands: that lasts until
   * new work starts (a submit or a report turn shows ⏳ again, both clearing it), not until
   * some unrelated task's own sweep decides the session reads idle again.
   */
  private async reactDoneIfIdle(signal?: AbortSignal): Promise<void> {
    this.showThreadStatus();
    if (!this.waitingForOwner && this.idle && !this.errorStanding && this.unlanded.size === 0) {
      this.noteStatus("done");
      await this.thread.showStatus("done", signal);
      return;
    }
    logger.info(`no done reaction in ${this.where}, held by: ${this.doneHeldBy().join(", ")}`);
  }

  /**
   * What keeps `reactDoneIfIdle` from ✅ right now, as flag names and counts, never content:
   * every condition it and `quiet` read, so a root left on ⏳ can be traced to one of them from
   * the log.
   */
  private doneHeldBy(): string[] {
    const counts: ReadonlyArray<readonly [string, number]> = [
      ["waiting_for_owner", this.waiting.size],
      ["sent", this.sent.length],
      ["pending_submits", this.pendingSubmits],
      ["queued", this.queue.size],
      ["unlanded", this.unlanded.size],
    ];
    const flags: ReadonlyArray<readonly [string, boolean]> = [
      ["active", this.active !== null],
      ["taken", this.taken !== null],
      ["unsettled", !this.settled.isSet],
      ["error_standing", this.errorStanding],
    ];
    const held = counts.filter(([, count]) => count > 0).map(([name, count]) => `${name}=${count}`);
    held.push(...flags.filter(([, on]) => on).map(([name]) => name));
    const running = this.runningKindsOf();
    if (running) held.push(`running=${running}`);
    return held;
  }

  /**
   * A reply whose end failed is not done: no ✅ until its one retry lands, ❌ if that fails
   * too. The persisted status stays as it is meanwhile (and after a final failure), so a crash
   * still gets the repair's notice.
   */
  private trackLanding(landed: boolean, reply: Reply): void {
    if (landed) return;
    this.unlanded.add(reply);
    if (this.closedFlag) return; // `close` settles every unlanded reply itself
    this.awaitLanding(reply).catch(() => {});
  }

  private async awaitLanding(reply: Reply): Promise<void> {
    const landed = await reply.waitLanded();
    this.unlanded.delete(reply);
    if (this.closedFlag) return;
    if (landed) {
      await this.reactDoneIfIdle();
    } else {
      // ❌ shown, but `state.json` keeps ⏳ or ✋: the answer never reached the chat.
      this.errorStanding = true;
      this.noteCrossOverStatus();
      this.thread.showStatus("error").catch(() => {});
    }
  }

  /**
   * (Re)arm the idle-close timer when the session is now idle with no approval or question
   * pending; cancel it otherwise. Cheap and idempotent, so every place that could change either
   * just calls it again.
   */
  private idleTimerCheck(): void {
    if (this.idleExpiry !== null) {
      this.idleExpiry.cancel();
      this.idleExpiry = null;
    }
    if (this.closedFlag || this.draining || this.waitingForOwner || !this.idle) return;
    this.idleExpiry = new Task((signal) => this.closeWhenIdle(signal));
  }

  /**
   * Close an agent process nothing has needed for IDLE_CLOSE_SECONDS. Silent: idle means
   * nothing is running, sent or queued for `close()` to end with a line.
   */
  private async closeWhenIdle(signal: AbortSignal): Promise<void> {
    await this.deps.clock.sleep(IDLE_CLOSE_SECONDS, signal);
    if (this.closedFlag || this.draining || this.waitingForOwner || !this.idle) {
      return; // something started again before the hour was up
    }
    logger.info(`closing an idle session in ${this.where}`);
    // Cleared first: `close()` cancels its tasks through `cancelTasks`, and a task cannot
    // await itself.
    this.idleExpiry = null;
    await this.close(texts.ENDED_IDLE);
  }

  /**
   * The prompt the turn that starts now answers, taken out of `sent`; null for a turn the
   * agent starts itself, to report a task.
   *
   * Claude Code replays a prompt of plain text after its turn's `init` and before the turn's
   * first words, and replays nothing before a report turn: so in every turn of the fixtures
   * `prompt-replay-*`, `compact` and `auto-compact*` (CLI 2.1.286 and 2.1.292). A replay
   * therefore names the turn's prompt, whatever was awaited. And when a task's notification is
   * waiting to be reported, a turn that opens with words (`worded`) and no replay is that
   * report, not the plain prompt that waits beside it.
   *
   * Everywhere else the answer is the older guess, which `settle` corrects at the turn's end.
   * No recording covers those turns: one that opens with a compaction starts before its
   * replay, a command (`/compact`) is not replayed as a prompt is, and a prompt with an image,
   * a turn that fails before its first word (a logged-out CLI, a limit reached) and a turn with
   * no notification waiting were never recorded without their replay.
   */
  private whoseTurn(worded: boolean): Turn | null {
    const announced = this.announced;
    this.announced = null;
    if (announced !== null && this.sent.includes(announced)) {
      this.sent.splice(this.sent.indexOf(announced), 1);
      this.reportDue = this.injectedExpected;
      return announced;
    }
    const first = this.sent[0];
    if (this.injectedExpected || first === undefined) return null;
    const head = first.prompt;
    const plain = typeof head === "string" && !head.trimStart().startsWith("/");
    if (worded && this.notificationWaits && plain) return null;
    this.sent.shift();
    return first;
  }

  private async startTurn(worded: boolean): Promise<ActiveTurn> {
    // Skipped while `stop()` is still winding an interrupt down (`finish` clears the flag once
    // that very turn's own end says so): this can be that turn's own trailing events, not a
    // new one, and `stop()`'s own ✅ must stand.
    if (!this.interrupting) {
      this.errorStanding = false; // a turn is sent, or a report turn starts: new work
      // A background task's own report turn can start while a hold waits on this very
      // session; ✋ must stand through it, not be overwritten by ⏳ (`waitingForOwner` covers a
      // hold the same way it covers an open approval or question).
      this.react(this.waitingForOwner ? "waiting" : "working");
    }
    const turn = this.whoseTurn(worded);
    this.injectedExpected = false;
    this.expiry?.cancel();
    let renderer: TurnRenderer;
    let opening: string | null = null;
    if (turn === null) {
      this.notificationWaits = false;
      // A report turn renders into the reply that started the task it reports, so no new
      // message follows for it; only when that reply is no longer tracked does it get one of
      // its own, as every reply always has.
      const [text, target] = this.openingTarget();
      renderer = target ?? this.newRenderer(this.newReply());
      opening = text;
    } else {
      renderer = this.newRenderer(turn.sink);
    }
    // (sync) The turn is the active one before its first write. Python set it once this method
    // returned and nothing ran in between; here every `await` below yields, and a turn already
    // out of `sent` and not yet active would read as idle to a drain, a `!stop` or a timer.
    const active: ActiveTurn = { turn, renderer, taken: [] };
    this.active = active;
    if (opening !== null) await renderer.feedNotice(opening);
    if (this.notice !== null) {
      const notice = this.notice;
      this.notice = null;
      await renderer.feedNotice(notice);
    }
    const held = this.held;
    this.held = [];
    for (const event of held) await renderer.feed(event);
    return active;
  }

  /**
   * The turn the CLI never started to report an ended task, on its own reply (usually the one
   * that started the task, `renderer`; `expireInjectedTurn` resolves it).
   */
  private async standalone(
    events: readonly TaskEvent[],
    text: string,
    renderer: TurnRenderer,
    signal?: AbortSignal,
  ): Promise<void> {
    await renderer.feedNotice(text);
    for (const event of events) await renderer.feed(event);
    await this.closeReply(renderer, null, { signal });
  }

  private async closeReply(
    renderer: TurnRenderer,
    footer: FooterFields | null,
    options: { readonly force?: boolean; readonly signal?: AbortSignal } = {},
  ): Promise<void> {
    for (const taskId of renderer.runningTasks) this.taskReplies.set(taskId, renderer);
    // Forgetting one whose reply has not ended yet would strand it exactly as a joint report
    // turn can: `stillOwed`, and the sweep that acts on it, both read this map.
    const ended = [...this.taskReplies]
      .filter(([taskId, reply]) => !reply.runningTasks.includes(taskId) && reply.closedOut)
      .map(([taskId]) => taskId);
    const kept = this.deps.taskRepliesKept ?? TASK_REPLIES_KEPT;
    for (const taskId of ended.slice(0, Math.max(0, this.taskReplies.size - kept))) {
      this.taskReplies.delete(taskId);
    }
    // Before the close, so the reply's last write already carries the list.
    this.showRunning();
    await renderer.close(footer, options.signal);
    // The reply ends at once unless a task this renderer started outlives this very turn;
    // `force` is a stop, an error or a restart, which never waits for one.
    if (options.force || !this.stillOwed(renderer)) {
      this.trackLanding(await renderer.closeOut(options.signal), this.reply(renderer));
    }
  }

  /**
   * The agent's process is going away with its tasks: no reply keeps showing one, and none is
   * left waiting on an end that will now never come. Each ends at once, with whatever footer
   * its own turn already decided.
   */
  private async stopTaskReplies(): Promise<void> {
    const renderers = new Set(this.taskReplies.values());
    this.taskReplies.clear();
    this.tasks.clear();
    this.ended = [];
    this.unreported.clear();
    this.notificationWaits = false;
    this.stopped.clear();
    for (const renderer of renderers) {
      await renderer.stopRunning().catch(() => {});
    }
    // Before closeOut: the latest reply's own running counts must already read empty, or its
    // end would still show a stale `⏳ 1 shell`.
    this.showRunning();
    for (const renderer of renderers) {
      try {
        this.trackLanding(await renderer.closeOut(), this.reply(renderer));
      } catch {
        // the others still end
      }
    }
    // The cards `stopRunning` just ended, and the running counts above, only debounce: a
    // caller closing everything down cannot wait a whole debounce for them. The latest reply
    // may not itself be one of `renderers` (its own turn need not have started any task).
    const replies = [...renderers].map((renderer) => this.reply(renderer));
    if (this.latest !== null) replies.push(this.latest);
    for (const reply of replies) await reply.settle().catch(() => {});
  }

  /**
   * The reply that holds the root of the call this task event's task belongs to, when a call
   * inside another call started it. Null when no tracked reply can tell (a restart dropped
   * it): the task is then treated as any other, since a top-level task of a call no reply holds
   * looks the same.
   */
  private nestingReply(event: TaskEvent): TurnRenderer | null {
    const replies = new Set(this.taskReplies.values());
    if (this.active !== null) replies.add(this.active.renderer);
    for (const reply of replies) if (reply.nests(event)) return reply;
    return null;
  }

  private originOf(callId: string): TurnRenderer | null {
    for (const reply of this.taskReplies.values()) if (reply.owns(callId)) return reply;
    return null;
  }

  /**
   * Whether one of `renderer`'s own tasks still keeps its reply's stream open: one still runs,
   * or ended less than INJECTED_TURN_WAIT ago with no notification yet (its report, if any, not
   * in yet). The CLI can suppress the notification altogether (SDK TaskUpdatedMessage
   * docstring), so past that wait this stops counting it: `expireUnreported` rechecks then,
   * since nothing else would.
   */
  private stillOwed(renderer: TurnRenderer): boolean {
    if (renderer.runningTasks.length > 0) return true;
    const now = this.deps.clock.time();
    for (const [taskId, ended] of this.unreported) {
      if (now - ended < INJECTED_TURN_WAIT && this.taskReplies.get(taskId) === renderer) {
        return true;
      }
    }
    return false;
  }

  /**
   * A report turn's opening names only the reply of the first task it covers when several end
   * together, so every other reply whose own tasks also finished is checked here instead, since
   * nothing else rechecks it once the wait that deferred it lifts. Only while nothing is still
   * expected or running session-wide: `closeReply` already handles the renderer whose own turn
   * or report just ended, and a currently active one is skipped outright, whatever it reads:
   * its own turn has not closed it yet, so nothing here is its call to make.
   */
  private async sweepClosedOut(signal?: AbortSignal): Promise<void> {
    const active = this.active?.renderer ?? null;
    for (const renderer of new Set(this.taskReplies.values())) {
      if (this.injectedExpected || !this.settled.isSet) {
        return; // a notification arrived meanwhile: its report may render into this reply
      }
      if (renderer !== active && !this.stillOwed(renderer)) {
        this.trackLanding(await renderer.closeOut(signal), this.reply(renderer));
      }
    }
  }

  /**
   * Give up waiting on a task's notification after INJECTED_TURN_WAIT (the CLI can suppress
   * it) and sweep for whatever that frees.
   */
  private async expireUnreported(taskId: string, signal: AbortSignal): Promise<void> {
    await this.deps.clock.sleep(INJECTED_TURN_WAIT, signal);
    this.unreported.delete(taskId);
    await this.sweepClosedOut(signal);
    await this.reactDoneIfIdle(signal);
  }

  /** `⏳ 1 shell · 2 agents`: the tasks that outlived their turn and still run. */
  private runningCounts(): string {
    const kinds = this.runningKindsOf();
    return kinds ? texts.fill(texts.RUNNING, { counts: kinds }) : "";
  }

  /** The tasks still running: those that outlived their turn, and the running turn's. */
  private runningTaskIds(): string[] {
    const outlived = [...this.taskReplies]
      .filter(([taskId, reply]) => reply.runningTasks.includes(taskId))
      .map(([taskId]) => taskId);
    const current = this.active?.renderer.runningTasks ?? [];
    return [...new Set([...outlived, ...current])];
  }

  /**
   * `1 shell · 2 agents`, or empty when no task outlived its turn. `awaitedOnly` leaves out
   * the tasks a stop does not wait for (`mayHaveOrderedRestart`).
   */
  private runningKindsOf(options: { readonly awaitedOnly?: boolean } = {}): string {
    const counts = new Map<string, number>();
    for (const [taskId, renderer] of this.taskReplies) {
      if (options.awaitedOnly && this.afterRestart.has(taskId)) continue;
      if (renderer.runningTasks.includes(taskId)) {
        const kind = (TASK_KINDS[this.tasks.get(taskId)?.[0] ?? ""] ?? UNKNOWN_KIND)[0];
        counts.set(kind, (counts.get(kind) ?? 0) + 1);
      }
    }
    return [...counts].map(([kind, n]) => `${n} ${kind}${n > 1 ? "s" : ""}`).join(" · ");
  }

  private showRunning(): void {
    this.showThreadStatus(); // the thread's activity line counts them too, once the turn ended
    this.latest?.setRunning(this.runningCounts());
  }

  private newRenderer(reply: Reply): TurnRenderer {
    const renderer = new TurnRenderer(reply, this.directory);
    this.replyOf.set(renderer, reply);
    return renderer;
  }

  /** The reply `renderer` writes: a renderer of this session is always made by `newRenderer`. */
  private reply(renderer: TurnRenderer): Reply {
    const reply = this.replyOf.get(renderer);
    if (reply === undefined) throw new Error("a renderer this session did not make");
    return reply;
  }

  /**
   * A new reply, which becomes this thread's latest and takes over the running counts. It
   * writes nothing until the agent has something to show. (sync) Python awaited calls here
   * that never waited; nothing may run between the reply being made and the counts moving.
   */
  private newReply(): Reply {
    const reply = this.thread.openReply((old, fresh) =>
      this.deps.state.replaceOpenReply(this.channelId, this.threadTs, old, fresh),
    );
    const previous = this.latest;
    this.latest = reply;
    reply.setRunning(this.runningCounts());
    if (previous !== null) {
      previous.setRunning("");
      previous.setLatest(false);
    }
    return reply;
  }

  /**
   * `newReply`, as the one step of `submit` that Python awaited. It does not wait; a test
   * replaces it with one that does, to hold a prompt between its arrival and its queue.
   */
  private sink(): Reply | Promise<Reply> {
    return this.newReply();
  }

  /**
   * Make `reply` the thread's latest reply again, taking over the footer and the running
   * counts, when the reply that was the latest will never be written.
   */
  private handLatestTo(reply: Reply): void {
    if (this.latest === reply) return;
    const previous = this.latest;
    this.latest = reply;
    reply.setRunning(this.runningCounts());
    reply.setLatest(true);
    if (previous !== null) {
      previous.setRunning("");
      previous.setLatest(false);
    }
  }

  /**
   * Close the turn's reply and release it; true when `!stop` cut it short, which the caller
   * reads once the turn is no longer active (it still is, for `closeReply`'s own checks), to
   * skip the ✅, since `stop()` already showed its own.
   */
  private async finish(
    active: ActiveTurn,
    result: TurnEnded,
    signal?: AbortSignal,
  ): Promise<boolean> {
    let stopped = false;
    const injected = result.startedBy === "agent";
    // The agent ends a turn it took prompts into with one result of its own for all of them.
    const released = injected ? active.taken : [];
    try {
      if (result.sessionId) {
        this.deps.state.setSession(this.channelId, this.threadTs, result.sessionId);
      }
      const [changed, effort] = effortChange(result.finalText ?? "");
      if (changed) {
        this.effort = effort;
        this.effortReported = true;
        // Stored so the next connect can pass it back (the model set with `/model` survives a
        // resume by itself; effort does not). `effortChange`'s own null means "unknown" (a
        // `/model` change with no effort in its output), not "back to default": the stored
        // override, if any, is left alone. "auto" is the CLI's own word for the default: that
        // IS an explicit reset, stored as none.
        if (effort !== null) {
          this.deps.state.setEffort(
            this.channelId,
            this.threadTs,
            effort === "auto" ? null : effort,
          );
        }
      }
      // Kept for `!status`, even when this result reports none (`/usage`, `/clear`): the two
      // lines never disagree.
      this.sessionTokens = sessionTokens(result.tokens);
      let footer: FooterFields | null;
      try {
        footer = await cancellable(this.footer(this.sessionTokens), signal);
      } catch (error) {
        if (error instanceof Cancelled) throw error;
        // the reply still ends, with no footer
        logger.warning(`could not build the footer in ${this.where}: ${describe(error)}`);
        footer = null;
      }
      stopped = result.ending === "interrupted";
      this.interrupting = false; // this turn's own tail, whichever way it ended
      if (active.turn === null && !injected && this.sent.length > 0) {
        // An owner prompt crossed a notification: this reply carries its answer, and `settle`
        // discards the owner turn's own reply, which is the latest now. The footer belongs
        // under the answer.
        this.handLatestTo(this.reply(active.renderer));
      }
      if (released.length > 0) this.endNotes.push(takenNote(released.length));
      // What a restart dropped meanwhile is said where the running turn ends.
      await this.feedEndNotes(active.renderer);
      // `!stop` cut this turn short, the owner's own or a report's: its reply ends like any
      // other, the footer on its stream's stop, and never waits on a task it may still owe
      // (`force`: `!stop` never does).
      await this.closeReply(active.renderer, footer, { force: stopped, signal });
    } finally {
      this.settle(active.turn, result, released);
      // A report turn's own reply is `closeReply`'s concern above; this catches every other
      // reply a joint one left stranded.
      await this.sweepClosedOut(live(signal));
      this.stopTailEnded();
    }
    return stopped;
  }

  /**
   * Release whoever waits on this turn. The result says whose turn it really was: when an
   * owner prompt and a task notification cross, the guess made at the turn's start can be
   * wrong; this puts the queue back in order (that one reply carries the other's text).
   * `released`: prompts the agent took into this turn; no result of their own follows.
   */
  private settle(turn: Turn | null, result: TurnEnded, released: readonly Turn[]): void {
    const injected = result.startedBy === "agent";
    // Read once and lowered: only the turn it was raised for may await the report again.
    const reportDue = this.reportDue;
    this.reportDue = false;
    for (const taken of released) {
      const at = this.sent.indexOf(taken);
      if (at !== -1) this.sent.splice(at, 1);
      taken.done.set();
    }
    const waiting = this.sent[0];
    if (turn === null && !injected && waiting !== undefined) {
      logger.warning(`an owner reply in ${this.where} went to a background reply`);
      // Only misrouted, not failed, so no ❌. Its answer is already in the reply that was
      // guessed to be the report: the owner turn's own reply is never written.
      this.sent.shift();
      waiting.done.set();
      this.expectInjectedTurn();
    } else if (turn !== null && injected && !released.includes(turn)) {
      logger.warning(`a background reply in ${this.where} went to an owner reply`);
      // That reply is spent: the owner's own turn gets a fresh one.
      turn.sink = this.newReply();
      this.sent.unshift(turn);
      this.settled.set();
    } else if (turn !== null && !injected) {
      turn.done.set();
      if (reportDue) {
        // The report that was awaited when this prompt's turn started is still to come.
        this.expectInjectedTurn();
      }
    } else {
      this.settled.set();
    }
  }

  /**
   * The client is gone: end the reply that was open with `line`, and release whoever waits on
   * it, on the turns sent and on `more` (queued ones, when the session closes). Those get no
   * reply of their own: one note says they were not sent, `because` of what. With no turn
   * active, an `error` says `line` in each reply that still waited for a task, and says nothing
   * at all, the cross included, when nothing ran and nothing waited.
   */
  private async abandon(
    line: string,
    options: {
      readonly error?: boolean;
      readonly more?: readonly Turn[];
      readonly because?: string;
    } = {},
  ): Promise<void> {
    const { error = false, more = [], because = texts.BECAUSE_STOPPED } = options;
    // (sync) The turn, the sent prompts and every flag of a turn in progress are taken before
    // the first write: whatever reads the session meanwhile sees it with no turn.
    const active = this.active;
    this.active = null;
    const dropped = [...this.sent, ...more];
    this.sent = [];
    const waiting = [...(active?.turn ? [active.turn] : []), ...dropped];
    this.injectedExpected = false;
    this.announced = null;
    this.reportDue = false;
    this.notificationWaits = false;
    this.interrupting = false; // whatever it was waiting on, this ends it
    this.compacting = false; // the process that was compacting is gone
    this.showThreadStatus();
    // The replies still waiting for a task of theirs, when no turn is active: a process lost
    // then leaves them the only place that can say so (issue #202). `taskReplies` also keeps
    // replies whose tasks all ended (TASK_REPLIES_KEPT): only one that has not closed still
    // waits.
    const waitingReplies = new Set<TurnRenderer>();
    if (active === null) {
      for (const reply of this.taskReplies.values()) {
        if (!reply.closedOut) waitingReplies.add(reply);
      }
    }
    const owed =
      active !== null || dropped.length > 0 || this.endNotes.length > 0 || waitingReplies.size > 0;
    if (error && owed) {
      // Not for a session that was idle with nothing running: its last reply ended well,
      // nothing is lost, and the next message connects a new process.
      this.reactError();
    }
    try {
      if (error) {
        for (const renderer of waitingReplies) await renderer.feedEnding(line).catch(() => {});
      }
      if (active !== null) {
        try {
          await active.renderer.feedEnding(line);
          if (dropped.length > 0) {
            this.handLatestTo(this.reply(active.renderer));
            await active.renderer.feedError(notSent(dropped, because));
          }
          await this.feedEndNotes(active.renderer);
          await this.closeReply(active.renderer, null, { force: true });
        } catch {
          // the tasks' replies below still end, and the waiters are still released
        }
      } else if (dropped.length > 0 || this.endNotes.length > 0) {
        const notes = this.endNotes;
        this.endNotes = [];
        if (dropped.length > 0) notes.push(notSent(dropped, because));
        await this.post(notes.join("\n")).catch(() => {});
      }
      await this.stopTaskReplies();
    } finally {
      for (const turn of waiting) turn.done.set();
      this.settled.set();
      this.stopTailEnded();
    }
  }

  /** The reply's footer, or null when it would say nothing. */
  private async footer(tokens: number | null): Promise<FooterFields | null> {
    const data = await this.footerData(tokens);
    const says =
      data.bypass ||
      (data.folder !== null && basename(data.folder) !== "") ||
      footerFields(data, this.now()).length > 0;
    return says ? data : null;
  }

  /** The footer's git lookup: for the folder this session started in (`directory`). */
  private repository(folder: string): Promise<Repository | null> {
    return this.deps.agent.trustedRepository(folder, this.directory);
  }

  /**
   * The footer's values from what the session knows now and `tokens`: the one source for both
   * the footer and `!status`, so the two never disagree.
   */
  private async footerData(tokens: number | null): Promise<FooterFields> {
    let context: ContextUsage = { model: null, percentage: null };
    if (this.client !== null) {
      try {
        context = await this.client.contextUsage();
      } catch (error) {
        // model and context are left out of the footer
        logger.warning(`could not read the context usage in ${this.channelId}: ${describe(error)}`);
      }
    }
    const here = this.workingDirectory || this.directory;
    const [branch, changes] = await gitState(here, (folder) => this.repository(folder));
    const usage = this.deps.usage.current;
    return {
      bypass: this.bypass,
      model: context.model,
      effort: this.effortReported ? this.effort || "default" : null,
      folder: this.directory,
      branch,
      changes,
      sessionTokens: tokens,
      contextPercent: context.percentage,
      sessionLimit: usage?.session ?? null,
      weekLimit: usage?.week ?? null,
    };
  }

  /**
   * A request of the agent's, put to the owner: a tool's permission, or its questions. Resolves
   * with what the owner chose, for as long as that takes; with a refusal when the chat could
   * not show the request, since nobody can answer one that was never shown.
   */
  private async askOwner(request: PermissionRequest | QuestionRequest): Promise<Decision> {
    const questions = request.type === "question" ? request.questions : null;
    const title =
      request.title ||
      (request.type === "question" ? request.toolName : taskTitle(request.toolName, request.input));
    const [approvalId, pending] = this.deps.approvals.open(
      this.channelId,
      this.threadTs,
      title,
      questions,
    );
    this.waiting.add(approvalId);
    this.idleTimerCheck(); // an open approval or question holds the session
    this.react("waiting");
    let messageTs: MessageId;
    let decision: Decision;
    try {
      try {
        messageTs = await this.thread.ask(approvalId, request, title);
      } catch (error) {
        // Nobody can answer a request that was never shown: deny it, and say why.
        logger.error(`could not post an approval request in ${this.where}: ${describe(error)}`);
        return questions === null
          ? { allow: false, message: texts.APPROVAL_UNPOSTED }
          : { answered: false, message: texts.APPROVAL_UNPOSTED };
      }
      logger.info(
        `posted ${questions === null ? "an approval" : "a question"} request in ` +
          `${this.where}: message ${messageTs}`,
      );
      if (this.deps.approvals.posted(approvalId, messageTs)) {
        // Crash repair (issue #19): still carrying buttons, until it is answered. Best-effort:
        // the message is already live either way, so a failed write here is logged loudly
        // rather than left silently unrecorded.
        try {
          this.deps.state.addRequest(this.channelId, this.threadTs, messageTs);
        } catch (error) {
          logger.error(
            `posted a request in ${this.where} that state.json could not record: ${describe(error)}`,
          );
        }
      } else {
        await this.deleteRequest(messageTs); // decided while it was posted
      }
      decision = await pending.decision;
    } finally {
      this.deps.approvals.discard(approvalId);
      this.waiting.delete(approvalId);
      this.idleTimerCheck(); // may (re)start the idle-close timer
      if (!this.interrupting) this.reactWaitingOrWorking();
    }
    if (questions !== null && "answered" in decision && decision.answered) {
      await this.keepAnswers(request.callId, messageTs, questions, decision.answers);
    }
    return decision;
  }

  /**
   * Where an answered question's answers stay. In the reply, under the line of the call, where
   * the question was asked (issue #82): the request has done its job and goes. When the reply
   * has no line of its own for that call, in the request itself, rewritten with nothing left to
   * press, so the answers are never lost. The permission request names its call (measured on
   * Claude Code 2.1.286, 2026-10-03).
   */
  private async keepAnswers(
    callId: string | null,
    messageTs: MessageId,
    questions: readonly Question[],
    answers: Readonly<Record<string, string | readonly string[]>>,
  ): Promise<void> {
    const active = this.active;
    if (callId !== null && active?.renderer.answered(callId, questions, answers)) {
      await this.deleteRequest(messageTs);
      return;
    }
    try {
      await this.thread.keepAnswers(messageTs, questions, answers);
    } catch (error) {
      // The request must not keep buttons that no longer work: remove it.
      logger.warning(`could not record an answer in ${this.where}: ${describe(error)}`);
      await this.deleteRequest(messageTs);
      return;
    }
    // Crash repair (issue #19): it carries no buttons now, so it leaves the tracked list.
    // Outside the try above: a failed write here must never delete a message just rewritten.
    try {
      this.deps.state.removeRequest(this.channelId, this.threadTs, messageTs);
    } catch (error) {
      logger.warning(
        `could not clear an answered request from state.json in ${this.where}: ${describe(error)}`,
      );
    }
  }

  /**
   * End a turn's reply with a line saying why, and release whoever waits on it. `error` reacts
   * ❌.
   */
  private async fail(
    turn: Turn,
    text: string,
    options: { readonly error?: boolean } = {},
  ): Promise<void> {
    try {
      await turn.sink.text(text, { notice: true });
      await turn.sink.finish([]);
      this.trackLanding(await turn.sink.closeOut(null), turn.sink);
      if (options.error) this.reactError();
    } finally {
      turn.done.set();
      this.showThreadStatus();
    }
  }

  /** A notice of the daemon's own, small and grey as the footer. */
  private async post(text: string): Promise<void> {
    await this.thread.notice(text);
  }

  /** Remove a decided request: the tool's line in the reply records what happened. */
  private async deleteRequest(messageTs: MessageId | null): Promise<void> {
    if (messageTs === null) return;
    await this.thread.withdraw(messageTs);
    try {
      // Crash repair (issue #19): the request is spoken for either way, so a startup repair
      // never retries a delete this call already made (or gave up on). Best-effort: a failed
      // write here must never surface past this point.
      this.deps.state.removeRequest(this.channelId, this.threadTs, messageTs);
    } catch (error) {
      logger.warning(
        `could not clear a deleted request from state.json in ${this.where}: ${describe(error)}`,
      );
    }
  }
}
