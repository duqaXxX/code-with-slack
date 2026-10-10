/**
 * One Slack thread as its session uses it (`ThreadChat` of the chat seam), composed from what
 * the provider already has: a `ReplySink` per reply, the `StatusReaction` on the thread's root,
 * the `ThreadStatus` line under its last message, and the request messages of `requests.ts`.
 *
 * What it adds to those parts is their wiring, which the Python session did itself: every pass
 * of a reply that wrote, and every notice, tells the status line (Slack clears a thread's status
 * when the app replies); the seam's four statuses are the four reactions; an answered question
 * is rewritten on the budget every reply draws from; and a failed post or rewrite crosses the
 * seam as a `ChatError` named by Slack's code.
 */
import type { KnownBlock, WebClient } from "@slack/web-api";
import type { PermissionRequest, Question, QuestionRequest } from "../../agent/seam.ts";
import { type Clock, systemClock } from "../../clock.ts";
import * as texts from "../../core/texts.ts";
import { getLogger } from "../../log.ts";
import {
  ChatError,
  type ChatProvider,
  type MessageId,
  type OpenMessageChange,
  type Reply,
  type SessionStatus,
  type ThreadChat,
} from "../seam.ts";
import { contextBlock, noticeText } from "./reply/blocks.ts";
import { deleteRequest, describe } from "./reply/errors.ts";
import { mrkdwnEscape } from "./reply/escape.ts";
import type { Limiter } from "./reply/limiter.ts";
import { ReplySink } from "./reply/sinks.ts";
import { Status, StatusReaction, ThreadStatus } from "./reply/status.ts";
import { answeredBlocks, approvalBlocks, questionBlocks } from "./requests.ts";

export const logger = getLogger("awaydesk.chat.slack.thread");

/** The reaction that shows each status of a session on its root message. */
export const REACTIONS: Readonly<Record<SessionStatus, Status>> = {
  working: Status.WORKING,
  waiting: Status.WAITING,
  done: Status.DONE,
  error: Status.ERROR,
};

/** Who a reply's stream is addressed to, and whose messages are read back after a reset. */
export interface SlackIdentity {
  readonly teamId: string;
  readonly ownerUserId: string;
  readonly botUserId: string;
}

export interface SlackChatOptions {
  /** The client every call but a reply's goes through. */
  readonly slack: WebClient;
  /**
   * The client replies are written with: one that never sends a create or a stream call twice
   * after a reset (`repliesClient`). Required, with no fallback to `slack`: a wiring that
   * forgot it would send a create twice after a reset and nothing would say so.
   */
  readonly replies: WebClient;
  readonly identity: SlackIdentity;
  /** Shared by every reply in the process and by every `chat.update` made beside them. */
  readonly limiter: Limiter;
  /** Wall-clock seconds and every pause of a reply and of the status line. */
  readonly clock?: Clock;
  /** `sinks.DEBOUNCE_SECONDS`, unless a test shortens it. */
  readonly debounceSeconds?: number;
  /** `sinks.FINAL_RETRY_SECONDS`, unless a test shortens it. */
  readonly finalRetrySeconds?: number;
}

/** The Slack provider as the session manager uses it: one `SlackThread` per thread. */
export class SlackChat implements ChatProvider {
  private readonly options: SlackChatOptions;

  constructor(options: SlackChatOptions) {
    this.options = options;
  }

  /**
   * The `chat.update` budget every reply in the process draws from, so an edit made outside a
   * reply (a handler's own) can share it.
   */
  get limiter(): Limiter {
    return this.options.limiter;
  }

  thread(channelId: string, threadTs: string): SlackThread {
    return new SlackThread(channelId, threadTs, this.options);
  }
}

export class SlackThread implements ThreadChat {
  private readonly channel: string;
  private readonly threadTs: string;
  private readonly options: SlackChatOptions;
  private readonly slack: WebClient;
  private readonly clock: Clock;
  // One reaction on the session's root message, which `threadTs` always is (a top-level owner
  // message, or the root of a `!resume` thread, the owner's own message).
  private readonly reaction: StatusReaction;
  /** Issues #83 and #95: Slack's status line at the bottom of the thread. */
  private readonly line: ThreadStatus;

