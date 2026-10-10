/**
 * A session's replies as native Slack streams: what starts one, what ends it, what cuts a turn
 * short, and what a failed end leaves behind. The sink's own behaviour is tested in
 * `test/chat/slack/reply/`; here the session drives it. Port of `tests/test_sessions_stream.py`.
 */
import assert from "node:assert/strict";
import { mkdirSync, rmdirSync } from "node:fs";
import { join } from "node:path";
import { mock, type TestContext, test } from "node:test";
import {
  BANNER_LIMIT,
  contextBlock,
  ZERO_WIDTH_SPACE,
} from "../../../src/chat/slack/reply/blocks.ts";
import { bannerText } from "../../../src/chat/slack/reply/markdown.ts";
import { STREAM_SECONDS } from "../../../src/chat/slack/reply/sinks.ts";
import { Status } from "../../../src/chat/slack/reply/status.ts";
import { APPROVE } from "../../../src/core/requests.ts";
import { asked } from "../../../src/core/sessions/turn.ts";
import * as texts from "../../../src/core/texts.ts";
import { AsyncEvent, CHANNEL, FakeSlack, networkDown, THREAD } from "../../support/fake-slack.ts";
import type { JsonObject } from "../../support/fixtures.ts";
import {
  canUseToolCall,
  END_OF_STREAM,
  type Harness,
  type HarnessOptions,
  harnessFor,
  type Item,
  type Script,
  sdkMessages,
  splitTurns,
} from "../../support/sessions.ts";
import {
  footerWrites,
  inside,
  isReport,
  nestedBackgroundEndingMidTurn,
  pending,
  splitBackground,
  systemRecord,
} from "./helpers.ts";

const WRITES = ["chat.postMessage", "chat.startStream", "chat.appendStream", "chat.stopStream"];

// Python's `fast` fixture: every test of this file runs with a short debounce.
const FAST: HarnessOptions = { debounceSeconds: 0.01 };

function harness(t: TestContext, scripts: Script[], options: HarnessOptions = {}): Harness {
  return harnessFor(t, { ...FAST, ...options })(...scripts);
}

/**
 * The recorded `tools` turn, asking for permission right after its Bash call starts: Claude has
 * already written by then.
 */
function withAsk(ask: Item): Item[] {
  const messages = sdkMessages("tools");
  return [...messages.slice(0, 21), ask, ...messages.slice(21)];
}

function writes(h: Harness): string[] {
  return h.slack.apiCalls
    .map((call) => call.method)
    .filter((method) => [...WRITES, "chat.update"].includes(method));
}

function openStreams(h: Harness): string[] {
  return [...h.slack.messages].filter(([, message]) => message.streaming).map(([ts]) => ts);
}

function blocksOf(args: JsonObject | undefined): JsonObject[] {
  return (args?.blocks ?? []) as JsonObject[];
}

function approve(h: Harness): void {
  const [approvalId] = pending(h.approvals);
  assert.ok(approvalId !== undefined);
  assert.notEqual(h.approvals.resolve(approvalId, CHANNEL, THREAD, APPROVE), null);
}

function thread(h: Harness) {
  const stored = h.state.thread(CHANNEL, THREAD);
  assert.ok(stored !== null);
  return stored;
}

test("a turn is one stream that stops with the footer", async (t) => {
  const h = harness(t, [{ turns: [sdkMessages("tools")] }]);
  const turn = await h.session().submit("list the files");
  await turn.done.wait();
  assert.ok(h.slack.streamTs.length === 1 && h.slack.postedTs.length === 0); // no post, no placeholder
  const [start, ...moreStarts] = h.slack.callsTo("chat.startStream");
  assert.equal(moreStarts.length, 0);
  assert.ok(start?.thread_ts === THREAD && start.recipient_user_id === "U000ALICE");
  const [stop, ...moreStops] = h.slack.callsTo("chat.stopStream");
  assert.equal(moreStops.length, 0);
  assert.deepEqual(blocksOf(stop)[0], { type: "divider" });
  assert.equal(blocksOf(stop).at(-1)?.type, "context");
  assert.equal(h.slack.pushes(), 1);
  assert.deepEqual(openStreams(h), []);
});

test("submitting writes nothing until claude has something to show", async (t) => {
  const h = harness(t, [{}]); // a turn that never answers
  const session = h.session();
  await session.submit("hello");
  await h.until(() => h.clients.length > 0 && h.clients.at(-1)?.queries.join() === "hello");
  await h.sleep(0.05);
  assert.deepEqual(writes(h), []); // nothing says Claude is writing, or waiting for the previous reply
});

