/**
 * The chat seam, first draft: the reply model the core writes and a chat provider draws.
 * No type of a chat library appears here. One provider exists, `chat/slack`, and it is the
 * only module that imports the Slack packages.
 *
 * The inbound half (messages, clicks, forms) and what a session does to the chat outside a
 * reply (status, notices, requests, the setup form) are added by the phases that port them.
 */

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

/** One reply in a thread, as the core writes it. */
export interface ReplySink {
  text(
    markdown: string,
    options?: { readonly notice?: boolean; readonly ending?: boolean },
  ): Promise<void>;
  task(update: TaskUpdate): Promise<void>;
  /** The reply's body has ended; `closing` are the lines still open, closed. */
  finish(closing: readonly TaskUpdate[]): Promise<void>;
  /** Ends the reply, with its footer when the turn has one. False when the end did not land. */
  closeOut(footer: FooterFields | null): Promise<boolean>;
  waitLanded(): Promise<boolean>;
  settle(): Promise<boolean>;
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
