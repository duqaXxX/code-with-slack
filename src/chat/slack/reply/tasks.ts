/**
 * What stands for asyncio here: a task that can be cancelled, a lock, a wait that a signal ends.
 * The rule they serve is the header of `sinks.ts`.
 */

/** What a wait rejects with when its signal aborts: Python's `asyncio.CancelledError`. */
export class Cancelled extends Error {
  constructor() {
    super("cancelled");
    this.name = "Cancelled";
  }
}

/**
 * `promise`, for a caller that may stop waiting: rejects with the signal's reason when it aborts
 * first. The promise itself goes on, which is `asyncio.shield`.
 */
export function cancellable<T>(promise: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (signal === undefined) return promise;
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(signal.reason);
    if (signal.aborted) {
      // Whoever shielded it still owns its outcome: no unhandled rejection from this side.
      promise.catch(() => {});
      onAbort();
      return;
    }
    signal.addEventListener("abort", onAbort, { once: true });
    promise.then(
      (value) => {
        signal.removeEventListener("abort", onAbort);
        resolve(value);
      },
      (error: unknown) => {
        signal.removeEventListener("abort", onAbort);
        reject(error);
      },
    );
  });
}

/**
 * `asyncio.create_task`: an async function that started running, and the signal it was given.
 * `cancel` aborts that signal with `Cancelled`; the function decides which of its waits take it.
 * A task that fails rejects `result()` and nothing else: it never surfaces as an unhandled
 * rejection.
 */
export class Task<T> {
  private readonly controller = new AbortController();
  private readonly promise: Promise<T>;
  private finished = false;

  constructor(run: (signal: AbortSignal) => Promise<T>) {
    this.promise = this.run(run);
    this.promise.catch(() => {});
  }

  private async run(run: (signal: AbortSignal) => Promise<T>): Promise<T> {
    try {
      return await run(this.controller.signal);
    } finally {
      this.finished = true;
    }
  }

  get signal(): AbortSignal {
    return this.controller.signal;
  }

  /** Whether the function has returned or thrown. True a turn of the microtask queue late. */
  get done(): boolean {
    return this.finished;
  }

  cancel(): void {
    if (!this.finished && !this.controller.signal.aborted) this.controller.abort(new Cancelled());
  }

  /** The function's own outcome. */
  result(): Promise<T> {
    return this.promise;
  }

  /** `asyncio.wait([task])`: resolves when the task ends, however it ends. */
  settled(): Promise<void> {
    return this.promise.then(
      () => {},
      () => {},
    );
  }
}

/** `asyncio.Lock`: one holder at a time, the waiters served in the order they came. */
export class Mutex {
  private held = false;
  private readonly waiters: Array<() => void> = [];

  /**
   * Resolves with the function that releases the lock, once it is this caller's turn. A waiter
   * whose signal aborts leaves the line and rejects with the signal's reason.
   */
  acquire(signal?: AbortSignal): Promise<() => void> {
    if (signal?.aborted) return Promise.reject(signal.reason);
    if (!this.held) {
      this.held = true;
      return Promise.resolve(this.releaser());
    }
    return new Promise((resolve, reject) => {
      const turn = () => {
        signal?.removeEventListener("abort", onAbort);
        resolve(this.releaser());
      };
      const onAbort = () => {
        const index = this.waiters.indexOf(turn);
        if (index !== -1) this.waiters.splice(index, 1);
        reject(signal?.reason);
      };
      signal?.addEventListener("abort", onAbort, { once: true });
      this.waiters.push(turn);
    });
  }

  private releaser(): () => void {
    let released = false;
    return () => {
      if (released) return;
      released = true;
      // Handed straight to the next in line: the lock is never free for a newcomer to take
      // ahead of a waiter.
      const next = this.waiters.shift();
      if (next === undefined) this.held = false;
      else next();
    };
  }
}