test("a queued message has no reply until its turn starts", async (t) => {
  const ask = canUseToolCall("Bash", { command: "ls" });
  const h = harness(t, [{ turns: [[ask, ...sdkMessages("tools")], sdkMessages("tools")] }]);
  const session = h.session();
  const first = await session.submit("first");
  const second = await session.submit("second");
  await h.until(() => pending(h.approvals).length > 0);
  await h.sleep(0.05);
  assert.deepEqual(h.slack.streamTs, []); // the approval is a post; neither reply has started
  approve(h);
  await second.done.wait();
  assert.ok(first.done.isSet);
  assert.equal(h.slack.streamTs.length, 2);
  assert.deepEqual(openStreams(h), []);
  assert.equal(h.slack.pushes(), 3); // the approval request, and each reply once
});

test("a tool first turn and its cards read as the recorded turn", async (t) => {
  const h = harness(t, [{ turns: [sdkMessages("tools")] }]);
  const turn = await h.session().submit("list the files");
  await turn.done.wait();
  const cards = h.slack.apiCalls
    .flatMap((call) => (call.args.chunks ?? []) as JsonObject[])
    .filter((chunk) => chunk.type === "task_update");
  assert.ok(cards.length > 0 && cards.every((card) => card.title));
  // Once the body has ended the run of calls is one line of counts, in the terminal's words.
  const [blocks, ...more] = h.slack.messageBlocks();
  assert.equal(more.length, 0);
  const elements = blocks?.[0]?.elements as JsonObject[];
  assert.equal(elements[0]?.text, "✓ Ran 1 shell command · Read 1 file");
  assert.deepEqual(h.slack.messageCards(), [[]]);
});

test("a reply waits for its background task before it stops", async (t) => {
  const [first, notice, injected] = splitBackground();
  const h = harness(t, [{ turns: [first] }]);
  await (await h.session().submit("start it")).done.wait();
  await h.sleep(0.05);
  assert.equal(h.slack.streamTs.length, 1);
  assert.deepEqual(openStreams(h), h.slack.streamTs); // still open
  assert.deepEqual(h.slack.callsTo("chat.stopStream"), []);
  assert.equal(h.slack.pushes(), 0);
  h.clients[0]?.inject([...notice, ...injected]);
  await h.until(() => h.slack.callsTo("chat.stopStream").length > 0);
  const summary = systemRecord(notice, "task_notification").summary;
  assert.ok(h.slack.streamTexts()[0]?.includes(`✓ ${summary}`)); // the report joins the same stream
  assert.ok(h.slack.streamTs.length === 1 && h.slack.pushes() === 1);
  assert.ok(h.slack.messageBlocks()[0]?.some((block) => block.type === "divider"));
});

test("a process lost while a task runs says so in the reply that waits for it", async (t) => {
  // Issue #202, seen live on 2026-10-10: the turn has ended, its background command still
  // runs, and the Claude Code process is lost. The reply closed with its footer as if all had
  // ended well; the cross on the root was the only sign.
  const [first] = splitBackground();
  const h = harness(t, [{ turns: [first] }]);
  await (await h.session().submit("start it")).done.wait();
  await h.sleep(0.05);
  assert.deepEqual(openStreams(h), h.slack.streamTs); // the reply waits for its task
  h.clients[0]?.inject([END_OF_STREAM]);
  await h.until(() => h.slack.callsTo("chat.stopStream").length > 0);
  await h.until(() => h.reactions().at(-1) === Status.ERROR);
  const said = h.slack.streamTexts()[0]?.split("Claude Code reported an error").length;
  assert.equal(said, 2);
  assert.ok(h.slack.streamTs.length === 1 && h.slack.pushes() === 1);
});

test("a process lost after its task was reported changes nothing in the thread", async (t) => {
  // The reply of a task that ended and was reported is still tracked while the process lives:
  // it has closed with its footer, so it waits for nothing and is left as it is.
  const [first, notice, injected] = splitBackground();
  const h = harness(t, [{ turns: [first] }]);
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  h.clients[0]?.inject([...notice, ...injected]);
  await h.until(() => h.slack.callsTo("chat.stopStream").length > 0);
  await h.until(() => h.reactions().at(-1) === Status.DONE);
  await h.sleep(0.1);
  const before = h.slack.apiCalls.length;
  h.clients[0]?.inject([END_OF_STREAM]);
  await h.until(() => inside(session).client === null);
  await h.sleep(0.1);
  assert.equal(h.reactions().at(-1), Status.DONE);
  const later = h.slack.apiCalls.slice(before).map((call) => call.method);
  assert.deepEqual(
    later.filter((name) => name !== "assistant.threads.setStatus"),
    [],
  );
  assert.ok(!JSON.stringify(h.slack.messageBlocks()).includes("reported an error"));
});

