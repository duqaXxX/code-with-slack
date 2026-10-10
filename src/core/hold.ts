/**
 * Questions that hold a message before it is sent. D8: before a message wakes an idle session
 * while a live session of another thread (any channel) is busy in the same resolved folder, the
 * daemon asks `Another session is working in this folder: <link>. Send anyway?`, with two
 * buttons, `Send anyway` and `Don't send`, which the code calls Continue and Cancel.
 * The session setup (`setup.ts`) is the other one: model, effort and bypass, then Start.
 *
 * Kept in memory only, like `Approvals`: a hold never outlives the process (`!stop`, a top-level
 * `!stop` of its channel, and a drain all cancel one exactly as Cancel does), so nothing here
 * needs to survive a restart. A thread has at most one open at a time, whichever kind.
 */
import { randomBytes } from "node:crypto";
import type { ModelInfo } from "../agent/seam.ts";
import type { Choice } from "./setup.ts";

/** What the owner chose: `true` for Continue on the same-folder hold, the `Choice` of a setup. */
export type HoldAnswer = true | Choice;

/** One question waiting for the owner. */
export interface Pending {
  readonly channelId: string;
  readonly threadTs: string;
  /** What the owner chose, or null when the question was cancelled (Cancel, `!stop`, a drain). */
  readonly answer: Promise<HoldAnswer | null>;
  /** Whether `answer` is settled. */
  decided: boolean;
  messageTs: string | null;
  /** What the asker needs back with a click that carries only the id (the setup's model list). */
  readonly context: readonly ModelInfo[] | null;
  /**
   * Set by `Holds.cancel` while an answer is being applied (the setup's Start settling): the
   * asker sends nothing once it is done.
   */
  cancelled: boolean;
}

interface Entry {
  readonly pending: Pending;
  readonly settle: (answer: HoldAnswer | null) => void;
}

/**
 * Questions waiting for the owner, by an id that only the posted buttons carry (as `Approvals`
 * keys its own requests).
 */
export class Holds {
  private readonly entries = new Map<string, Entry>();

  open(
    channelId: string,
    threadTs: string,
    context: readonly ModelInfo[] | null = null,
  ): [holdId: string, pending: Pending] {
    const holdId = randomBytes(16).toString("base64url");
    let settle: (answer: HoldAnswer | null) => void = () => {};
    const answer = new Promise<HoldAnswer | null>((resolve) => {
      settle = resolve;
    });
    const pending: Pending = {
      channelId,
      threadTs,
      answer,
      decided: false,
      messageTs: null,
      context,
      cancelled: false,
    };
    this.entries.set(holdId, { pending, settle });
    return [holdId, pending];
  }

  get(holdId: string): Pending | null {
    return this.entries.get(holdId)?.pending ?? null;
  }

  /**
   * Resolve once with `value` (null cancels), and only from the channel and thread the request
   * was posted in. A value keeps the entry until the asker `discard`s it, so a stop that arrives
   * while the answer is applied still finds it (`cancel`).
   */
  resolve(
    holdId: string,
    channelId: string,
    threadTs: string,
    value: HoldAnswer | null,
  ): Pending | null {
    const entry = this.entries.get(holdId);
    if (
      entry === undefined ||
      entry.pending.channelId !== channelId ||
      entry.pending.threadTs !== threadTs ||
      entry.pending.decided
    ) {
      return null;
    }
    entry.pending.decided = true;
    entry.settle(value);
    if (value === null) this.entries.delete(holdId);
    return entry.pending;
  }

  /**
   * Record the message that shows a hold, so it can be removed once decided; false when the hold
   * was decided before its message was known (a click or `!stop` while posting).
   */
  posted(holdId: string, messageTs: string): boolean {
    const entry = this.entries.get(holdId);
    if (entry === undefined) return false;
    entry.pending.messageTs = messageTs;
    return !entry.pending.decided;
  }

  /**
   * Cancel the hold open in this thread, if any: the same outcome as the owner clicking Cancel
   * (`!stop`, a top-level `!stop` of its channel, a drain). One already answered but still being
   * applied is flagged `cancelled` instead: its asker sends nothing.
   */
  cancel(channelId: string, threadTs: string): Pending | null {
    for (const [holdId, entry] of [...this.entries]) {
      const { pending } = entry;
      if (pending.channelId !== channelId || pending.threadTs !== threadTs) continue;
      if (!pending.decided) {
        pending.decided = true;
        entry.settle(null);
        this.entries.delete(holdId);
        return pending;
      }
      if (!pending.cancelled) {
        pending.cancelled = true;
        return pending;
      }
    }
    return null;
  }

  /**
   * The id of the open hold shown by this message, for a control that carries no id of its own
   * (a select): null when it is decided, gone, or shown elsewhere.
   */
  atMessage(channelId: string, threadTs: string, messageTs: string): string | null {
    for (const [holdId, { pending }] of this.entries) {
      if (
        pending.channelId === channelId &&
        pending.threadTs === threadTs &&
        pending.messageTs === messageTs &&
        !pending.decided
      ) {
        return holdId;
      }
    }
    return null;
  }

  discard(holdId: string): void {
    this.entries.delete(holdId);
  }
}
