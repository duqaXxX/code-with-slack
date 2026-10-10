/**
 * What `SlackThread` adds to the parts it composes: the wiring between a reply, a notice and
 * the thread's status line, the seam's statuses as reactions, a request as a message the core
 * can name, and a failed call as a `ChatError`. The sink, the reaction, the status line and
 * the request blocks have their own tests.
 */
import assert from "node:assert/strict";
import { beforeEach, mock, test } from "node:test";
import type { PermissionRequest, QuestionRequest } from "../../../src/agent/seam.ts";
import { ChatError, type SessionStatus } from "../../../src/chat/seam.ts";
import type { Limiter } from "../../../src/chat/slack/reply/limiter.ts";
import {
  resetStatusFlags,
  logger as statusLogger,
  THREAD_STATUS_AFTER_WRITE_SECONDS,
} from "../../../src/chat/slack/reply/status.ts";
import {
  answeredBlocks,
  approvalBlocks,
  questionBlocks,
} from "../../../src/chat/slack/requests.ts";
import { logger, SlackChat, type SlackThread } from "../../../src/chat/slack/thread.ts";
import * as texts from "../../../src/core/texts.ts";
import {
  BOT,
  CHANNEL,
  FakeClock,
  FakeSlack,
  OTHER_THREAD,
  OWNER,
  rejected,
  TEAM,
  THREAD,
} from "../../support/fake-slack.ts";

beforeEach(() => resetStatusFlags());

class CountingLimiter implements Limiter {
  acquired = 0;
  /** The Slack calls made before each token was taken. */
  readonly before: number[] = [];
  readonly #slack: FakeSlack;

  constructor(slack: FakeSlack) {
    this.#slack = slack;
  }

