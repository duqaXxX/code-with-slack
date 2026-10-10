/**
 * The handlers, through Bolt's own entry point. Port of `tests/test_slack_app.py`, in its order,
 * from its first test to the same-folder hold's section (which is `slack-app-hold.test.ts`):
 * messages and words, the Home controls, approvals and the question form, `!resume`, attached
 * files and `!bind`.
 */
import assert from "node:assert/strict";
import { existsSync, mkdirSync, readdirSync, readFileSync, rmdirSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { isDeepStrictEqual } from "node:util";
import { type Question, ResumeRefused } from "../../../../src/agent/seam.ts";
import { ago, logger, slackUnescape } from "../../../../src/chat/slack/app/app.ts";
import { DownloadFailed } from "../../../../src/chat/slack/attachments.ts";
import { HOLD_CONTINUE, holdBlocks } from "../../../../src/chat/slack/hold.ts";
import {
  CHANNEL_ACTION,
  CLEAN_ACTION,
  DELETE_ACTION,
  EDIT_ACTION,
  EDIT_OFF,
  EDIT_ON,
  FILTER_ACTIONS,
  FILTERS_BLOCK,
  homeFilter,
  NEW_THREAD_ACTION,
  SEARCH_ACTION,
  SEARCH_BLOCK,
  SHOW_ALL_ACTION,
  STATUS_ACTION,
} from "../../../../src/chat/slack/home.ts";
import { FALLBACK_LIMIT } from "../../../../src/chat/slack/reply/blocks.ts";
import { take } from "../../../../src/chat/slack/reply/chars.ts";
import {
  answeredBlocks,
  loadDraft,
  newDraft,
  SECTION_LIMIT,
} from "../../../../src/chat/slack/requests.ts";
import { STOP_TAIL_WAIT } from "../../../../src/core/sessions/constants.ts";
import type { ThreadSession } from "../../../../src/core/sessions/session.ts";
import * as texts from "../../../../src/core/texts.ts";
import { fill } from "../../../../src/core/texts.ts";
import {
  AsyncEvent,
  BOT,
  CHANNEL,
  OTHER_CHANNEL,
  OTHER_TEAM,
  OTHER_THREAD,
  OWNER,
  STRANGER,
  TEAM,
  THREAD,
} from "../../../support/fake-slack.ts";
import { canUseToolCall, sdkMessages, splitTurns } from "../../../support/sessions.ts";
import {
  type Body,
  buttonValue,
  CLICK_THREAD,
  chosenOption,
  click,
  clickIn,
  composed,
  FORM_THREAD,
  formBody,
  HOME_THREAD,
  homeAction,
  idleMessage,
  listed,
  message,
  PERMALINK,
  picked,
  QUESTIONS,
  reactionsOn,
  recorded,
  recordedThreadReply,
  reply,
  resumeClick,
  SESSION_A,
  SESSION_B,
  said,
  sharedFile,
  twoSessions,
  type World,
  worldFor,
  worldOf,
} from "../../../support/slack-app.ts";

const ANYONE_ELSE: ReadonlyArray<readonly [user: string, team: string]> = [
  [STRANGER, TEAM],
  [OWNER, OTHER_TEAM],
];
// Python's `user0` and `user1`: a click by another user, and by the owner from another workspace.
const OTHER_USERS: readonly Body[] = [{ id: STRANGER }, { team_id: OTHER_TEAM }];
// What the recorded files download to.
const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47]);
const HELLO = new TextEncoder().encode("hello\n");

test("owner message becomes a prompt", async (t) => {
  const world = worldOf(t);
  const body = message("list the files");
  await world.dispatch(body);
  assert.deepEqual(world.queries(), ["list the files"]);
});

test("a reply in a known session s thread is a prompt too", async (t) => {
  const world = worldOf(t);
  const threadBody = recordedThreadReply();
  const threadTs = threadBody.event.thread_ts;
  world.sessions.open(CHANNEL, threadTs); // a session already lives in this thread
  await world.dispatch(structuredClone(threadBody));
  assert.deepEqual(world.queries(), [threadBody.event.text]);
});

test("a reply also sent to the channel is a prompt of its thread", async (t) => {
  const world = worldOf(t);
  // Recorded 2026-10-09: "Also send to #channel" makes the reply a thread_broadcast with no
  // `team`, then a hidden message_changed that wraps it.
  const body = recorded("event_callback-thread_broadcast");
  world.sessions.open(CHANNEL, body.event.thread_ts);
  await world.dispatch(structuredClone(body));
  await world.dispatch(recorded("event_callback-message_changed-thread_broadcast"));
  assert.deepEqual(world.queries(), [body.event.text]);
});

test("a reply also sent to a channel shared outside is ignored", async (t) => {
  const world = worldOf(t);
  const body = recorded("event_callback-thread_broadcast");
  world.sessions.open(CHANNEL, body.event.thread_ts);
  body.is_ext_shared_channel = true;
  await world.dispatch(body);
  assert.deepEqual(world.queries(), []);
  assert.deepEqual(world.ephemerals(), []);
});

test("a reply in a thread that holds no session is refused", async (t) => {
  const world = worldOf(t);
  // Same recorded shape as above, but its thread was never opened: nothing to continue.
  const threadBody = recordedThreadReply();
  await world.dispatch(structuredClone(threadBody));
  assert.deepEqual(world.queries(), []);
  assert.deepEqual(world.ephemerals(), [texts.NOT_A_SESSION]);
});

for (const word of [
  "!stop",
  "!resume",
  "!resume 68da9311-0000-4000-8000-00000000000b",
  "!bind elsewhere",
]) {
  test(`a word that acts on the channel is refused in a thread with no session [${word}]`, async (t) => {
    const world = worldOf(t);
    // A word typed under a post that is no session reads as being about that thread: one that
    // would stop every session, bind the channel or turn the thread into a session says where
    // to send it instead, for the owner alone.
    await world.dispatch(message("first"));
    const busy = world.queries().length;
    const folder = world.state.channel(CHANNEL);
    await world.dispatch(reply(word, OTHER_THREAD));
    const name = (word.split(/\s+/)[0] as string).replace(/^!/, "");
    const expected =
      name === "stop" ? texts.STOP_OUTSIDE_SESSION : fill(texts.WORD_IN_THREAD, { word: name });
    assert.deepEqual(world.ephemerals(), [expected]);
    assert.deepEqual(said(world), []);
    assert.deepEqual(world.state.channel(CHANNEL), folder);
    assert.equal(world.state.thread(CHANNEL, OTHER_THREAD), null);
    assert.equal(world.queries().length, busy);
  });
}

for (const word of ["!help", "!guide", "!status", "!bind"]) {
  test(`a word that only shows acts as in the channel in a thread with no session [${word}]`, async (t) => {
    const world = worldOf(t);
    await world.dispatch(reply(word, OTHER_THREAD));
    assert.deepEqual(world.ephemerals(), []);
    assert.equal(said(world).length, 1);
  });
}

test("a reply in a thread being deleted starts nothing", async (t) => {
  const world = worldOf(t);
  // The thread still has its entry while its messages are deleted: a reply sent then must not
  // rebuild a session from it (the delete would leave it running on a thread that is gone).
  world.state.openThread(CHANNEL, THREAD, "68da9311-0000-4000-8000-00000000beef");
  assert.ok(await world.sessions.release(CHANNEL, THREAD));
  await world.dispatch(reply("one more thing", THREAD));
  assert.deepEqual(world.queries(), []);
  assert.deepEqual(world.clients, []);
  assert.deepEqual(world.ephemerals(), [texts.NOT_A_SESSION]);
  // The delete failed and let the thread go: it takes a prompt again.
  world.sessions.free(CHANNEL, THREAD);
  await world.dispatch(reply("one more thing", THREAD));
  await world.until(() => isDeepStrictEqual(world.queries(), ["one more thing"]));
});

test("a reply after a gone session starts nothing", async (t) => {
  const world = worldOf(t);
  // A gone session's close removes the thread's entry with it, so the next reply finds a thread
  // that holds no session: it is refused, never treated as a fresh top-level message.
  world.state.openThread(CHANNEL, THREAD, "68da9311-0000-4000-8000-00000000dead");
  world.connectError = new ResumeRefused();
  await world.dispatch(reply("hello", THREAD));
  assert.equal(world.state.thread(CHANNEL, THREAD), null);
  world.connectError = null;
  await world.dispatch(reply("hello again", THREAD));
  assert.deepEqual(world.queries(), []);
  assert.equal(world.ephemerals().at(-1), texts.NOT_A_SESSION);
});

test("the thread refusals send the owner to the channel", () => {
  // Both are shown inside a thread, where `!bind` is refused and a reply meets the same
  // refusal again: each has to say that its way out is typed in the channel (issue #78).
  assert.ok(
    texts.DIRECTORY_MISSING.includes("in the channel, bind another folder with `!bind <path>`"),
  );
  assert.ok(texts.NOT_A_SESSION.includes("In the channel, send a new message"));
  assert.ok(texts.NOT_A_SESSION.includes("`!resume`"));
  // No cause: the daemon cannot tell a thread that never held a session from one whose
  // entry it dropped while Claude Code still has the session.
  assert.ok(!texts.NOT_A_SESSION.includes("deleted"));
});

for (const [user, team] of ANYONE_ELSE) {
  test(`a message from anyone else does nothing [${user}-${team}]`, async (t) => {
    const world = worldOf(t);
    await world.dispatch(message("rm -rf /", { user, team }));
    assert.deepEqual(world.queries(), []);
    assert.ok(!world.postedAnything());
  });
}

for (const [user, team] of ANYONE_ELSE) {
  test(`a reply from anyone else does nothing [${user}-${team}]`, async (t) => {
    const world = worldOf(t);
    await world.dispatch(message("hi", { ts: THREAD })); // the owner opens the session
    await world.dispatch(reply("rm -rf /", THREAD, { user, team }));
    assert.deepEqual(world.queries(), ["hi"]);
    assert.deepEqual(world.ephemerals(), []);
  });
}

test("edits do nothing", async (t) => {
  const world = worldOf(t);
  await world.dispatch(recorded("event_callback-message_changed"));
  assert.deepEqual(world.queries(), []);
});

test("a refused channel tells only the owner", async (t) => {
  const world = worldOf(t);
  world.slack.responses["conversations.members"] = { ok: true, members: [OWNER, BOT, STRANGER] };
  await world.dispatch(message());
  assert.deepEqual(world.queries(), []);
  assert.deepEqual(world.ephemerals(), [
    fill(texts.CHANNEL_REFUSED, { reason: texts.REASON_MEMBERS }),
  ]);
  const refusals = world.slack.callsTo("chat.postEphemeral") as Body[];
  assert.equal(refusals.length, 1);
  const [refused] = refusals as [Body];
  assert.equal(refused.user, OWNER);
  assert.equal(refused.blocks[0].type, "context");
});

test("an unbound channel explains how to bind", async (t) => {
  const world = worldFor(t)({ bound: false });
  await world.dispatch(message());
  assert.deepEqual(said(world), [fill(texts.UNBOUND, { root: world.root })]);
  assert.ok(!("thread_ts" in (world.slack.callsTo("chat.postMessage")[0] as Body)));
});

test("bang runs a known command", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("!compact"));
  assert.deepEqual(world.queries(), ["/compact"]);
});

test("bang leaves other text alone", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("!important: read the notes"));
  assert.deepEqual(world.queries(), ["!important: read the notes"]);
});

test("slack escapes are undone", () => {
  assert.equal(slackUnescape("a &lt;b&gt; &amp;&amp; c"), "a <b> && c");
});