test("a process lost while the session is idle changes nothing in the thread", async (t) => {
  // Issue #202, seen live on 2026-10-10: nothing runs and nothing waits, so the reply that
  // ended well keeps its check. The next message connects a new process.
  const h = harness(t, [{ turns: [sdkMessages("usage")] }, { turns: [sdkMessages("usage")] }]);
  const session = h.session();
  await (await session.submit("hello")).done.wait();
  await h.until(() => h.reactions().at(-1) === Status.DONE);
  const before = h.slack.apiCalls.length;
  h.clients[0]?.inject([END_OF_STREAM]);
  await h.until(() => inside(session).client === null);
  await h.sleep(0.1);
  assert.equal(h.reactions().at(-1), Status.DONE);
  // Clearing a thread status that was already empty shows nothing: no message, no reaction.
  const later = h.slack.apiCalls.slice(before).map((call) => call.method);
  assert.deepEqual(
    later.filter((name) => name !== "assistant.threads.setStatus"),
    [],
  );
  await (await session.submit("again")).done.wait();
  assert.equal(h.clients.length, 2);
});

test("a reply that outlives the stream ends with its last words and the footer", async (t) => {
  const ask = canUseToolCall("Bash", { command: "ls" });
  const h = harness(t, [{ turns: [withAsk(ask)] }]);
  const turn = await h.session().submit("list the files");
  await h.until(() => pending(h.approvals).length > 0);
  await h.sleep(0.1);
  await h.slackClock.advance(STREAM_SECONDS + 1);
  assert.equal(h.slack.callsTo("chat.stopStream").length, 1); // the 280 s stop pushes (accepted)
  approve(h);
  await turn.done.wait();
  const ending = h.slack.callsTo("chat.postMessage").at(-1);
  assert.equal(blocksOf(ending).at(-1)?.type, "context"); // the footer
  assert.equal(blocksOf(ending)[0]?.type, "markdown"); // under the answer's last paragraph
  const lastWords = String(blocksOf(ending)[0]?.text);
  assert.equal(ending?.text, Array.from(bannerText(lastWords)).slice(0, BANNER_LIMIT).join(""));
  assert.ok(!h.slack.streamTexts()[0]?.includes(lastWords)); // moved, not shown twice
  assert.equal(h.slack.pushes(), 3); // the approval request, the 280 s stop, the ending
});

// `!stop` ends like any other end, and a restart with queued messages says so once.

test("stop ends the stream with the footer and shows the checkmark", async (t) => {
  const h = harness(t, [
    { turns: [[canUseToolCall("Bash", { command: "rm -rf build" }), ...sdkMessages("interrupt")]] },
  ]);
  const session = h.session();
  const turn = await session.submit("clean");
  await h.until(() => pending(h.approvals).length > 0);
  assert.equal(await session.stop(), true);
  await turn.done.wait();
  assert.ok(h.slack.streamTs.length === 1 && openStreams(h).length === 0);
  const [stop, ...more] = h.slack.callsTo("chat.stopStream");
  assert.equal(more.length, 0);
  assert.equal(blocksOf(stop).at(-1)?.type, "context"); // a normal end: the footer, one push
  assert.equal(h.slack.pushes(), 2); // the approval request (deleted by the stop), and the reply
  await h.until(() => h.reactions().at(-1) === Status.DONE);
});

test("a restart with queued messages ends the running reply with one note", async (t) => {
  const ask = canUseToolCall("Bash", { command: "ls" });
  const h = harness(t, [{ turns: [withAsk(ask)] }]);
  const session = h.session();
  const first = await session.submit("first question");
  await session.submit("second question");
  await session.submit("third question");
  await h.until(() => pending(h.approvals).length > 0 && session.busy);
  await session.dropQueued({ error: true });
  approve(h);
  await first.done.wait();
  const [reply, ...more] = h.slack.streamTexts();
  assert.equal(more.length, 0);
  const note = "2 messages were not sent because awaydesk restarted: send them again.";
  assert.ok(reply?.includes(note));
  assert.ok(reply?.includes('"second question"') && reply.includes('"third question"'));
  assert.equal(h.slack.streamTs.length, 1); // the dropped messages get no reply of their own
  assert.equal(h.slack.pushes(), 2); // the approval request, and this one end
  // the reply that says it is the thread's latest: the footer is under it
  assert.ok(blocksOf(h.slack.callsTo("chat.stopStream").at(-1)).some((b) => b.type === "divider"));
  assert.ok(h.reactions().includes(Status.ERROR)); // a dropped message reacts ❌
});

test("dropped messages with nothing running get one message", async (t) => {
  const gate = new AsyncEvent();
  const h = harness(t, [{ startGate: gate }]);
  const session = h.session();
  await session.submit("first"); // taken by the worker, waiting for the client to connect
  await session.submit("second");
  await session.submit("third");
  await h.sleep(0.05);
  await session.dropQueued({ error: true });
  const [post, ...more] = h.slack.callsTo("chat.postMessage");
  assert.equal(more.length, 0);
  assert.ok(String(post?.text).includes("2 messages were not sent because awaydesk restarted"));
  assert.deepEqual(h.slack.streamTs, []);
  gate.set();
});

