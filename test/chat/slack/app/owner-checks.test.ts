/**
 * Invariant 1, listener by listener: every inbound path checks the owner and the workspace on
 * its own. Each case is a payload that acts when the owner sends it (the first test of the pair
 * proves it does), sent again by another user and by the owner's id from another workspace: the
 * daemon then calls Slack for nothing, not even to read the channel, and changes nothing.
 *
 * Not a port: the Python suite holds these refusals in the section of each feature
 * (`test_nobody_else_can_approve`, `test_a_setup_click_from_someone_else_is_ignored`,
 * `test_typing_by_anyone_else_updates_nothing`, ...), and this file keeps them in one table so a
 * listener added to `buildApp` without its check has one place to fail.
 */
import assert from "node:assert/strict";
import { join } from "node:path";
import { test } from "node:test";
import { HOLD_CANCEL, HOLD_CONTINUE } from "../../../../src/chat/slack/hold.ts";
import {
  CLEAN_ACTION,
  DELETE_ACTION,
  EDIT_ACTION,
  EDIT_ON,
  FILTERS_BLOCK,
  homeFilter,
  SHOW_ALL_ACTION,
  STATUS_ACTION,
} from "../../../../src/chat/slack/home.ts";
import { newDraft } from "../../../../src/chat/slack/requests.ts";
import { SETUP_MODEL, SETUP_START } from "../../../../src/chat/slack/setup.ts";
import * as texts from "../../../../src/core/texts.ts";
import { CHANNEL, OTHER_TEAM, OWNER, STRANGER, THREAD } from "../../../support/fake-slack.ts";
import {
  type Body,
  CLICK_THREAD,
  chooseClick,
  chosenOption,
  click,
  clipInfo,
  clipMessage,
  FORM_THREAD,
  formBody,
  HOME_THREAD,
  homeAction,
  listed,
  ModalSlack,
  message,
  picked,
  put,
  QUESTIONS,
  recorded,
  resumeClick,
  SESSION_A,
  setupState,
  submitted,
  typing,
  type World,
  worldOf,
} from "../../../support/slack-app.ts";

/** Who sends a payload: the owner, another user, or the owner's id from another workspace. */
type Sender = "owner" | "stranger" | "other workspace";

/** What a click's `user` says of `sender`: the user's id, or the user's own workspace. */
function userOf(sender: Sender): Body {
  if (sender === "stranger") return { id: STRANGER };
  return sender === "other workspace" ? { team_id: OTHER_TEAM } : {};
}

/** A click or a form by `sender`. */
function sentBy(body: Body, sender: Sender): Body {
  Object.assign(body.user, userOf(sender));
  return body;
}

/** A Home control used by `sender`: such a payload names the workspace in `team.id`. */
function homeBy(action: Body, sender: Sender, values: Body = {}): Body {
  const body = homeAction(action, sender === "stranger" ? STRANGER : OWNER, values);
  if (sender === "other workspace") body.team.id = OTHER_TEAM;
  return body;
}

interface Case {
  /** The listener, by what `buildApp` registers it for. */
  readonly listener: string;
  /** Arrange the world, and answer the payload `sender` sends and whether it acted. */
  readonly arrange: (world: World, sender: Sender) => Promise<{ body: Body; acted: () => boolean }>;
}

