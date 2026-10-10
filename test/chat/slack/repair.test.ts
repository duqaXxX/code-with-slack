import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, test } from "node:test";
import { logger, repairCrash } from "../../../src/chat/slack/repair.ts";
import { UpdateLimiter } from "../../../src/chat/slack/reply/limiter.ts";
import { resetStatusFlags, Status } from "../../../src/chat/slack/reply/status.ts";
import { StateStore } from "../../../src/core/state.ts";
import * as texts from "../../../src/core/texts.ts";
import {
  type Args,
  CHANNEL,
  FakeSlack,
  OTHER_THREAD,
  rejected,
  THREAD,
} from "../../support/fake-slack.ts";
import { type JsonObject, slackPayload } from "../../support/fixtures.ts";

// ported from tests/test_repair.py

const STOPPED_BLOCK = {
  type: "context",
  elements: [{ type: "mrkdwn", text: texts.STOPPED_BEFORE_ANSWER }],
};

const made: string[] = [];

after(() => {
  for (const directory of made) rmSync(directory, { recursive: true, force: true });
});

function scratch(): string {
  const directory = mkdtempSync(join(tmpdir(), "awd-repair-"));
  made.push(directory);
  return directory;
}

interface World {
  directory: string;
  state: StateStore;
  slack: FakeSlack;
}

function makeState(): World {
  const directory = scratch();
  const state = new StateStore(join(directory, "state.json"));
  state.bind(CHANNEL, directory);
  state.openThread(CHANNEL, THREAD);
  return { directory, state, slack: new FakeSlack() };
}

function thread(state: StateStore, ts = THREAD) {
  const found = state.thread(CHANNEL, ts);
  assert.ok(found, "the thread is in state.json");
  return found;
}

function blocksOf(args: Args): Record<string, unknown>[] {
  return args.blocks as unknown as Record<string, unknown>[];
}

test("a second start repairs nothing", async () => {
  const { state, slack } = makeState();
  await repairCrash(slack, state, new UpdateLimiter());
  assert.deepEqual(slack.apiCalls, []);
});

test("an open reply is stopped then rewritten closing its running cards", async () => {
  // Recorded from a real `conversations.replies` response on 2026-09-29 (slack-sdk 3.44.1) of
  // a stopped stream, scrubbed as other fixtures are, one card set back to `in_progress` as
  // an update-phase message a crash cut off would still show it. A card left running in a
  // stopped stream is stored as an error (M33); a card in a message that was updated is not.
  const { state, slack } = makeState();
  const fixture = slackPayload("api-conversations-replies-stream");
  const messages = fixture.messages as { ts: string; blocks: JsonObject[] }[];
  const first = messages[0];
  assert.ok(first);
  const messageTs = first.ts;
  slack.responses["conversations.replies"] = fixture;
  state.replaceOpenReply(CHANNEL, THREAD, null, messageTs);
  await repairCrash(slack, state, new UpdateLimiter());
  assert.deepEqual(
    slack.apiCalls.map((call) => call.method).filter((method) => method.startsWith("chat.")),
    ["chat.stopStream", "chat.update"],
  );
  const [stop] = slack.callsTo("chat.stopStream");
  assert.ok(stop);
  assert.ok(stop.channel === CHANNEL && stop.ts === messageTs && !("blocks" in stop));
  const [call] = slack.callsTo("conversations.replies");
  assert.ok(call);
  assert.ok(call.channel === CHANNEL && call.ts === messageTs && call.limit === 1);
  assert.ok(!("oldest" in call) && !("latest" in call) && !("inclusive" in call));
  const [update] = slack.callsTo("chat.update");
  assert.ok(update);
  assert.ok(update.ts === messageTs && update.text === texts.STOPPED_BEFORE_ANSWER);
  const original = first.blocks;
  assert.deepEqual(
    blocksOf(update)
      .filter((block) => block.type === "task_card")
      .map((block) => block.status),
    ["complete", "error", "complete"],
  );
  // everything else as Slack read it back, and the line that says it stopped, last
  assert.deepEqual(
    blocksOf(update).slice(0, -1),
    original.map((block) => (block.task_id === "t2" ? { ...block, status: "error" } : block)),
  );
  assert.deepEqual(blocksOf(update).at(-1), STOPPED_BLOCK);
  assert.deepEqual(slack.callsTo("chat.postMessage"), []); // nothing is posted: the stop is the push
  assert.deepEqual(thread(state).openReplies, []);
});

