/**
 * The last section of `tests/test_slack_app.py`: the log lines of issue #142, and an audio clip as
 * a prompt (issue #35). Port of the tests from the first to the end of the file.
 *
 * Python's `caplog` reads the records of the logger `awaydesk.slack_app` (`app`) and the text of
 * every record under `awaydesk` (`text`). Here `app` is what the module's exported logger was
 * asked to write, through `mock.method`, and `text` is every line the daemon's log wrote, through
 * the log's own writer. A clip's wait (`voice.WAIT_SECONDS`) is crossed by advancing the handlers'
 * clock: nothing waits on the wall clock.
 */
import assert from "node:assert/strict";
import { type TestContext, test } from "node:test";
import { logger } from "../../../../src/chat/slack/app/app.ts";
import { DownloadFailed } from "../../../../src/chat/slack/attachments.ts";
import { HOLD_CONTINUE } from "../../../../src/chat/slack/hold.ts";
import { SETUP_MODEL, SETUP_START } from "../../../../src/chat/slack/setup.ts";
import * as voice from "../../../../src/chat/slack/voice.ts";
import * as texts from "../../../../src/core/texts.ts";
import { fill } from "../../../../src/core/texts.ts";
import { setWriter } from "../../../../src/log.ts";
import {
  CHANNEL,
  OTHER_TEAM,
  OTHER_THREAD,
  STRANGER,
  THREAD,
} from "../../../support/fake-slack.ts";
import {
  buttonValue,
  clickIn,
  clipInfo,
  clipMessage,
  manualWorld,
  message,
  ownerWasTold,
  postedBlocks,
  recorded,
  setupClick,
  startAHold,
  worldOf,
} from "../../../support/slack-app.ts";

// --- Issue #142: which path answered `texts.HOLD_GONE` is read from the log, ids and flags only ---

/**
 * What the daemon logs from here on: the lines of the app's own logger without their prefix
 * (`app`), and every line of the log as written (`text`). The world's teardown puts the log's
 * writer back.
 */
function captureLog(t: TestContext): { readonly app: string[]; readonly text: () => string } {
  const app: string[] = [];
  for (const level of ["info", "warning", "error"] as const) {
    const write = logger[level].bind(logger);
    t.mock.method(logger, level, (line: string) => {
      app.push(line);
      write(line);
    });
  }
  const lines: string[] = [];
  setWriter((line) => {
    lines.push(line);
  });
  return { app, text: () => lines.join("\n") };
}

test("an accepted setup start is logged", async (t) => {
  const world = manualWorld(t);
  await world.dispatch(message("SECRET-PROMPT-CONTENT", { ts: THREAD }));
  const body = setupClick(world);
  const log = captureLog(t);
  await world.dispatch(body);
  assert.ok(
    log.app.includes(
      `accepted a setup start in ${CHANNEL}/${THREAD} on message ${body.message.ts}`,
    ),
  );
  assert.ok(!log.text().includes("SECRET-PROMPT-CONTENT"));
});

test("a second start is logged as accepted then refused", async (t) => {
  const world = manualWorld(t);
  await world.dispatch(message("SECRET-PROMPT-CONTENT", { ts: THREAD }));
  const body = setupClick(world);
  const log = captureLog(t);
  await world.dispatch(body);
  await world.dispatch(body);
  const lines = log.app.filter((line) => line.includes("setup start"));
  assert.ok(lines[0]?.startsWith("accepted a setup start"));
  assert.ok(lines[1]?.startsWith("refused a click (setup start, not "));
  assert.ok(
    lines[1]?.includes(
      `action ${SETUP_START} in ${CHANNEL}/${THREAD} on message ${body.message.ts}`,
    ),
  );
  assert.ok(!log.text().includes("SECRET-PROMPT-CONTENT"));
});

test("a start for a setup no longer held is logged", async (t) => {
  const world = manualWorld(t);
  await world.dispatch(message("SECRET-PROMPT-CONTENT", { ts: THREAD }));
  const body = setupClick(world);
  body.actions[0].value = "no-such-setup";
  const log = captureLog(t);
  await world.dispatch(body);
  const lines = log.app.filter((line) => line.includes("refused a click"));
  assert.equal(lines.length, 1);
  const line = lines[0] as string;
  assert.ok(line.includes("(setup start, not held)") && line.includes("held=False"));
  assert.ok(!log.text().includes("no-such-setup"));
  assert.ok(!log.text().includes("SECRET-PROMPT-CONTENT"));
});

