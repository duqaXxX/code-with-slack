/**
 * The chat seam: what the core writes in a thread and a chat provider draws. No type of a chat
 * library appears here. One provider exists, `chat/slack`, and it is the only module that
 * imports the Slack packages.
 *
 * Two halves are here: the reply model (`ReplySink`, `TaskUpdate`, the footer's fields), and
 * what a session does to its thread outside a reply (`ThreadChat`: the root's status, the
 * thread's activity line, a notice, a request put to the owner). The inbound half (messages,
 * clicks, forms), the setup form and the session index are added by the phases that port them.
 *
 * The one value declared here is `ChatError`, which names a failed call for the core's log.
 */
import type { PermissionRequest, Question, QuestionRequest } from "../agent/seam.ts";

export type TaskStatus = "pending" | "in_progress" | "complete" | "error";

/** The terminal's own view of a finished call. */
export interface Preview {
  /** `Update(notes.txt)` */
  readonly title: string;
  /** `Added 1 line, removed 1 line` */
  readonly summary: string;
  /** Numbered lines, as the terminal prints them. */
  readonly body: string;
  /** `diff` for a diff, which a provider may colour. */
  readonly language: "" | "diff";
  /** The body is lines of words, shown as written and in no code block. */
  readonly plain: boolean;
}

/** One line of a reply: a tool call, or a subagent's or a background command's task. */
export interface TaskUpdate {
  readonly id: string;
  readonly title: string;
  readonly status: TaskStatus;
  readonly details: string | null;
  readonly output: string | null;
  /** The tool's name, which chooses its preview. */
  readonly name: string;
  /** A subagent's or a background command's line. */
  readonly task: boolean;
  /** Calls made inside it (a subagent's), counted in its title. */
  readonly calls: number;
  readonly preview: Preview | null;
  /**
   * On a line of a run of calls: what replaces it once the reply's body has ended; empty
   * removes it. Null on any other line, which stays.
   */
  readonly folded: string | null;
}

export interface UsageLimit {
  readonly percent: number;
  /** Milliseconds since the epoch. */
  readonly resetsAt: number | null;
}

/** The footer as fields. A provider formats them in its own markup. */
export interface FooterFields {
  readonly bypass: boolean;
  readonly model: string | null;
  readonly effort: string | null;
  /** The folder the thread was opened in. */
  readonly folder: string | null;
  readonly branch: string | null;
  /** Lines inserted and deleted since the last commit. */
  readonly changes: readonly [inserted: number, deleted: number] | null;
  readonly sessionTokens: number | null;
  readonly contextPercent: number | null;
  readonly sessionLimit: UsageLimit | null;
  readonly weekLimit: UsageLimit | null;
}

/**
 * One reply in a thread, as the core writes it. A write that has begun always runs to its end:
 * a `signal` only releases the caller, who stops waiting for it.
 */
export interface ReplySink {
  text(
    markdown: string,
    options?: { readonly notice?: boolean; readonly ending?: boolean },
  ): Promise<void>;
  task(update: TaskUpdate): Promise<void>;
  /** The reply's body has ended; `closing` are the lines still open, closed. */
  finish(closing: readonly TaskUpdate[], signal?: AbortSignal): Promise<void>;
  /** Ends the reply, with its footer when the turn has one. False when the end did not land. */
  closeOut(footer: FooterFields | null, signal?: AbortSignal): Promise<boolean>;
  waitLanded(signal?: AbortSignal): Promise<boolean>;
  settle(signal?: AbortSignal): Promise<boolean>;
}

/** What a chat provider supports, and its size limits. */
export interface ChatCapabilities {
  readonly nativeStreaming: boolean;
  readonly messageEdit: boolean;
  readonly buttons: boolean;
  readonly forms: boolean;
  readonly reactions: boolean;
  readonly homeIndex: boolean;
  readonly threads: boolean;
  readonly fileUpload: boolean;
  readonly ephemeralMessages: boolean;
  /** Characters one message holds. */
  readonly messageLimit: number;
  /** Blocks or cards one message holds. */
  readonly blocksLimit: number;
}