test("bang bind inside and outside the root", async (t) => {
  const world = worldOf(t);
  const app = join(world.root, "app");
  await world.dispatch(message(`!bind ${app}`));
  assert.equal(world.state.channel(CHANNEL)?.directory, app);
  await world.dispatch(message("!bind /"));
  assert.equal(said(world).at(-1), fill(texts.BIND_OUTSIDE, { path: "/", root: world.root }));
  await world.dispatch(message("!bind app")); // relative to the allowed root, as the text says
  assert.equal(world.state.channel(CHANNEL)?.directory, app);
});

test("bang bind works in an unbound channel", async (t) => {
  const world = worldFor(t)({ bound: false });
  const app = join(world.root, "app");
  await world.dispatch(message(`!bind ${app}`));
  assert.equal(world.state.channel(CHANNEL)?.directory, app);
  assert.deepEqual(world.queries(), []);
  await world.sessions.closeAll();
});

test("bang bind is refused inside a thread", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("hi", { ts: THREAD })); // opens a session at THREAD
  await world.dispatch(reply(`!bind ${join(world.root, "docs")}`, THREAD));
  assert.deepEqual(world.ephemerals(), [fill(texts.WORD_IN_THREAD, { word: "bind" })]);
  assert.equal(world.state.channel(CHANNEL)?.directory, join(world.root, "app"));
});

test("bang resume is refused inside a thread", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("hi", { ts: THREAD }));
  await world.dispatch(reply("!resume", THREAD));
  assert.deepEqual(world.ephemerals(), [fill(texts.WORD_IN_THREAD, { word: "resume" })]);
});

test("bang bypass is refused at top level", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("!bypass on"));
  assert.deepEqual(said(world), [texts.BYPASS_TOP_LEVEL]);
  assert.ok(!("thread_ts" in (world.slack.callsTo("chat.postMessage")[0] as Body)));
  assert.deepEqual(world.clients, []); // no session was even started
});

test("bang clear is refused inside a thread", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("hi", { ts: THREAD }));
  await world.dispatch(reply("!clear", THREAD));
  assert.deepEqual(world.ephemerals(), [texts.CLEAR_IN_THREAD]);
  assert.deepEqual(world.queries(), ["hi"]); // the earlier prompt is the only one that reached Claude
});

for (const word of ["!reset", "!new", "!NEW keep this"]) {
  test(`an alias of clear is refused inside a thread [${word}]`, async (t) => {
    const world = worldOf(t);
    // `/reset` and `/new` are `/clear` under other names: commands reference and the recorded
    // list (SDK 0.2.163) both give them as its aliases.
    await world.dispatch(message("hi", { ts: THREAD }));
    await world.dispatch(reply(word, THREAD));
    assert.deepEqual(world.ephemerals(), [texts.CLEAR_IN_THREAD]);
    assert.deepEqual(world.queries(), ["hi"]);
  });
}

for (const [word, answer] of [
  ["!login", texts.LOGIN_ON_HOST],
  ["!LOGIN now", texts.LOGIN_ON_HOST],
  ["!logout", texts.LOGOUT_ON_HOST],
] as const) {
  test(`login and logout never reach claude code from the channel [${word}]`, async (t) => {
    const world = worldOf(t);
    // They act on the host's own login, which the daemon and every session run on.
    await world.dispatch(message(word));
    assert.deepEqual(said(world), [answer]);
    assert.ok(!("thread_ts" in (world.slack.callsTo("chat.postMessage")[0] as Body)));
    assert.deepEqual(world.clients, []); // no session was even started
  });
}

for (const [word, answer] of [
  ["!login", texts.LOGIN_ON_HOST],
  ["!logout", texts.LOGOUT_ON_HOST],
] as const) {
  test(`login and logout never reach claude code from a thread [${word}]`, async (t) => {
    const world = worldOf(t);
    await world.dispatch(message("hi", { ts: THREAD }));
    await world.dispatch(reply(word, THREAD));
    assert.deepEqual(world.ephemerals(), [answer]);
    assert.deepEqual(world.queries(), ["hi"]);
  });
}

test("bang clear at top level opens a session like any other word", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("!clear"));
  assert.deepEqual(world.queries(), ["/clear"]);
});

test("a daemon word in a dead thread acts as top level", async (t) => {
  const world = worldOf(t);
  // A reply inside the thread of an old request (never a session) still gets an answer there,
  // exactly as a top-level `!bind` would: the channel's own folder ("app") shows as current.
  await world.dispatch(reply("!bind", CLICK_THREAD));
  const posts = world.slack.callsTo("chat.postMessage") as Body[];
  assert.equal(posts.length, 1);
  const [post] = posts as [Body];
  assert.ok(!("thread_ts" in post)); // answered in the channel, like a top-level `!bind`
  const blocks = post.blocks as Body[];
  const rows: string[] = blocks.filter((b) => b.type === "section").map((b) => b.text.text);
  assert.ok(rows.some((row) => row.startsWith("`app`") && row.includes(texts.BIND_CURRENT)));
  const values = blocks.filter((b) => "accessory" in b).map((b) => b.accessory.value);
  assert.deepEqual(values, ["."]); // the current folder ("app") shows with no button
});

test("bang help lists the session commands", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("hi", { ts: THREAD }));
  await world.dispatch(reply("!help", THREAD));
  const shown = world.ephemerals(); // inside a session's thread the owner alone sees the answer
  assert.equal(shown.length, 1);
  const [text] = shown as [string];
  assert.ok(text.includes("`!compact") && text.includes("`!bypass"));
  const asked = world.slack.callsTo("chat.postEphemeral") as Body[];
  assert.equal(asked.length, 1);
  assert.equal(asked[0]?.thread_ts, THREAD);
  assert.equal(asked[0]?.user, OWNER);
});

test("bang help top level lists only the daemon words", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("!help", { ts: THREAD }));
  const shown = said(world);
  assert.equal(shown.length, 1);
  const [text] = shown as [string];
  assert.ok(text.includes("`!bypass") && !text.includes("`!compact"));
  assert.ok(text.includes(texts.HELP_UNBOUND));
  assert.deepEqual(world.clients, []);
  // Typed in the channel, the answer is a normal top-level post that survives a reload.
  assert.ok(!("thread_ts" in (world.slack.callsTo("chat.postMessage")[0] as Body)));
  assert.deepEqual(world.ephemerals(), []);
});

test("bang help with a filter lists only matches", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("hi", { ts: THREAD }));
  await world.dispatch(reply("!help compact", THREAD));
  const shown = world.ephemerals();
  assert.equal(shown.length, 1);
  const [text] = shown as [string];
  assert.ok(text.includes("`!compact") && !text.includes("`!bypass"));
});

test("bang help works in an unbound channel", async (t) => {
  const world = worldFor(t)({ bound: false });
  await world.dispatch(message("!help"));
  const shown = said(world);
  assert.equal(shown.length, 1);
  assert.ok(shown[0]?.includes("`!bind"));
  assert.deepEqual(world.clients, []);
});

test("bang bypass on inside a thread switches the live client", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("hi", { ts: THREAD }));
  const word = reply("!bypass on", THREAD);
  await world.dispatch(word);
  assert.deepEqual(world.clients[0]?.modes, ["bypassPermissions"]);
  // The answer says what changed, to the owner alone and under their word, and a ✅ stays on
  // the word after a reload takes the line away. Neither rings.
  assert.deepEqual(reactionsOn(world, word.event.ts), ["white_check_mark"]);
  const answers = world.slack.callsTo("chat.postEphemeral") as Body[];
  assert.equal(answers.length, 1);
  assert.equal(answers[0]?.text, texts.BYPASS_ON_THREAD);
  assert.equal(answers[0]?.thread_ts, THREAD);
  // No placeholder for the session's reply: nothing is posted until Claude has something to say.
  assert.deepEqual(said(world), []);
});

test("bang bypass off inside a thread says so too", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("hi", { ts: THREAD }));
  await world.dispatch(reply("!bypass on", THREAD));
  const word = reply("!bypass off", THREAD);
  await world.dispatch(word);
  assert.deepEqual(world.clients[0]?.modes, ["bypassPermissions", "default"]);
  assert.deepEqual(reactionsOn(world, word.event.ts), ["white_check_mark"]);
  assert.deepEqual(world.ephemerals(), [texts.BYPASS_ON_THREAD, texts.BYPASS_OFF_THREAD]);
  assert.deepEqual(said(world), []);
  assert.equal(world.state.thread(CHANNEL, THREAD)?.bypass, false);
});

test("a notice is small and grey a reference full size", async (t) => {
  const world = worldOf(t);
  // The daemon's notices read apart from Claude's replies, as the footer does (the owner,
  // 2026-09-27); `!help`, `!guide` and `!status` stay full size, since they are read.
  await world.dispatch(message("hi", { ts: THREAD }));
  await world.dispatch(reply("!bind", THREAD));
  await world.dispatch(reply("!help", THREAD));
  const shown = world.slack.callsTo("chat.postEphemeral") as Body[];
  assert.equal(shown.length, 2);
  const [notice, reference] = shown as [Body, Body];
  const refusal = fill(texts.WORD_IN_THREAD, { word: "bind" });
  assert.deepEqual(notice.blocks, [
    { type: "context", elements: [{ type: "mrkdwn", text: refusal }] },
  ]);
  assert.deepEqual(
    (reference.blocks as Body[]).map((b) => b.type),
    ["markdown"],
  );
});

test("a bound folder is shown as written in the notice", async (t) => {
  const world = worldOf(t);
  const folder = join(world.root, "R&D");
  mkdirSync(folder);
  await world.dispatch(message(`!bind ${folder}`));
  const notices = world.slack.callsTo("chat.postMessage") as Body[];
  assert.equal(notices.length, 1);
  assert.ok(notices[0]?.blocks[0].elements[0].text.includes("R&amp;D"));
});

test("a bind never touches an existing thread s bypass", async (t) => {
  const world = worldOf(t);
  mkdirSync(join(world.root, "docs"));
  await world.dispatch(message("hi", { ts: THREAD }));
  await world.dispatch(reply("!bypass on", THREAD));
  await world.dispatch(message("!bind docs")); // a new default-folder thread from here on
  assert.equal(world.state.thread(CHANNEL, THREAD)?.bypass, true);
  assert.equal(world.clients[0]?.connected, true); // never closed by the bind
});

test("a gone session answers bypass with no new session", async (t) => {
  const world = worldOf(t);
  world.state.openThread(CHANNEL, THREAD, "68da9311-0000-4000-8000-00000000dead");
  world.connectError = new ResumeRefused();
  const word = reply("!bypass on", THREAD);
  await world.dispatch(word);
  assert.deepEqual(world.ephemerals(), [texts.SESSION_GONE]);
  assert.deepEqual(reactionsOn(world, word.event.ts), []); // nothing took effect: no ✅
  assert.equal(world.state.thread(CHANNEL, THREAD), null);
});

test("a word in code formatting is still a word", async (t) => {
  const world = worldOf(t);
  // Pasted from where it was shown as code, a word keeps the formatting: the event's text
  // starts with a backtick, the composer's blocks hold the word (issue #178).
  await world.dispatch(message("`!stop`", { blocks: composed("!stop", { code: true }) }));
  assert.deepEqual(said(world), [texts.NOTHING_TO_STOP]);
  assert.deepEqual(world.queries(), []);
});

test("a command in code formatting runs as the command", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("`!compact`", { blocks: composed("!compact", { code: true }) }));
  assert.deepEqual(world.queries(), ["/compact"]);
});

test("anything before the bang keeps a formatted message a prompt", async (t) => {
  const world = worldOf(t);
  // The escape the setup doc gives, where the two readings differ: the text opens with a
  // mark, the composer's run with the backslash.
  await world.dispatch(message("`\\!stop`", { blocks: composed("\\!stop", { code: true }) }));
  assert.deepEqual(world.queries(), ["`\\!stop`"]);
  assert.deepEqual(said(world), []);
});