test("one dropped message is named in the singular", async (t) => {
  const gate = new AsyncEvent();
  const h = harness(t, [{ startGate: gate }]);
  const session = h.session();
  await session.submit("first");
  await session.submit("only the second");
  await h.sleep(0.05);
  await session.dropQueued({ error: true });
  const [post, ...more] = h.slack.callsTo("chat.postMessage");
  assert.equal(more.length, 0);
  assert.ok(
    String(post?.text).includes(
      "1 message was not sent because awaydesk restarted: send it again.",
    ),
  );
  gate.set();
});

test("closing a session ends its open reply and leaves no stream open", async (t) => {
  const h = harness(t, [{ turns: [sdkMessages("tools").slice(0, 21)] }]); // starts, never ends
  const session = h.session();
  await session.submit("hello");
  await h.until(() => h.slack.streamTs.length > 0);
  await session.close();
  assert.deepEqual(openStreams(h), []);
  const ended = texts.fill(texts.ENDED, { reason: texts.ENDED_SHUTDOWN });
  assert.ok(h.slack.streamTexts()[0]?.includes(ended));
  assert.equal(h.slack.pushes(), 1);
});

test("an error that cuts a turn ends the stream with the cross", async (t) => {
  const h = harness(t, [{ turns: [[...sdkMessages("tools").slice(0, 21), END_OF_STREAM]] }]);
  const turn = await h.session().submit("hello");
  await turn.done.wait();
  assert.deepEqual(openStreams(h), []);
  await h.until(() => h.reactions().at(-1) === Status.ERROR);
  assert.ok(h.slack.streamTexts()[0]?.includes("Claude Code reported an error"));
  // the call that was running is closed, and counted like any call that ended
  const [blocks, ...more] = h.slack.messageBlocks();
  assert.equal(more.length, 0);
  const lines = (blocks ?? [])
    .filter((block) => block.type === "context")
    .map((block) => (block.elements as JsonObject[])[0]?.text);
  assert.equal(lines[0], "✓ Ran 1 shell command");
  assert.equal(h.slack.pushes(), 1);
});

test("an error that cuts a turn after its stream stopped says so in the message that rings", async (t) => {
  // Issue #165: the process lost past STREAM_SECONDS, the reply's last part a tool call. The
  // closing message showed empty, and its text named a command that had run.
  const messages = sdkMessages("tools");
  const h = harness(t, [{ turns: [messages.slice(0, 21)] }]);
  const turn = await h.session().submit("list the files");
  await h.until(() => h.slack.callsTo("chat.startStream").length > 0);
  await h.sleep(0.1);
  await h.slackClock.advance(STREAM_SECONDS + 1);
  h.clients[0]?.inject([...messages.slice(21, 58), END_OF_STREAM]); // cut after the second tool result
  await turn.done.wait();
  await h.until(() => h.reactions().at(-1) === Status.ERROR);
  await h.until(() => h.slack.callsTo("chat.postMessage").length > 0);
  const [closing, ...more] = h.slack.callsTo("chat.postMessage");
  assert.equal(more.length, 0);
  const [block, ...moreBlocks] = blocksOf(closing);
  assert.equal(moreBlocks.length, 0);
  assert.equal(block?.type, "markdown");
  assert.ok(String(block?.text).startsWith("Claude Code reported an error"));
  assert.ok(String(closing?.text).startsWith("Claude Code reported an error"));
  await h.sleep(0.1);
  // moved, not shown twice
  assert.ok(!JSON.stringify(h.slack.messageBlocks()[0]).includes("reported an error"));
  assert.equal(h.slack.pushes(), 2); // the 280 s stop, and the message that says how it ended
});

test("an error that cuts a turn while its subagent works says so in the message that rings", async (t) => {
  // Issue #165 with a task still counted: the reply closes while `⏳ 1 agent` still shows, and
  // the list empties right after. The closing message was that list alone, then empty.
  const messages = sdkMessages("subagent-foreground");
  const h = harness(t, [{ turns: [messages.slice(0, 37)] }]);
  const turn = await h.session().submit("run it in a subagent");
  await h.until(() => h.slack.callsTo("chat.startStream").length > 0);
  await h.sleep(0.1);
  await h.slackClock.advance(STREAM_SECONDS + 1);
  h.clients[0]?.inject([...messages.slice(37, 44), END_OF_STREAM]); // cut while the subagent works
  await turn.done.wait();
  await h.until(() => h.slack.callsTo("chat.postMessage").length > 0);
  const [closing, ...more] = h.slack.callsTo("chat.postMessage");
  assert.equal(more.length, 0);
  assert.ok(String(closing?.text).startsWith("Claude Code reported an error"));
  assert.ok(String(blocksOf(closing)[0]?.text).startsWith("Claude Code reported an error"));
  await h.until(() => h.slack.messageBlocks().at(-1)?.length === 1); // the list went with the process
  const [block] = h.slack.messageBlocks().at(-1) ?? [];
  assert.equal(block?.type, "markdown");
  assert.ok(String(block?.text).startsWith("Claude Code reported an error"));
  // moved, not shown twice
  assert.ok(!JSON.stringify(h.slack.messageBlocks()[0]).includes("reported an error"));
  assert.equal(h.slack.pushes(), 2);
});

