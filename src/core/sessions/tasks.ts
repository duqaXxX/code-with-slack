/**
 * What stands for asyncio in the sessions: a task that can be cancelled, an event, a lock, a
 * queue, and a wait that a signal ends. The rule they serve is the header of `session.ts`.
 *
 * asyncio ran what became ready (a new task, the waiter of an event that was set) on a later
 * turn of its loop, once the code that made it ready had reached a real wait. A promise
 * callback runs sooner than that: at the next `await` of that code, whether or not the `await`
 * waits for anything. So a new task starts, and the waiters of an event wake, on the next turn
 * of the event loop (`setImmediate`), in the order they became ready.
 */

/** Resolves on the next turn of the event loop, after every promise callback that is ready. */
function nextTurn(): Promise<void> {
  return new Promise((resolve) => {
    setImmediate(resolve);
  });
}

/** What a wait rejects with when its signal aborts: Python's `asyncio.CancelledError`. */
export class Cancelled extends Error {
  constructor() {
    super("cancelled");
    this.name = "Cancelled";
  }
}

/**
 * `promise`, for a caller that may stop waiting: rejects with the signal's reason when it aborts
 * first. The promise itself goes on, which is `asyncio.shield`: what it began runs to its end.
 */
export function cancellable<T>(promise: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (signal === undefined) return promise;
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(signal.reason);
    if (signal.aborted) {
      // Whoever began it still owns its outcome: no unhandled rejection from this side.
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
 * `asyncio.create_task`. The function starts on the next turn of the event loop, never inside
 * the call that made the task: asyncio ran a new task only once the code that created it had
 * yielded, and the sessions set state right after creating one. A task cancelled before it
 * started never runs.
 * `cancel` aborts the task's signal with `Cancelled`; the function decides which of its waits
 * take it. A task that fails rejects `result()` and nothing else: it never surfaces as an
 * unhandled rejection.
 */
export class Task<T = void> {
  private readonly controller = new AbortController();
  private readonly promise: Promise<T>;
  private finished = false;

  constructor(run: (signal: AbortSignal) => Promise<T>) {
    this.promise = this.start(run);
    this.promise.catch(() => {});
  }

  private async start(run: (signal: AbortSignal) => Promise<T>): Promise<T> {
    try {
      await nextTurn();
      this.controller.signal.throwIfAborted();
      return await run(this.controller.signal);
    } finally {
      this.finished = true;
    }
  }

  get signal(): AbortSignal {
    return this.controller.signal;
  }

  /** Whether the function has returned or thrown: `asyncio.Task.done()`. */
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

  /** Resolves when the task ends, however it ends: awaiting a task one has just cancelled. */
  settled(): Promise<void> {
    return this.promise.then(
      () => {},
      () => {},
    );
  }
}

/**
 * `asyncio.Event`. `set` wakes its waiters on the next turn of the event loop: whoever set it
 * goes on to its own next real wait first, as under asyncio, so a waiter never sees the state of
 * the setter half-way through what it was doing. A waiter whose signal aborts leaves and rejects
 * with the signal's reason.
 */
export class Event {
  private value = false;
  private readonly waiters = new Set<() => void>();

  get isSet(): boolean {
    return this.value;
  }

  set(): void {
    this.value = true;
    if (this.waiters.size === 0) return;
    const waiting = [...this.waiters];
    this.waiters.clear();
    setImmediate(() => {
      for (const wake of waiting) wake();
    });
  }

  clear(): void {
    this.value = false;
  }

  wait(signal?: AbortSignal): Promise<void> {
    if (signal?.aborted) return Promise.reject(signal.reason);
    if (this.value) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const wake = () => {
        signal?.removeEventListener("abort", onAbort);
        resolve();
      };
      const onAbort = () => {
        this.waiters.delete(wake);
        reject(signal?.reason);
      };
      signal?.addEventListener("abort", onAbort, { once: true });
      this.waiters.add(wake);
    });
  }
}

/** `asyncio.Lock`: one holder at a time, the waiters served in the order they came. */
export class Mutex {
  private held = false;
  private readonly waiters: Array<() => void> = [];

  get locked(): boolean {
    return this.held;
  }

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

/** `asyncio.Queue`, unbounded, for one consumer. */
export class Queue<T> {
  private readonly items: T[] = [];
  private readonly filled = new Event();

  get size(): number {
    return this.items.length;
  }

  get empty(): boolean {
    return this.items.length === 0;
  }

  putNowait(item: T): void {
    this.items.push(item);
    this.filled.set();
  }

  /** The next item now; throws on an empty queue, as `get_nowait` raised. */
  getNowait(): T {
    const item = this.items.shift();
    if (item === undefined) throw new RangeError("the queue is empty");
    if (this.items.length === 0) this.filled.clear();
    return item;
  }

  /** The next item, waiting for one; the wait leaves through its signal. */
  async get(signal?: AbortSignal): Promise<T> {
    while (this.items.length === 0) await this.filled.wait(signal);
    return this.getNowait();
  }
}