const CASES: readonly Case[] = [
  ...["approval_allow", "approval_deny", "question_skip"].map(
    (actionId): Case => ({
      listener: `action ${actionId}`,
      arrange: async (world, sender) => {
        const [approvalId, pending] = world.approvals.open(CHANNEL, CLICK_THREAD, "Bash: ls");
        return {
          body: sentBy(click(actionId, approvalId), sender),
          acted: () => pending.decided,
        };
      },
    }),
  ),
  {
    listener: "action question_open",
    arrange: async (world, sender) => {
      const [approvalId] = world.approvals.open(CHANNEL, CLICK_THREAD, "Colour", QUESTIONS);
      const body = sentBy(click("question_open", approvalId), sender);
      body.trigger_id = "0000000000.0000000000.fake";
      return { body, acted: () => world.slack.callsTo("views.open").length > 0 };
    },
  },
  {
    listener: "view question_form",
    arrange: async (world, sender) => {
      const [approvalId, pending] = world.approvals.open(CHANNEL, FORM_THREAD, "Colour", [
        QUESTIONS[0] as (typeof QUESTIONS)[number],
      ]);
      const draft = newDraft(approvalId, CHANNEL, FORM_THREAD);
      return {
        body: sentBy(formBody("view_submission", draft, picked(0, "0")), sender),
        acted: () => pending.decided,
      };
    },
  },
  ...[HOLD_CONTINUE, HOLD_CANCEL].map(
    (actionId): Case => ({
      listener: `action ${actionId}`,
      arrange: async (world, sender) => {
        const [holdId, pending] = world.holds.open(CHANNEL, CLICK_THREAD);
        return { body: sentBy(click(actionId, holdId), sender), acted: () => pending.decided };
      },
    }),
  ),
  {
    listener: `action ${SETUP_START}`,
    arrange: async (world, sender) => {
      const [setupId, pending] = world.holds.open(CHANNEL, CLICK_THREAD, []);
      const body = sentBy(click(SETUP_START, setupId), sender);
      body.state = { values: setupState(undefined, undefined, true) };
      return { body, acted: () => pending.decided };
    },
  },
  {
    listener: `action ${SETUP_MODEL}`,
    arrange: async (world, sender) => {
      const [setupId] = world.holds.open(CHANNEL, CLICK_THREAD, []);
      const body = sentBy(click(SETUP_MODEL, "opus"), sender);
      world.holds.posted(setupId, body.message.ts);
      body.state = { values: setupState("opus") };
      return { body, acted: () => world.slack.callsTo("chat.update").length > 0 };
    },
  },
  {
    listener: "action open_choose",
    arrange: async (world, sender) => {
      world.state.openThread(CHANNEL, THREAD, SESSION_A);
      return {
        body: chooseClick("", userOf(sender)),
        acted: () => world.slack.callsTo("views.open").length > 0,
      };
    },
  },
  {
    listener: "action open_query",
    arrange: async (world, sender) => {
      world.state.openThread(CHANNEL, THREAD, SESSION_A);
      new ModalSlack(world.slack);
      const body = typing("notes", 1790000100, {
        ...(sender === "stranger" && { user: STRANGER }),
        ...(sender === "other workspace" && { team: OTHER_TEAM }),
      });
      return { body, acted: () => world.slack.callsTo("views.update").length > 0 };
    },
  },
  {
    listener: "view open_form",
    arrange: async (world, sender) => {
      world.state.openThread(CHANNEL, THREAD, SESSION_A);
      put(join(world.root, "app"), "notes.txt", "hello\n");
      const body = submitted("notes.txt", {
        ...(sender === "stranger" && { user: STRANGER }),
        ...(sender === "other workspace" && { team: OTHER_TEAM }),
      });
      return {
        body,
        acted: () => world.slack.callsTo("files.completeUploadExternal").length > 0,
      };
    },
  },
  {
    listener: "action folder_bind",
    arrange: async (world, sender) => {
      world.state.removeChannel(CHANNEL);
      return {
        body: sentBy(click("folder_bind", "app"), sender),
        acted: () => world.state.channel(CHANNEL) !== null,
      };
    },
  },
  {
    listener: "action session_resume",
    arrange: async (world, sender) => {
      world.storedSessions = [listed(SESSION_A, "footer", 0, 1)];
      return {
        body: sentBy(resumeClick(SESSION_A, THREAD), sender),
        acted: () => world.state.thread(CHANNEL, THREAD) !== null,
      };
    },
  },
  {
    listener: `action ${STATUS_ACTION} (each Home filter shares its listener)`,
    arrange: async (world, sender) => {
      const values = { [FILTERS_BLOCK]: { [STATUS_ACTION]: chosenOption("raised_hand") } };
      const action = {
        type: "static_select",
        action_id: STATUS_ACTION,
        ...chosenOption("raised_hand"),
      };
      return {
        body: homeBy(action, sender, values),
        acted: () => world.home.chosen.status === "raised_hand",
      };
    },
  },
  {
    listener: `action ${SHOW_ALL_ACTION}`,
    arrange: async (world, sender) => ({
      body: homeBy({ type: "button", action_id: SHOW_ALL_ACTION, value: CHANNEL }, sender),
      acted: () => world.home.chosen.channel === CHANNEL,
    }),
  },
  {
    listener: `action ${EDIT_ACTION}`,
    arrange: async (world, sender) => ({
      body: homeBy({ type: "button", action_id: EDIT_ACTION, value: EDIT_ON }, sender),
      acted: () => world.slack.callsTo("views.publish").length > 0,
    }),
  },
  {
    listener: `action ${DELETE_ACTION}`,
    arrange: async (world, sender) => {
      await world.home.edit(true);
      world.slack.apiCalls.length = 0;
      const value = `${CHANNEL}:${HOME_THREAD}`;
      return {
        body: homeBy({ type: "button", action_id: DELETE_ACTION, value }, sender),
        acted: () => world.deleted.length > 0,
      };
    },
  },
  {
    listener: `action ${CLEAN_ACTION}`,
    arrange: async (world, sender) => {
      await world.home.edit(true);
      world.slack.apiCalls.length = 0;
      return {
        body: homeBy({ type: "button", action_id: CLEAN_ACTION, value: CHANNEL }, sender),
        acted: () => world.cleaned.length > 0,
      };
    },
  },
];