  async acquire(): Promise<void> {
    this.acquired += 1;
    this.before.push(this.#slack.apiCalls.length);
  }

  async refund(): Promise<void> {}
}

interface Made {
  readonly slack: FakeSlack;
  readonly clock: FakeClock;
  readonly limiter: CountingLimiter;
  readonly chat: SlackChat;
  readonly thread: SlackThread;
}

function made(options: { replies?: FakeSlack } = {}): Made {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const limiter = new CountingLimiter(slack);
  const chat = new SlackChat({
    slack,
    ...(options.replies !== undefined && { replies: options.replies }),
    identity: { teamId: TEAM, ownerUserId: OWNER, botUserId: BOT },
    limiter,
    clock,
  });
  return { slack, clock, limiter, chat, thread: chat.thread(CHANNEL, THREAD) };
}

/** Let what is ready run: the status line's own task, a reaction asked for and not awaited. */
async function settle(clock: FakeClock): Promise<void> {
  await clock.advance(0);
}

const PERMISSION: PermissionRequest = {
  type: "permission",
  requestId: "request-1",
  callId: "toolu_1",
  toolName: "Bash",
  input: { command: "ls <here> & there" },
  title: null,
  description: null,
};

const QUESTION: QuestionRequest = {
  type: "question",
  requestId: "request-2",
  callId: "toolu_2",
  toolName: "AskUserQuestion",
  title: null,
  questions: [
    {
      header: "Colour",
      text: "Which colour?",
      multiSelect: false,
      options: [
        { label: "Red", description: null, preview: null },
        { label: "Blue", description: null, preview: null },
      ],
    },
  ],
};

test("each status of a session is one reaction on the threads root", async () => {
  const { slack, thread } = made();
  const shown: Array<[SessionStatus, string]> = [
    ["working", "hourglass_flowing_sand"],
    ["waiting", "raised_hand"],
    ["done", "white_check_mark"],
    ["error", "x"],
  ];
  for (const [status] of shown) await thread.showStatus(status);
  const added = slack.callsTo("reactions.add");
  assert.deepEqual(
    added.map((args) => [args.channel, args.timestamp, args.name]),
    shown.map(([, name]) => [CHANNEL, THREAD, name]),
  );
  await thread.clearStatus();
  assert.equal(slack.callsTo("reactions.remove").at(-1)?.name, "x");
  // Nothing is asked for and nothing shows: settling makes no call.
  const before = slack.apiCalls.length;
  await thread.settleStatus();
  assert.equal(slack.apiCalls.length, before);
});

test("a reply is written in the thread to the owner and names its open message", async () => {
  const replies = new FakeSlack();
  const { slack, thread } = made({ replies });
  const changes: Array<[string | null, string | null]> = [];
  const reply = thread.openReply((old, fresh) => changes.push([old, fresh]));
  await reply.text("hello");
  await reply.finish([]);
  const [start] = replies.callsTo("chat.startStream");
  assert.equal(start?.channel, CHANNEL);
  assert.equal(start?.thread_ts, THREAD);
  assert.equal(start?.recipient_team_id, TEAM);
  assert.equal(start?.recipient_user_id, OWNER);
  assert.deepEqual(changes, [[null, replies.streamTs[0]]]); // what a crash would leave unfinished
  assert.equal(reply.footerShown, false);
  assert.equal(await reply.closeOut(null), true);
  assert.deepEqual(changes.at(-1), [replies.streamTs[0], null]);
  assert.equal(reply.footerShown, true);
  assert.deepEqual(slack.streamTs, []); // the client made for replies wrote it, and no other
});

test("a write of a reply sets the thread status again", async () => {
  const { slack, clock, thread } = made();
  thread.showActivity("Working…", "is working…");
  await settle(clock);
  assert.equal(slack.callsTo("assistant.threads.setStatus").length, 1);
  const reply = thread.openReply(() => {});
  await reply.text("hello");
  await reply.finish([]); // Slack clears a thread's status when the app replies
  await clock.advance(THREAD_STATUS_AFTER_WRITE_SECONDS);
  const [, again, ...more] = slack.callsTo("assistant.threads.setStatus");
  assert.equal(more.length, 0);
  assert.deepEqual(again, {
    channel_id: CHANNEL,
    thread_ts: THREAD,
    status: "is working…",
    loading_messages: ["Working…"],
  });
  await thread.close();
  assert.equal(slack.callsTo("assistant.threads.setStatus").at(-1)?.status, "");
});

test("a message posted outside the session sets the thread status again", async () => {
  const { slack, clock, thread } = made();
  thread.showActivity("Working…", "is working…");
  await settle(clock);
  thread.written();
  await clock.advance(THREAD_STATUS_AFTER_WRITE_SECONDS);
  assert.equal(slack.callsTo("assistant.threads.setStatus").length, 2);
});

test("a notice is one grey line in the thread shown as written", async () => {
  const { slack, clock, thread } = made();
  thread.showActivity("Working…", "is working…");
  await settle(clock);
  await thread.notice('1 message was not sent: send it again.\n- "a <b> & c"');
  const [post, ...more] = slack.callsTo("chat.postMessage");
  assert.equal(more.length, 0);
  const shown = '1 message was not sent: send it again.\n- "a &lt;b&gt; &amp; c"';
  assert.deepEqual(post, {
    channel: CHANNEL,
    thread_ts: THREAD,
    text: shown,
    blocks: [{ type: "context", elements: [{ type: "mrkdwn", text: shown }] }],
    unfurl_links: false,
    unfurl_media: false,
  });
  // The post cleared the thread's status: it is set again.
  await clock.advance(THREAD_STATUS_AFTER_WRITE_SECONDS);
  assert.equal(slack.callsTo("assistant.threads.setStatus").length, 2);
});

test("a notice slack refuses is logged by its code and never rejects", async (t) => {
  const { slack, thread } = made();
  const errors: string[] = [];
  t.mock.method(logger, "error", (line: string) => errors.push(line));
  slack.responses["chat.postMessage"] = rejected("channel_not_found");
  await thread.notice("the words of the owner");
  assert.deepEqual(errors, [`could not post in ${CHANNEL}/${THREAD}: channel_not_found`]);
});

test("a permission request is a message of its own and the core gets its ts", async () => {
  const { slack, thread } = made();
  const ts = await thread.ask("approval-1", PERMISSION, "Bash: ls");
  assert.equal(ts, slack.postedTs[0]);
  const [post] = slack.callsTo("chat.postMessage");
  assert.deepEqual(post, {
    channel: CHANNEL,
    thread_ts: THREAD,
    text: "Bash: ls",
    blocks: JSON.parse(JSON.stringify(approvalBlocks("approval-1", PERMISSION))),
    unfurl_links: false,
    unfurl_media: false,
  });
});

test("a question request is posted with the questions buttons", async () => {
  const { slack, thread } = made();
  await thread.ask("approval-2", QUESTION, "AskUserQuestion");
  const [post] = slack.callsTo("chat.postMessage");
  assert.equal(post?.text, "AskUserQuestion");
  assert.deepEqual(
    post?.blocks,
    JSON.parse(JSON.stringify(questionBlocks("approval-2", QUESTION.questions))),
  );
});

test("a request slack refuses rejects with a chat error named by slacks code", async () => {
  const { slack, thread } = made();
  slack.responses["chat.postMessage"] = rejected("invalid_blocks");
  await assert.rejects(thread.ask("approval-1", PERMISSION, "Bash: ls"), (error: unknown) => {
    assert.ok(error instanceof ChatError);
    assert.equal(error.name, "invalid_blocks");
    return true;
  });
});

test("a withdrawn request is deleted and one already gone counts as removed", async () => {
  const { slack, thread } = made();
  const ts = await thread.ask("approval-1", PERMISSION, "Bash: ls");
  await thread.withdraw(ts);
  assert.deepEqual(slack.callsTo("chat.delete"), [{ channel: CHANNEL, ts }]);
  slack.responses["chat.delete"] = rejected("message_not_found");
  await thread.withdraw(ts); // never rejects
  assert.equal(slack.callsTo("chat.delete").length, 2);
});

test("an answered question is rewritten on the budget every reply draws from", async () => {
  const { slack, limiter, thread } = made();
  const ts = await thread.ask("approval-2", QUESTION, "AskUserQuestion");
  const answers = { "Which colour?": "Red" };
  await thread.keepAnswers(ts, QUESTION.questions, answers);
  const [update, ...more] = slack.callsTo("chat.update");
  assert.equal(more.length, 0);
  assert.deepEqual(update, {
    channel: CHANNEL,
    ts,
    text: texts.ANSWERED,
    blocks: JSON.parse(JSON.stringify(answeredBlocks(QUESTION.questions, answers))),
  });
  // One token, taken before the update was sent.
  assert.equal(limiter.acquired, 1);
  assert.equal(slack.apiCalls[limiter.before[0] ?? -1]?.method, "chat.update");
});

test("a rewrite slack refuses rejects with a chat error named by slacks code", async () => {
  const { slack, thread } = made();
  const ts = await thread.ask("approval-2", QUESTION, "AskUserQuestion");
  slack.responses["chat.update"] = rejected("msg_too_long");
  await assert.rejects(
    thread.keepAnswers(ts, QUESTION.questions, { "Which colour?": "Red" }),
    (error: unknown) => error instanceof ChatError && error.name === "msg_too_long",
  );
});

test("the activity line is refused once slack says the token cannot set one", async () => {
  const { slack, clock, thread } = made();
  assert.equal(thread.activityRefused, false);
  slack.responses["assistant.threads.setStatus"] = rejected("missing_scope");
  const warnings = mock.method(statusLogger, "warning", () => {});
  try {
    thread.showActivity("Working…", "is working…");
    await settle(clock);
  } finally {
    warnings.mock.restore();
  }
  assert.equal(thread.activityRefused, true);
});

test("the provider gives each thread its own object and shares one budget", async () => {
  const { slack, limiter, chat, thread } = made();
  const other = chat.thread(CHANNEL, OTHER_THREAD);
  assert.notEqual(other, thread);
  assert.equal(chat.limiter, limiter);
  await other.showStatus("working");
  assert.equal(slack.callsTo("reactions.add")[0]?.timestamp, OTHER_THREAD);
});
