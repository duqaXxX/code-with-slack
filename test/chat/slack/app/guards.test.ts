import assert from "node:assert/strict";
import { test } from "node:test";
import {
  ChannelGuard,
  type Identity,
  interactionActor,
  isOwner,
  isPromptMessage,
  messageActor,
  type Payload,
} from "../../../../src/chat/slack/app/guards.ts";
import * as texts from "../../../../src/core/texts.ts";
import {
  BOT,
  CHANNEL,
  FakeSlack,
  networkDown,
  OTHER_TEAM,
  OWNER,
  STRANGER,
  TEAM,
} from "../../../support/fake-slack.ts";
import { type JsonObject, slackPayload, slackPayloads } from "../../../support/fixtures.ts";

const IDENTITY: Identity = { ownerUserId: OWNER, teamId: TEAM, botUserId: BOT };

/** The first recorded payload whose name ends in `-<kind>`. */
function recorded(kind: string): JsonObject {
  const name = slackPayloads().find((candidate) => candidate.endsWith(`-${kind}`));
  if (name === undefined) throw new Error(`no recorded payload of kind ${kind}`);
  return slackPayload(name);
}

/** The `event` of a recorded envelope. */
function eventOf(body: JsonObject): JsonObject {
  return body.event as JsonObject;
}

const OWNER_CASES: Array<[string | null, string | null, boolean]> = [
  [OWNER, TEAM, true],
  [STRANGER, TEAM, false],
  [OWNER, OTHER_TEAM, false],
  [null, TEAM, false],
  [OWNER, null, false],
  ["", "", false],
];

for (const [user, team, allowed] of OWNER_CASES) {
  test(`is_owner checks user and team separately [${user}-${team}-${allowed}]`, () => {
    assert.equal(isOwner(IDENTITY, user, team), allowed);
  });
}

test("actors read the recorded payloads", () => {
  const plain = recorded("event_callback-message");
  assert.deepEqual(messageActor(eventOf(plain), plain), [OWNER, TEAM]);
  assert.deepEqual(interactionActor(recorded("block_actions")), [OWNER, TEAM]);
});

test("interaction from a user of another home team is not the owner", () => {
  const body = structuredClone(recorded("block_actions"));
  (body.user as JsonObject).team_id = OTHER_TEAM;
  assert.deepEqual(interactionActor(body), [OWNER, null]);
});

test("only plain human messages are prompts", () => {
  const message = eventOf(recorded("event_callback-message"));
  assert.ok(isPromptMessage(message));
  assert.ok(!isPromptMessage(eventOf(recorded("event_callback-message_changed"))));
  assert.ok(!isPromptMessage({ ...message, bot_id: "B000BOT" }));
  assert.ok(!isPromptMessage({ ...message, subtype: "message_deleted" }));
  assert.ok(!isPromptMessage({ ...message, subtype: null }));
  assert.ok(!isPromptMessage({ ...message, text: "" }));
});

test("a message with a file is a prompt even with no text", () => {
  // Measured 2026-09-25: a file arrives as subtype file_share, although Slack's reference calls
  // that subtype legacy.
  const shared = eventOf(recorded("event_callback-file_share-image"));
  assert.ok(isPromptMessage(shared));
  assert.ok(isPromptMessage({ ...shared, text: "" }));
  assert.ok(!isPromptMessage({ ...shared, bot_id: "B000BOT" }));
});

function channelInfo(flags: Payload): JsonObject {
  const info = structuredClone(slackPayload("api-conversations-info"));
  Object.assign(info.channel as JsonObject, flags);
  return info;
}

test("private channel with owner and bot is allowed", async () => {
  const slack = new FakeSlack();
  assert.equal(await new ChannelGuard(slack, IDENTITY).refusal(CHANNEL), null);
});

const FLAG_CASES: Array<[string, Payload, string]> = [
  ["is_private false", { is_private: false }, texts.REASON_NOT_PRIVATE],
  ["is_ext_shared", { is_ext_shared: true }, texts.REASON_SHARED],
  ["is_shared", { is_shared: true }, texts.REASON_SHARED],
  ["is_org_shared", { is_org_shared: true }, texts.REASON_SHARED],
  ["is_mpim", { is_mpim: true }, texts.REASON_NOT_PRIVATE],
  ["is_im", { is_im: true }, texts.REASON_NOT_PRIVATE],
];

for (const [label, flags, reason] of FLAG_CASES) {
  test(`channel flags are refused [${label}]`, async () => {
    const slack = new FakeSlack();
    slack.responses["conversations.info"] = channelInfo(flags as JsonObject);
    assert.equal(await new ChannelGuard(slack, IDENTITY).refusal(CHANNEL), reason);
  });
}

test("a third member is refused", async () => {
  const slack = new FakeSlack();
  slack.responses["conversations.members"] = { ok: true, members: [OWNER, BOT, STRANGER] };
  assert.equal(await new ChannelGuard(slack, IDENTITY).refusal(CHANNEL), texts.REASON_MEMBERS);
});

test("a missing owner is refused", async () => {
  const slack = new FakeSlack();
  slack.responses["conversations.members"] = { ok: true, members: [BOT] };
  assert.equal(await new ChannelGuard(slack, IDENTITY).refusal(CHANNEL), texts.REASON_MEMBERS);
});

test("a paginated member list is refused", async () => {
  const slack = new FakeSlack();
  slack.responses["conversations.members"] = {
    ok: true,
    members: [OWNER, BOT],
    response_metadata: { next_cursor: "abc" },
  };
  assert.equal(await new ChannelGuard(slack, IDENTITY).refusal(CHANNEL), texts.REASON_MEMBERS);
});

