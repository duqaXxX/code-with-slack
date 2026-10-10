/**
 * One reaction on a session's root message (Decision D10): working, waiting for the owner,
 * everything ended, error, or stopped/restarted. Adding or removing a reaction on the owner's own
 * message notifies nobody and only shows in the channel list on iOS (measured 2026-09-28, Slack
 * free plan). Arguments and error codes read from docs.slack.dev/reference/methods/reactions.add
 * and .../reactions.remove, 2026-09-28.
 *
 * And Slack's own status line under a thread's last message (`ThreadStatus`), for as long as a
 * prompt is on its way or a turn runs. Arguments, the two minute timeout and the rate limit (600
 * calls a minute for the app) read from
 * docs.slack.dev/reference/methods/assistant.threads.setStatus, 2026-10-02.
 *
 * Port of `render/status.py`. How Python's asyncio became JavaScript here:
 * - A task cancelled by its owner is an `AbortSignal`. `StatusReaction.show` takes one and looks
 *   at it while it waits for the lock and after each Slack call returns; a `WebClient` call cannot
 *   be interrupted, so a cancel lands when the call in flight is back, never in the middle of it.
 *   `ThreadStatus` gives its own task an `AbortController` that `close` aborts, with the same
 *   rule: the call in flight is awaited and its answer used.
 * - `asyncio.Lock` is a small FIFO lock whose wait the signal can leave.
 * - `asyncio.Event` plus `wait_for(event.wait(), delay)` is a wake flag with one waiter, raced
 *   against `clock.sleep(delay)`; the clock is a parameter, in seconds.
 * - `asyncio.create_task` starts its coroutine on the next turn of the loop, so the code that
 *   called `show` finishes first. The keeper does the same by yielding one microtask before its
 *   first look at the state.
 * - The two process-wide flags are module state; `resetStatusFlags` is what Python's tests did
 *   with their autouse fixture.
 */
import { WebAPIPlatformError, type WebClient } from "@slack/web-api";
import type { Clock } from "../../../clock.ts";
import * as texts from "../../../core/texts.ts";

// Kept local until `reply/clock.ts` exists: the part of a clock this module needs.
/** Time on a clock's own scale, in seconds, and a sleep the signal can cut short. */
/** Where a line goes; ids and error names only, never message content. */
export interface Logger {
  debug(message: string): void;
  info(message: string): void;
  warning(message: string): void;
  error(message: string): void;
}

export const logger: Logger = {
  debug: (message) => console.error(`DEBUG chat.slack.reply.status: ${message}`),
  info: (message) => console.error(`INFO chat.slack.reply.status: ${message}`),
  warning: (message) => console.error(`WARNING chat.slack.reply.status: ${message}`),
  error: (message) => console.error(`ERROR chat.slack.reply.status: ${message}`),
};

// Slack removes a status two minutes after it was set (the method's reference): set again well
// before that.
export const THREAD_STATUS_REFRESH_SECONDS = 60.0;
// The reference says a status is cleared "when the app sends a reply". Whether a stream append
// or an edit counts was not measured, so the status is set again this long after a write: at
// most one call every two seconds per thread while its replies are written (the method allows
// 600 a minute for the app), whichever way Slack treats them.
export const THREAD_STATUS_AFTER_WRITE_SECONDS = 2.0;
// Answers that say the app's token cannot call the method at all (its reference's error list).
export const THREAD_STATUS_REFUSED: readonly string[] = ["missing_scope", "not_allowed_token_type"];

/** The reaction names, in the order Python's enum listed them (it is the order of a strip). */
export const Status = {
  WORKING: "hourglass_flowing_sand",
  WAITING: "raised_hand",
  DONE: "white_check_mark",
  ERROR: "x",
} as const;
export type Status = (typeof Status)[keyof typeof Status];

// Process-wide, once true: no instance calls Slack for reactions again this run.
let missingScope = false;
// Process-wide, once true: no instance calls Slack for a thread status again this run.
let refusedStatus = false;

/** Forget both "stop for the rest of the run" flags: for the tests, as Python's fixture did. */
export function resetStatusFlags(): void {
  missingScope = false;
  refusedStatus = false;
}

/** Slack's error code, or the error's class name: never the request or its content. */
function describe(error: unknown): string {
  if (error instanceof WebAPIPlatformError) return String(error.data.error);
  return error instanceof Error ? error.constructor.name : typeof error;
}

/** `asyncio.Lock`: first come, first served; a waiter can leave through its signal. */
class Lock {
  #held = false;
  #queue: Array<() => void> = [];