  constructor(channelId: string, threadTs: string, options: SlackChatOptions) {
    this.channel = channelId;
    this.threadTs = threadTs;
    this.options = options;
    this.slack = options.slack;
    this.clock = options.clock ?? systemClock;
    this.reaction = new StatusReaction(this.slack, { channel: channelId, rootTs: threadTs });
    this.line = new ThreadStatus(this.slack, {
      channel: channelId,
      threadTs,
      clock: this.clock,
    });
  }

  openReply(onOpenMessage: OpenMessageChange): Reply {
    const { identity } = this.options;
    return new ReplySink(this.options.replies, {
      channel: this.channel,
      threadTs: this.threadTs,
      teamId: identity.teamId,
      userId: identity.ownerUserId,
      botUserId: identity.botUserId,
      limiter: this.options.limiter,
      clock: this.clock,
      onOpenReply: onOpenMessage,
      onWrite: () => this.line.wrote(),
      ...(this.options.debounceSeconds !== undefined && {
        debounceSeconds: this.options.debounceSeconds,
      }),
      ...(this.options.finalRetrySeconds !== undefined && {
        finalRetrySeconds: this.options.finalRetrySeconds,
      }),
    });
  }

  showStatus(status: SessionStatus, signal?: AbortSignal): Promise<void> {
    return this.reaction.show(REACTIONS[status], signal);
  }

  clearStatus(): Promise<void> {
    return this.reaction.clear();
  }

  settleStatus(): Promise<void> {
    return this.reaction.settle();
  }

  showActivity(text: string, afterName: string): void {
    this.line.show(text, afterName);
  }

  get activityRefused(): boolean {
    return ThreadStatus.refused();
  }

  written(): void {
    this.line.wrote();
  }

  async notice(text: string): Promise<void> {
    // mrkdwn reads `&`, `<` and `>` as markup, and a notice can quote the owner's own message.
    const shown = mrkdwnEscape(text);
    try {
      await this.slack.chat.postMessage({
        channel: this.channel,
        thread_ts: this.threadTs,
        text: shown,
        blocks: [contextBlock(noticeText(shown))],
        unfurl_links: false,
        unfurl_media: false,
      });
      this.line.wrote(); // a post of the app's clears the thread's status
    } catch (error) {
      logger.error(`could not post in ${this.channel}/${this.threadTs}: ${describe(error)}`);
    }
  }

  async ask(
    requestId: string,
    request: PermissionRequest | QuestionRequest,
    title: string,
  ): Promise<MessageId> {
    const blocks =
      request.type === "question"
        ? questionBlocks(requestId, request.questions)
        : approvalBlocks(requestId, request);
    let ts: unknown;
    try {
      const posted = await this.slack.chat.postMessage({
        channel: this.channel,
        thread_ts: this.threadTs,
        text: title,
        // Built to Block Kit's reference by `requests.ts`: opaque to the library's types.
        blocks: blocks as unknown as KnownBlock[],
        unfurl_links: false,
        unfurl_media: false,
      });
      ts = posted.ts;
    } catch (error) {
      throw new ChatError(describe(error), { cause: error });
    }
    // The message is the only handle on its buttons: one Slack did not name cannot be removed.
    if (typeof ts !== "string" || ts === "") throw new ChatError("no_ts");
    return ts;
  }

  async withdraw(request: MessageId): Promise<void> {
    await deleteRequest(this.slack, { channel: this.channel, ts: request });
  }

  async keepAnswers(
    request: MessageId,
    questions: readonly Question[],
    answers: Readonly<Record<string, string | readonly string[]>>,
  ): Promise<void> {
    try {
      // One more chat.update against the app's budget, which every reply draws from.
      await this.options.limiter.acquire();
      await this.slack.chat.update({
        channel: this.channel,
        ts: request,
        text: texts.ANSWERED,
        blocks: answeredBlocks(questions, answers) as unknown as KnownBlock[],
      });
    } catch (error) {
      throw new ChatError(describe(error), { cause: error });
    }
  }

  close(): Promise<void> {
    return this.line.close();
  }
}