test("a turn that fails before claude answers is a reply of its own", async (t) => {
  const h = harness(t, [{}]);
  const gone = join(h.tmpPath, "gone");
  mkdirSync(gone);
  h.state.bind(CHANNEL, gone);
  rmdirSync(gone);
  const failed = await h.session().submit("hello");
  await failed.done.wait();
  assert.deepEqual(h.slack.streamTexts(), [
    texts.fill(texts.DIRECTORY_MISSING, { directory: gone }),
  ]);
  assert.ok(h.slack.pushes() === 1 && openStreams(h).length === 0);
});

// A final write that fails: no checkmark, the cross, and the persisted status kept for repair.

// Python's `lose_stops`: every stop of a stream fails on the network, and the one retry of a
// reply's end comes after `finalRetrySeconds` (the harness option).
function loseStops(h: Harness): void {
  h.slack.responses["chat.stopStream"] = networkDown();
}

test("a reply whose end failed shows no checkmark then the cross", async (t) => {
  const h = harness(t, [{ turns: [sdkMessages("tools")] }], { finalRetrySeconds: 0.05 });
  loseStops(h);
  const turn = await h.session().submit("list the files");
  await turn.done.wait();
  await h.sleep(0.02); // the stop failed; its retry is still waiting
  assert.ok(!h.reactions().includes(Status.DONE));
  assert.equal(thread(h).status, Status.WORKING);
  await h.until(() => h.reactions().at(-1) === Status.ERROR);
  assert.ok(!h.reactions().includes(Status.DONE));
  // kept, so a crash right now is still repaired
  assert.equal(thread(h).status, Status.WORKING);
  // and the cross the root shows is what the session index reads
  assert.equal(thread(h).ended, Status.ERROR);
  assert.deepEqual(thread(h).openReplies, [h.slack.streamTs[0]]);
});

test("a reply whose appends slack refuses ends whole with the checkmark", async (t) => {
  const [first, notice, injected] = splitBackground();
  const h = harness(t, [{ turns: [first] }], { finalRetrySeconds: 0.05 });
  await (await h.session().submit("start it")).done.wait();
  await h.sleep(0.05);
  assert.deepEqual(openStreams(h), h.slack.streamTs); // open: the report is owed to this stream
  h.slack.responses["chat.appendStream"] = { ok: false, error: "msg_too_long" };
  h.clients[0]?.inject([...notice, ...injected]);
  await h.until(() => h.slack.callsTo("chat.appendStream").length > 0); // the refusal did happen
  await h.sleep(0.2); // past where a failed end's retry would have shown the cross
  assert.equal(h.reactions().at(-1), Status.DONE);
  assert.ok(!h.reactions().includes(Status.ERROR));
  const summary = systemRecord(notice, "task_notification").summary;
  assert.ok(h.slack.streamTexts()[0]?.includes(`✓ ${summary}`)); // the report reached the reply
  assert.deepEqual(openStreams(h), []);
  assert.equal(thread(h).status, null);
  assert.deepEqual(thread(h).openReplies, []);
  // the footer is in the closing message, as for a reply past STREAM_SECONDS
  assert.equal(blocksOf(h.slack.callsTo("chat.postMessage").at(-1)).at(-1)?.type, "context");
});

test("a reply whose stream slack stopped is not ended while its turn is still active", async (t) => {
  // Issue #149 as it was seen live: a refused append had already stopped the stream, so the
  // early end took the form of a closing message with nothing in it, and no footer followed.
  const [head, commandEnd, tail] = nestedBackgroundEndingMidTurn();
  const h = harness(t, [{ turns: [head] }]);
  h.slack.responses["chat.appendStream"] = { ok: false, error: "msg_too_long" };
  const session = h.session();
  const turn = await session.submit("start it");
  await h.until(() => h.slack.callsTo("chat.startStream").length > 0);
  h.clients[0]?.inject(commandEnd); // its card is the append Slack refuses
  await h.until(() => h.slack.callsTo("chat.stopStream").length === 1); // which stops the stream
  await h.sleep(0.3);
  assert.ok(inside(session).active !== null && !turn.done.isSet);
  assert.deepEqual(h.slack.callsTo("chat.postMessage"), []); // no closing message before the turn ends

  h.clients[0]?.inject(tail);
  await turn.done.wait();
  await h.until(() => h.reactions().at(-1) === Status.DONE);
  assert.equal(footerWrites(h).length, 1); // the turn's own end wrote the footer, once
  const empty = JSON.stringify([contextBlock(ZERO_WIDTH_SPACE)]);
  assert.ok(h.slack.apiCalls.every((call) => JSON.stringify(call.args.blocks) !== empty));
});