  acquire(signal?: AbortSignal): Promise<void> {
    signal?.throwIfAborted();
    if (!this.#held) {
      this.#held = true;
      return Promise.resolve();
    }
    return new Promise((resolve, reject) => {
      const grant = () => {
        signal?.removeEventListener("abort", leave);
        resolve();
      };
      const leave = () => {
        this.#queue = this.#queue.filter((waiter) => waiter !== grant);
        reject(signal?.reason);
      };
      this.#queue.push(grant);
      signal?.addEventListener("abort", leave, { once: true });
    });
  }

  release(): void {
    const next = this.#queue.shift();
    if (next === undefined) this.#held = false;
    else next(); // the lock passes to the next waiter without being free in between
  }
}

export interface StatusReactionOptions {
  readonly channel: string;
  readonly rootTs: string;
  readonly logger?: Logger;
}

/**
 * Keeps one reaction on `rootTs` in sync with the session's state. Showing the same state
 * again makes no call. A change adds the new reaction before removing the previous one, so the
 * root is never bare between them, and only once the add succeeded (or was already there):
 * a failed add leaves `current` as it was, so the next `show` retries rather than stripping
 * the root's own reaction for nothing. A fresh instance (a new session's reaction, next to
 * whatever an earlier one already left on the same root) also strips the other three names on
 * its own first successful add, so the root never carries more than one. Calls are serialized
 * per instance (a lock): two quick changes end on the last one. A change is two calls,
 * so a caller aborted between them leaves both reactions on the root: the instance then
 * reads as a fresh one, whose next change strips the other names, and `settle` makes that
 * change, to the state asked for last, for whoever aborted it. `already_reacted` on
 * add and `no_reaction` on remove count as done. `missing_scope` (the workspace has not
 * reinstalled the app for `reactions:write`) is logged once for the whole process, and every
 * instance stops calling Slack for reactions from then on; any other failure is logged
 * (channel, ts and the error code only) and swallowed, since a reaction must never break a
 * turn.
 */
export class StatusReaction {
  readonly #slack: WebClient;
  readonly #channel: string;
  readonly #rootTs: string;
  readonly #log: Logger;
  readonly #lock = new Lock();
  #current: Status | null = null;
  // What `show` or `clear` was asked for last, set before any call: it differs from
  // `#current` while a change is on its way, and after one that never finished.
  #wanted: Status | null = null;

  constructor(slack: WebClient, options: StatusReactionOptions) {
    this.#slack = slack;
    this.#channel = options.channel;
    this.#rootTs = options.rootTs;
    this.#log = options.logger ?? logger;
  }

  /**
   * The reaction this instance last showed successfully; `null` before that, and after a
   * change that was aborted on its way.
   */
  get current(): Status | null {
    return this.#current;
  }

  /** Shows `state`; with a `signal`, rejects with its reason if it aborts (see the file header). */
  async show(state: Status, signal?: AbortSignal): Promise<void> {
    if (missingScope) return;
    this.#wanted = state;
    await this.#lock.acquire(signal);
    try {
      if (state === this.#current) return;
      const previous = this.#current;
      try {
        const added = await this.#add(state);
        signal?.throwIfAborted();
        if (!added) return; // `#current` stays as it was: the next `show` retries the add
        if (previous !== null) {
          await this.#remove(previous);
          signal?.throwIfAborted();
        } else {
          // A fresh instance: an earlier session's reaction may still sit on this
          // root (an idle close's DONE, a restart's ERROR). Strip every other name so
          // the root carries exactly this one (D10).
          for (const other of Object.values(Status)) {
            if (other !== state) {
              await this.#remove(other);
              signal?.throwIfAborted();
            }
          }
        }
      } catch (error) {
        // Issue #104: the root may carry the new name, the previous one or both. Read
        // as a fresh instance from here, so the next change strips every other name. Nothing
        // else throws in here: `#add` and `#remove` swallow every Slack failure.
        this.#current = null;
        throw error;
      }
      this.#current = state;
    } finally {
      this.#lock.release();
    }
  }

  /**
   * Remove the current reaction, leaving the root bare: for a caller with nothing to
   * revert to (D8's Cancel on a session that had shown no reaction yet).
   */
  async clear(): Promise<void> {
    this.#wanted = null;
    await this.#lock.acquire();
    try {
      if (this.#current === null) return;
      await this.#remove(this.#current);
      this.#current = null;
    } finally {
      this.#lock.release();
    }
  }

  /**
   * Bring the root to the state asked for last, making no call when it already shows it.
   * For the caller that aborted a change in the middle (a session's close, issue #104), which is
   * the only one left to finish it.
   */
  async settle(): Promise<void> {
    if (this.#wanted === null) await this.clear();
    else await this.show(this.#wanted);
  }

  /** Whether the root now carries `state`: true on success or `already_reacted`. */
  async #add(state: Status): Promise<boolean> {
    try {
      await this.#slack.reactions.add({
        channel: this.#channel,
        timestamp: this.#rootTs,
        name: state,
      });
    } catch (error) {
      const code = describe(error);
      if (code === "already_reacted") return true;
      if (code === "missing_scope") this.#noteMissingScope();
      else {
        this.#log.warning(`reactions.add failed on ${this.#channel}/${this.#rootTs}: ${code}`);
      }
      return false;
    }
    return true;
  }

  async #remove(state: Status): Promise<void> {
    try {
      await this.#slack.reactions.remove({
        channel: this.#channel,
        timestamp: this.#rootTs,
        name: state,
      });
    } catch (error) {
      const code = describe(error);
      if (code === "no_reaction") return;
      if (code === "missing_scope") this.#noteMissingScope();
      else {
        this.#log.warning(`reactions.remove failed on ${this.#channel}/${this.#rootTs}: ${code}`);
      }
    }
  }

  #noteMissingScope(): void {
    if (!missingScope) {
      this.#log.warning(
        `reactions.add/remove failed on ${this.#channel}/${this.#rootTs}: missing_scope ` +
          "(reactions:write); the app needs reinstalling with the current scopes; no more " +
          "reactions will be attempted this run",
      );
    }
    missingScope = true;
  }
}

