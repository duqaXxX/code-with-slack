/**
 * Repair what a crashed daemon left open (issue #19): a reply whose stream never stopped or
 * whose cards still run, an approval, question or hold request still carrying buttons, and the
 * ⏳/✋ reaction a turn mid-flight left on its root. Runs once on start, right after `auth.test`
 * and before opening the Socket Mode connection (the web client works without it) and before
 * `state.prune` (a pruned thread's leftovers must still be repaired); a graceful stop clears
 * these same fields itself, so a second start finds nothing to do.
 *
 * A reply's message is stopped first (`chat.stopStream`; Slack ends a stream itself 5 minutes
 * after it started, and answers `message_not_in_streaming_state` to a stop then), which is the
 * one notification the message owes; the edit that follows says it stopped and never notifies,
 * and nothing is posted.
 *
 * `conversations.replies` (docs.slack.dev/reference/methods/conversations.replies, read
 * 2026-09-28, confirmed against a real `conversations.replies` response recorded from a real
 * workspace on 2026-09-28, scrubbed and kept as
 * `tests/fixtures/slack/api-conversations-replies-by-ts.json`, and of a stopped stream on
 * 2026-09-29, `api-conversations-replies-stream.json`): `ts` set to the reply's own ts, with
 * `limit=1`, returns only that one message (an `oldest`/`latest`/`inclusive` range built around
 * the same ts instead returned the thread's root too).
 */
import type { KnownBlock, WebClient } from "@slack/web-api";
import type { StateStore, ThreadState } from "../../core/state.ts";
import * as texts from "../../core/texts.ts";
import { getLogger } from "../../log.ts";
import { contextBlock } from "./reply/blocks.ts";
import { deleteRequest, describe, NOT_STREAMING } from "./reply/errors.ts";
import type { Limiter } from "./reply/limiter.ts";
import { Status, StatusReaction } from "./reply/status.ts";

export const logger = getLogger("awaydesk.chat.slack.repair");

type Block = Record<string, unknown>;

const STOPPED_BLOCK = contextBlock(texts.STOPPED_BEFORE_ANSWER);
// What a card left running becomes: it never ended, and will not.
const RUNNING_CARD = ["pending", "in_progress"];
// Slack's own cap: the read-back count is Slack's (streamed markdown reads back as several
// blocks), not the sink's, which keeps a margin under it for what it writes itself.
export const SLACK_BLOCKS = 50;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The records of a wire list; `[]` for a field that is absent or not a list. */
function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

/**
 * Repair every thread state.json still shows as left open. One thread's failure (a deleted
 * message, a Slack error) is logged and does not stop the others; each of a thread's three
 * fields is cleared once its own repair has been attempted, successfully or not, so a second
 * start never retries what this one already gave up on.
 */
export async function repairCrash(
  slack: WebClient,
  state: StateStore,
  limiter: Limiter,
): Promise<void> {
  for (const [channelId, threadTs, thread] of state.repairsPending()) {
    try {
      await repairThread(slack, limiter, state, channelId, threadTs, thread);
    } catch (error) {
      logger.error(
        `could not repair a crashed thread's state in ${channelId}/${threadTs}: ${describe(error)}`,
      );
    }
  }
}

async function repairThread(
  slack: WebClient,
  limiter: Limiter,
  state: StateStore,
  channelId: string,
  threadTs: string,
  thread: ThreadState,
): Promise<void> {
  for (const messageTs of thread.openReplies) {
    await repairReply(slack, limiter, channelId, threadTs, messageTs);
    safe(() => state.replaceOpenReply(channelId, threadTs, messageTs, null));
  }
  for (const messageTs of thread.requests) {
    await deleteRequest(slack, { channel: channelId, ts: messageTs }, { logger });
    safe(() => state.removeRequest(channelId, threadTs, messageTs));
  }
  if (thread.status !== null) {
    const ended = await repairStatus(slack, channelId, threadTs, thread.status);
    safe(() => state.setStatusPending(channelId, threadTs, null, ended));
  }
}

/**
 * A `StateStore` write is a plain synchronous file write, so it can throw like any other (disk
 * full, a permission problem): best-effort here, since repair itself must never crash the
 * daemon's startup over its own bookkeeping. Logged with the error name only.
 */