test("a reply slack refuses to grow by append and by update shows the cross", async (t) => {
  const [first, notice, injected] = splitBackground();
  const h = harness(t, [{ turns: [first] }], { finalRetrySeconds: 0.05 });
  await (await h.session().submit("start it")).done.wait();
  await h.sleep(0.05);
  h.slack.responses["chat.appendStream"] = { ok: false, error: "msg_too_long" };
  h.slack.responses["chat.update"] = { ok: false, error: "msg_too_long" };
  h.clients[0]?.inject([...notice, ...injected]);
  await h.until(() => h.reactions().at(-1) === Status.ERROR);
  await h.sleep(0.1);
  assert.notEqual(h.reactions().at(-1), Status.DONE);
  const summary = systemRecord(notice, "task_notification").summary;
  // the report never reached the reply
  assert.ok(!h.slack.streamTexts()[0]?.includes(`✓ ${summary}`));
});

test("a retry that lands shows the checkmark and clears the status", async (t) => {
  const h = harness(t, [{ turns: [sdkMessages("tools")] }], { finalRetrySeconds: 0.05 });
  h.slack.responses["chat.stopStream"] = [networkDown(), { ok: true }];
  const turn = await h.session().submit("list the files");
  await turn.done.wait();
  await h.sleep(0.01);
  assert.ok(!h.reactions().includes(Status.DONE));
  await h.until(() => h.reactions().at(-1) === Status.DONE);
  assert.ok(!h.reactions().includes(Status.ERROR));
  assert.equal(thread(h).status, null);
  assert.deepEqual(thread(h).openReplies, []);
});

test("a close that cannot end a reply shows the cross and keeps what repair needs", async (t) => {
  // The close falls in the window before the one retry.
  const h = harness(t, [{ turns: [sdkMessages("tools")] }], { finalRetrySeconds: 60.0 });
  loseStops(h);
  const session = h.session();
  await (await session.submit("list")).done.wait();
  assert.ok(!h.reactions().includes(Status.DONE));
  await session.close();
  assert.equal(h.reactions().at(-1), Status.ERROR);
  const stored = thread(h);
  assert.equal(stored.status, Status.WORKING); // repair covers it
  assert.equal(stored.ended, Status.ERROR); // what the root shows, for the session index
  assert.deepEqual(stored.openReplies, [h.slack.streamTs[0]]); // the stream Slack still holds open
});

test("shutdown ends every reply with a retry pending", async (t) => {
  const h = harness(t, [{ turns: [sdkMessages("tools"), sdkMessages("tools")] }], {
    finalRetrySeconds: 60.0,
  });
  h.slack.responses["chat.stopStream"] = networkDown();
  const session = h.session();
  await (await session.submit("a")).done.wait();
  await (await session.submit("b")).done.wait();
  assert.equal(openStreams(h).length, 2); // both stops failed, their retries pending
  h.slack.responses["chat.stopStream"] = { ok: true };
  await session.close();
  assert.deepEqual(openStreams(h), []); // each ended once at the close
  assert.equal(thread(h).status, null);
});

test("the status is persisted until the end lands", async (t) => {
  // A crash before the stop lands must still be repaired: the persisted status is cleared only
  // after the end has landed.
  const h = harness(t, [{ turns: [sdkMessages("tools")] }]);
  const seen: Array<string | null> = [];
  // Python wrapped `api_call`; the fake's scripted answer is called with the call's arguments at
  // the same point.
  h.slack.responses["chat.stopStream"] = (args) => {
    seen.push(thread(h).status);
    return { ok: true, channel: args.channel ?? null, ts: args.ts ?? null };
  };
  const turn = await h.session().submit("list the files");
  await turn.done.wait();
  assert.deepEqual(seen, [Status.WORKING]);
});

test("the open reply is the stream until it ends", async (t) => {
  const ask = canUseToolCall("Bash", { command: "ls" });
  const h = harness(t, [{ turns: [withAsk(ask)] }]);
  const turn = await h.session().submit("list the files");
  await h.until(() => pending(h.approvals).length > 0 && h.slack.streamTs.length > 0);
  assert.deepEqual(thread(h).openReplies, [h.slack.streamTs[0]]);
  approve(h);
  await turn.done.wait();
  assert.deepEqual(thread(h).openReplies, []);
});