test("a stream that slack already closed is rewritten all the same", async () => {
  // Slack ends a stream 5 minutes after it started: the stop then answers that it is over.
  const { state, slack } = makeState();
  const bodyBlocks = [{ type: "rich_text", block_id: "auto1", elements: [] }];
  slack.responses["chat.stopStream"] = rejected("message_not_in_streaming_state");
  slack.responses["conversations.replies"] = {
    ok: true,
    messages: [{ ts: "1790000000.000001", blocks: bodyBlocks }],
  };
  state.replaceOpenReply(CHANNEL, THREAD, null, "1790000000.000001");
  await repairCrash(slack, state, new UpdateLimiter());
  const [update] = slack.callsTo("chat.update");
  assert.ok(update);
  assert.deepEqual(update.blocks, [...bodyBlocks, STOPPED_BLOCK]);
});

test("a stop that fails is logged and the rewrite is still tried", async (t) => {
  const { state, slack } = makeState();
  const warnings: string[] = [];
  t.mock.method(logger, "warning", (message: string) => warnings.push(message));
  slack.responses["chat.stopStream"] = new Error("network down");
  slack.responses["conversations.replies"] = {
    ok: true,
    messages: [{ ts: "1790000000.000001", blocks: [] }],
  };
  state.replaceOpenReply(CHANNEL, THREAD, null, "1790000000.000001");
  await repairCrash(slack, state, new UpdateLimiter());
  assert.ok(warnings.join("\n").includes("could not stop"));
  assert.equal(slack.callsTo("chat.update").length, 1);
});

async function repairedBlocks(
  bodyBlocks: JsonObject[],
): Promise<[Record<string, unknown>[], FakeSlack]> {
  const { state, slack } = makeState();
  slack.responses["conversations.replies"] = {
    ok: true,
    messages: [{ ts: "1790000000.000001", blocks: bodyBlocks }],
  };
  state.replaceOpenReply(CHANNEL, THREAD, null, "1790000000.000001");
  await repairCrash(slack, state, new UpdateLimiter());
  const [update] = slack.callsTo("chat.update");
  assert.ok(update);
  return [blocksOf(update), slack];
}

function content(n: number): JsonObject[] {
  return Array.from({ length: n }, (_, i) => ({
    type: "rich_text",
    block_id: `b${i}`,
    elements: [],
  }));
}

test("a message up to slacks cap less one still gets the stopped line", async () => {
  // Slack's cap is 50 blocks, not the sink's 45: a read-back message (streamed markdown reads
  // back as several blocks) at 49 has room for the line.
  const body = content(49);
  const [blocks] = await repairedBlocks(body);
  assert.deepEqual(blocks, [...body, STOPPED_BLOCK]);
});

test("a message at the cap puts the line in its last context block", async () => {
  const footer = { type: "context", elements: [{ type: "mrkdwn", text: "main · 12% ctx" }] };
  const body = [...content(49), footer];
  const [blocks] = await repairedBlocks(body);
  assert.deepEqual(blocks.slice(0, 49), body.slice(0, 49));
  assert.equal(blocks.length, 50); // no content block lost
  const last = blocks.at(-1) as { elements: { text: string }[] };
  assert.equal(last.elements[0]?.text, `main · 12% ctx · ${texts.STOPPED_BEFORE_ANSWER}`);
});

test("a message at the cap with no context block loses no content", async () => {
  const body = content(50);
  const [blocks, slack] = await repairedBlocks(body);
  assert.deepEqual(blocks, body); // the line is dropped: the edit's `text` still says it stopped
  const [update] = slack.callsTo("chat.update");
  assert.ok(update);
  assert.equal(update.text, texts.STOPPED_BEFORE_ANSWER);
});

test("a deleted reply message is left alone and the field still clears", async () => {
  const { state, slack } = makeState();
  slack.responses["conversations.replies"] = { ok: true, messages: [] };
  state.replaceOpenReply(CHANNEL, THREAD, null, "1790000000.000001");
  await repairCrash(slack, state, new UpdateLimiter());
  assert.deepEqual(slack.callsTo("chat.update"), []);
  assert.deepEqual(thread(state).openReplies, []);
});

test("a failed read leaves the message untouched and still clears the field", async () => {
  const { state, slack } = makeState();
  slack.responses["conversations.replies"] = new Error("network down");
  state.replaceOpenReply(CHANNEL, THREAD, null, "1790000000.000001");
  await repairCrash(slack, state, new UpdateLimiter());
  assert.deepEqual(slack.callsTo("chat.update"), []);
  assert.deepEqual(thread(state).openReplies, []);
});