test("a formatted command keeps its arguments as sent", async (t) => {
  const world = worldOf(t);
  const blocks = composed("!compact", { code: true });
  blocks[0]?.elements[0].elements.push(
    { type: "text", text: " keep " },
    { type: "text", text: "the plan", style: { code: true } },
  );
  await world.dispatch(message("`!compact` keep `the plan`", { blocks }));
  assert.deepEqual(world.queries(), ["/compact keep `the plan`"]);
});

test("bang status and stop top level summarize the channel", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("!stop"));
  await world.dispatch(message("!status"));
  assert.equal(said(world)[0], texts.NOTHING_TO_STOP);
  assert.equal(
    said(world)[1],
    [
      fill(texts.STATUS_CHANNEL_HEADER, { directory: join(world.root, "app") }),
      texts.STATUS_CHANNEL_EMPTY,
    ].join("\n"),
  );
  assert.deepEqual(world.queries(), []);
});

test("bang status lists every live session with a link", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("hello", { ts: THREAD })); // never ends: stays busy
  await world.dispatch(message("!status", { ts: OTHER_THREAD }));
  const lines = (said(world).at(-1) as string).split("\n");
  const directory = join(world.root, "app");
  assert.equal(lines[0], fill(texts.STATUS_CHANNEL_HEADER, { directory }));
  // `say` posts a markdown block: a standard Markdown link, not mrkdwn's `<url|label>`.
  const link = `https://example.slack.com/archives/${CHANNEL}/p1780000000000001`;
  assert.equal(lines[1], `[Session](${link}): busy`);
});

test("bang status names an idle session by its title and last activity", async (t) => {
  const world = worldOf(t);
  // Issue #76: five rows reading `Session: idle` told the owner nothing about which was which.
  const twoHoursAgo = Math.trunc((world.now() - 7_200) * 1000);
  world.storedSessions = [listed(SESSION_A, "Fix the footer", twoHoursAgo, 1)];
  await world.dispatch(message(`!resume ${SESSION_A}`)); // a live, idle session with that id
  await world.dispatch(message("!status", { ts: OTHER_THREAD }));
  assert.equal(
    (said(world).at(-1) as string).split("\n")[1],
    `[Fix the footer](${PERMALINK}): idle · 2h ago`,
  );
});

test("bang status dates an idle session by its last message not its file", async (t) => {
  const world = worldOf(t);
  // Seen live on 2026-10-07: a session idle for 16 minutes read `idle · just now`. Its file had
  // just been written to, by the entries with no timestamp Claude Code appends when a session
  // is connected again, so the file's time says nothing of when the owner last heard from it.
  const nowMs = Math.trunc(world.now() * 1000);
  const other = "68da9311-0000-4000-8000-00000000000b";
  world.storedSessions = [
    listed(SESSION_A, "Fix the footer", nowMs, 1), // its file: touched just now
    listed(other, "Another session of the folder", nowMs, 1), // no live thread
  ];
  const asked: string[][] = [];
  // Python replaced `sessions.by_last_activity`: here the back end's own dating answers.
  world.backend.datedSessions = async (_folder, found) => {
    asked.push(found.map((session) => session.id));
    return found.map((session) => ({ ...session, lastModified: nowMs - 16 * 60_000 }));
  };
  await world.dispatch(message(`!resume ${SESSION_A}`));
  asked.length = 0;
  await world.dispatch(message("!status", { ts: OTHER_THREAD }));
  assert.equal(
    (said(world).at(-1) as string).split("\n")[1],
    `[Fix the footer](${PERMALINK}): idle · 16m ago`,
  );
  assert.deepEqual(asked, [[SESSION_A]]); // only the sessions the answer shows are read
});

test("bang status keeps a title s own brackets out of the link", async (t) => {
  const world = worldOf(t);
  world.storedSessions = [listed(SESSION_A, "fix [urgent](x) *now*", 0, 1)];
  await world.dispatch(message(`!resume ${SESSION_A}`));
  await world.dispatch(message("!status", { ts: OTHER_THREAD }));
  const row = (said(world).at(-1) as string).split("\n")[1] as string;
  assert.ok(row.startsWith(`[fix \\[urgent\\]\\(x\\) \\*now\\*](${PERMALINK}): idle`));
});

test("bang status falls back to session when the titles cannot be read", async (t) => {
  const world = worldOf(t);
  world.storedSessions = [listed(SESSION_A, "Fix the footer", 0, 1)];
  await world.dispatch(message(`!resume ${SESSION_A}`));
  world.backend.listSessions = async () => {
    throw new Error("unreadable");
  };
  await world.dispatch(message("!status", { ts: OTHER_THREAD }));
  // no title, no time
  assert.equal((said(world).at(-1) as string).split("\n")[1], `[Session](${PERMALINK}): idle`);
});

for (const [seconds, shown] of [
  [5, "just now"],
  [59, "just now"],
  [60, "1m ago"],
  [3_599, "59m ago"],
  [3_600, "1h ago"],
  [86_399, "23h ago"],
  [86_400, "1d ago"],
  [30 * 86_400, "30d ago"],
  [-30, "just now"],
] as const) {
  test(`how long ago reads in one unit [${seconds}-${shown}]`, () => {
    assert.equal(ago(seconds), shown);
  });
}

test("the hold question s buttons answer it in its own words", () => {
  // Issue #76: `Continue` beside the shorter `Cancel` made the second look the lesser choice,
  // and a button's width is its text. One `primary` in the set, as the button reference asks.
  const [, actions] = holdBlocks("hold-1", "<https://example.slack.com/x|Session>") as Body[];
  const [send, keep] = (actions as Body).elements as [Body, Body];
  assert.deepEqual([send.text.text, send.style], ["Send anyway", "primary"]);
  assert.deepEqual([keep.text.text, keep.style], ["Don't send", undefined]);
  assert.ok(Math.abs(send.text.text.length - keep.text.text.length) <= 1);
});

test("bang status fetches permalinks concurrently", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("hello", { ts: THREAD }));
  await world.dispatch(message("hello", { ts: OTHER_THREAD }));
  await world.dispatch(message("hello", { ts: CLICK_THREAD }));

  let inFlight = 0;
  let maxInFlight = 0;
  const real = world.slack.chat.getPermalink;
  world.slack.chat.getPermalink = (async (args) => {
    inFlight += 1;
    maxInFlight = Math.max(maxInFlight, inFlight);
    await new Promise<void>((resolve) => setImmediate(resolve));
    try {
      return await real(args);
    } finally {
      inFlight -= 1;
    }
  }) as typeof real;
  await world.dispatch(message("!status", { ts: "1790300000.000001" }));
  assert.equal(maxInFlight, 3); // all three fetched at once, not one after another
  const text = said(world).at(-1) as string;
  assert.equal(text.split("\n").length, 4); // header + one row per session, order kept
});

test("bang status shows waiting for the owner", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("hi", { ts: THREAD }));
  world.clients[0]?.inject([canUseToolCall("Bash", { command: "ls" })]);
  await world.until(() => world.approvals.pendingIn(CHANNEL, THREAD).length > 0);
  await world.dispatch(message("!status", { ts: OTHER_THREAD }));
  const line = (said(world).at(-1) as string).split("\n")[1] as string;
  assert.ok(line.includes(`: ${texts.STATUS_CHANNEL_WAITING}`));
});

test("bang status shows a background only session as busy not idle", async (t) => {
  const world = worldOf(t);
  // A task that outlived its turn: `session.busy` is false, yet it is not idle either.
  const first = splitTurns(sdkMessages("background"))[0];
  assert.ok(first !== undefined);
  await world.dispatch(message("start it", { ts: THREAD }));
  world.clients[0]?.answer(first);
  const session = world.sessions.get(CHANNEL, THREAD);
  assert.ok(session !== null);
  await world.until(() => !session.busy && session.runningKinds !== "");
  await world.dispatch(message("!status", { ts: OTHER_THREAD }));
  const line = (said(world).at(-1) as string).split("\n")[1] as string;
  assert.ok(line.includes(`: ${texts.STATUS_CHANNEL_BUSY}`));
  assert.ok(line.includes("⏳"));
});

test("bang status inside a thread shows that session", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("hi", { ts: THREAD }));
  await world.dispatch(reply("!status", THREAD));
  const shown = world.ephemerals();
  assert.equal(shown.length, 1);
  const [text] = shown as [string];
  assert.ok(text.startsWith("Directory:"));
  assert.ok(text.includes("\nContext: `7%`"));
});

test("an answer in a session thread sets its status line again", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("hi", { ts: THREAD }));
  const told: Array<[string, string]> = [];
  world.sessions.wrote = (channel, thread) => {
    told.push([channel, thread]);
  };
  await world.dispatch(reply("!status", THREAD));
  assert.deepEqual(told, [[CHANNEL, THREAD]]); // the answer cleared the status: it is set again
  await world.dispatch(message("!status")); // top-level: no thread, no status line
  assert.deepEqual(told, [[CHANNEL, THREAD]]);
});

test("bang stop inside a thread stops only that session", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("hello", { ts: THREAD })); // never ends
  await world.dispatch(message("hello", { ts: OTHER_THREAD })); // held: THREAD's session is busy
  const holdId = buttonValue(
    (world.slack.callsTo("chat.postMessage").at(-1) as Body).blocks,
    HOLD_CONTINUE,
  );
  await world.dispatch(clickIn(HOLD_CONTINUE, holdId, CHANNEL, OTHER_THREAD));
  const posted = world.slack.callsTo("chat.postMessage").length;
  await world.dispatch(reply("!stop", THREAD));
  // The fake client's turn never ends, so the answer goes out once the stop's wait for the
  // reply's end is over: Python lowered `STOP_TAIL_WAIT`, here the sessions' clock crosses it.
  await world.clock.advance(STOP_TAIL_WAIT);
  await world.until(() => world.slack.callsTo("chat.postMessage").length > posted);
  // One line that stays in the thread: an ephemeral one is gone on reload (issue #85).
  const answers = world.slack.callsTo("chat.postMessage").slice(posted) as Body[];
  assert.equal(answers.length, 1);
  assert.equal(answers[0]?.text, texts.STOPPED_THREAD);
  assert.equal(answers[0]?.thread_ts, THREAD);
  assert.deepEqual(world.ephemerals(), []);
  assert.equal(world.clients[0]?.interrupts, 1);
  assert.equal(world.clients[1]?.interrupts, 0);
});

test("bang stop inside an idle thread says nothing is running", async (t) => {
  const world = worldOf(t);
  await idleMessage(world, "hi", THREAD);
  await world.dispatch(reply("!stop", THREAD));
  // A post in the thread, which stays: the owner can tell the stop was received (issue #85).
  const answer = world.slack.callsTo("chat.postMessage").at(-1) as Body;
  assert.equal(answer.text, texts.NOTHING_TO_STOP_THREAD);
  assert.equal(answer.thread_ts, THREAD);
  assert.deepEqual(world.ephemerals(), []);
});

test("a malformed daemon word at top level shows only the daemon words", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("!bypass maybe"));
  const text = said(world).at(-1) as string;
  assert.ok(text.includes("`!bypass") && !text.includes("`!compact"));
  assert.ok(text.includes(texts.HELP_UNBOUND));
  assert.deepEqual(world.queries(), []);
});

test("a malformed daemon word inside a thread shows the session s help too", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("hi", { ts: THREAD }));
  await world.dispatch(reply("!bypass maybe", THREAD));
  const shown = world.ephemerals();
  assert.equal(shown.length, 1);
  const [text] = shown as [string];
  assert.ok(text.includes("`!bypass") && text.includes("`!compact"));
  assert.ok(!text.includes(texts.HELP_UNBOUND));
});

test("bang from anyone else does nothing", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("!bypass on", { user: STRANGER }));
  assert.deepEqual(world.clients, []);
  assert.ok(!world.postedAnything());
});

