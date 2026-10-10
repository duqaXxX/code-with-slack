/**
 * The lookup of a bound channel the cleanup is given. Moved with `channelLookup` from
 * `test/main.test.ts`, where it was tested while the function lived in `main.ts`.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import { channelLookup } from "../../../src/chat/slack/channels.ts";
import { CHANNEL, FakeSlack, rejected } from "../../support/fake-slack.ts";

test("the channel lookup answers gone only for channel_not_found", async () => {
  const slack = new FakeSlack();
  const lookup = channelLookup(slack);
  assert.equal(await lookup(CHANNEL), "there");
  slack.responses["conversations.info"] = rejected("channel_not_found");
  assert.equal(await lookup(CHANNEL), "gone");
  slack.responses["conversations.info"] = rejected("ratelimited");
  await assert.rejects(
    lookup(CHANNEL),
    (error: unknown) => error instanceof Error && error.name === "ratelimited",
  );
});
