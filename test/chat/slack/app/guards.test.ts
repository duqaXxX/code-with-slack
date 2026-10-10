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

// Review round 2026-10-10: a wrong-typed value is a refusal, as in Python (`guards.py` falls back
// only on `None`). Every expected value below is the answer `awaydesk.guards` gave on the same
// payload; where Python raised (`AttributeError`, `TypeError`), the payload never reaches Claude
// there and is refused here, the safe side of the same outcome.

/** The owner verdict over an actor. */
function allowed(actor: readonly [string | null, string | null]): boolean {
  return isOwner(IDENTITY, actor[0], actor[1]);
}

const OWNER_FILES = [{ user_team: TEAM }];
const BROADCAST_BODY = { team_id: TEAM, is_ext_shared_channel: false };

const WRONG_TYPES: Array<[string, unknown]> = [
  ["object", { id: "T000EVIL" }],
  ["false", false],
  ["zero", 0],
  ["five", 5],
  ["list", [TEAM]],
  ["empty list", []],
  ["empty object", {}],
  ["empty string", ""],
];

for (const [label, wrong] of WRONG_TYPES) {
  test(`a wrong-typed team is no fallback to the files [${label}]`, () => {
    const event = { user: OWNER, team: wrong, files: OWNER_FILES };
    assert.equal(allowed(messageActor(event, {})), false);
  });

  test(`a wrong-typed team is no fallback to the envelope [${label}]`, () => {
    const event = { user: OWNER, team: wrong, subtype: "thread_broadcast" };
    assert.equal(allowed(messageActor(event, BROADCAST_BODY)), false);
  });
}

const NOT_AN_ARRAY: Array<[string, unknown]> = [
  ["object", { a: 1 }],
  ["string", "abc"],
  ["number", 3],
  ["true", true],
];

for (const [label, files] of NOT_AN_ARRAY) {
  // Python raised here (AttributeError or TypeError) and the handler's failure path dropped the
  // message; refusing is the same outcome without a raise.
  test(`files that are truthy and not a list are refused [${label}]`, () => {
    assert.equal(allowed(messageActor({ user: OWNER, files }, BROADCAST_BODY)), false);
    const broadcast = { user: OWNER, subtype: "thread_broadcast", files };
    assert.equal(allowed(messageActor(broadcast, BROADCAST_BODY)), false);
  });
}

test("files that are a list of strings are refused", () => {
  const broadcast = { user: OWNER, subtype: "thread_broadcast", files: ["x"] };
  assert.equal(allowed(messageActor(broadcast, BROADCAST_BODY)), false);
});

const FALSY_FILES: Array<[string, unknown]> = [
  ["empty object", {}],
  ["empty string", ""],
  ["zero", 0],
  ["false", false],
  ["empty list", []],
];

for (const [label, files] of FALSY_FILES) {
  // Python: a falsy `files` skips the files rule, so a broadcast still takes the envelope.
  test(`falsy files leave the envelope to a broadcast [${label}]`, () => {
    const broadcast = { user: OWNER, subtype: "thread_broadcast", files };
    assert.equal(allowed(messageActor(broadcast, BROADCAST_BODY)), true);
    assert.equal(allowed(messageActor({ user: OWNER, files }, BROADCAST_BODY)), false);
  });
}

test("a wrong-typed envelope team or file team is refused", () => {
  const broadcast = { user: OWNER, subtype: "thread_broadcast" };
  for (const wrong of [5, { a: 1 }, null]) {
    const body = { ...BROADCAST_BODY, team_id: wrong };
    assert.equal(allowed(messageActor(broadcast, body)), false);
  }
  for (const wrong of [5, [TEAM], { a: 1 }, null]) {
    const event = { user: OWNER, files: [{ user_team: wrong }] };
    assert.equal(allowed(messageActor(event, {})), false);
  }
});

const WRONG_USERS: Array<[string, unknown]> = [
  ["object", { a: 1 }],
  ["five", 5],
  ["false", false],
  ["list", [OWNER]],
  ["zero", 0],
];

for (const [label, wrong] of WRONG_USERS) {
  test(`a wrong-typed message user is refused [${label}]`, () => {
    assert.equal(allowed(messageActor({ user: wrong, team: TEAM }, {})), false);
  });
}

test("a message with no user is refused", () => {
  assert.equal(allowed(messageActor({ team: TEAM }, {})), false);
  assert.equal(allowed(messageActor({ user: OWNER, team: TEAM }, {})), true);
});

