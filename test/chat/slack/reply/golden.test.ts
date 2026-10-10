/**
 * Parity with the Python daemon: for every recorded SDK stream, the Slack calls this sink makes
 * for the renderer's calls are the ones Python's `ReplySink` made (`tests/golden.py`, seam 2).
 * The discipline is the one each golden file states: `settle()` after a text or a task call, a
 * clock that never advances, a limiter whose budget no recording can spend.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import type { Preview, TaskStatus, TaskUpdate } from "../../../../src/chat/seam.ts";
import { ReplySink, UpdateLimiter } from "../../../../src/chat/slack/reply/sinks.ts";
import {
  BOT,
  CHANNEL,
  FakeClock,
  FakeSlack,
  OWNER,
  TEAM,
  THREAD,
} from "../../../support/fake-slack.ts";
import { GOLDEN, golden, type Json, type JsonObject } from "../../../support/fixtures.ts";

const manifest = JSON.parse(readFileSync(join(GOLDEN, "manifest.json"), "utf8")) as {
  recordings: string[];
};

function object(value: Json | undefined, what: string): JsonObject {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new TypeError(`${what} is not an object`);
  }
  return value;
}

function list(value: Json | undefined, what: string): Json[] {
  if (!Array.isArray(value)) throw new TypeError(`${what} is not a list`);
  return value;
}

function text(value: Json | undefined, what: string): string {
  if (typeof value !== "string") throw new TypeError(`${what} is not a string`);
  return value;
}

function optional(value: Json | undefined, what: string): string | null {
  return value === null || value === undefined ? null : text(value, what);
}

/** A `TaskUpdate` as seam 1 recorded it (`dataclasses.asdict`). */
function taskUpdate(value: Json | undefined): TaskUpdate {
  const fields = object(value, "update");
  let preview: Preview | null = null;
  if (fields.preview !== null && fields.preview !== undefined) {
    const view = object(fields.preview, "preview");
    preview = {
      title: text(view.title, "preview.title"),
      summary: text(view.summary, "preview.summary"),
      body: text(view.body, "preview.body"),
      language: text(view.language, "preview.language") as Preview["language"],
      plain: view.plain === true,
    };
  }
  return {
    id: text(fields.id, "id"),
    title: text(fields.title, "title"),
    status: text(fields.status, "status") as TaskStatus,
    details: optional(fields.details, "details"),
    output: optional(fields.output, "output"),
    name: text(fields.name, "name"),
    task: fields.task === true,
    calls: Number(fields.calls),
    preview,
    folded: optional(fields.folded, "folded"),
  };
}

async function replay(calls: Json[]): Promise<FakeSlack> {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = new ReplySink(slack, {
    channel: CHANNEL,
    threadTs: THREAD,
    teamId: TEAM,
    userId: OWNER,
    botUserId: BOT,
    limiter: new UpdateLimiter({ limit: 10 ** 9, burst: 10 ** 9, clock }),
    clock,
  });
  for (const item of calls) {
    const call = object(item, "call");
    switch (call.call) {
      case "text":
        await sink.text(text(call.markdown, "markdown"), {
          notice: call.notice === true,
          ending: call.ending === true,
        });
        await sink.settle();
        break;
      case "task":
        await sink.task(taskUpdate(call.update));
        await sink.settle();
        break;
      case "finish":
        await sink.finish(list(call.closing, "closing").map(taskUpdate));
        break;
      case "close_out":
        await sink.closeOutFormatted(optional(call.footer, "footer"));
        break;
      case "wait_landed":
        await sink.waitLanded();
        break;
      case "settle":
        await sink.settle();
        break;
      default:
        throw new Error(`unknown sink call ${JSON.stringify(call.call)}`);
    }
  }
  return slack;
}

for (const name of manifest.recordings) {
  test(`the slack calls of ${name} are the ones python made`, async () => {
    const whole = object(golden("reply", name).whole, "whole");
    const expected = golden("slack", name);
    const slack = await replay(list(whole.calls, "calls"));
    const wanted = list(expected.slack_calls, "slack_calls").map((item) => {
      const call = object(item, "slack call");
      return { method: call.method, args: call.args };
    });
    const made = slack.apiCalls.map((call) => ({ method: call.method, args: call.args }));
    // Call for call first, so that a difference names the call it starts at.
    for (const [index, call] of wanted.entries()) {
      assert.deepEqual(made[index], call, `Slack call ${index} of ${name}`);
    }
    assert.equal(made.length, wanted.length);
    assert.deepEqual(
      {
        message_blocks: slack.messageBlocks(),
        message_cards: slack.messageCards(),
        message_texts: slack.messageTexts(),
        pushes: slack.pushes(),
      },
      expected.final,
    );
  });
}