function safe(write: () => void): void {
  try {
    write();
  } catch (error) {
    logger.warning(`could not update state.json during crash repair: ${describe(error)}`);
  }
}

/**
 * Stop the reply's message, then rewrite it: its blocks as Slack keeps them, every card left
 * running closed as an error (a stopped message would show it so anyway, until it is updated:
 * measured 2026-09-28), plus the line that says it stopped. Kept within Slack's 50 blocks: at
 * the cap the line goes into the last context block (the footer), or is left out if there is
 * none; a block that holds content is never replaced. A failed read or a message already gone
 * is logged and left alone: never replaced with a shorter form that would lose its content.
 */
async function repairReply(
  slack: WebClient,
  limiter: Limiter,
  channelId: string,
  threadTs: string,
  messageTs: string,
): Promise<void> {
  try {
    await slack.chat.stopStream({ channel: channelId, ts: messageTs });
  } catch (error) {
    if (describe(error) !== NOT_STREAMING) {
      // the stream is over already when it is: nothing to stop
      logger.warning(
        `could not stop a crashed reply's stream in ${channelId}/${threadTs}: ${describe(error)}`,
      );
    }
  }
  let answer: unknown;
  try {
    answer = await slack.conversations.replies({ channel: channelId, ts: messageTs, limit: 1 });
  } catch (error) {
    logger.warning(
      `could not read a crashed reply's message in ${channelId}/${threadTs}: ${describe(error)}`,
    );
    return;
  }
  const messages = records(isRecord(answer) ? answer.messages : undefined);
  const message = messages.find((found) => String(found.ts) === messageTs);
  if (message === undefined) {
    logger.info(`a crashed reply's message is gone in ${channelId}/${threadTs}`);
    return;
  }
  let blocks: Block[] = records(message.blocks).map((block) =>
    block.type === "task_card" && RUNNING_CARD.includes(String(block.status))
      ? { ...block, status: "error" }
      : block,
  );
  if (blocks.length < SLACK_BLOCKS) blocks.push({ ...STOPPED_BLOCK });
  else blocks = sayStoppedInTheFooter(blocks);
  try {
    await limiter.acquire();
    await slack.chat.update({
      channel: channelId,
      ts: messageTs,
      text: texts.STOPPED_BEFORE_ANSWER,
      // Blocks as Slack read them back, sent back as they are: opaque to the library's types.
      blocks: blocks as unknown as KnownBlock[],
    });
  } catch (error) {
    logger.warning(
      `could not rewrite a crashed reply in ${channelId}/${threadTs}: ${describe(error)}`,
    );
  }
}

/**
 * The blocks with the stopped line added to the text of the last context block; unchanged when
 * the message has none: the edit's own `text` says it.
 */
function sayStoppedInTheFooter(blocks: Block[]): Block[] {
  const index = blocks.findLastIndex((block) => block.type === "context");
  const last = blocks[index];
  if (last === undefined) return blocks;
  const elements = records(last.elements);
  const first = elements[0];
  if (first === undefined || (first.type !== "mrkdwn" && first.type !== "plain_text")) {
    return blocks;
  }
  const text = typeof first.text === "string" ? first.text : "";
  const joined = `${text} · ${texts.STOPPED_BEFORE_ANSWER}`.replace(/^[ ·]+/, "");
  const rewritten = { ...last, elements: [{ ...first, text: joined }, ...elements.slice(1)] };
  return blocks.map((block, i) => (i === index ? rewritten : block));
}

/**
 * Set ❌ on a root left ⏳ or ✋, through the same `StatusReaction` a live session uses (issue
 * #19 fix round item 9): a fresh instance's first `show` also strips every other stray
 * reaction from the root on its own (D10), which is strictly more thorough than removing just
 * the one name state.json recorded. Returns the reaction asked for, the thread's ended one from
 * now on; null for a name that is neither.
 */
async function repairStatus(
  slack: WebClient,
  channelId: string,
  threadTs: string,
  name: string,
): Promise<string | null> {
  if (name !== Status.WORKING && name !== Status.WAITING) return null;
  await new StatusReaction(slack, { channel: channelId, rootTs: threadTs }).show(Status.ERROR);
  return Status.ERROR;
}