test("a crossed owner query is answered in the reply it landed in", async (t) => {
  // `settle`: the start guessed a background report, the result says the owner asked. The
  // answer is in the misrouted reply; the owner turn's own reply is never written.
  const h = harness(t, [{ turns: [] }]);
  const session = h.session();
  const owner = await session.submit("what happened");
  await h.until(() => h.clients.length > 0 && h.clients.at(-1)?.queries.join() === "what happened");
  inside(session).expectInjectedTurn();
  h.clients[0]?.inject(splitTurns(sdkMessages("tools"))[0] ?? []);
  await owner.done.wait();
  assert.ok(h.slack.streamTs.length === 1 && h.slack.postedTs.length === 0);
  assert.deepEqual(openStreams(h), []);
  // the reply that carried the answer is the thread's latest, not the discarded owner reply:
  // it ends with the footer
  const [stop, ...more] = h.slack.callsTo("chat.stopStream");
  assert.equal(more.length, 0);
  assert.ok(blocksOf(stop).some((block) => block.type === "divider"));
  assert.ok(inside(session).latest !== null && inside(session).latest !== owner.sink);
});

test("a crossed owner query hands latest to the reply that carried its answer", async (t) => {
  // The answer went into the reply that started a task (an older reply, no longer the latest
  // once the owner wrote again). The owner turn's own reply is never written, so it must not
  // stay the thread's latest, or the footer that belongs under the answer has no reply to show.
  const [first] = splitBackground();
  const taskId = String(systemRecord(first, "task_started").task_id);
  const h = harness(t, [{ turns: [first] }]);
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  const holder = inside(session).taskReplies.get(taskId);
  assert.ok(holder !== undefined);
  const carrier = holder.sink;
  const owner = await session.submit("next");
  await h.until(() => h.clients[0]?.queries.join() === "start it,next");
  assert.ok(inside(session).latest === owner.sink && carrier !== owner.sink);
  inside(session).injectedExpected = true; // a report was expected too: the guess that goes wrong
  inside(session).ended = [[taskId, "✓ the task ended"]];
  h.clients[0]?.inject(splitTurns(sdkMessages("tools"))[0] ?? []);
  await owner.done.wait();
  assert.equal(inside(session).latest, carrier);
  // the discarded reply is no longer the latest
  assert.equal((owner.sink as unknown as { latest: boolean }).latest, false);
});

test("the report helper still reads a report", () => {
  assert.ok(isReport(texts.BACKGROUND_NOTICE));
  assert.equal(asked("hello"), "hello");
});

test("a close leaves no task waiting on a reply it could not end", async (t) => {
  const h = harness(t, [{ turns: [withAsk(canUseToolCall("Bash", { command: "ls" }))] }], {
    finalRetrySeconds: 60.0,
  });
  h.slack.responses["chat.stopStream"] = networkDown();
  const session = h.session();
  // Python looked for a live `_await_landing` task; here each wait for a landing is counted in
  // and out.
  let waiting = 0;
  const original = inside(session).awaitLanding.bind(session);
  mock.method(inside(session), "awaitLanding", async (reply: Parameters<typeof original>[0]) => {
    waiting += 1;
    try {
      await original(reply);
    } finally {
      waiting -= 1;
    }
  });
  await session.submit("hello");
  await h.until(() => pending(h.approvals).length > 0 && h.slack.streamTs.length > 0);
  await session.close();
  assert.equal(waiting, 0); // `close` settles the reply itself: nobody would await these
});

test("a plain shutdown does not say the daemon restarted", async (t) => {
  const gate = new AsyncEvent();
  const h = harness(t, [{ startGate: gate }]);
  const session = h.session();
  await session.submit("hello");
  await h.sleep(0.05);
  await session.close(); // SIGINT: no drain ran, nothing says a restart
  const [note, ...more] = h.slack.callsTo("chat.postMessage");
  assert.equal(more.length, 0);
  assert.ok(!String(note?.text).includes("restarted"));
  const said = texts.fill(texts.NOT_SENT_ONE, { count: 1, because: texts.BECAUSE_SHUTDOWN });
  assert.ok(String(note?.text).includes(said));
});

test("a reply is written through the client made for replies", async (t) => {
  const replies = new FakeSlack();
  const h = harness(t, [{ turns: [sdkMessages("tools")] }], { replies });
  const turn = await h.session().submit("list the files");
  await turn.done.wait();
  // the reply, not the approvals, uses it
  assert.ok(replies.streamTs.length > 0 && h.slack.streamTs.length === 0);
});

// The thread's status line: `Working…` (issue #83), then what still runs (issue #95).

/** Every status the thread was given, in order: the empty one clears it. */
function statuses(h: Harness): string[] {
  return h.slack.callsTo("assistant.threads.setStatus").map((args) => String(args.status));
}

/**
 * What the thread's status line said, in order: the loading message a client shows, and an
 * empty string where it was cleared.
 */
function lines(h: Harness): string[] {
  return h.slack.callsTo("assistant.threads.setStatus").map((args) => {
    const messages = args.loading_messages;
    return Array.isArray(messages) && messages.length > 0 ? String(messages[0]) : "";
  });
}