for (const user of [OWNER, STRANGER]) {
  test(`a click on new thread is acknowledged and does nothing [${user}]`, async (t) => {
    const world = worldOf(t);
    // A link button still sends its click to the app, which must acknowledge it (button element
    // reference, read 2026-10-01); Slack itself opens the link.
    const action = { type: "button", action_id: NEW_THREAD_ACTION };
    const response = await world.dispatch(homeAction(action, user));
    assert.equal(response.status, 200);
    assert.deepEqual(world.clients, []);
    assert.deepEqual(world.slack.apiCalls, []);
  });
}

for (const actionId of FILTER_ACTIONS) {
  test(`a home filter is read from the pages state and published [${actionId}]`, async (t) => {
    const world = worldOf(t);
    // Whichever of the four controls was used, the payload carries the state of them all.
    const values = {
      [FILTERS_BLOCK]: {
        [CHANNEL_ACTION]: chosenOption(CHANNEL),
        [STATUS_ACTION]: chosenOption("raised_hand"),
      },
      [SEARCH_BLOCK]: { [SEARCH_ACTION]: { type: "plain_text_input", value: "Footer" } },
    };
    const action = { type: "static_select", action_id: actionId, ...chosenOption("raised_hand") };
    const response = await world.dispatch(homeAction(action, OWNER, values));
    assert.equal(response.status, 200);
    const chosen = homeFilter({ channel: CHANNEL, status: "raised_hand", search: "Footer" });
    await world.until(
      () =>
        isDeepStrictEqual(world.home.chosen, chosen) &&
        world.slack.callsTo("views.publish").length > 0,
    );
    assert.deepEqual(
      new Set(world.slack.callsTo("views.publish").map((call) => call.user_id)),
      new Set([OWNER]),
    );
  });
}

test("show all chooses that channel", async (t) => {
  const world = worldOf(t);
  const action = { type: "button", action_id: SHOW_ALL_ACTION, value: CHANNEL };
  await world.dispatch(homeAction(action));
  await world.until(() => isDeepStrictEqual(world.home.chosen, homeFilter({ channel: CHANNEL })));
  // A channel that is not bound is no filter: the value of a click is untrusted.
  await world.dispatch(homeAction({ ...action, value: "C000NOPE" }));
  await world.until(() => isDeepStrictEqual(world.home.chosen, homeFilter()));
});

for (const [user, team] of ANYONE_ELSE) {
  test(`a home control used by anyone else changes nothing [${user}-${team}]`, async (t) => {
    const world = worldOf(t);
    const values = { [FILTERS_BLOCK]: { [STATUS_ACTION]: chosenOption("raised_hand") } };
    for (const action of [
      { type: "static_select", action_id: STATUS_ACTION, ...chosenOption("raised_hand") },
      { type: "button", action_id: SHOW_ALL_ACTION, value: CHANNEL },
    ]) {
      const body = homeAction(action, user, values);
      body.team.id = team;
      const response = await world.dispatch(body);
      assert.equal(response.status, 200);
    }
    assert.deepEqual(world.home.chosen, homeFilter());
    assert.deepEqual(world.slack.callsTo("views.publish"), []);
  });
}

test("edit and delete reach the home", async (t) => {
  const world = worldOf(t);
  const edit = { type: "button", action_id: EDIT_ACTION, value: EDIT_ON };
  const remove = { type: "button", action_id: DELETE_ACTION, value: `${CHANNEL}:${HOME_THREAD}` };
  // Out of edit mode a Delete click is one the page did not offer.
  await world.dispatch(homeAction(remove));
  await world.dispatch(homeAction(edit));
  await world.until(() => world.slack.callsTo("views.publish").length > 0);
  assert.deepEqual(world.deleted, []);
  await world.dispatch(homeAction(remove));
  await world.until(() => isDeepStrictEqual(world.deleted, [[CHANNEL, HOME_THREAD]]));
  const clean = { type: "button", action_id: CLEAN_ACTION, value: CHANNEL };
  await world.dispatch(homeAction(clean));
  await world.until(() => isDeepStrictEqual(world.cleaned, [CHANNEL]));
  await world.dispatch(homeAction({ ...edit, value: EDIT_OFF }));
  await world.dispatch(homeAction(remove));
  await world.dispatch(homeAction(clean));
  await world.idle();
  assert.deepEqual(world.deleted, [[CHANNEL, HOME_THREAD]]);
  assert.deepEqual(world.cleaned, [CHANNEL]);
});

for (const [user, team] of ANYONE_ELSE) {
  test(`edit and delete from anyone else do nothing [${user}-${team}]`, async (t) => {
    const world = worldOf(t);
    await world.home.edit(true);
    const published = world.slack.callsTo("views.publish").length;
    for (const action of [
      { type: "button", action_id: EDIT_ACTION, value: EDIT_OFF },
      { type: "button", action_id: DELETE_ACTION, value: `${CHANNEL}:${HOME_THREAD}` },
      { type: "button", action_id: CLEAN_ACTION, value: CHANNEL },
    ]) {
      const body = homeAction(action, user);
      body.team.id = team;
      assert.equal((await world.dispatch(body)).status, 200);
    }
    await world.idle();
    assert.deepEqual(world.deleted, []);
    assert.deepEqual(world.cleaned, []);
    assert.equal(world.slack.callsTo("views.publish").length, published); // still in edit mode
  });
}

test("the owner approves", async (t) => {
  const world = worldOf(t);
  const [approvalId, pending] = world.approvals.open(CHANNEL, CLICK_THREAD, "Bash: ls");
  const body = click("approval_allow", approvalId);
  await world.dispatch(body);
  assert.ok(pending.decided);
  // The tool's line in the reply records the call: the request message goes away.
  const deleted = world.slack.callsTo("chat.delete").map((args) => [args.channel, args.ts]);
  assert.deepEqual(deleted, [[CHANNEL, body.message.ts]]);
  assert.deepEqual(world.slack.callsTo("chat.update"), []);
});

for (const [index, user] of OTHER_USERS.entries()) {
  test(`nobody else can approve [user${index}]`, async (t) => {
    const world = worldOf(t);
    const [approvalId, pending] = world.approvals.open(CHANNEL, CLICK_THREAD, "Bash: ls");
    await world.dispatch(click("approval_allow", approvalId, user));
    assert.ok(!pending.decided);
    assert.ok(!world.postedAnything());
  });
}

test("stale approval click gets a note", async (t) => {
  const world = worldOf(t);
  await world.dispatch(click("approval_allow", "no-such-request"));
  assert.deepEqual(world.ephemerals(), [texts.APPROVAL_GONE]);
});

test("a request from another channel cannot be answered here", async (t) => {
  const world = worldOf(t);
  const [approvalId, pending] = world.approvals.open("C000ELSEWHERE", CLICK_THREAD, "Bash: ls");
  await world.dispatch(click("approval_allow", approvalId));
  assert.ok(!pending.decided);
  assert.deepEqual(world.ephemerals(), [texts.APPROVAL_GONE]);
});

test("a request from another thread cannot be answered here", async (t) => {
  const world = worldOf(t);
  const [approvalId, pending] = world.approvals.open(CHANNEL, OTHER_THREAD, "Bash: ls");
  await world.dispatch(click("approval_allow", approvalId));
  assert.ok(!pending.decided);
  assert.deepEqual(world.ephemerals(), [texts.APPROVAL_GONE]);
});

test("answer opens the form", async (t) => {
  const world = worldOf(t);
  const [approvalId] = world.approvals.open(CHANNEL, FORM_THREAD, "Colour", QUESTIONS);
  const body = recorded("open-click"); // the Answer click, recorded 2026-09-24
  body.actions[0].value = approvalId;
  body.trigger_id = "0000000000.0000000000.fake"; // real clicks carry one; the scrub drops it
  await world.dispatch(body);
  const opened = world.slack.callsTo("views.open") as Body[];
  assert.equal(opened.length, 1);
  assert.equal(opened[0]?.trigger_id, body.trigger_id);
  const view = opened[0]?.view as Body;
  assert.equal(view.callback_id, "question_form");
  assert.deepEqual(loadDraft(view.private_metadata), newDraft(approvalId, CHANNEL, FORM_THREAD));
});

for (const [index, user] of OTHER_USERS.entries()) {
  test(`nobody else can open the form [user${index}]`, async (t) => {
    const world = worldOf(t);
    const [approvalId] = world.approvals.open(CHANNEL, FORM_THREAD, "Colour", QUESTIONS);
    await world.dispatch(click("question_open", approvalId, user));
    assert.deepEqual(world.slack.callsTo("views.open"), []);
    assert.ok(!world.postedAnything());
  });
}

test("submit with the open question unanswered shows an error", async (t) => {
  const world = worldOf(t);
  const [approvalId, pending] = world.approvals.open(CHANNEL, FORM_THREAD, "Colour", QUESTIONS);
  const draft = newDraft(approvalId, CHANNEL, FORM_THREAD);
  const response = await world.dispatch(formBody("view_submission", draft, {}));
  assert.deepEqual(response.body, {
    response_action: "errors",
    errors: { q0: texts.QUESTION_MISSING },
  });
  assert.ok(!pending.decided);
});

test("next with an answer moves to the next question", async (t) => {
  const world = worldOf(t);
  const [approvalId, pending] = world.approvals.open(CHANNEL, FORM_THREAD, "Colour", QUESTIONS);
  const draft = newDraft(approvalId, CHANNEL, FORM_THREAD);
  const response = await world.dispatch(formBody("view_submission", draft, picked(0, "0")));
  const answer = response.body as Body;
  assert.equal(answer.response_action, "update");
  assert.deepEqual(
    loadDraft(answer.view.private_metadata),
    newDraft(approvalId, CHANNEL, FORM_THREAD, { active: 1, picks: new Map([[0, [0]]]) }),
  );
  assert.ok(!pending.decided);
});

test("a complete submit answers claude and leaves the request to the session", async (t) => {
  const world = worldOf(t);
  const [approvalId, pending] = world.approvals.open(CHANNEL, FORM_THREAD, "Colour", QUESTIONS);
  pending.messageTs = "1790000000.000009";
  const draft = newDraft(approvalId, CHANNEL, FORM_THREAD, {
    active: 1,
    picks: new Map([[0, [1]]]),
  });
  const values = {
    q1: { answer: { type: "checkboxes", selected_options: [{ value: "0" }] } },
    o1: { other: { type: "plain_text_input", value: "xl" } },
  };
  await world.dispatch(formBody("view_submission", draft, values));
  assert.ok(pending.decided);
  assert.deepEqual(await pending.decision, {
    answered: true,
    answers: { "Colour?": "blue", "Sizes?": ["s", "xl"] },
  });
  // The session that asked decides what becomes of the request (`keepAnswers`): its reply
  // keeps the answers and the request goes, or the request itself becomes the record.
  assert.deepEqual(world.slack.callsTo("chat.delete"), []);
  assert.deepEqual(world.slack.callsTo("chat.update"), []);
});

for (const [index, user] of OTHER_USERS.entries()) {
  test(`nobody else can submit the form [user${index}]`, async (t) => {
    const world = worldOf(t);
    const [approvalId, pending] = world.approvals.open(CHANNEL, FORM_THREAD, "Colour", QUESTIONS);
    const draft = newDraft(approvalId, CHANNEL, FORM_THREAD, {
      picks: new Map([
        [0, [0]],
        [1, [0]],
      ]),
    });
    await world.dispatch(formBody("view_submission", draft, {}, user));
    assert.ok(!pending.decided);
  });
}