/**
 * A call the chat refused or could not take. `name` is the provider's code for the failure
 * (`channel_not_found`), or the error's own name: all a log line may say of it, never content.
 */
export class ChatError extends Error {
  constructor(code: string, options?: ErrorOptions) {
    super(code, options);
    this.name = code;
  }
}

/** A message of the chat, by the id its provider gives it. The core stores it and hands it back. */
export type MessageId = string;

/** What the root of a thread shows of its session. */
export type SessionStatus = "working" | "waiting" | "done" | "error";

/**
 * One reply as a session holds it: the reply model, and its place in the thread. Only the
 * thread's latest reply says what still runs, at its end.
 */
export interface Reply extends ReplySink {
  /**
   * What still runs in the thread (`⏳ 1 shell · 1 agent`), shown after the footer; empty
   * removes it. Takes effect at once and is written with the reply's next write.
   */
  setRunning(counts: string): void;
  /** Whether this is the thread's latest reply: an older one drops the counts, keeps its footer. */
  setLatest(latest: boolean): void;
  /**
   * Whether the reply's end has landed in the chat: its footer is then what says the running
   * counts, and the thread's activity line does not repeat them.
   */
  readonly footerShown: boolean;
}

/**
 * Called when the message a crash would leave unfinished changes: `(old, fresh)` is one reply's
 * own transition, add `fresh` or drop `old`. Synchronous; a throw is the provider's to log.
 */
export type OpenMessageChange = (old: MessageId | null, fresh: MessageId | null) => void;

/**
 * One thread, as its session uses it. A call that has begun always runs to its end: a `signal`
 * only releases the caller. Unless it says otherwise a member never rejects: the provider logs
 * a failure (ids and codes only) and goes on, since the chat must never stop a turn.
 */
export interface ThreadChat {
  /**
   * A new reply in the thread. It writes nothing until it is given something to show.
   * `onOpenMessage` is told which of its messages a crash would leave unfinished.
   */
  openReply(onOpenMessage: OpenMessageChange): Reply;
  /**
   * Shows the session's status on the thread's root. Changes are made in the order asked; with
   * a `signal`, rejects with its reason when it aborts, and the change may be left half made.
   */
  showStatus(status: SessionStatus, signal?: AbortSignal): Promise<void>;
  /** Leaves the root with no status of this session. */
  clearStatus(): Promise<void>;
  /** Brings the root to the status asked for last: for whoever aborted a change on its way. */
  settleStatus(): Promise<void>;
  /**
   * What the thread says is going on under its last message, from now on; empty for nothing.
   * `afterName` says the same after the app's name, for a client that draws it that way.
   * Never waits on the chat.
   */
  showActivity(text: string, afterName: string): void;
  /** Whether the chat refuses this app an activity line for good: the session then posts. */
  readonly activityRefused: boolean;
  /** Something of the app's was posted in the thread outside this object. */
  written(): void;
  /** A line of the daemon's own, small and grey, as a message of its own; shown as written. */
  notice(text: string): Promise<void>;
  /**
   * Puts a request to the owner: a tool's permission, or the agent's questions. `requestId`
   * is what the owner's answer comes back with; `title` is the request in one line. Resolves
   * with the message that shows it; rejects with `ChatError` when it could not be shown.
   */
  ask(
    requestId: string,
    request: PermissionRequest | QuestionRequest,
    title: string,
  ): Promise<MessageId>;
  /** Removes a request that was decided or is stale; one already gone counts as removed. */
  withdraw(request: MessageId): Promise<void>;
  /**
   * Rewrites an answered question into the record of its answers, with nothing left to press.
   * Rejects with `ChatError` when the chat refuses the rewrite.
   */
  keepAnswers(
    request: MessageId,
    questions: readonly Question[],
    answers: Readonly<Record<string, string | readonly string[]>>,
  ): Promise<void>;
  /** The session closed: the activity line is cleared, and nothing is left running. */
  close(): Promise<void>;
}

/** The chat as the session manager uses it: one `ThreadChat` per thread that holds a session. */
export interface ChatProvider {
  thread(channelId: string, threadTs: string): ThreadChat;
}