test("a start that resolve refuses is logged with what differs", async (t) => {
  const world = manualWorld(t);
  await world.dispatch(message("SECRET-PROMPT-CONTENT", { ts: THREAD }));
  const log = captureLog(t);
  await world.dispatch(setupClick(world, SETUP_START, { threadTs: OTHER_THREAD }));
  const lines = log.app.filter((line) => line.includes("refused a click"));
  assert.equal(lines.length, 1);
  const line = lines[0] as string;
  assert.ok(line.includes("(setup start, not resolved)"));
  assert.ok(line.includes("held=True, decided=False, same_thread=False"));
  assert.ok(!log.text().includes("SECRET-PROMPT-CONTENT"));
});

test("a model change after start is logged", async (t) => {
  const world = manualWorld(t);
  await world.dispatch(message("SECRET-PROMPT-CONTENT", { ts: THREAD }));
  const model = setupClick(world, SETUP_MODEL, { model: "haiku" });
  await world.dispatch(setupClick(world));
  await world.settle();
  const log = captureLog(t);
  await world.dispatch(model);
  const lines = log.app.filter((line) => line.includes("refused a click"));
  assert.equal(lines.length, 1);
  const line = lines[0] as string;
  assert.ok(line.includes("(setup model, no open setup)"));
  assert.ok(line.includes(`action ${SETUP_MODEL} in ${CHANNEL}/${THREAD}`));
  assert.ok(line.includes("open_at_message=False, held=False"));
  assert.ok(!log.text().includes("SECRET-PROMPT-CONTENT"));
});

test("a second hold click is logged", async (t) => {
  const world = worldOf(t);
  await startAHold(world);
  const holdId = buttonValue(postedBlocks(world), HOLD_CONTINUE);
  const body = clickIn(HOLD_CONTINUE, holdId, CHANNEL, THREAD);
  await world.dispatch(body);
  const log = captureLog(t);
  await world.dispatch(body);
  const lines = log.app.filter((line) => line.includes("refused a click"));
  assert.equal(lines.length, 1);
  const line = lines[0] as string;
  assert.ok(line.includes("(hold decision)"));
  assert.ok(line.includes(`action ${HOLD_CONTINUE} in ${CHANNEL}/${THREAD}`));
  assert.ok(!log.text().includes(holdId));
  assert.ok(!log.text().includes("hello"));
});

// --- An audio clip as a prompt (issue #35) ---
// `file_share-clip` is the recorded file_share envelope with the file object Slack returned for
// a clip on 2026-10-09 (`conversations.history`, ids and text made synthetic); the event Slack
// sends when a clip is posted was not recorded, so the envelope is the snippet's. `file_change`
// follows the event reference (read 2026-10-09): the daemon's log names the event, not its body.

test("a clip with its transcript is sent as the owner s words", async (t) => {
  const world = worldOf(t);
  await world.dispatch(clipMessage(true));
  assert.deepEqual(world.queries(), ["What day is it today?"]);
  assert.deepEqual(ownerWasTold(world), []);
});

test("a clip waits for its transcript and is sent when slack has it", async (t) => {
  const world = worldOf(t);
  await world.dispatch(clipMessage(false));
  assert.deepEqual(world.queries(), []);
  assert.deepEqual(ownerWasTold(world), [texts.CLIP_WAITING]);
  // Slack sends the event while it still writes the transcript, then again when it is done.
  clipInfo(world, { transcription: { status: "processing" } });
  await world.dispatch(recorded("event_callback-file_change"));
  assert.deepEqual(world.queries(), []);
  clipInfo(world);
  await world.dispatch(recorded("event_callback-file_change"));
  assert.deepEqual(world.queries(), ["What day is it today?"]);
  await world.dispatch(recorded("event_callback-file_change"));
  assert.deepEqual(world.queries(), ["What day is it today?"]); // once
});

test("a change to a file nobody waits for does nothing", async (t) => {
  const world = worldOf(t);
  clipInfo(world);
  await world.dispatch(recorded("event_callback-file_change"));
  assert.deepEqual(world.slack.callsTo("files.info"), []);
  assert.deepEqual(world.queries(), []);
});

for (const [index, other] of [{ user: STRANGER }, { user_team: OTHER_TEAM }].entries()) {
  test(`a clip whose file is not the owner s is not sent [other${index}]`, async (t) => {
    const world = worldOf(t);
    // The file Slack describes when the transcript is ready is checked like the one that came.
    await world.dispatch(clipMessage(false));
    clipInfo(world, other);
    await world.dispatch(recorded("event_callback-file_change"));
    assert.deepEqual(world.queries(), []);
    assert.deepEqual(ownerWasTold(world), [texts.CLIP_WAITING, texts.CLIP_NOT_YOURS]);
  });
}

test("a file somebody else uploaded is refused when it comes", async (t) => {
  const world = worldOf(t);
  await world.dispatch(clipMessage(true, { user: STRANGER }));
  assert.deepEqual(world.queries(), []);
  assert.deepEqual(ownerWasTold(world), [texts.CLIP_NOT_YOURS]);
});