for (const { listener, arrange } of CASES) {
  test(`${listener}: the owner's payload acts`, async (t) => {
    const world = worldOf(t);
    const { body, acted } = await arrange(world, "owner");
    const response = await world.dispatch(body);
    await world.settle();
    assert.equal(response.status, 200);
    assert.ok(acted());
  });

  for (const sender of ["stranger", "other workspace"] as const) {
    test(`${listener}: the same payload from ${sender === "stranger" ? "another user" : "another workspace"} does nothing`, async (t) => {
      const world = worldOf(t);
      const { body, acted } = await arrange(world, sender);
      const response = await world.dispatch(body);
      await world.settle();
      // Acknowledged, as Slack requires of every payload, and nothing else.
      assert.equal(response.status, 200);
      assert.equal(response.body, null);
      assert.ok(!acted());
      assert.deepEqual(world.slack.apiCalls, []); // not even the channel is read
      assert.deepEqual(world.clients, []);
      assert.deepEqual(world.home.chosen, homeFilter());
    });
  }
}

// The one path with no user in its payload: Slack changed a file. It acts only on a clip the
// owner's own message left waiting, and checks the owner again on the file Slack now describes.

test("event file_change: the owner's clip is sent once its transcript is there", async (t) => {
  const world = worldOf(t);
  await world.dispatch(clipMessage(false));
  clipInfo(world);
  await world.dispatch(recorded("event_callback-file_change"));
  await world.settle();
  assert.equal(world.queries().length, 1);
});

for (const [who, fields] of [
  ["another user", { user: STRANGER }],
  ["another workspace", { user_team: OTHER_TEAM }],
] as const) {
  test(`event file_change: a file that became ${who}'s is never sent`, async (t) => {
    const world = worldOf(t);
    await world.dispatch(clipMessage(false));
    clipInfo(world, fields);
    await world.dispatch(recorded("event_callback-file_change"));
    await world.settle();
    assert.deepEqual(world.queries(), []);
    assert.deepEqual(world.clients, []);
    assert.equal(world.ephemerals().at(-1), texts.CLIP_NOT_YOURS);
  });
}

test("event file_change: a file nobody waits for is not even read", async (t) => {
  const world = worldOf(t);
  clipInfo(world);
  await world.dispatch(recorded("event_callback-file_change"));
  assert.deepEqual(world.slack.apiCalls, []);
});

for (const [who, event] of [
  ["another user", { user: STRANGER }],
  ["another workspace", { team: OTHER_TEAM }],
] as const) {
  test(`event message: a message from ${who} does nothing`, async (t) => {
    const world = worldOf(t);
    await world.dispatch(message("!bypass on", event));
    await world.dispatch(message("rm -rf /", { ...event, ts: THREAD }));
    assert.deepEqual(world.slack.apiCalls, []);
    assert.deepEqual(world.clients, []);
    assert.equal(world.state.thread(CHANNEL, THREAD), null);
  });
}