test("a bypass inside a thread fails when the directory is gone", async (t) => {
  const world = worldOf(t);
  // A session that ran before: one that never did answers the word without connecting.
  world.state.openThread(CHANNEL, THREAD, "68da9311-0000-4000-8000-00000000beef");
  rmdirSync(join(world.root, "app"));
  await world.dispatch(reply("!bypass on", THREAD));
  assert.deepEqual(world.ephemerals(), [
    fill(texts.DIRECTORY_MISSING, { directory: join(world.root, "app") }),
  ]);
});

test("a form that cannot open tells the owner", async (t) => {
  const world = worldOf(t);
  const [approvalId] = world.approvals.open(CHANNEL, FORM_THREAD, "Colour", QUESTIONS);
  world.slack.responses["views.open"] = { ok: false, error: "expired_trigger_id" };
  const body = recorded("open-click");
  body.actions[0].value = approvalId;
  body.trigger_id = "0000000000.0000000000.fake";
  await world.dispatch(body);
  assert.deepEqual(world.ephemerals(), [
    fill(texts.QUESTION_NOT_OPENED, { error: "expired_trigger_id" }),
  ]);
});

test("a submit that needs more answers makes no slack call first", async (t) => {
  const world = worldOf(t);
  const [approvalId] = world.approvals.open(CHANNEL, FORM_THREAD, "Colour", QUESTIONS);
  const draft = newDraft(approvalId, CHANNEL, FORM_THREAD);
  await world.dispatch(formBody("view_submission", draft, {}));
  assert.deepEqual(
    world.slack.apiCalls.filter((call) => call.method.startsWith("conversations.")),
    [],
  );
});

test("slack links reach claude as typed", () => {
  // Message formatting reference (read 2026-09-25): Slack sends a link as <url|label> or <url>.
  assert.equal(slackUnescape("open <http://main.py|main.py> now"), "open main.py now");
  assert.equal(
    slackUnescape("see <https://example.com/a?b=1&amp;c=2>"),
    "see https://example.com/a?b=1&c=2",
  );
  assert.equal(
    slackUnescape("mail <mailto:bob@example.com|bob@example.com>"),
    "mail bob@example.com",
  );
  assert.equal(slackUnescape("hi <@U000BOB>"), "hi <@U000BOB>"); // a mention stays as Slack sent it
  // A link the owner named keeps its address: Claude could not open the label alone.
  assert.equal(
    slackUnescape("read <https://example.com/x|the docs>"),
    "read the docs (https://example.com/x)",
  );
});

/** An error named as Python's exception class was: the name is what `ERROR_REPLY` shows. */
function named(name: string, message: string): Error {
  const error = new Error(message);
  error.name = name;
  return error;
}

test("bang resume lists the directory s sessions with buttons", async (t) => {
  const world = worldOf(t);
  twoSessions(world);
  await world.dispatch(message("!resume", { ts: THREAD }));
  const posts = world.slack.callsTo("chat.postMessage") as Body[];
  assert.equal(posts.length, 1);
  const [post] = posts as [Body];
  const values = (post.blocks as Body[])
    .filter((b) => "accessory" in b)
    .map((b) => b.accessory.value);
  // The list is a top-level post; each button names the thread of the owner's `!resume`.
  assert.deepEqual(values, [`${SESSION_A}@${THREAD}`, `${SESSION_B}@${THREAD}`]);
  assert.ok(!("thread_ts" in post));
  assert.deepEqual(world.ephemerals(), []);
  assert.equal(post.unfurl_links, false);
  assert.equal(post.unfurl_media, false);
  assert.deepEqual(world.clients, []); // listing starts no Claude Code process
});

test("bang resume by name or id opens a new thread on it", async (t) => {
  const world = worldOf(t);
  twoSessions(world);
  await world.dispatch(message("!resume footer", { ts: THREAD }));
  assert.equal(world.state.thread(CHANNEL, THREAD)?.sessionId, SESSION_A);
  await world.dispatch(message(`!resume ${SESSION_B}`, { ts: OTHER_THREAD }));
  assert.equal(world.state.thread(CHANNEL, OTHER_THREAD)?.sessionId, SESSION_B);
  const confirmation = said(world).at(-1) as string;
  assert.ok(confirmation.includes("Trust gate"));
  assert.ok(!(confirmation.split("**")[1] as string).includes("\n"));
  assert.equal((world.slack.callsTo("chat.postMessage").at(-1) as Body).thread_ts, OTHER_THREAD);
});

test("bang resume of an unknown session says so", async (t) => {
  const world = worldOf(t);
  twoSessions(world);
  await world.dispatch(message("!resume nothing-like-it"));
  assert.ok((said(world).at(-1) as string).includes("`nothing-like-it`"));
  assert.ok(!("thread_ts" in (world.slack.callsTo("chat.postMessage").at(-1) as Body)));
  assert.deepEqual(world.ephemerals(), []);
  assert.equal(world.state.channel(CHANNEL)?.threads.size, 0);
});

test("the owner resumes from the list", async (t) => {
  const world = worldOf(t);
  twoSessions(world);
  const body = resumeClick(SESSION_B, THREAD);
  await world.dispatch(body);
  // The session lives in the thread of the owner's `!resume` message, not the picker's.
  assert.equal(world.state.thread(CHANNEL, THREAD)?.sessionId, SESSION_B);
  assert.equal(world.state.thread(CHANNEL, CLICK_THREAD), null);
  const posts = world.slack.callsTo("chat.postMessage") as Body[];
  assert.equal(posts.length, 1);
  assert.equal(posts[0]?.thread_ts, THREAD);
  assert.ok(posts[0]?.text.includes("Trust gate"));
  // The picker has done its job: it is deleted, and the thread holds the one record (#70).
  const deleted = world.slack.callsTo("chat.delete") as Body[];
  assert.equal(deleted.length, 1);
  assert.deepEqual([deleted[0]?.channel, deleted[0]?.ts], [CHANNEL, body.message.ts]);
  assert.deepEqual(world.slack.callsTo("chat.update"), []);
  assert.deepEqual(world.ephemerals(), []);
});

test("a list that cannot be deleted is rewritten without its buttons", async (t) => {
  const world = worldOf(t);
  twoSessions(world);
  world.slack.responses["chat.delete"] = named("RuntimeError", "network down");
  const body = resumeClick(SESSION_B, THREAD);
  await world.dispatch(body);
  assert.equal(world.state.thread(CHANNEL, THREAD)?.sessionId, SESSION_B);
  const edits = world.slack.callsTo("chat.update") as Body[];
  assert.equal(edits.length, 1);
  const [edited] = edits as [Body];
  assert.equal(edited.ts, body.message.ts);
  assert.ok(edited.text.includes("Trust gate") && edited.text.includes(PERMALINK));
  assert.ok((edited.blocks as Body[]).every((b) => !("accessory" in b)));
});

test("a resume click with a malformed thread changes nothing", async (t) => {
  const world = worldOf(t);
  twoSessions(world);
  for (const value of [SESSION_B, `${SESSION_B}@`, `${SESSION_B}@not-a-ts`, `@${THREAD}`]) {
    await world.dispatch(click("session_resume", value));
  }
  assert.equal(world.state.channel(CHANNEL)?.threads.size, 0);
  // A bare session id is a list posted before the value carried its thread.
  assert.deepEqual(said(world), Array(4).fill(texts.RESUME_STALE));
});

for (const [index, user] of OTHER_USERS.entries()) {
  test(`nobody else can resume [user${index}]`, async (t) => {
    const world = worldOf(t);
    twoSessions(world);
    await world.dispatch(resumeClick(SESSION_B, THREAD, user));
    assert.equal(world.state.channel(CHANNEL)?.threads.size, 0);
  });
}

test("a resume click on a thread stored but not live is refused", async (t) => {
  const world = worldOf(t);
  // An idle close or a restart evicts the live object, not the entry: the click must not
  // report a resume that `openThread` would silently not perform.
  twoSessions(world);
  world.state.openThread(CHANNEL, THREAD, SESSION_A);
  await world.dispatch(resumeClick(SESSION_B, THREAD));
  assert.equal(world.state.thread(CHANNEL, THREAD)?.sessionId, SESSION_A);
  assert.deepEqual(said(world), [texts.RESUME_HELD]);
  assert.deepEqual(world.slack.callsTo("chat.update"), []);
});

test("a typed resume in a non session thread lists nothing", async (t) => {
  const world = worldOf(t);
  // It listed the folder's sessions with buttons naming that thread, so a click made a thread
  // under some other post into a session. `!resume` is a word for the channel.
  twoSessions(world);
  await world.dispatch(reply("!resume", OTHER_THREAD));
  assert.deepEqual(world.slack.callsTo("chat.postMessage"), []);
  assert.deepEqual(world.ephemerals(), [fill(texts.WORD_IN_THREAD, { word: "resume" })]);
});

test("a button is never trusted for a session of another directory", async (t) => {
  const world = worldOf(t);
  twoSessions(world);
  await world.dispatch(resumeClick("68da9311-0000-4000-8000-0000000000ff"));
  assert.equal(world.state.channel(CHANNEL)?.threads.size, 0);
  assert.deepEqual(said(world), [texts.RESUME_GONE]);
  assert.deepEqual(world.ephemerals(), []);
});

test("a typed resume of a session held by another thread is refused", async (t) => {
  const world = worldOf(t);
  // Two live processes on one transcript is never reachable.
  twoSessions(world);
  await world.dispatch(message(`!resume ${SESSION_B}`, { ts: OTHER_THREAD }));
  assert.equal(world.state.thread(CHANNEL, OTHER_THREAD)?.sessionId, SESSION_B);
  await world.dispatch(message(`!resume ${SESSION_B}`, { ts: THREAD }));
  assert.equal(world.state.thread(CHANNEL, THREAD), null);
  const link = `<${PERMALINK}|Session>`;
  assert.ok(said(world).includes(fill(texts.RESUME_ELSEWHERE, { link })));
  assert.deepEqual(world.ephemerals(), []);
});

test("a resume click of a session held by another thread is refused", async (t) => {
  const world = worldOf(t);
  twoSessions(world);
  await world.dispatch(message(`!resume ${SESSION_B}`, { ts: OTHER_THREAD }));
  await world.dispatch(resumeClick(SESSION_B, THREAD)); // a different thread
  assert.equal(world.state.thread(CHANNEL, THREAD), null);
  const link = `<${PERMALINK}|Session>`;
  assert.ok(said(world).includes(fill(texts.RESUME_ELSEWHERE, { link })));
  assert.deepEqual(world.slack.callsTo("chat.update"), []); // the picker is left as it was
});

test("a held session s link falls back when the permalink fails", async (t) => {
  const world = worldOf(t);
  twoSessions(world);
  await world.dispatch(message(`!resume ${SESSION_B}`, { ts: OTHER_THREAD }));
  world.slack.responses["chat.getPermalink"] = named("RuntimeError", "network down");
  await world.dispatch(message(`!resume ${SESSION_B}`, { ts: THREAD }));
  assert.equal(world.state.thread(CHANNEL, THREAD), null);
  const fallback = fill(texts.STATUS_CHANNEL_LINK_FALLBACK, { thread_ts: OTHER_THREAD });
  assert.ok(said(world).includes(fill(texts.RESUME_ELSEWHERE, { link: fallback })));
});

test("the list counts a session held elsewhere and gives it no row", async (t) => {
  const world = worldOf(t);
  twoSessions(world);
  await world.dispatch(message(`!resume ${SESSION_B}`, { ts: OTHER_THREAD }));
  await world.dispatch(message("!resume"));
  const blocks = (world.slack.callsTo("chat.postMessage").at(-1) as Body).blocks as Body[];
  const rows = blocks.filter((b) => b.block_id).map((b) => b.block_id);
  assert.deepEqual(rows, [`session-${SESSION_A}`]); // the rows are the sessions that can be resumed
  assert.equal(blocks.at(-1)?.elements[0].text, texts.RESUME_OPEN_ONE);
  assert.deepEqual(world.slack.callsTo("chat.getPermalink"), []); // no row links to a thread
});