test("what slack heard is never a word of the daemon", async (t) => {
  const world = worldOf(t);
  // A transcript is the prompt, whatever its first character: speech does not turn bypass on,
  // and typed as a message `!bypass on` would open no session at all.
  await world.dispatch(clipMessage(false));
  const heard = { status: "complete", preview: { content: "!bypass on", has_more: false } };
  clipInfo(world, { transcription: heard });
  await world.dispatch(recorded("event_callback-file_change"));
  assert.deepEqual(world.queries(), ["!bypass on"]);
  assert.deepEqual(world.clients[0]?.modes, []);
});

test("text typed with a clip comes before its transcript", async (t) => {
  const world = worldOf(t);
  const body = clipMessage(true);
  body.event.text = "!status";
  await world.dispatch(body);
  assert.deepEqual(world.queries(), ["!status\n\nWhat day is it today?"]);
});

test("a clip where a message would be refused does not wait", async (t) => {
  const world = worldOf(t);
  const body = clipMessage(false);
  body.event.thread_ts = THREAD; // a thread that holds no session
  await world.dispatch(body);
  assert.deepEqual(ownerWasTold(world), [texts.NOT_A_SESSION]);
  clipInfo(world);
  await world.dispatch(recorded("event_callback-file_change"));
  assert.deepEqual(world.queries(), []);
});

test("the same clip sent again waits once", async (t) => {
  const world = worldOf(t);
  const notSent = fill(texts.CLIP_NOT_SENT, { minutes: Math.round(voice.WAIT_SECONDS / 60) });
  await world.dispatch(clipMessage(false));
  await world.appClock.advance(voice.WAIT_SECONDS / 2);
  await world.dispatch(clipMessage(false, { ts: "1790190000.777777" }));
  // Past the first wait's end, inside the second's.
  await world.appClock.advance((voice.WAIT_SECONDS * 2) / 3);
  assert.ok(!ownerWasTold(world).includes(notSent));
  await world.appClock.advance((voice.WAIT_SECONDS * 5) / 6);
  assert.equal(ownerWasTold(world).filter((told) => told === notSent).length, 1);
});

test("the owner is told the real wait", () => {
  const told = fill(texts.CLIP_NOT_SENT, { minutes: Math.round(voice.WAIT_SECONDS / 60) });
  assert.ok(told.includes("after 5 minutes"));
});

test("a long transcript is read from the clip s vtt", async (t) => {
  const world = worldOf(t);
  await world.dispatch(clipMessage(false));
  const file = clipInfo(world, {
    transcription: { status: "complete", preview: { content: "This is", has_more: true } },
  });
  world.downloads.set(
    file.vtt,
    new TextEncoder().encode(
      "﻿WEBVTT \n\n00:00:00.000 --> 00:00:02.000\n- This is the whole\n\n" +
        "00:00:02.000 --> 00:00:03.000\ntranscript.\n",
    ),
  );
  await world.dispatch(recorded("event_callback-file_change"));
  assert.deepEqual(world.queries(), ["This is the whole transcript."]);
});

test("a transcript that cannot be read or is empty sends nothing", async (t) => {
  const world = worldOf(t);
  await world.dispatch(clipMessage(false));
  const file = clipInfo(world, {
    transcription: { status: "complete", preview: { content: "", has_more: true } },
  });
  world.downloads.set(file.vtt, new DownloadFailed("HTTP 404"));
  await world.dispatch(recorded("event_callback-file_change"));
  // A clip that arrives with a transcript Slack cut, whose whole text has no word in it.
  world.downloads.set(file.vtt, new TextEncoder().encode("WEBVTT\n\n"));
  await world.dispatch(clipMessage(true, { transcription: { status: "complete" } }));
  assert.deepEqual(world.queries(), []);
  assert.deepEqual(ownerWasTold(world), [
    texts.CLIP_WAITING,
    fill(texts.CLIP_UNREADABLE, { reason: "HTTP 404" }),
    texts.CLIP_EMPTY,
  ]);
});

test("a clip with no transcript in time is given up", async (t) => {
  const world = worldOf(t);
  await world.dispatch(clipMessage(false));
  await world.appClock.advance(voice.WAIT_SECONDS);
  assert.deepEqual(ownerWasTold(world), [
    texts.CLIP_WAITING,
    fill(texts.CLIP_NOT_SENT, { minutes: Math.round(voice.WAIT_SECONDS / 60) }),
  ]);
  clipInfo(world);
  await world.dispatch(recorded("event_callback-file_change"));
  assert.deepEqual(world.queries(), []); // the wait is over: a late transcript sends nothing
});