const WRONG_HOMES: Array<[string, unknown]> = [
  ["object", { id: TEAM }],
  ["five", 5],
  ["false", false],
  ["zero", 0],
  ["list", [TEAM]],
  ["empty string", ""],
  ["other team", OTHER_TEAM],
];

for (const [label, home] of WRONG_HOMES) {
  test(`a click whose home team is wrong-typed is refused [${label}]`, () => {
    const body = { user: { id: OWNER, team_id: home }, team: { id: TEAM } };
    assert.equal(allowed(interactionActor(body)), false);
  });
}

test("a click whose home team is absent, null or the same is the owner", () => {
  for (const home of [undefined, null, TEAM]) {
    const body = { user: { id: OWNER, team_id: home }, team: { id: TEAM } };
    assert.equal(allowed(interactionActor(body)), true);
  }
});

const WRONG_CLICK_USERS: Array<[string, unknown]> = [
  ["object", { id: TEAM }],
  ["five", 5],
  ["false", false],
  ["list", [OWNER]],
  ["zero", 0],
  ["string", OWNER],
  ["empty object", {}],
  ["null", null],
];

for (const [label, user] of WRONG_CLICK_USERS) {
  // Python: AttributeError for a truthy non-object, False for the rest; never the owner.
  test(`a click with a wrong-typed user is refused [${label}]`, () => {
    assert.equal(allowed(interactionActor({ user, team: { id: TEAM } })), false);
  });
}

const WRONG_CLICK_TEAMS: Array<[string, unknown]> = [
  ["five", 5],
  ["false", false],
  ["zero", 0],
  ["list", [TEAM]],
  ["string", TEAM],
  ["empty object", {}],
];

for (const [label, team] of WRONG_CLICK_TEAMS) {
  test(`a click with a wrong-typed team is refused [${label}]`, () => {
    assert.equal(allowed(interactionActor({ user: { id: OWNER }, team })), false);
    // A string in `team.id` is the right shape, so only the other types are tried there.
    if (typeof team !== "string") {
      assert.equal(allowed(interactionActor({ user: { id: OWNER }, team: { id: team } })), false);
    }
  });
}

test("a click with the owner and the team is allowed", () => {
  assert.equal(allowed(interactionActor({ user: { id: OWNER }, team: { id: TEAM } })), true);
});

// The channel flags read as Python reads them: by truthiness, in which `[]` and `{}` are false.
const FLAG_VALUES: Array<[string, unknown, boolean]> = [
  ["[]", [], false],
  ["{}", {}, false],
  ['"false"', "false", true],
  ["0", 0, false],
  ["1", 1, true],
  ['"1"', "1", true],
];

for (const [value, raw, truthy] of FLAG_VALUES) {
  for (const flag of ["is_im", "is_mpim"]) {
    test(`a channel flag is read by Python truthiness [${flag} ${value}]`, async () => {
      const slack = new FakeSlack();
      slack.responses["conversations.info"] = channelInfo({ [flag]: raw });
      const expected = truthy ? texts.REASON_NOT_PRIVATE : null;
      assert.equal(await new ChannelGuard(slack, IDENTITY).refusal(CHANNEL), expected);
    });
  }

  test(`a channel flag is read by Python truthiness [is_private ${value}]`, async () => {
    const slack = new FakeSlack();
    slack.responses["conversations.info"] = channelInfo({ is_private: raw });
    const expected = truthy ? null : texts.REASON_NOT_PRIVATE;
    assert.equal(await new ChannelGuard(slack, IDENTITY).refusal(CHANNEL), expected);
  });

  for (const flag of ["is_shared", "is_ext_shared", "is_org_shared", "is_pending_ext_shared"]) {
    test(`a channel flag is read by Python truthiness [${flag} ${value}]`, async () => {
      const slack = new FakeSlack();
      slack.responses["conversations.info"] = channelInfo({ [flag]: raw });
      const expected = truthy ? texts.REASON_SHARED : null;
      assert.equal(await new ChannelGuard(slack, IDENTITY).refusal(CHANNEL), expected);
    });
  }

  test(`a next cursor is read by Python truthiness [${value}]`, async () => {
    const slack = new FakeSlack();
    slack.responses["conversations.members"] = {
      ok: true,
      members: [OWNER, BOT],
      response_metadata: { next_cursor: raw as JsonObject[string] },
    };
    const expected = truthy ? texts.REASON_MEMBERS : null;
    assert.equal(await new ChannelGuard(slack, IDENTITY).refusal(CHANNEL), expected);
  });
}