export interface ThreadStatusOptions {
  readonly channel: string;
  readonly threadTs: string;
  readonly clock: Clock;
  readonly refresh?: number;
  readonly afterWrite?: number;
  readonly logger?: Logger;
}

// The task that makes the calls. `done` is set in the task's own `finally`, the moment it
// returns, as `asyncio.Task.done()` is: a `kick` between that and any later tick must start a
// new one.
interface Keeper {
  readonly stop: AbortController;
  done: boolean;
  finished: Promise<void>;
}

/**
 * Slack's status line under a thread's last message, saying what `show` was last given:
 * `Working…` as the sign that a prompt was received and that its turn runs, where a reply
 * that has written nothing yet, or nothing for a while, gives none (issue #83), then what
 * still runs once the turn has ended (`1 shell still running`, issue #95). A state that
 * changes belongs here and not in a reply: a stream cannot change what it was told.
 * It notifies nobody (measured 2026-09-28) and shows on desktop and on iOS once it carries
 * `loading_messages` (measured 2026-10-02). `show` and `wrote` never wait on Slack: one task
 * of this instance's own makes the calls, in order, so two quick changes end on the last one.
 * It sets the status at once, again within THREAD_STATUS_AFTER_WRITE_SECONDS of a `wrote`,
 * and every THREAD_STATUS_REFRESH_SECONDS; `show("")` clears it, only if it was set, and a
 * clearing call that fails is tried once more. A
 * failure is logged (channel, ts and the error code only, once per code in a row) and
 * swallowed, since a status line must never break a turn. A refusal no retry can change
 * (`THREAD_STATUS_REFUSED`: the token cannot call the method) stops every instance from
 * calling Slack for the rest of the run, as `StatusReaction` does for `missing_scope`.
 */
export class ThreadStatus {
  readonly #slack: WebClient;
  readonly #channel: string;
  readonly #threadTs: string;
  readonly #clock: Clock;
  readonly #refresh: number;
  readonly #afterWrite: number;
  readonly #log: Logger;
  #text = ""; // what it says; empty: nothing is shown
  #fallback: string = texts.THREAD_WORKING_STATUS;
  #shown = false; // whether Slack was last asked to show it
  #due = 0; // when it is next set, by the clock
  #woken = false; // `asyncio.Event`: asked for again since the task last looked
  #waiter: (() => void) | null = null; // the task's nap, when it is in one
  #keeper: Keeper | null = null;
  #failed: string | null = null;

  constructor(slack: WebClient, options: ThreadStatusOptions) {
    this.#slack = slack;
    this.#channel = options.channel;
    this.#threadTs = options.threadTs;
    this.#clock = options.clock;
    this.#refresh = options.refresh ?? THREAD_STATUS_REFRESH_SECONDS;
    this.#afterWrite = options.afterWrite ?? THREAD_STATUS_AFTER_WRITE_SECONDS;
    this.#log = options.logger ?? logger;
  }

  /**
   * Whether Slack has said this app's token cannot set a thread status at all: what a
   * caller with something the owner must be told reads before it falls back on a message.
   */
  static refused(): boolean {
    return refusedStatus;
  }

