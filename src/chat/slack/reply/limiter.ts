/** The budget of writes every reply in the process shares. */
import { type Clock, monotonicClock } from "../../../clock.ts";
import { Mutex } from "./tasks.ts";

// chat.update is Tier 3, "50+ per minute" per app (chat.update reference, read 2026-09-28): 40
// per 60 s plus a burst of 5, worst case 45 in one window, a real margin under the documented
// floor. Paced evenly (a token bucket, not a sliding window) past the burst, so a busy minute is
// a steady trickle rather than every reply racing through the budget together and then freezing
// until it resets.
export const UPDATE_LIMIT = 40;
export const UPDATE_WINDOW_SECONDS = 60.0;
// How many writes the budget lets through at once before pacing kicks in: enough for a reply
// that just started to show its first few lines without waiting on threads that were already busy.
export const UPDATE_BURST = 5;

/** What a reply asks of the budget: its turn before a write, and a token back when none followed. */
export interface Limiter {
  acquire(signal?: AbortSignal): Promise<void>;
  refund(): Promise<void>;
}

export interface UpdateLimiterOptions {
  readonly limit?: number;
  /** Seconds. */
  readonly window?: number;
  readonly burst?: number;
  /** What the bucket refills and waits by; monotonic seconds by default. */
  readonly clock?: Clock;
}

/**
 * One instance shared by every `ReplySink` in the process, so their chat.update writes stay
 * under Slack's app-wide budget: a token bucket refilling at `limit` tokens per `window` seconds
 * (evenly, one token every `window / limit`), holding at most `burst` at once. A caller that has
 * to wait keeps its place in line, since it holds the lock for as long as it waits, so the next
 * caller queues up behind it. A retry the Slack client makes under the hood, inside one
 * `chat.update` call, spends no extra token here: the limiter only gates the call itself, not
 * what the client does while it is in flight.
 */
export class UpdateLimiter implements Limiter {
  /** Tokens regained per second. */
  private readonly rate: number;
  private readonly burst: number;
  private readonly clock: Clock;
  private tokens: number;
  private checked: number;
  private readonly lock = new Mutex();

  constructor(options: UpdateLimiterOptions = {}) {
    const limit = options.limit ?? UPDATE_LIMIT;
    const window = options.window ?? UPDATE_WINDOW_SECONDS;
    this.rate = limit / window;
    this.burst = options.burst ?? UPDATE_BURST;
    this.clock = options.clock ?? monotonicClock;
    this.tokens = this.burst;
    this.checked = this.clock.time();
  }

  /** Waits until a token is available, then spends it. A signal that aborts gives the place up. */
  async acquire(signal?: AbortSignal): Promise<void> {
    const release = await this.lock.acquire(signal);
    try {
      for (;;) {
        const now = this.clock.time();
        this.tokens = Math.min(this.burst, this.tokens + (now - this.checked) * this.rate);
        this.checked = now;
        if (this.tokens >= 1) {
          this.tokens -= 1;
          return;
        }
        await this.clock.sleep((1 - this.tokens) / this.rate, signal);
      }
    } finally {
      release();
    }
  }

  /**
   * Give back a token `acquire` spent on a write that, once inside the caller's own lock, turned
   * out not to be needed after all (the reply caught up to what it now shows while this one
   * waited its turn): capped at `burst`, as a token earned by waiting would be. Never takes the
   * lock: a concurrent `acquire` can hold it for as long as its own wait takes, and this must
   * land at once regardless; `acquire` always rereads the tokens fresh on its own next pass.
   */
  async refund(): Promise<void> {
    this.tokens = Math.min(this.burst, this.tokens + 1);
  }
}