test("a prompt shows the thread status before claude writes anything", async (t) => {
  const h = harness(t, [{}]); // a turn that never answers
  await h.session().submit("hello");
  await h.until(() => statuses(h).length > 0);
  const [call, ...more] = h.slack.callsTo("assistant.threads.setStatus");
  assert.equal(more.length, 0);
  assert.deepEqual(call, {
    channel_id: CHANNEL,
    thread_ts: THREAD,
    status: texts.THREAD_WORKING_STATUS,
    loading_messages: [texts.THREAD_WORKING],
  });
  assert.deepEqual(writes(h), []); // the status is not a message
});

test("the thread status is cleared when the turn ends", async (t) => {
  const h = harness(t, [{ turns: [sdkMessages("tools")] }]);
  const turn = await h.session().submit("list the files");
  await turn.done.wait();
  await h.until(() => statuses(h).at(-1) === "");
  assert.equal(statuses(h)[0], texts.THREAD_WORKING_STATUS);
});

test("the thread status goes while an approval waits and comes back after it", async (t) => {
  const ask = canUseToolCall("Bash", { command: "ls" });
  const h = harness(t, [{ turns: [withAsk(ask)] }]);
  const turn = await h.session().submit("list the files");
  await h.until(() => pending(h.approvals).length > 0);
  await h.until(() => statuses(h).at(-1) === ""); // ✋: the session waits on the owner
  const waiting = statuses(h).length;
  // Python slowed its fake Slack here, since its turn ended within one step of the event loop
  // and took the status back before it was ever set. Here the status line's own task looks at
  // the line one microtask after it changes, which the rest of the turn does not outrun.
  approve(h);
  await turn.done.wait();
  await h.until(() => statuses(h).at(-1) === "");
  // shown again after the answer
  assert.ok(statuses(h).slice(waiting).includes(texts.THREAD_WORKING_STATUS));
});

test("a task that outlives its turn is counted by the thread status", async (t) => {
  const [first, notice, injected] = splitBackground();
  const h = harness(t, [{ turns: [first] }]);
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  const still = `${session.runningKinds} still running`;
  await h.until(() => lines(h).at(-1) === still); // the turn ended: the prompt is back
  assert.equal(statuses(h).at(-1), `has ${session.runningKinds} still running`);
  assert.ok(lines(h)[0] === texts.THREAD_WORKING && session.runningKinds !== "");
  // The count is a state of the thread, never a line of the reply: a stream only grows.
  const told = h.slack.apiCalls.flatMap((call) => (call.args.chunks ?? []) as JsonObject[]);
  assert.ok(!JSON.stringify(told).includes("still running"));
  // the reply is open
  assert.deepEqual(openStreams(h), h.slack.streamTs);
  assert.equal(h.slack.pushes(), 0);
  h.clients[0]?.inject([...notice, ...injected]);
  await h.until(() => h.slack.callsTo("chat.stopStream").length > 0);
  // The report turn says `Working…` again, and the end of everything clears the status.
  await h.until(() => lines(h).at(-1) === "");
  assert.ok(lines(h).slice(lines(h).indexOf(still)).includes(texts.THREAD_WORKING));
  assert.equal(h.slack.pushes(), 1);
});

test("stop clears the thread status", async (t) => {
  const h = harness(t, [{}]); // a turn that never answers: nothing waits on the owner
  const session = h.session();
  await session.submit("hello");
  await h.until(() => h.clients.length > 0 && h.clients.at(-1)?.queries.join() === "hello");
  await h.until(() => statuses(h).join() === texts.THREAD_WORKING_STATUS);
  assert.equal(await session.stop(), true);
  await h.until(() => statuses(h).at(-1) === ""); // while the interrupt still winds down
});

test("a later reply that ended counts the task in its footer not in the status", async (t) => {
  const [first] = splitBackground();
  const h = harness(t, [{ turns: [first, sdkMessages("tools")] }]);
  const session = h.session();
  await (await session.submit("start it")).done.wait();
  const still = `${session.runningKinds} still running`;
  await h.until(() => lines(h).at(-1) === still); // the only reply is open: no footer yet
  await (await session.submit("list the files")).done.wait();
  await h.until(() => h.slack.callsTo("chat.stopStream").length === 1);
  // The thread's last reply has ended: its footer says what still runs, and a status line
  // under that footer would say it twice.
  await h.until(() => lines(h).at(-1) === "");
  const [, second] = h.slack.streamTs;
  const last = h.slack.messages.get(second ?? "")?.blocks.at(-1);
  const footer = String(((last?.elements ?? []) as JsonObject[])[0]?.text);
  assert.ok(session.runningKinds !== "" && footer.endsWith(`⏳ ${session.runningKinds}`));
  await h.sleep(0.05);
  assert.equal(lines(h).at(-1), "");
});

test("closing a session clears the thread status", async (t) => {
  const h = harness(t, [{}]); // a turn that never answers
  const session = h.session();
  await session.submit("hello");
  await h.until(() => statuses(h).length > 0);
  await session.close();
  assert.equal(statuses(h).at(-1), "");
});