  /**
   * Say `text` from now on, or with an empty one stop showing the status. The same text
   * again is a no-op. `fallback` says the same after the app's name, for a client that
   * draws `<app name> <status>` instead of the loading message.
   */
  show(text: string, fallback: string = texts.THREAD_WORKING_STATUS): void {
    if (text === this.#text) return;
    this.#text = text;
    this.#fallback = fallback;
    this.#due = this.#clock.time();
    this.#kick();
  }

  /**
   * Something of the app's was written in this thread, which may have cleared the
   * status: it is set again within THREAD_STATUS_AFTER_WRITE_SECONDS, never later than it
   * was already due (writes that keep coming must not put off the refresh).
   */
  wrote(): void {
    if (this.#text) {
      this.#due = Math.min(this.#due, this.#clock.time() + this.#afterWrite);
      this.#kick();
    }
  }

  /** Stop for good: the status is cleared if it shows, and nothing is left running. */
  async close(): Promise<void> {
    this.#text = "";
    const keeper = this.#keeper;
    this.#keeper = null;
    if (keeper !== null && !keeper.done) {
      keeper.stop.abort();
      await keeper.finished.catch(() => undefined);
    }
    if (this.#shown) {
      this.#shown = false;
      await this.#set(false);
    }
  }

  #kick(): void {
    this.#woken = true;
    this.#waiter?.();
    if (this.#keeper === null || this.#keeper.done) {
      const stop = new AbortController();
      const keeper: Keeper = { stop, done: false, finished: Promise.resolve() };
      keeper.finished = this.#keep(keeper);
      this.#keeper = keeper;
    }
  }

  /** Waits `seconds` on the clock, until `#kick` wakes it, or until the task is stopped. */
  #nap(seconds: number, signal: AbortSignal): Promise<void> {
    if (this.#woken || signal.aborted) return Promise.resolve();
    const napping = new AbortController();
    const until = AbortSignal.any([signal, napping.signal]);
    return new Promise((resolve) => {
      let over = false;
      const wake = () => {
        if (over) return;
        over = true;
        this.#waiter = null;
        napping.abort(); // lets the clock forget the sleep
        resolve();
      };
      this.#waiter = wake;
      this.#clock.sleep(seconds, until).then(wake, wake);
    });
  }

  async #keep(keeper: Keeper): Promise<void> {
    const signal = keeper.stop.signal;
    let retried = false;
    try {
      await Promise.resolve(); // `create_task` runs its coroutine after the caller's code
      while (true) {
        this.#woken = false;
        if (signal.aborted) return;
        if (!this.#text) {
          if (this.#shown) {
            // Cleared only once the call is back and went through: a `close` that
            // stops it meanwhile, or that follows a failed one, still reads the
            // status as shown and clears it itself.
            if (await this.#set(false)) {
              this.#shown = false;
            } else if (!retried) {
              // Once more after a moment: a status left standing says a session
              // works that does not, until Slack removes it by itself.
              retried = true;
              await this.#nap(this.#afterWrite, signal);
              continue;
            }
          }
          if (this.#woken) continue; // asked for again while that call was out
          return;
        }
        retried = false;
        const delay = this.#due - this.#clock.time();
        if (delay <= 0) {
          // Before the call, so a `wrote` that arrives while it is out still moves it.
          this.#due = this.#clock.time() + this.#refresh;
          this.#shown = true;
          await this.#set(true);
          continue;
        }
        await this.#nap(delay, signal);
      }
    } finally {
      keeper.done = true;
    }
  }

  /** Tell Slack; whether it went through, or nothing is left to try (`refusedStatus`). */
  async #set(on: boolean): Promise<boolean> {
    if (refusedStatus) return true;
    try {
      if (on) {
        await this.#slack.assistant.threads.setStatus({
          channel_id: this.#channel,
          thread_ts: this.#threadTs,
          status: this.#fallback,
          loading_messages: [this.#text],
        });
      } else {
        // An empty status clears it (the method's reference).
        await this.#slack.assistant.threads.setStatus({
          channel_id: this.#channel,
          thread_ts: this.#threadTs,
          status: "",
        });
      }
    } catch (error) {
      const code = describe(error);
      if (code !== this.#failed) {
        this.#log.warning(
          `assistant.threads.setStatus failed on ${this.#channel}/${this.#threadTs}: ${code}`,
        );
      }
      this.#failed = code;
      if (THREAD_STATUS_REFUSED.includes(code)) refusedStatus = true;
      return refusedStatus;
    }
    this.#failed = null;
    return true;
  }
}
