/**
 * The time the daemon waits by: every pause (a reply's debounce and its stream's 280 seconds,
 * the limiter's pacing, a status refresh, an idle close) goes through a `Clock`, so a test
 * crosses them without waiting. Any layer may import it.
 */
import { setTimeout as pause } from "node:timers/promises";

export interface Clock {
  /** Resolves `seconds` from now; rejects with the signal's reason when it aborts first. */
  sleep(seconds: number, signal?: AbortSignal): Promise<void>;
  /** Seconds on this clock's own scale. */
  time(): number;
}

async function sleep(seconds: number, signal?: AbortSignal): Promise<void> {
  try {
    await pause(seconds * 1000, undefined, { signal });
  } catch (error) {
    // `node:timers/promises` rejects with its own AbortError, the reason as its cause.
    if (signal?.aborted) throw signal.reason;
    throw error;
  }
}

/** Wall-clock seconds, as a Slack ts counts them. */
export const systemClock: Clock = { sleep, time: () => Date.now() / 1000 };

/** Seconds that only move forward: what a rate is paced by. */
export const monotonicClock: Clock = { sleep, time: () => performance.now() / 1000 };