test("more than one open reply in a thread are all repaired", async () => {
  // A background task's own reply can outlive the turn that started it: two open at once.
  const { state, slack } = makeState();
  slack.responses["conversations.replies"] = { ok: true, messages: [] };
  state.replaceOpenReply(CHANNEL, THREAD, null, "1790000000.000001");
  state.replaceOpenReply(CHANNEL, THREAD, null, "1790000000.000002");
  await repairCrash(slack, state, new UpdateLimiter());
  const read = slack.callsTo("conversations.replies").map((call) => call.ts);
  assert.deepEqual(read, ["1790000000.000001", "1790000000.000002"]);
  assert.deepEqual(thread(state).openReplies, []);
});

test("stale requests are deleted and a gone one still counts as done", async () => {
  const { state, slack } = makeState();
  state.addRequest(CHANNEL, THREAD, "1790000000.000002");
  state.addRequest(CHANNEL, THREAD, "1790000000.000003");
  slack.responses["chat.delete"] = [{ ok: true }, rejected("message_not_found")];
  await repairCrash(slack, state, new UpdateLimiter());
  const deleted = slack.callsTo("chat.delete").map((call) => call.ts);
  assert.deepEqual(deleted, ["1790000000.000002", "1790000000.000003"]);
  assert.deepEqual(thread(state).requests, []);
});

test("a root left waiting or working gets x and the field clears", async () => {
  resetStatusFlags();
  const { state, slack } = makeState();
  state.setStatusPending(CHANNEL, THREAD, Status.WAITING);
  await repairCrash(slack, state, new UpdateLimiter());
  // Through `StatusReaction` (fix round item 9): a fresh instance's first `show` also strips
  // every other stray reaction on the root, not just the one name state.json recorded.
  const removed = new Set(slack.callsTo("reactions.remove").map((call) => call.name));
  const added = slack.callsTo("reactions.add").map((call) => call.name);
  assert.deepEqual(removed, new Set(Object.values(Status).filter((name) => name !== Status.ERROR)));
  assert.deepEqual(added, [Status.ERROR]);
  assert.equal(thread(state).status, null);
  // What the Home tab shows for it from now on.
  assert.equal(thread(state).ended, Status.ERROR);
});

test("already reacted and no reaction count as done", async () => {
  resetStatusFlags();
  const { state, slack } = makeState();
  state.setStatusPending(CHANNEL, THREAD, Status.WORKING);
  slack.responses["reactions.remove"] = rejected("no_reaction");
  slack.responses["reactions.add"] = rejected("already_reacted");
  await repairCrash(slack, state, new UpdateLimiter());
  assert.equal(thread(state).status, null);
});

test("one threads failure does not stop the others", async () => {
  resetStatusFlags();
  const { state, slack } = makeState();
  state.openThread(CHANNEL, OTHER_THREAD);
  state.replaceOpenReply(CHANNEL, THREAD, null, "1790000000.000001");
  state.setStatusPending(CHANNEL, OTHER_THREAD, Status.WORKING);
  slack.responses["conversations.replies"] = new Error("boom");
  await repairCrash(slack, state, new UpdateLimiter());
  assert.deepEqual(thread(state).openReplies, []);
  assert.equal(thread(state, OTHER_THREAD).status, null);
  const added = slack.callsTo("reactions.add").map((call) => call.name);
  assert.deepEqual(added, [Status.ERROR]);
});

test("repair never stores or sends message content", async () => {
  const { directory, state, slack } = makeState();
  state.replaceOpenReply(CHANNEL, THREAD, null, "1790000000.000001");
  const secretBlock = { type: "markdown", text: "secret" };
  slack.responses["conversations.replies"] = {
    ok: true,
    messages: [{ ts: "1790000000.000001", blocks: [secretBlock] }],
  };
  await repairCrash(slack, state, new UpdateLimiter());
  const raw = readFileSync(join(directory, "state.json"), "utf8");
  assert.ok(!raw.includes("secret"));
});

test("a failed state write is logged and swallowed", async (t) => {
  const { state, slack } = makeState();
  state.replaceOpenReply(CHANNEL, THREAD, null, "1790000000.000001");
  slack.responses["conversations.replies"] = { ok: true, messages: [] };
  t.mock.method(state, "replaceOpenReply", () => {
    throw new Error("disk full");
  });
  await repairCrash(slack, state, new UpdateLimiter()); // must not throw
});

test("a context block that leads with an image is left as it is", async () => {
  const image = { type: "image", image_url: "https://example.com/a.png", alt_text: "a" };
  const footer = { type: "context", elements: [image] };
  const body = [...content(49), footer];
  const before = structuredClone(body);
  const [blocks] = await repairedBlocks(body);
  assert.deepEqual(blocks, before); // only text is joined to; the edit's `text` still says it stopped
});