test("a still running first turn already holds its session id", async (t) => {
  const world = worldOf(t);
  // The session id is recorded as soon as Claude Code reports it (the init message), not only
  // at the turn's result, so a still-running first turn is already this session id's holder and
  // cannot be resumed a second time into another thread.
  await world.dispatch(message("hello", { ts: THREAD }));
  world.clients[0]?.inject(sdkMessages("tools").slice(0, -1)); // no result: still running
  await world.until(() => Boolean(world.state.thread(CHANNEL, THREAD)?.sessionId));
  const heldId = world.state.thread(CHANNEL, THREAD)?.sessionId as string;
  world.storedSessions = [listed(heldId, "tools", 0, 1)];
  await world.dispatch(message(`!resume ${heldId}`, { ts: OTHER_THREAD }));
  assert.equal(world.state.thread(CHANNEL, OTHER_THREAD), null);
  const link = `<${PERMALINK}|Session>`;
  assert.ok(said(world).includes(fill(texts.RESUME_ELSEWHERE, { link })));
});

test("resume opens an independent thread while another is busy", async (t) => {
  const world = worldOf(t);
  twoSessions(world);
  await world.dispatch(message("hello", { ts: THREAD })); // the fake Claude Code never ends this turn
  await world.dispatch(message(`!resume ${SESSION_B}`, { ts: OTHER_THREAD }));
  assert.equal(world.state.thread(CHANNEL, OTHER_THREAD)?.sessionId, SESSION_B);
  assert.ok((said(world).at(-1) as string).includes("Trust gate"));
});

test("a top level word whose failure report fails pushes nothing", async (t) => {
  const world = worldOf(t);
  // The word's own failure and then the channel post that reports it both fail: the outer
  // handler must not fall back to a threaded `ERROR_REPLY` under the word (a push).
  world.backend.listSessions = async () => {
    throw named("PermissionError", "transcripts unreadable");
  };
  world.slack.responses["chat.postMessage"] = named("RuntimeError", "network down");
  await world.dispatch(message("!resume"));
  const posts = world.slack.callsTo("chat.postMessage");
  assert.ok(posts.length > 0 && posts.every((post) => !("thread_ts" in post)));
});

test("a failing resume click tells the owner", async (t) => {
  const world = worldOf(t);
  world.backend.listSessions = async () => {
    throw named("PermissionError", "transcripts unreadable");
  };
  await world.dispatch(resumeClick(SESSION_B));
  assert.deepEqual(said(world), [fill(texts.ERROR_REPLY, { error: "PermissionError" })]);
  assert.ok(!("thread_ts" in (world.slack.callsTo("chat.postMessage")[0] as Body)));
});

test("bang guide works in an unbound channel", async (t) => {
  const world = worldFor(t)({ bound: false });
  await world.dispatch(message("!guide"));
  // The block holds the whole guide; `text` is its fallback, cut at FALLBACK_LIMIT.
  const posts = world.slack.callsTo("chat.postMessage") as Body[];
  assert.equal(posts.length, 1);
  assert.deepEqual(posts[0]?.blocks, [{ type: "markdown", text: texts.GUIDE }]);
  assert.equal(posts[0]?.text, take(texts.GUIDE, FALLBACK_LIMIT));
  assert.deepEqual(world.clients, []);
});

test("bang guide inside a thread is for the owner alone", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("hi", { ts: THREAD }));
  await world.dispatch(reply("!guide", THREAD));
  assert.ok(!said(world).includes(texts.GUIDE));
  const guides = world.slack.callsTo("chat.postEphemeral") as Body[];
  assert.equal(guides.length, 1);
  assert.equal(guides[0]?.thread_ts, THREAD);
  assert.ok(guides[0]?.text.startsWith("**awaydesk**"));
});

for (const word of [
  "!help",
  "!guide",
  "!status",
  "!stop",
  "!bind",
  "!bind /",
  "!bypass on",
  "!resume",
]) {
  test(`a word typed in the channel is answered by a top level post [${word}]`, async (t) => {
    const world = worldOf(t);
    twoSessions(world);
    await world.dispatch(message(word, { ts: THREAD }));
    const posts = world.slack.callsTo("chat.postMessage");
    assert.ok(posts.length > 0 && posts.every((post) => !("thread_ts" in post)));
    assert.deepEqual(world.ephemerals(), []);
    assert.deepEqual(world.clients, []);
  });
}

for (const word of ["!bind", "!status", "!bypass on"]) {
  test(`a word typed in a thread that is no session is answered top level [${word}]`, async (t) => {
    const world = worldOf(t);
    twoSessions(world);
    await world.dispatch(reply(word, CLICK_THREAD));
    const posts = world.slack.callsTo("chat.postMessage");
    assert.ok(posts.length > 0 && posts.every((post) => !("thread_ts" in post)));
    assert.deepEqual(world.ephemerals(), []);
  });
}

test("a failure starting a prompt lands in its thread", async (t) => {
  const world = worldOf(t);
  // The turn's one push: the failure of a prompt that starts its session is posted in that
  // session's thread (a command needs the connection at once), not shown to the owner alone.
  world.connectError = named("RuntimeError", "boom");
  await world.dispatch(message("!compact", { ts: THREAD }));
  assert.deepEqual(said(world), [fill(texts.ERROR_REPLY, { error: "RuntimeError" })]);
  assert.equal((world.slack.callsTo("chat.postMessage")[0] as Body).thread_ts, THREAD);
  assert.deepEqual(world.ephemerals(), []);
});

test("a refusal to a thread message is for the owner alone", async (t) => {
  const world = worldOf(t);
  await world.dispatch(recordedThreadReply()); // a thread that holds no session
  assert.deepEqual(world.ephemerals(), [texts.NOT_A_SESSION]);
  assert.deepEqual(said(world), []);
});

test("bang bind alone lists the folders with buttons", async (t) => {
  const world = worldFor(t)({ bound: false });
  mkdirSync(join(world.root, "docs"));
  await world.dispatch(message("!bind"));
  const posts = world.slack.callsTo("chat.postMessage") as Body[];
  assert.equal(posts.length, 1);
  const [post] = posts as [Body];
  const values = (post.blocks as Body[])
    .filter((b) => "accessory" in b)
    .map((b) => b.accessory.value);
  assert.deepEqual(values, [".", "app", "docs"]); // the root first: the fake trusts every folder
  assert.equal(post.unfurl_links, false);
  assert.equal(post.unfurl_media, false);
  assert.deepEqual(world.clients, []); // listing starts no Claude Code process
  await world.sessions.closeAll();
});

test("the owner binds from the list", async (t) => {
  const world = worldFor(t)({ bound: false });
  const body = click("folder_bind", "app");
  await world.dispatch(body);
  const app = join(world.root, "app");
  assert.equal(world.state.channel(CHANNEL)?.directory, app);
  assert.deepEqual(said(world), [fill(texts.BIND_OK, { directory: app })]);
  // top-level, like the list
  assert.ok(!("thread_ts" in (world.slack.callsTo("chat.postMessage")[0] as Body)));
  const deleted = world.slack.callsTo("chat.delete").map((args) => [args.channel, args.ts]);
  assert.deepEqual(deleted, [[CHANNEL, body.message.ts]]);
  await world.sessions.closeAll();
});

for (const [index, user] of OTHER_USERS.entries()) {
  test(`nobody else can bind from the list [user${index}]`, async (t) => {
    const world = worldFor(t)({ bound: false });
    await world.dispatch(click("folder_bind", "app", user));
    assert.equal(world.state.channel(CHANNEL), null);
    assert.ok(!world.postedAnything());
  });
}

test("a bind button is never trusted for a folder outside the root", async (t) => {
  const world = worldOf(t);
  await world.dispatch(click("folder_bind", "../.."));
  assert.equal(world.state.channel(CHANNEL)?.directory, join(world.root, "app"));
  assert.deepEqual(said(world), [fill(texts.BIND_OUTSIDE, { path: "../..", root: world.root })]);
  assert.deepEqual(world.slack.callsTo("chat.delete"), []);
});

test("no trusted folder says so in the notification too", async (t) => {
  const world = worldFor(t)({ bound: false });
  world.backend.trusted = async () => false;
  await world.dispatch(message("!bind"));
  const posts = world.slack.callsTo("chat.postMessage") as Body[];
  assert.equal(posts.length, 1);
  assert.equal(posts[0]?.text, fill(texts.BIND_EMPTY, { root: world.root }));
  await world.sessions.closeAll();
});

test("a bind click on the channel s own folder changes nothing", async (t) => {
  const world = worldOf(t);
  await world.dispatch(click("folder_bind", "app"));
  const app = join(world.root, "app");
  assert.deepEqual(said(world), [fill(texts.BIND_ALREADY, { directory: app })]);
  assert.equal(world.state.channel(CHANNEL)?.directory, app);
});

test("a bind click while a turn runs is refused and the list stays", async (t) => {
  const world = worldOf(t);
  mkdirSync(join(world.root, "docs"));
  await world.dispatch(message("hello")); // the fake Claude Code never ends this turn
  await world.dispatch(click("folder_bind", "docs"));
  assert.equal(world.state.channel(CHANNEL)?.directory, join(world.root, "app"));
  assert.equal(said(world).at(-1), texts.BIND_BUSY);
  assert.deepEqual(world.slack.callsTo("chat.delete"), []);
});

test("only the list reads the transcripts for dates", async (t) => {
  const world = worldOf(t);
  // Matching an id or a title, or checking a click, needs no dates: they cost a file read each.
  const dated: string[] = [];
  // Python replaced `sessions.by_last_activity`: here the back end's own dating answers.
  world.backend.datedSessions = async (folder, stored) => {
    dated.push(folder);
    return [...stored];
  };
  twoSessions(world);
  await world.dispatch(message("!resume footer"));
  await world.dispatch(resumeClick(SESSION_B));
  assert.deepEqual(dated, []);
  await world.dispatch(message("!resume"));
  assert.deepEqual(dated, [join(world.root, "app")]);
});

test("a second resume click on the same list is refused", async (t) => {
  const world = worldOf(t);
  twoSessions(world);
  await world.dispatch(resumeClick(SESSION_A));
  await world.dispatch(resumeClick(SESSION_B)); // a quick second click, same list
  assert.equal(world.state.thread(CHANNEL, THREAD)?.sessionId, SESSION_A);
  assert.ok(said(world).includes(texts.RESUME_HELD));
});

test("a typed resume under the picker resumes nothing", async (t) => {
  const world = worldOf(t);
  twoSessions(world);
  // The picker's own thread holds no session yet: `!resume footer` typed there is refused like
  // in any such thread, and the picker's buttons still work.
  await world.dispatch(reply("!resume footer", CLICK_THREAD));
  assert.equal(world.state.thread(CHANNEL, CLICK_THREAD), null);
  assert.deepEqual(world.ephemerals(), [fill(texts.WORD_IN_THREAD, { word: "resume" })]);
  await world.dispatch(resumeClick(SESSION_B, CLICK_THREAD));
  assert.equal(world.state.thread(CHANNEL, CLICK_THREAD)?.sessionId, SESSION_B);
});

/**
 * A listing of the folder's sessions that says when it began and answers when the test lets it:
 * Python's `list_sessions` reading the old folder's transcripts for 200 ms.
 */
function slowListing(world: World): { listed: AsyncEvent; release: AsyncEvent } {
  const listed = new AsyncEvent();
  const release = new AsyncEvent();
  world.backend.listSessions = async () => {
    listed.set();
    await release.wait();
    return world.storedSessions;
  };
  return { listed, release };
}