/** A logger that keeps its lines, so the log stays out of the test output. */
function kept(): { lines: string[]; warning(message: string): void } {
  const lines: string[] = [];
  return { lines, warning: (message) => lines.push(message) };
}

test("an unreadable channel is refused", async () => {
  const slack = new FakeSlack();
  slack.responses["conversations.info"] = { ok: false, error: "channel_not_found" };
  const log = kept();
  assert.equal(
    await new ChannelGuard(slack, IDENTITY, log).refusal(CHANNEL),
    texts.REASON_UNREADABLE,
  );
  // The log names Slack's error code and the channel, nothing else.
  assert.deepEqual(log.lines, [`could not read channel ${CHANNEL}: channel_not_found`]);
});

test("the guard asks slack every time", async () => {
  const slack = new FakeSlack();
  const guard = new ChannelGuard(slack, IDENTITY);
  await guard.refusal(CHANNEL);
  slack.responses["conversations.members"] = { ok: true, members: [OWNER, BOT, STRANGER] };
  assert.equal(await guard.refusal(CHANNEL), texts.REASON_MEMBERS);
});

test("a network failure reading the channel is a refusal", async () => {
  const slack = new FakeSlack();
  slack.responses["conversations.info"] = networkDown();
  const log = kept();
  assert.equal(
    await new ChannelGuard(slack, IDENTITY, log).refusal(CHANNEL),
    texts.REASON_UNREADABLE,
  );
  assert.deepEqual(log.lines, [`could not read channel ${CHANNEL}: WebAPIRequestError`]);
});

test("a file share names its workspace in the file", () => {
  // Measured 2026-09-25: a file_share event has no `team`; each file carries `user_team`.
  const body = recorded("event_callback-file_share-image");
  const shared = eventOf(body);
  assert.ok(!("team" in shared));
  assert.deepEqual(messageActor(shared, body), [OWNER, TEAM]);
  const files = shared.files as JsonObject[];
  const other = { ...(files[0] as JsonObject), user_team: OTHER_TEAM };
  assert.deepEqual(messageActor({ ...shared, files: [...files, other] }, body), [OWNER, null]);
});

test("a reply also sent to the channel is a prompt", () => {
  // Recorded 2026-10-09: a thread reply sent with "Also send to #channel" arrives as subtype
  // thread_broadcast, followed by a hidden message_changed that wraps the same reply.
  const event = eventOf(recorded("event_callback-thread_broadcast"));
  assert.equal(event.subtype, "thread_broadcast");
  assert.ok(isPromptMessage(event));
  assert.ok(!isPromptMessage({ ...event, bot_id: "B000BOT" }));
  const followed = eventOf(recorded("event_callback-message_changed-thread_broadcast"));
  assert.equal((followed.message as JsonObject).subtype, "thread_broadcast");
  assert.ok(!isPromptMessage(followed));
});

test("a reply also sent to the channel takes its workspace from the envelope", () => {
  // Recorded 2026-10-09: the event has no `team` (its `root` names the root author's); the
  // envelope's `team_id` is where the event happened.
  const body = recorded("event_callback-thread_broadcast");
  const event = eventOf(body);
  assert.ok(!("team" in event));
  assert.deepEqual(messageActor(event, body), [OWNER, TEAM]);
  assert.deepEqual(messageActor(event, { ...body, team_id: OTHER_TEAM }), [OWNER, OTHER_TEAM]);
  // A team the event names is never replaced by the envelope's.
  assert.deepEqual(messageActor({ ...event, team: OTHER_TEAM }, body), [OWNER, OTHER_TEAM]);
});

test("the envelope stands in only in a channel not shared outside", () => {
  const body = recorded("event_callback-thread_broadcast");
  const event = eventOf(body);
  assert.equal(body.is_ext_shared_channel, false);
  assert.deepEqual(messageActor(event, { ...body, is_ext_shared_channel: true }), [OWNER, null]);
  const { is_ext_shared_channel: _unsaid, ...unsaid } = body;
  assert.deepEqual(messageActor(event, unsaid), [OWNER, null]);
  // Only the JSON `false` counts: nothing that merely reads as false.
  for (const unclear of [null, 0, "false"]) {
    assert.deepEqual(messageActor(event, { ...body, is_ext_shared_channel: unclear }), [
      OWNER,
      null,
    ]);
  }
});

test("a reply also sent to the channel with files follows the files", () => {
  // Not recorded: whether Slack sends a broadcast reply with a file as thread_broadcast. If it
  // does, the files decide, as for any message that carries them, and the envelope never does.
  const body = recorded("event_callback-thread_broadcast");
  const files = eventOf(recorded("event_callback-file_share-image")).files as JsonObject[];
  const event = { ...eventOf(body), files };
  assert.deepEqual(messageActor(event, body), [OWNER, TEAM]);
  const mixed = [...files, { ...(files[0] as JsonObject), user_team: OTHER_TEAM }];
  assert.deepEqual(messageActor({ ...event, files: mixed }, body), [OWNER, null]);
  const { user_team: _named, ...bare } = files[0] as JsonObject;
  assert.deepEqual(messageActor({ ...event, files: [bare] }, body), [OWNER, null]);
});

test("the envelope stands in for no other kind of message", () => {
  const body = recorded("event_callback-thread_broadcast");
  const { team: _team, ...teamless } = eventOf(recorded("event_callback-message"));
  assert.deepEqual(messageActor(teamless, body), [OWNER, null]);
  assert.deepEqual(messageActor({ ...teamless, subtype: "file_share" }, body), [OWNER, null]);
});
