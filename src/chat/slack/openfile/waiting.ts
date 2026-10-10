/**
 * The waits `!open` is made of: a counting gate for git's turns and for one modal's updates, a
 * time limit on the injected clock, and a wait that a requester can give up without stopping the
 * work. Python's `asyncio.Semaphore`, `asyncio.Lock`, `asyncio.timeout` and `asyncio.shield`.
 */
import type { Clock } from "../../../clock.ts";

/** What `within` answers when the time ran out first. */
export const TIMED_OUT = Symbol("timed out");

/** At most `limit` holders at once; the others wait their turn, first come first served. */
export class Gate {
  readonly #limit: number;
  #active = 0;
  readonly #queue: Array<() => void> = [];

  constructor(limit: number) {
    this.#limit = limit;
  }

  /** Takes a turn; rejects with the signal's reason when it aborts while waiting for one. */
  acquire(signal?: AbortSignal): Promise<void> {
    if (signal?.aborted) return Promise.reject(signal.reason);
    if (this.#active < this.#limit) {
      this.#active += 1;
      return Promise.resolve();
    }
    return new Promise((resolve, reject) => {
      const turn = () => {
        signal?.removeEventListener("abort", onAbort);
        resolve();
      };
      const onAbort = () => {
        const index = this.#queue.indexOf(turn);
        if (index >= 0) this.#queue.splice(index, 1);
        reject(signal?.reason);
      };
      signal?.addEventListener("abort", onAbort, { once: true });
      this.#queue.push(turn);
    });
  }

  /** Gives the turn back: to the first one waiting, else to nobody. */
  release(): void {
    const next = this.#queue.shift();
    if (next === undefined) this.#active -= 1;
    else next();
  }

  /** `work` while holding a turn. */
  async run<T>(work: () => Promise<T>, signal?: AbortSignal): Promise<T> {
    await this.acquire(signal);
    try {
      return await work();
    } finally {
      this.release();
    }
  }
}

/**
 * `work` with `seconds` of the clock to finish: `TIMED_OUT` when they pass first, after the signal
 * given to `work` is aborted. A `seconds` of nothing is out of time at once, with `work` not
 * started. Work that ignores its signal keeps running unseen: a call to Slack or a lookup cannot be
 * cut short.
 */
export async function within<T>(
  clock: Clock,
  seconds: number,
  work: (signal: AbortSignal) => Promise<T>,
): Promise<T | typeof TIMED_OUT> {
  if (seconds <= 0) return TIMED_OUT;
  const limit = new AbortController();
  const finished = new AbortController();
  const expired = new Promise<typeof TIMED_OUT>((resolve) => {
    clock.sleep(seconds, finished.signal).then(
      () => {
        limit.abort(new Error("time limit"));
        resolve(TIMED_OUT);
      },
      () => {},
    );
  });
  const running = work(limit.signal);
  // Raced out, it may still reject later: nobody is left to read that.
  running.catch(() => {});
  try {
    return await Promise.race([running, expired]);
  } catch (error) {
    if (limit.signal.aborted) return TIMED_OUT;
    throw error;
  } finally {
    finished.abort();
  }
}

/**
 * `promise`, or the signal's reason when it aborts first. The promise is left running: the other
 * requests for the same work still wait for it.
 */
export function waitFor<T>(promise: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (signal === undefined) return promise;
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(signal.reason);
      return;
    }
    const onAbort = () => reject(signal.reason);
    signal.addEventListener("abort", onAbort, { once: true });
    promise.then(resolve, reject).finally(() => signal.removeEventListener("abort", onAbort));
  });
}