test("a resume click never stores a session of a folder bound meanwhile", async (t) => {
  const world = worldOf(t);
  twoSessions(world);
  mkdirSync(join(world.root, "docs"));
  const { listed: began, release } = slowListing(world);
  const clicking = world.dispatch(resumeClick(SESSION_B));
  await began.wait();
  await world.dispatch(message("!bind docs"));
  release.set();
  await clicking;
  await world.idle();
  assert.equal(world.state.channel(CHANNEL)?.directory, join(world.root, "docs"));
  assert.equal(world.state.channel(CHANNEL)?.threads.size, 0);
  assert.ok(said(world).includes(texts.RESUME_GONE));
});

test("a typed resume never stores a session of a folder bound meanwhile", async (t) => {
  const world = worldOf(t);
  twoSessions(world);
  mkdirSync(join(world.root, "docs"));
  const { listed: began, release } = slowListing(world);
  const resuming = world.dispatch(message(`!resume ${SESSION_B}`, { ts: THREAD }));
  await began.wait();
  await world.dispatch(message("!bind docs"));
  release.set();
  await resuming;
  await world.idle();
  assert.equal(world.state.channel(CHANNEL)?.directory, join(world.root, "docs"));
  assert.equal(world.state.thread(CHANNEL, THREAD), null);
  assert.ok(said(world).includes(texts.RESUME_GONE));
});

for (const [index, [title, shown]] of (
  [
    // Measured live 2026-09-25: an unescaped lone `*` paired with the bold's own asterisks.
    ["Clean up *.pyc files", "Clean up \\*.pyc files"],
    ["a_b [x](y) `c` {d} & e\\f", "a\\_b \\[x\\]\\(y\\) \\`c\\` \\{d\\} \\& e\\\\f"],
    // Measured live 2026-09-25: `~~old~~` rendered as strikethrough; `<x>` stayed text.
    ["fix ~~old~~ <example.com>", "fix \\~\\~old\\~\\~ <example.com>"],
  ] as const
).entries()) {
  test(`a resumed title keeps its characters inside the bold [${index}: ${title}]`, async (t) => {
    const world = worldOf(t);
    world.storedSessions = [listed(SESSION_A, title, 0, 1, { customTitle: title })];
    await world.dispatch(message(`!resume ${SESSION_A}`));
    assert.deepEqual(said(world), [fill(texts.RESUME_OK, { title: shown })]);
  });
}

test("the resumed list keeps a title with underscores readable", async (t) => {
  const world = worldOf(t);
  const title = "fix_the_parser";
  world.storedSessions = [listed(SESSION_A, title, 0, 1, { customTitle: title })];
  world.slack.responses["chat.delete"] = named("RuntimeError", "network down"); // the list is rewritten
  const body = resumeClick(SESSION_A, THREAD);
  await world.dispatch(body);
  const edits = world.slack.callsTo("chat.update") as Body[];
  assert.equal(edits.length, 1);
  assert.ok(edits[0]?.text.includes(title) && !edits[0]?.text.includes(`_${title}_`));
});

test("a failing confirmation still updates the list", async (t) => {
  const world = worldOf(t);
  twoSessions(world);
  // The first post (the confirmation) fails; the failure report that follows goes through.
  world.slack.responses["chat.postMessage"] = [
    named("RuntimeError", "network down"),
    { ok: true, ts: "1" },
  ];
  await world.dispatch(resumeClick(SESSION_B, THREAD));
  assert.equal(world.state.thread(CHANNEL, THREAD)?.sessionId, SESSION_B);
  // No confirmation reached the thread: the list is kept, rewritten, as the record.
  const edits = world.slack.callsTo("chat.update") as Body[];
  assert.equal(edits.length, 1);
  assert.ok(((edits[0] as Body).blocks as Body[]).every((b) => !("accessory" in b)));
  assert.deepEqual(world.slack.callsTo("chat.delete"), []);
});

test("a failing list edit does not block the confirmation", async (t) => {
  const world = worldOf(t);
  twoSessions(world);
  world.slack.responses["chat.update"] = named("RuntimeError", "network down");
  await world.dispatch(resumeClick(SESSION_B, THREAD));
  assert.equal(world.state.thread(CHANNEL, THREAD)?.sessionId, SESSION_B);
  assert.ok((said(world).at(-1) as string).includes("Trust gate"));
});

test("a click failure whose report fails raises nothing", async (t) => {
  const world = worldOf(t);
  world.backend.listSessions = async () => {
    throw named("PermissionError", "transcripts unreadable");
  };
  world.slack.responses["chat.postMessage"] = named("RuntimeError", "network down");
  const failures = t.mock.method(logger, "error");
  // would reach Bolt's error handler if the report threw
  const response = await world.dispatch(resumeClick(SESSION_B));
  assert.equal(response.error, null);
  assert.ok(
    failures.mock.calls.every((call) => !String(call.arguments[0]).startsWith("handler failed")),
  );
});

test("a resumed session with no title shows its id", async (t) => {
  const world = worldOf(t);
  world.storedSessions = [listed(SESSION_A, "", 0, 1)];
  await world.dispatch(message(`!resume ${SESSION_A}`));
  assert.deepEqual(said(world), [fill(texts.RESUME_OK, { title: SESSION_A })]);
});

test("an image reaches claude as an image block", async (t) => {
  const world = worldOf(t);
  const body = sharedFile("image");
  world.downloads.set(body.event.files[0].url_private_download, PNG);
  await world.dispatch(body);
  const queries = world.queries();
  assert.equal(queries.length, 1);
  // The seam's parts of one message: Python's SDK took `message.content` blocks.
  const content = queries[0] as unknown as Body[];
  assert.deepEqual(content[0], { type: "text", text: body.event.text });
  assert.equal(content[1]?.type, "image");
  assert.equal(content[1]?.mediaType, "image/png");
});

test("a file reaches claude as a path to its saved copy", async (t) => {
  const world = worldOf(t);
  const body = sharedFile("snippet");
  world.downloads.set(body.event.files[0].url_private_download, HELLO);
  await world.dispatch(body);
  const queries = world.queries();
  assert.equal(queries.length, 1);
  const saved = join(world.uploads, "F000FILE-notes.txt");
  assert.deepEqual(readFileSync(saved), Buffer.from(HELLO));
  assert.equal(queries[0], `${body.event.text}\n\nAttached files:\n- ${saved}`);
});

test("a refused file sends nothing and says why", async (t) => {
  const world = worldOf(t);
  await world.dispatch(sharedFile("image", { mimetype: "image/heic" }));
  const reason = fill(texts.UPLOAD_IMAGE_TYPE, { mimetype: "image/heic" });
  assert.deepEqual(world.ephemerals(), [fill(texts.UPLOAD_FAILED, { name: "photo.png", reason })]);
  assert.deepEqual(world.queries(), []);
});

test("a failed download sends nothing and says why", async (t) => {
  const world = worldOf(t);
  const body = sharedFile("image");
  world.downloads.set(body.event.files[0].url_private_download, new DownloadFailed("HTTP 404"));
  await world.dispatch(body);
  const reason = fill(texts.UPLOAD_DOWNLOAD, { error: "HTTP 404" });
  assert.deepEqual(world.ephemerals(), [fill(texts.UPLOAD_FAILED, { name: "photo.png", reason })]);
  assert.deepEqual(world.queries(), []);
});

test("a file in an unbound channel explains how to bind", async (t) => {
  const world = worldFor(t)({ bound: false });
  await world.dispatch(sharedFile("image"));
  assert.deepEqual(said(world), [fill(texts.UNBOUND, { root: world.root })]);
  assert.ok(!("thread_ts" in (world.slack.callsTo("chat.postMessage")[0] as Body)));
  await world.sessions.closeAll();
});

for (const [index, user] of ([{ user: STRANGER }, { team: OTHER_TEAM }] as Body[]).entries()) {
  test(`a file from anyone else is never downloaded [user${index}]`, async (t) => {
    const world = worldOf(t);
    const body = sharedFile("image");
    if ("team" in user) body.event.files[0].user_team = OTHER_TEAM;
    else Object.assign(body.event, user);
    await world.dispatch(body);
    assert.deepEqual(world.fetched, []);
    assert.ok(!world.postedAnything());
  });
}

test("a file on another host is never downloaded", async (t) => {
  const world = worldOf(t);
  await world.dispatch(sharedFile("image", { url_private_download: "https://evil.example/x.png" }));
  assert.deepEqual(world.fetched, []);
  assert.deepEqual(world.ephemerals(), [
    fill(texts.UPLOAD_FAILED, { name: "photo.png", reason: texts.UPLOAD_NOT_SHARED }),
  ]);
});

/**
 * A message with a file whose download is still running when this returns (it takes 0.2 s of
 * the handlers' clock), with the session its thread opened: `submit` records what is queued.
 */
async function downloading(
  world: World,
  body: Body,
): Promise<{ session: ThreadSession; queued: unknown[] }> {
  world.downloads.set(body.event.files[0].url_private_download, HELLO);
  world.slowDownloads = 0.2;
  await world.dispatch(body); // comes to rest inside the download
  const session = world.sessions.get(CHANNEL, String(body.event.ts));
  assert.ok(session !== null);
  const queued: unknown[] = [];
  const submit = session.submit.bind(session);
  session.submit = async (prompt) => {
    queued.push(prompt);
    return submit(prompt);
  };
  return { session, queued };
}

test("a message with files keeps its place in the queue", async (t) => {
  const world = worldOf(t);
  // Downloads take time: a reply sent right after must not enter the queue first.
  const body = sharedFile("snippet");
  const fileThread = String(body.event.ts);
  const { queued } = await downloading(world, body);
  await world.dispatch(reply("focus on the errors", fileThread));
  await world.appClock.advance(0.2);
  await world.settle();
  assert.deepEqual(
    queued.map((prompt) => String(prompt).startsWith(body.event.text)),
    [true, false],
  );
});

test("a failed download leaves no saved copy", async (t) => {
  const world = worldOf(t);
  const body = sharedFile("snippet");
  const good = structuredClone(body.event.files[0]);
  const bad = { ...good, id: "F000FAIL", url_private_download: `${good.url_private_download}x` };
  body.event.files = [good, bad];
  world.downloads.set(good.url_private_download, HELLO);
  world.downloads.set(bad.url_private_download, new DownloadFailed("HTTP 404"));
  await world.dispatch(body);
  assert.deepEqual(world.queries(), []);
  assert.deepEqual(existsSync(world.uploads) ? readdirSync(world.uploads) : [], []);
});

test("too many images send nothing and download nothing", async (t) => {
  const world = worldOf(t);
  const body = sharedFile("image");
  body.event.files = Array(6).fill(body.event.files[0]);
  await world.dispatch(body);
  assert.deepEqual(world.fetched, []);
  assert.deepEqual(world.queries(), []);
  assert.deepEqual(world.ephemerals(), [fill(texts.UPLOAD_TOO_MANY, { count: 6, limit: 5 })]);
});

test("a bind during the downloads never moves the thread already opened", async (t) => {
  const world = worldOf(t);
  // The file's own thread keeps the folder it opened in: a bind only affects new threads.
  mkdirSync(join(world.root, "docs"));
  const body = sharedFile("snippet");
  await downloading(world, body);
  await world.dispatch(message("!bind docs"));
  await world.appClock.advance(0.2);
  await world.settle();
  assert.notDeepEqual(world.queries(), []);
  assert.equal(world.state.channel(CHANNEL)?.directory, join(world.root, "docs"));
});

test("a command after a message with files waits its turn", async (t) => {
  const world = worldOf(t);
  const body = sharedFile("snippet");
  const fileThread = String(body.event.ts);
  const { queued } = await downloading(world, body);
  await world.dispatch(reply("!compact", fileThread));
  await world.appClock.advance(0.2);
  await world.settle();
  assert.deepEqual(
    queued.map((prompt) => String(prompt).startsWith(body.event.text)),
    [true, false],
  );
});

