/**
 * The agent's permission requests and questions waiting for the owner. This is the bookkeeping
 * that belongs to no chat provider: which request is pending, who may resolve it, and what the
 * answer is. A provider draws the request and reports what the owner chose.
 *
 * Kept in memory only: a request never outlives the process. The wait is a promise the agent
 * side holds (`Pending.decision`), settled by `resolve` or `denyAll` and by nothing else.
 */
import { randomBytes } from "node:crypto";
import type { PermissionAnswer, Question, QuestionAnswer } from "../agent/seam.ts";
import { DENY_MESSAGE, SKIP_MESSAGE } from "./texts.ts";

/** What the owner chose, before it is turned into the answer the agent takes. */
export type Outcome =
  | { readonly kind: "approve" }
  | { readonly kind: "deny" }
  | {
      readonly kind: "answer";
      readonly answers: Readonly<Record<string, string | readonly string[]>>;
    };

export const APPROVE: Outcome = { kind: "approve" };
export const DENY: Outcome = { kind: "deny" };

/** The agent's answer to a request: a permission's for a tool, a question's for a question. */
export type Decision = PermissionAnswer | QuestionAnswer;

/**
 * The answer the agent takes for what the owner chose. A refusal is a dismissal when the request
 * was a question (`questions` is not null), so Claude is told the form was skipped, not denied.
 */
export function toAnswer(outcome: Outcome, questions: readonly Question[] | null): Decision {
  switch (outcome.kind) {
    case "answer":
      return { answered: true, answers: outcome.answers };
    case "approve":
      return { allow: true };
    case "deny":
      return questions !== null && questions.length > 0
        ? { answered: false, message: SKIP_MESSAGE }
        : { allow: false, message: DENY_MESSAGE };
  }
}

/**
 * A question's answer as Claude Code takes it: the labels of the options picked and the text
 * typed under Other, which is the answer itself (tools reference); null when there is neither.
 * `picked` are indexes into `question.options`; one that names no option throws, since the
 * form only offers the options the question has.
 */
export function answerOf(
  question: Question,
  picked: readonly number[],
  typed: string | undefined,
): string | string[] | null {
  let labels = picked.map((index) => {
    const option = question.options[index];
    if (option === undefined) throw new RangeError(`no option ${index} in the question`);
    return option.label;
  });
  if (typed) labels = question.multiSelect ? [...labels, typed] : [typed];
  if (labels.length === 0) return null;
  return question.multiSelect ? labels : (labels[0] as string);
}

/** One request waiting for the owner. */
export interface Pending {
  readonly channelId: string;
  readonly threadTs: string;
  readonly title: string;
  /** The questions of a question request, or null for a permission request. */
  readonly questions: readonly Question[] | null;
  /** Settles once, with the answer the agent takes. */
  readonly decision: Promise<Decision>;
  /** Whether `decision` is settled. */
  decided: boolean;
  /** The message that shows the request, once it is known. */
  messageTs: string | null;
}

interface Entry {
  readonly pending: Pending;
  readonly settle: (decision: Decision) => void;
}

/** Requests waiting for the owner, by an id that only the posted buttons carry. */
export class Approvals {
  private readonly entries = new Map<string, Entry>();

  /** Starts waiting for `title`'s request; `questions` makes it a question. */
  open(
    channelId: string,
    threadTs: string,
    title: string,
    questions: readonly Question[] | null = null,
  ): [approvalId: string, pending: Pending] {
    const approvalId = randomBytes(16).toString("base64url");
    let settle: (decision: Decision) => void = () => {};
    const decision = new Promise<Decision>((resolve) => {
      settle = resolve;
    });
    const pending: Pending = {
      channelId,
      threadTs,
      title,
      questions,
      decision,
      decided: false,
      messageTs: null,
    };
    this.entries.set(approvalId, { pending, settle });
    return [approvalId, pending];
  }

  get(approvalId: string): Pending | null {
    return this.entries.get(approvalId)?.pending ?? null;
  }

  /** Resolve once, and only from the channel and thread the request was posted in. */
  resolve(
    approvalId: string,
    channelId: string,
    threadTs: string,
    outcome: Outcome,
  ): Pending | null {
    const entry = this.entries.get(approvalId);
    if (
      entry === undefined ||
      entry.pending.channelId !== channelId ||
      entry.pending.threadTs !== threadTs ||
      entry.pending.decided
    ) {
      return null;
    }
    this.decide(approvalId, entry, outcome);
    return entry.pending;
  }

  /** Denies every request still open in this thread, and returns them. */
  denyAll(channelId: string, threadTs: string): Pending[] {
    const denied: Pending[] = [];
    for (const [approvalId, entry] of [...this.entries]) {
      const { pending } = entry;
      if (pending.channelId === channelId && pending.threadTs === threadTs && !pending.decided) {
        this.decide(approvalId, entry, DENY);
        denied.push(pending);
      }
    }
    return denied;
  }

  /**
   * A read-only snapshot of this thread's open requests, for a caller that needs to know which
   * messages are still live without resolving them: `ThreadSession.close` captures these before
   * cancelling its reader, since `denyAll`'s own list is empty by the time close reaches it
   * (issue #19 fix round item 8).
   */
  pendingIn(channelId: string, threadTs: string): Pending[] {
    return [...this.entries.values()]
      .map((entry) => entry.pending)
      .filter((pending) => pending.channelId === channelId && pending.threadTs === threadTs);
  }

  /**
   * Record the message that shows a request, so it can be removed once decided; false when the
   * request was decided before its message was known (`!stop` while posting).
   */
  posted(approvalId: string, messageTs: string): boolean {
    const entry = this.entries.get(approvalId);
    if (entry === undefined) return false;
    entry.pending.messageTs = messageTs;
    return true;
  }

  discard(approvalId: string): void {
    this.entries.delete(approvalId);
  }

  private decide(approvalId: string, entry: Entry, outcome: Outcome): void {
    entry.pending.decided = true;
    entry.settle(toAnswer(outcome, entry.pending.questions));
    this.entries.delete(approvalId);
  }
}
