import assert from "node:assert/strict";
import { test } from "node:test";
import {
  type JsonObject,
  sdkRecordings,
  sdkRecords,
  slackPayload,
  slackPayloads,
} from "./support/fixtures.ts";

/**
 * The ids packed in a recorded `event_context`: `4-`, then unpadded base64 of a JSON object
 * with the team, the app and the channel (shape recorded 2026-09-23).
 */
function contextIds(payload: JsonObject): Record<string, unknown> {
  const context = payload.event_context as string;
  const blob = context.slice(context.indexOf("-") + 1);
  const packed = JSON.parse(Buffer.from(blob, "base64").toString("utf8")) as JsonObject;
  return { tid: packed.tid, aid: packed.aid, cid: packed.cid };
}

test("an event context names the ids the payload shows", () => {
  // A scrub that replaces ids field by field does not see inside the base64.
  let checked = 0;
  for (const name of slackPayloads()) {
    const payload = slackPayload(name);
    if (!("event_context" in payload)) continue;
    const event = payload.event as JsonObject;
    assert.deepEqual(
      contextIds(payload),
      { tid: payload.team_id, aid: payload.api_app_id, cid: event.channel },
      name,
    );
    checked += 1;
  }
  assert.ok(checked > 0);
});

test("every record of a recorded stream names its type", () => {
  const recordings = sdkRecordings();
  assert.ok(recordings.length > 0);
  for (const name of recordings) {
    const records = sdkRecords(name);
    assert.ok(records.length > 0, name);
    for (const record of records) assert.equal(typeof record.type, "string", name);
  }
});