test("a session closed during a slow download is retried on a fresh one", async (t) => {
  const world = worldOf(t);
  // Whatever closed the session handed to this call (an idle close, most likely, though
  // `touch()` at the lookup already guards that common case) during the slow step below must
  // not lose the prompt: `submitToSession` retries once against a freshly looked-up session.
  const body = sharedFile("snippet");
  const fileThread = String(body.event.ts);
  const { session } = await downloading(world, body);
  await session.close(); // something else closes it while the download is still running
  await world.appClock.advance(0.2);
  await world.settle(); // let the slow download finish and the retried submit run
  const queries = world.queries();
  assert.equal(queries.length, 1); // the retry queues the prompt once, never twice
  assert.ok(String(queries[0]).startsWith(body.event.text));
  const rebuilt = world.sessions.get(CHANNEL, fileThread);
  assert.ok(rebuilt !== null && rebuilt !== session);
});

test("a retry that finds the thread gone answers session gone", async (t) => {
  const world = worldOf(t);
  // A `SessionGone` close (unlike an idle close) also removes the thread's own entry, so the
  // retry's fresh lookup finds nothing: answering the stale `SessionClosed` text, which
  // promises a retry will help, would be wrong.
  const body = sharedFile("snippet");
  const fileThread = String(body.event.ts);
  const { session } = await downloading(world, body);
  await session.close(); // closed, and its thread's entry gone, as SessionGone leaves it
  world.state.removeThread(CHANNEL, fileThread);
  await world.appClock.advance(0.2);
  await world.settle();
  assert.deepEqual(world.queries(), []);
  assert.ok(said(world).includes(texts.SESSION_GONE)); // in the prompt's thread: the turn's one push
});

test("a prompt while the daemon stops is refused and words still work", async (t) => {
  const world = worldOf(t);
  await world.sessions.drain(new AbortController().signal); // nothing runs: returns at once
  await world.dispatch(message("list the files"));
  await world.dispatch(message("!stop"));
  assert.deepEqual(world.ephemerals(), [texts.RESTARTING]);
  assert.deepEqual(said(world), [texts.NOTHING_TO_STOP]);
  assert.deepEqual(world.queries(), []);
});

/** A stop under way: the drain runs until the test cuts it short. */
async function draining(world: World): Promise<() => Promise<void>> {
  const cutShort = new AbortController();
  const drain = world.sessions.drain(cutShort.signal);
  await world.idle();
  return async () => {
    cutShort.abort();
    await drain;
  };
}

test("a refusal during a stop names the thread the stop waits for", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("hello", { ts: THREAD })); // never ends: it holds the stop
  const cut = await draining(world);
  await world.dispatch(message("list the files", { ts: OTHER_THREAD }));
  await cut();
  assert.equal(
    world.ephemerals().at(-1),
    [
      texts.RESTARTING,
      texts.RESTART_WAITS_FOR,
      fill(texts.RESTART_WAIT_ROW, {
        channel: CHANNEL,
        link: `<${PERMALINK}|${texts.RESTART_WAIT_SESSION}>`,
        hold: texts.RESTART_HOLD_TURN,
      }),
      texts.RESTART_WAIT_STOP,
    ].join("\n"),
  );
  assert.deepEqual(world.queries(), ["hello"]);
});

test("a channel status during a stop lists what the stop waits for", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("hello", { ts: THREAD })); // never ends: it holds the stop
  const cut = await draining(world);
  await world.dispatch(message("!status"));
  await cut();
  const waits = said(world).at(-1) as string;
  assert.ok(waits.startsWith(`${texts.RESTART_WAITS_HEADER}\n• `));
  assert.ok(waits.includes(texts.RESTART_HOLD_TURN) && waits.endsWith(texts.RESTART_WAIT_STOP));
});

test("a channel status during a stop counts other channels without naming them", async (t) => {
  const world = worldOf(t);
  mkdirSync(join(world.root, "other")); // another folder: no hold between the two sessions
  world.state.bind(OTHER_CHANNEL, join(world.root, "other"));
  await world.dispatch(message("hello", { ts: THREAD }));
  await world.dispatch(message("busy elsewhere", { ts: OTHER_THREAD, channel: OTHER_CHANNEL }));
  assert.deepEqual(world.queries(), ["hello", "busy elsewhere"]);
  const cut = await draining(world);
  await world.dispatch(message("!status"));
  await cut();
  const waits = said(world).at(-1) as string;
  // A post the channel's members read: the other channel's thread is counted, never named.
  assert.ok(!waits.includes(OTHER_CHANNEL));
  assert.equal(waits.split("• ").length - 1, 1);
  assert.ok(waits.includes(fill(texts.RESTART_WAITS_ELSEWHERE, { count: 1 })));
});

test("a thread that only waits for a report gets no stop advice", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("hello", { ts: THREAD }));
  const session = world.sessions.get(CHANNEL, THREAD);
  assert.ok(session !== null);
  world.sessions.restartHolds = () => [[session, texts.RESTART_HOLD_REPORT]];
  world.sessions.draining = true;
  await world.dispatch(message("list the files", { ts: OTHER_THREAD }));
  const refusal = world.ephemerals().at(-1) as string;
  assert.ok(refusal.includes(texts.RESTART_HOLD_REPORT));
  assert.ok(!refusal.includes(texts.RESTART_WAIT_STOP));
  world.sessions.draining = false;
});

test("a long list of waits is cut between rows", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("hello", { ts: THREAD }));
  const session = world.sessions.get(CHANNEL, THREAD);
  assert.ok(session !== null);
  world.sessions.restartHolds = () => Array(11).fill([session, texts.RESTART_HOLD_TURN]);
  world.sessions.draining = true;
  await world.dispatch(message("list the files", { ts: OTHER_THREAD }));
  const lines = (world.ephemerals().at(-1) as string).split("\n");
  assert.equal(lines.filter((line) => line.startsWith("• ")).length, 8);
  assert.deepEqual(lines.slice(-2), [
    fill(texts.RESTART_WAITS_MORE, { count: 3 }),
    texts.RESTART_WAIT_STOP,
  ]);
  world.sessions.draining = false;
});

test("a channel status with no stop under way says nothing of a restart", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("hello", { ts: THREAD }));
  await world.dispatch(message("!status"));
  assert.ok(!said(world).some((text) => text.includes(texts.RESTART_WAITS_FOR)));
});

test("a stop during the downloads sends the prompt nowhere", async (t) => {
  const world = worldOf(t);
  const body = sharedFile("snippet");
  await downloading(world, body);
  await world.sessions.drain(new AbortController().signal);
  await world.appClock.advance(0.2);
  await world.settle();
  assert.deepEqual(world.queries(), []);
  assert.deepEqual(world.clients, []);
  assert.equal(world.ephemerals().at(-1), texts.RESTARTING);
});

for (const how of ["typed", "clicked"]) {
  test(`a bind to an untrusted folder says no session can start yet [${how}]`, async (t) => {
    const world = worldOf(t);
    const docs = join(world.root, "docs");
    mkdirSync(docs);
    world.backend.trusted = async (directory) => directory !== docs;
    if (how === "typed") await world.dispatch(message("!bind docs"));
    else await world.dispatch(click("folder_bind", "docs"));
    const reason = fill(texts.DIRECTORY_UNTRUSTED, { directory: docs });
    assert.equal(said(world).at(-1), fill(texts.BIND_UNAVAILABLE, { directory: docs, reason }));
    assert.equal(world.state.channel(CHANNEL)?.directory, docs);
    await world.sessions.closeAll();
  });
}

test("bind names the old folder a thread keeps", async (t) => {
  const world = worldOf(t);
  // The thread stays in `app`, the folder it was created in; the bind answer names it.
  await idleMessage(world, "hi", THREAD);
  mkdirSync(join(world.root, "docs"));
  await world.dispatch(message("!bind docs"));
  const old = join(world.root, "app");
  const fresh = join(world.root, "docs");
  assert.equal(
    said(world).at(-1),
    fill(texts.BIND_OK_ELSEWHERE, { directory: fresh, old: `\`${old}\`` }),
  );
  await world.sessions.closeAll();
});

test("bind names every old folder still in use", async (t) => {
  const world = worldOf(t);
  await idleMessage(world, "hi", THREAD); // a thread in `app`
  mkdirSync(join(world.root, "docs"));
  await world.dispatch(message("!bind docs"));
  await idleMessage(world, "hi", OTHER_THREAD); // a thread in `docs`
  mkdirSync(join(world.root, "notes"));
  await world.dispatch(message("!bind notes"));
  const app = join(world.root, "app");
  const docs = join(world.root, "docs");
  const notes = join(world.root, "notes");
  const old = [app, docs]
    .sort()
    .map((folder) => `\`${folder}\``)
    .join(", ");
  assert.equal(said(world).at(-1), fill(texts.BIND_OK_ELSEWHERE, { directory: notes, old }));
  await world.sessions.closeAll();
});

test("bind stays plain with no thread in another folder", async (t) => {
  const world = worldOf(t);
  mkdirSync(join(world.root, "docs"));
  await world.dispatch(message("!bind docs"));
  assert.deepEqual(said(world), [fill(texts.BIND_OK, { directory: join(world.root, "docs") })]);
});

test("a reply in an old folder thread gets the notice on every prompt", async (t) => {
  const world = worldOf(t);
  await idleMessage(world, "hi", THREAD); // opens a session in `app`
  mkdirSync(join(world.root, "docs"));
  await world.dispatch(message("!bind docs"));
  const expected = fill(texts.OLD_THREAD_FOLDER, {
    old: join(world.root, "app"),
    new: join(world.root, "docs"),
  });
  const count = () => world.ephemerals().filter((text) => text === expected).length;
  await world.dispatch(reply("go on", THREAD));
  assert.equal(count(), 1);
  assert.ok(!said(world).includes(expected)); // for the owner alone: no push
  await world.dispatch(reply("again", THREAD));
  // An ephemeral vanishes on reload, so "once" could mean never: every prompt shows it.
  assert.equal(count(), 2);
  await world.sessions.closeAll();
});

test("a failed old folder notice still submits the prompt", async (t) => {
  const world = worldOf(t);
  await idleMessage(world, "hi", THREAD); // opens a session in `app`
  mkdirSync(join(world.root, "docs"));
  await world.dispatch(message("!bind docs"));
  const expected = fill(texts.OLD_THREAD_FOLDER, {
    old: join(world.root, "app"),
    new: join(world.root, "docs"),
  });
  world.slack.responses["chat.postEphemeral"] = [
    named("RuntimeError", "network down"),
    { ok: true },
  ];
  await world.dispatch(reply("go on", THREAD));
  assert.equal(world.clients.at(-1)?.queries.at(-1), "go on"); // never dropped, despite the failed notice
  await world.dispatch(reply("again", THREAD));
  // The next reply shows it again (and this one succeeds), so its text was sent twice.
  assert.equal(world.ephemerals().filter((text) => text === expected).length, 2);
  await world.sessions.closeAll();
});

test("a thread in the current folder never gets the notice", async (t) => {
  const world = worldOf(t);
  await world.dispatch(message("hi", { ts: THREAD })); // opens a session in `app`, still current
  await world.dispatch(reply("go on", THREAD));
  assert.ok(
    !world.ephemerals().some((text) => text.includes("Claude Code resumes a session only there")),
  );
  await world.sessions.closeAll();
});

test("long answers fit slack s limit", () => {
  const questions: Question[] = Array.from({ length: 4 }, () => ({
    text: "q".repeat(900),
    header: "",
    multiSelect: false,
    options: [],
  }));
  const answers = { ["q".repeat(900)]: "a".repeat(300) };
  const blocks = answeredBlocks(questions, answers) as Body[];
  assert.equal(blocks.length, 1);
  assert.ok(Array.from(blocks[0]?.elements[0].text as string).length <= SECTION_LIMIT);
});
