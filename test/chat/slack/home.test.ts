/**
 * Port of `tests/test_home.py`. How the Python tests that waited on the wall clock or reached
 * into `Home` were written here:
 * - the debounce, the retry and the close timeout run on a `FakeClock`, advanced by the test;
 *   `Home` reads the page's "now" from it too, so it starts at `EPOCH`;
 * - a Slack call held open is `FakeSlack.gate`, not a replaced client method;
 * - the state's observer is wired to `Home.request`, which marks the threads stale as
 *   `told_by_state` did through `_stale`; the debounced publish it schedules never fires, since
 *   those tests never advance the clock;
 * - the machine's time zone is `process.env.TZ`, which Node reads again on every change.
 */
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { type TestContext, test } from "node:test";
import type { ListedSession } from "../../../src/agent/seam.ts";
import {
  ALL,
  CHANNEL_ACTION,
  CLEAN_ACTION,
  CLOSE_SECONDS,
  CONFIRM_TEXT,
  DATE_ACTION,
  DELETE_ACTION,
  EDIT_ACTION,
  EDIT_OFF,
  EDIT_ON,
  FILTERS_BLOCK,
  HOME_BLOCKS,
  Home,
  type HomeFilter,
  type HomeOptions,
  type HomeRow,
  type HomeViewOptions,
  homeFilter,
  homeView,
  LAST_7,
  LAST_30,
  LAST_48,
  logger,
  NEW_THREAD_ACTION,
  PER_CHANNEL,
  RETRY_SECONDS,
  readFilter,
  SEARCH_ACTION,
  SEARCH_BLOCK,
  SHOW_ALL_ACTION,
  SPACER,
  STATUS_ACTION,
  TODAY,
  threadFacts,
  YESTERDAY,
} from "../../../src/chat/slack/home.ts";
import { Status } from "../../../src/chat/slack/reply/status.ts";
import { StateStore } from "../../../src/core/state.ts";
import * as texts from "../../../src/core/texts.ts";
import {
  AsyncEvent,
  FakeClock,
  FakeSlack,
  rejected,
  type Scripted,
} from "../../support/fake-slack.ts";
import { slackPayload, type Json as Wire } from "../../support/fixtures.ts";

// Today and Yesterday are days of the machine's time zone: the same on every machine here.
process.env.TZ = "UTC";

// A Slack block read in a test, whatever its type.
// biome-ignore lint/suspicious/noExplicitAny: blocks are read by path
type Json = Record<string, any>;

const OWNER = "U000ALICE";
const TEAM = "T000TEAM";
const CHANNEL = "C000CHAN";
const OTHER_CHANNEL = "C000OTHR";
const EMPTY_CHANNEL = "C000NONE";
const CHANNELS: ReadonlyMap<string, string> = new Map([
  [CHANNEL, "cc-articles"],
  [OTHER_CHANNEL, "cc-shop"],
  [EMPTY_CHANNEL, "cc-tools"],
]);
const NOW = new Date(Date.UTC(2026, 8, 21, 14, 13));
const EPOCH = NOW.getTime() / 1000;
const LINK = "https://example.slack.com/archives/C000CHAN/p1780000000000001";

function row(title = "Refactor the feed parser", fields: Partial<HomeRow> = {}): HomeRow {
  return {
    channelId: CHANNEL,
    threadTs: "1789990000.000100",
    title,
    status: Status.WORKING,
    replies: 3,
    lastActivity: EPOCH - 120,
    permalink: LINK,
    ...fields,
  };
}

type ViewExtra = Partial<HomeViewOptions> & { channels?: ReadonlyMap<string, string> };

function view(rows: HomeRow[], chosen: HomeFilter = homeFilter(), extra: ViewExtra = {}): Json {
  const { channels = CHANNELS, ...options } = extra;
  return homeView(rows, channels, {
    teamId: TEAM,
    chosen,
    now: NOW,
    ...options,
  }) as unknown as Json;
}

function named(channels: Record<string, string>): ReadonlyMap<string, string> {
  return new Map(Object.entries(channels));
}

/** The sessions' title rows: the sections that carry no button. */
function cards(page: Json): Json[] {
  return page.blocks.filter((b: Json) => b.type === "section" && !("accessory" in b));
}

function titles(page: Json): string[] {
  return cards(page).map((c) => c.text.text);
}

function headers(page: Json): string[] {
  return page.blocks
    .filter((b: Json) => b.type === "section" && "accessory" in b)
    .map((b: Json) => b.text.text);
}

/** The small lines of the page, the blank rows between two sessions left out. */
function notes(page: Json): string[] {
  const found: string[] = page.blocks
    .filter((b: Json) => b.type === "context")
    .map((b: Json) => b.elements[0].text);
  return found.filter((text) => text !== SPACER);
}

const OPEN = `<${LINK}|${texts.HOME_OPEN}>`;

function ago(epoch: number, fallback: string): string {
  return `<!date^${epoch}^{ago}|${fallback}>`;
}

test("a channel is a header with a new thread link and a card per session", () => {
  const page = view([row()], homeFilter(), { channels: named({ [CHANNEL]: "cc-articles" }) });
  assert.equal(page.type, "home");
  // The view shape and the 100 block cap: docs.slack.dev/surfaces/app-home, read 2026-10-01.
  const [controls, search, written, divider, header, card, details] = page.blocks;
  assert.deepEqual([controls.type, search.type], ["actions", "input"]);
  assert.equal(
    written.elements[0].text,
    texts.fill(texts.HOME_HEADER, { time: `<!date^${EPOCH}^{time}|2026-09-21 14:13 UTC>` }),
  );
  assert.deepEqual(divider, { type: "divider" });
  // The documented deep link to a channel (docs.slack.dev/interactivity/deep-linking).
  assert.deepEqual(header, {
    type: "section",
    text: { type: "mrkdwn", text: `*<#${CHANNEL}>*` },
    accessory: {
      type: "button",
      action_id: NEW_THREAD_ACTION,
      text: { type: "plain_text", text: texts.HOME_NEW_THREAD },
      url: `slack://channel?team=${TEAM}&id=${CHANNEL}`,
    },
  });
  assert.deepEqual(card, {
    type: "section",
    text: { type: "mrkdwn", text: ":hourglass_flowing_sand:  *Refactor the feed parser*" },
  });
  // Slack's own relative date (formatting-message-text, read 2026-10-01): it stays right while
  // the page sits unpublished.
  // Open is a link in the small line under the title, not a button beside it.
  assert.equal(
    details.elements[0].text,
    `working · 3 replies · last reply ${ago(EPOCH - 120, "2026-09-21 14:11 UTC")} · <${LINK}|Open>`,
  );
});

function many(count: number, fields: Partial<HomeRow> = {}): HomeRow[] {
  return Array.from({ length: count }, (_, i) =>
    row(`s${i}`, {
      threadTs: `17899${String(i).padStart(5, "0")}.000100`,
      lastActivity: EPOCH - 60 * i,
      ...fields,
    }),
  );
}

test("a blank row separates two sessions of a channel", () => {
  const page = view(many(3), homeFilter(), { channels: named({ [CHANNEL]: "cc-articles" }) });
  const kinds = page.blocks
    .slice(3)
    .map((b: Json) => (b.type === "context" && b.elements[0].text === SPACER ? "blank" : b.type));
  // Under the channel's header: title and details, then a blank row before each next one.
  assert.deepEqual(kinds, [
    "divider",
    "section",
    "section",
    "context",
    "blank",
    "section",
    "context",
    "blank",
    "section",
    "context",
  ]);
});

for (const [status, word, name] of [
  [Status.WORKING, texts.HOME_WORKING, "working"],
  [Status.WAITING, texts.HOME_WAITING, "waiting"],
  [Status.DONE, texts.HOME_ENDED, "done"],
  [Status.ERROR, texts.HOME_ERROR, "error"],
] as const) {
  test(`each reaction has its word [${name}]`, () => {
    const page = view([row("Refactor the feed parser", { status })]);
    assert.deepEqual(titles(page), [`:${status}:  *Refactor the feed parser*`]);
    assert.ok(
      notes(page).some((note) => note.startsWith(`${word} · 3 replies · last reply <!date^`)),
    );
  });
}

test("a card with no reaction shows the title and the age alone", () => {
  const page = view([row("Refactor the feed parser", { status: null })], homeFilter(), {
    channels: named({ [CHANNEL]: "cc-articles" }),
  });
  assert.deepEqual(titles(page), ["*Refactor the feed parser*"]);
  const when = ago(EPOCH - 120, "2026-09-21 14:11 UTC");
  assert.equal(notes(page).at(-1), `3 replies · last reply ${when} · ${OPEN}`);
});

test("a card counts its replies and says started when it has none", () => {
  const when = ago(EPOCH - 120, "2026-09-21 14:11 UTC");
  const channels = named({ [CHANNEL]: "cc-articles" });
  const one = view([row("Refactor the feed parser", { status: null, replies: 1 })], homeFilter(), {
    channels,
  });
  assert.equal(notes(one).at(-1), `1 reply · last reply ${when} · ${OPEN}`);
  const none = view([row("Refactor the feed parser", { status: null, replies: 0 })], homeFilter(), {
    channels,
  });
  assert.equal(notes(none).at(-1), `started ${when} · ${OPEN}`);
});

test("a title is shown as written on one line", () => {
  // Model-written text: unescaped, `<!channel>` would read as a mention.
  const [title] = titles(view([row("ping <!channel> & `more`\nsecond line")])) as [string];
  assert.ok(!title.includes("<!channel>") && title.includes("&lt;!channel&gt; &amp;"));
  assert.ok(!title.includes("\n"));
});

test("channels with sessions come first by their newest then the empty ones", () => {
  const rows = [
    row("newest, in the shop", { channelId: OTHER_CHANNEL, lastActivity: EPOCH - 60 }),
    row("older, in articles", { lastActivity: EPOCH - 600 }),
  ];
  const page = view(rows);
  assert.deepEqual(headers(page), [
    `*<#${OTHER_CHANNEL}>*`,
    `*<#${CHANNEL}>*`,
    `*<#${EMPTY_CHANNEL}>*`,
  ]);
  assert.equal(notes(page).at(-1), texts.HOME_NO_SESSIONS);
});

test("a channel shows its newest five and a button for all of them", () => {
  const page = view(many(8), homeFilter(), { channels: named({ [CHANNEL]: "cc-articles" }) });
  assert.deepEqual(
    titles(page),
    Array.from({ length: PER_CHANNEL }, (_, i) => `:hourglass_flowing_sand:  *s${i}*`),
  );
  assert.deepEqual(page.blocks.at(-1), {
    type: "actions",
    elements: [
      {
        type: "button",
        action_id: SHOW_ALL_ACTION,
        text: { type: "plain_text", text: texts.fill(texts.HOME_SHOW_ALL, { count: 8 }) },
        value: CHANNEL,
      },
    ],
  });
});

test("choosing a channel shows all of it and nothing else", () => {
  const rows = [...many(8), row("in the shop", { channelId: OTHER_CHANNEL })];
  const page = view(rows, homeFilter({ channel: CHANNEL }));
  assert.deepEqual(headers(page), [`*<#${CHANNEL}>*`]);
  assert.equal(cards(page).length, 8);
  // No Show all.
  assert.ok(page.blocks.every((b: Json) => b.type !== "actions" || b.block_id));
});

test("a chosen channel with no session says so", () => {
  const page = view([row()], homeFilter({ channel: EMPTY_CHANNEL }));
  assert.deepEqual(headers(page), [`*<#${EMPTY_CHANNEL}>*`]);
  assert.equal(notes(page).at(-1), texts.HOME_NO_SESSIONS);
});

test("a status filter keeps the sessions in that status whatever their number", () => {
  const rows = [...many(7, { status: Status.WAITING }), row("done", { status: Status.DONE })];
  const page = view(rows, homeFilter({ status: Status.WAITING }));
  assert.equal(cards(page).length, 7); // no five-per-channel cut under a filter
  assert.deepEqual(headers(page), [`*<#${CHANNEL}>*`]); // a channel with no match is not listed
});

test("the search matches a part of the title whatever the case", () => {
  const rows = [row("Fix the Footer on long replies"), row("Bump the SDK pin")];
  assert.deepEqual(titles(view(rows, homeFilter({ search: "footer" }))), [
    ":hourglass_flowing_sand:  *Fix the Footer on long replies*",
  ]);
});

const HOUR = 3600 * 1000;
const DAY = 24 * HOUR;
const AGES: [string, number][] = [
  ["an hour ago", HOUR],
  ["yesterday evening", 18 * HOUR], // 20:13 the day before
  ["five days ago", 5 * DAY],
  ["three weeks ago", 21 * DAY],
  ["old", 45 * DAY],
];
for (const [date, expected] of [
  [LAST_48, ["an hour ago", "yesterday evening"]],
  [TODAY, ["an hour ago"]],
  [YESTERDAY, ["yesterday evening"]], // that day alone
  [LAST_7, ["an hour ago", "yesterday evening", "five days ago"]],
  [LAST_30, ["an hour ago", "yesterday evening", "five days ago", "three weeks ago"]],
  [null, ["an hour ago", "yesterday evening", "five days ago", "three weeks ago", "old"]],
] as const) {
  test(`the date filter reads the sessions last message [${date}]`, () => {
    const rows = AGES.map(([title, age]) =>
      row(title, { lastActivity: (NOW.getTime() - age) / 1000, status: null }),
    );
    assert.deepEqual(
      titles(view(rows, homeFilter({ date }))),
      expected.map((title) => `*${title}*`),
    );
  });
}

test("the page starts on the last 48 hours and keeps its shape under a period", () => {
  assert.equal(homeFilter().date, LAST_48);
  const old = row("three days ago", {
    lastActivity: EPOCH - 3 * 86400,
    channelId: OTHER_CHANNEL,
  });
  const page = view([...many(8), old]);
  assert.equal(cards(page).length, PER_CHANNEL); // a period alone still cuts a channel to its newest
  // Every channel keeps its group and its New thread button; one says its sessions are older.
  assert.deepEqual(headers(page), [
    `*<#${CHANNEL}>*`,
    `*<#${OTHER_CHANNEL}>*`,
    `*<#${EMPTY_CHANNEL}>*`,
  ]);
  assert.deepEqual(notes(page).slice(-2), [texts.HOME_NO_MATCH, texts.HOME_NO_SESSIONS]);
});

test("a day a clock change makes longer is still one day", (t: TestContext) => {
  // Europe/Rome, 2026-10-25: summer time ends at 03:00. A reply at 00:30 that morning, read at
  // noon with noon's offset, would fall on the day before.
  process.env.TZ = "Europe/Rome";
  t.after(() => {
    process.env.TZ = "UTC";
  });
  const noon = new Date(Date.UTC(2026, 9, 25, 11, 0)); // 12:00 in Rome, winter time
  const early = row("just after midnight", {
    lastActivity: Date.UTC(2026, 9, 24, 22, 30) / 1000,
  });
  const options = { teamId: TEAM, now: noon };
  let page = homeView([early], CHANNELS, { ...options, chosen: homeFilter({ date: TODAY }) });
  assert.deepEqual(titles(page as unknown as Json), [
    ":hourglass_flowing_sand:  *just after midnight*",
  ]);
  page = homeView([early], CHANNELS, { ...options, chosen: homeFilter({ date: YESTERDAY }) });
  assert.deepEqual(titles(page as unknown as Json), []);
});

test("filters add up and no match says so", () => {
  const rows = [row("Fix the footer", { status: Status.DONE }), row("Fix the header")];
  const both = homeFilter({ status: Status.DONE, search: "fix" });
  assert.deepEqual(titles(view(rows, both)), [":white_check_mark:  *Fix the footer*"]);
  const nothing = view(rows, homeFilter({ status: Status.ERROR, search: "fix" }));
  assert.ok(cards(nothing).length === 0 && notes(nothing).at(-1) === texts.HOME_NO_MATCH);
});

test("no bound channel says how to bind one", () => {
  const page = view([], homeFilter(), { channels: new Map() });
  assert.equal(notes(page).at(-1), texts.HOME_EMPTY);
});

test("the page stops within slacks blocks and says how many it shows", () => {
  const page = view(many(60), homeFilter({ channel: CHANNEL }));
  assert.ok(page.blocks.length <= HOME_BLOCKS);
  const shown = cards(page).length;
  assert.ok(25 < shown && shown < 40);
  assert.equal(notes(page).at(-1), texts.fill(texts.HOME_MORE, { rows: shown }));
});

test("many channels stop within slacks blocks too", () => {
  const channels = new Map(
    Array.from({ length: 12 }, (_, i): [string, string] => [
      `C${String(i).padStart(8, "0")}`,
      `project-${i}`,
    ]),
  );
  const rows = [...channels.keys()].flatMap((c, n) =>
    Array.from({ length: 6 }, (_, i) =>
      row(`s${c}-${i}`, {
        channelId: c,
        threadTs: `1789${i}.${n}`,
        lastActivity: EPOCH - n * 60 - i,
      }),
    ),
  );
  const page = view(rows, homeFilter(), { channels });
  assert.ok(page.blocks.length <= HOME_BLOCKS);
  assert.equal(notes(page).at(-1), texts.fill(texts.HOME_MORE, { rows: cards(page).length }));
});

test("empty channels falling off the end are not called hidden sessions", () => {
  const channels = new Map<string, string>([
    [CHANNEL, "cc-articles"],
    ...Array.from({ length: 40 }, (_, i): [string, string] => [
      `C${String(i).padStart(8, "0")}`,
      `empty-${i}`,
    ]),
  ]);
  const page = view([row()], homeFilter(), { channels });
  assert.ok(page.blocks.length <= HOME_BLOCKS);
  assert.equal(cards(page).length, 1);
  assert.ok(!notes(page).includes(texts.fill(texts.HOME_MORE, { rows: 1 })));
});

function control(page: Json, actionId: string): Json {
  const found = page.blocks.filter((b: Json) => String(b.block_id ?? "").startsWith(FILTERS_BLOCK));
  assert.equal(found.length, 1);
  return found[0].elements.find((e: Json) => e.action_id === actionId);
}

test("the channel menu stops at slacks hundred options", () => {
  // A select menu holds 100 options (select menu reference): one more and Slack refuses the page.
  const channels = new Map(
    Array.from({ length: 120 }, (_, i): [string, string] => [
      `C${String(i).padStart(8, "0")}`,
      `project-${i}`,
    ]),
  );
  assert.equal(control(view([], homeFilter(), { channels }), CHANNEL_ACTION).options.length, 100);
});

test("the controls start on all and show what is chosen", () => {
  let page = view([row()]);
  for (const actionId of [CHANNEL_ACTION, STATUS_ACTION]) {
    assert.equal(control(page, actionId).initial_option.value, ALL);
  }
  assert.equal(control(page, DATE_ACTION).initial_option.value, LAST_48);
  assert.deepEqual(control(view([row()], homeFilter({ date: null })), DATE_ACTION).initial_option, {
    text: { type: "plain_text", text: texts.HOME_ANY_TIME },
    value: ALL,
  });
  const optionTexts = (actionId: string): string[] =>
    control(page, actionId).options.map((o: Json) => o.text.text);
  assert.deepEqual(optionTexts(CHANNEL_ACTION), [
    texts.HOME_ALL_CHANNELS,
    "cc-articles",
    "cc-shop",
    "cc-tools",
  ]);
  assert.deepEqual(optionTexts(STATUS_ACTION), [
    texts.HOME_ALL_STATUSES,
    "Waiting for you",
    "Working",
    "Ended",
    "Error",
  ]);
  assert.deepEqual(
    control(page, DATE_ACTION).options.map((o: Json) => o.value),
    [LAST_48, TODAY, YESTERDAY, LAST_7, LAST_30, ALL],
  );
  const searchBlock = (): Json => {
    const found = page.blocks.filter((b: Json) =>
      String(b.block_id ?? "").startsWith(SEARCH_BLOCK),
    );
    assert.equal(found.length, 1);
    return found[0];
  };
  assert.ok(searchBlock().dispatch_action === true && !("initial_value" in searchBlock().element));

  const chosen = homeFilter({
    channel: OTHER_CHANNEL,
    status: Status.ERROR,
    date: LAST_7,
    search: "x",
  });
  page = view([row()], chosen);
  assert.equal(control(page, CHANNEL_ACTION).initial_option.value, OTHER_CHANNEL);
  assert.equal(control(page, STATUS_ACTION).initial_option.value, Status.ERROR);
  assert.equal(control(page, DATE_ACTION).initial_option.value, LAST_7);
  assert.equal(searchBlock().element.initial_value, "x");
});

test("the controls blocks change their id with the choice", () => {
  // Slack keeps what a control shows while its block keeps its id (seen 2026-10-01: menus
  // still on the choices of the run before a restart): the id follows the choice.
  const ids = (chosen: HomeFilter): string[] =>
    view([row()], chosen)
      .blocks.filter((b: Json) => "block_id" in b)
      .map((b: Json) => b.block_id);

  assert.deepEqual(ids(homeFilter()), ids(homeFilter()));
  const disjoint = (a: string[], b: string[]): boolean => a.every((id) => !b.includes(id));
  assert.ok(disjoint(ids(homeFilter()), ids(homeFilter({ channel: CHANNEL }))));
  assert.ok(disjoint(ids(homeFilter({ search: "a" })), ids(homeFilter({ search: "b" }))));
});

// The mark is the first 8 hex digits of the SHA-256 of Python's `repr` of the filter, so a page
// built for the same choice keeps the id it had under the Python daemon. The expected values were
// computed with `hashlib.sha256(repr(HomeFilter(...)).encode()).hexdigest()[:8]` on 2026-10-10.
test("the controls block id is the one the Python daemon wrote for the same choice", () => {
  const ids = (chosen: HomeFilter): string[] =>
    view([row()], chosen)
      .blocks.filter((b: Json) => "block_id" in b)
      .map((b: Json) => b.block_id);
  const cases: [HomeFilter, string][] = [
    [homeFilter(), "fef96678"],
    [homeFilter({ channel: CHANNEL, search: 'it\'s "a" \\ é\n\u200b' }), "f6b5c36b"],
    [homeFilter({ search: "it's", date: null }), "723d1034"],
    [homeFilter({ search: "\x00\x7f\u{1F600}x\xa0\u{e0001}" }), "09f19add"],
  ];
  for (const [chosen, mark] of cases) {
    assert.deepEqual(ids(chosen), [`home_filters:${mark}`, `home_search:${mark}`]);
  }
});

function option(value: string): Json {
  // A static_select's state, as tests/fixtures/slack/001-block_actions.json records its action.
  return { type: "static_select", selected_option: { value } };
}

test("a filter is read from the state of every control", () => {
  // Read by action id: the blocks' ids change with the choice.
  const values = {
    [`${FILTERS_BLOCK}:0a1b2c3d`]: {
      [CHANNEL_ACTION]: option(CHANNEL),
      [STATUS_ACTION]: option(Status.WAITING),
      [DATE_ACTION]: option(YESTERDAY),
    },
    [`${SEARCH_BLOCK}:0a1b2c3d`]: {
      [SEARCH_ACTION]: { type: "plain_text_input", value: "  Fix\nthe footer " },
    },
  };
  assert.deepEqual(
    readFilter(values, homeFilter()),
    homeFilter({
      channel: CHANNEL,
      status: Status.WAITING,
      date: YESTERDAY,
      search: "Fix the footer",
    }),
  );
});

test("all clears a filter and an emptied search clears it", () => {
  const current = homeFilter({ channel: CHANNEL, status: Status.DONE, date: TODAY, search: "x" });
  const values = {
    [FILTERS_BLOCK]: Object.fromEntries(
      [CHANNEL_ACTION, STATUS_ACTION, DATE_ACTION].map((a) => [a, option(ALL)]),
    ),
    [SEARCH_BLOCK]: { [SEARCH_ACTION]: { type: "plain_text_input", value: null } },
  };
  assert.deepEqual(readFilter(values, current), homeFilter({ date: null }));
});

test("a value the page never offered and a missing control keep what was chosen", () => {
  const current = homeFilter({ status: Status.DONE, date: TODAY, search: "x" });
  const values = {
    [FILTERS_BLOCK]: { [STATUS_ACTION]: option("tada"), [DATE_ACTION]: option("365") },
  };
  assert.deepEqual(readFilter(values, current), current);
  assert.deepEqual(readFilter({}, current), current);
});

for (const [name, values] of [
  ["list", ["not", "a", "mapping"]],
  ["block", { [FILTERS_BLOCK]: "not a block" }],
  ["string option", { [FILTERS_BLOCK]: { [STATUS_ACTION]: { selected_option: "raised_hand" } } }],
  ["list option", { [FILTERS_BLOCK]: { [STATUS_ACTION]: { selected_option: ["raised_hand"] } } }],
  ["control", { [SEARCH_BLOCK]: { [SEARCH_ACTION]: "not a control" } }],
] as const) {
  test(`a shape slack does not send changes nothing and never raises [${name}]`, () => {
    const current = homeFilter({ status: Status.DONE, search: "x" });
    assert.deepEqual(readFilter(values, current), current);
  });
}

// --- the publisher ---

const OLD = "68da9311-0000-4000-8000-00000000000a";
const NEW = "68da9311-0000-4000-8000-00000000000b";
const OLD_THREAD = "1789000000.000100";
const NEW_THREAD = "1789000500.000100";
const EMPTY_THREAD = "1789000900.0001";

/** An error whose class name is what the log line carries (Python: `OSError`). */
class OSError extends Error {}

function info(id: string, title: string): ListedSession {
  return { id, title, customTitle: null, branch: null, size: null, lastModified: 1 };
}

/**
 * A thread's root message as `conversations.replies` returns it (measured on a real workspace,
 * 2026-10-01; kept, scrubbed, as api-conversations-replies-root.json), changed by `fields`; a
 * field set to null is left out, as Slack leaves it out of a root with no reply or no reaction.
 */
function root(ts: string, fields: Record<string, Wire> = {}): Json {
  const recorded = (slackPayload("api-conversations-replies-root").messages as Wire[])[0] as Json;
  const message: Json = { ...recorded, ts, thread_ts: ts, ...fields };
  return Object.fromEntries(Object.entries(message).filter(([, value]) => value !== null));
}

function reacted(name: string): Wire {
  return [{ name, users: ["U000BOT"], count: 1 }];
}

type Roots = Record<string, Json | Error>;

/** What Slack answers about each thread, by its root's ts: a root, or an error. */
function inSlack(slack: FakeSlack, roots: Roots): void {
  slack.responses["conversations.replies"] = (args) => {
    const found = roots[String(args.ts)];
    if (found === undefined) throw new Error(`no root ${String(args.ts)}`);
    if (found instanceof Error) return found;
    return { ok: true, messages: [found], has_more: false };
  };
}

interface World {
  directory: string;
  slack: FakeSlack;
  state: StateStore;
  roots: Roots;
  clock: FakeClock;
}

function world(t: TestContext): World {
  const directory = mkdtempSync(join(tmpdir(), "awd-home-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const slack = new FakeSlack();
  const roots: Roots = {
    [OLD_THREAD]: root(OLD_THREAD, { reply_count: 19, latest_reply: `${EPOCH - 3600}.000200` }),
    [NEW_THREAD]: root(NEW_THREAD, { reply_count: 1, latest_reply: `${EPOCH - 600}.000200` }),
  };
  const state = new StateStore(join(directory, "state.json"));
  state.bind(CHANNEL, join(directory, "project"));
  state.bind(OTHER_CHANNEL, join(directory, "other"));
  state.openThread(CHANNEL, OLD_THREAD, OLD);
  state.openThread(OTHER_CHANNEL, NEW_THREAD, NEW);
  state.openThread(CHANNEL, EMPTY_THREAD); // its setup is still open: no session yet
  state.setStatusPending(CHANNEL, OLD_THREAD, null, Status.DONE);
  state.setStatusPending(OTHER_CHANNEL, NEW_THREAD, Status.WAITING);
  inSlack(slack, roots);
  const clock = new FakeClock();
  clock.now = EPOCH;
  return { directory, slack, state, roots, clock };
}

type Listing = ReadonlyMap<string, ListedSession[]>;

function listing(w: World): Listing {
  return new Map([
    [
      join(w.directory, "project"),
      [
        info(OLD, "Fix the footer"),
        info("68da9311-0000-4000-8000-00000000000c", "A terminal session, in no thread"),
      ],
    ],
    [join(w.directory, "other"), [info(NEW, "Add retry to the uploader")]],
  ]);
}

function makeHome(w: World, sessions: Listing, extra: Partial<HomeOptions> = {}): Home {
  return new Home(w.slack, {
    ownerUserId: OWNER,
    teamId: TEAM,
    state: w.state,
    sessionsOf: (directory) => {
      const found = sessions.get(directory);
      if (found === undefined) throw new Error(`no listing for ${directory}`);
      return found;
    },
    debounce: 0.01,
    clock: w.clock,
    ...extra,
  });
}

/**
 * Wire the state's observer to the page as `run` does: the threads a write touched are read
 * again at the next publish. The debounced publish it asks for never fires here, since the
 * test publishes itself and never advances the clock. Returns what the state announced.
 */
function toldByState(home: Home, state: StateStore): (readonly (readonly [string, string])[])[] {
  const heard: (readonly (readonly [string, string])[])[] = [];
  state.onSessionsChange = (changed) => {
    heard.push(changed);
    home.request(changed);
  };
  return heard;
}

function published(slack: FakeSlack): Json[] {
  return slack.callsTo("views.publish").map((args) => args.view as Json);
}

/** Lets what a woken task does run to its next wait. */
async function settle(clock: FakeClock): Promise<void> {
  await clock.advance(0);
}

/** The warnings the module's logger got, as one text. */
function warnings(t: TestContext): () => string {
  const warned = t.mock.method(logger, "warning", () => {});
  return () => warned.mock.calls.map((call) => String(call.arguments[0])).join("\n");
}

test("a root is read as slack returns it", () => {
  const recorded = (slackPayload("api-conversations-replies-root").messages as Wire[])[0] as Json;
  assert.deepEqual(threadFacts(recorded), {
    replies: 19,
    latestReply: 1789996400,
    latestTs: "1789996400.000200",
    reaction: Status.DONE,
  });
  // A root nobody replied to and nobody reacted to carries neither field.
  const bare = root(OLD_THREAD, { reply_count: null, latest_reply: null, reactions: null });
  assert.deepEqual(threadFacts(bare), {
    replies: 0,
    latestReply: null,
    latestTs: null,
    reaction: null,
  });
  // The owner's own reactions are not a status.
  assert.equal(threadFacts(root(OLD_THREAD, { reactions: reacted("eyes") })).reaction, null);
});

test("publish lists every held session by channel last reply first", async (t) => {
  const w = world(t);
  await makeHome(w, listing(w)).publish();

  const calls = w.slack.callsTo("views.publish");
  assert.equal(calls.length, 1);
  const [call] = calls as [Json];
  assert.equal(call.user_id, OWNER); // the owner's Home, whoever opens the app
  const page = call.view as Json;
  assert.deepEqual(headers(page), [`*<#${OTHER_CHANNEL}>*`, `*<#${CHANNEL}>*`]);
  assert.deepEqual(titles(page), [
    ":raised_hand:  *Add retry to the uploader*",
    ":white_check_mark:  *Fix the footer*",
  ]);
  // The replies and the last reply are the thread's own, as Slack shows them in the channel.
  assert.ok(
    (notes(page)[1] as string).includes(
      `waiting for you · 1 reply · last reply <!date^${EPOCH - 600}^{ago}|`,
    ),
  );
  assert.ok(
    (notes(page)[2] as string).includes(
      `ended · 19 replies · last reply <!date^${EPOCH - 3600}^{ago}|`,
    ),
  );
  assert.ok(
    notes(page)
      .slice(1)
      .every((note) => note.endsWith(` · ${OPEN}`)),
  );
  // The root alone is asked for: its own ts, one message.
  const asked = w.slack.callsTo("conversations.replies");
  assert.deepEqual(
    asked.map((a) => [a.ts, a.limit]),
    [
      [OLD_THREAD, 1],
      [NEW_THREAD, 1],
    ],
  );
  // The channel menu names the channels as Slack does (conversations.info).
  const name = (slackPayload("api-conversations-info").channel as Json).name;
  assert.deepEqual(
    control(page, CHANNEL_ACTION)
      .options.map((o: Json) => o.text.text)
      .slice(1),
    [name, name],
  );
});

test("a thread is read again only once its session moved", async (t) => {
  const w = world(t);
  const home = makeHome(w, listing(w));
  toldByState(home, w.state);
  await home.publish();
  await home.publish();
  assert.equal(w.slack.callsTo("conversations.replies").length, 2); // one per thread, not per publish
  // A turn starts in one thread: that thread alone is read again.
  w.roots[OLD_THREAD] = root(OLD_THREAD, { reply_count: 20, latest_reply: `${EPOCH - 5}.000300` });
  w.state.setStatusPending(CHANNEL, OLD_THREAD, Status.WORKING);
  await home.publish();
  assert.deepEqual(
    w.slack
      .callsTo("conversations.replies")
      .map((a) => a.ts)
      .slice(2),
    [OLD_THREAD],
  );
  const page = published(w.slack).at(-1) as Json;
  assert.equal(titles(page)[0], ":hourglass_flowing_sand:  *Fix the footer*"); // now the last reply
  assert.ok((notes(page)[1] as string).startsWith("working · 20 replies · last reply "));
});

test("a turn that starts and ends between two pages is still read", async (t) => {
  // The thread ends as it started (✅, a turn, ✅ again) before the page is rebuilt: its state
  // reads the same, and the write that touched it is what says it moved.
  const w = world(t);
  const home = makeHome(w, listing(w));
  toldByState(home, w.state);
  await home.publish();
  w.roots[OLD_THREAD] = root(OLD_THREAD, { reply_count: 21, latest_reply: `${EPOCH - 5}.000300` });
  w.state.setStatusPending(CHANNEL, OLD_THREAD, Status.WORKING);
  w.state.setStatusPending(CHANNEL, OLD_THREAD, null, Status.DONE);
  await home.publish();
  assert.ok(
    notes(published(w.slack).at(-1) as Json).some((n) => n.startsWith("ended · 21 replies")),
  );
});

test("a thread with no kept reaction shows the one on its root", async (t) => {
  // A thread that ended before the daemon kept its last reaction: state.json has none.
  const w = world(t);
  w.state.setStatusPending(CHANNEL, OLD_THREAD, null, null);
  w.roots[OLD_THREAD] = root(OLD_THREAD, { reactions: reacted(Status.ERROR) });
  // What the daemon keeps wins over what the root shows (a reaction lands after its request).
  w.roots[NEW_THREAD] = root(NEW_THREAD, { reactions: reacted(Status.WORKING) });
  await makeHome(w, listing(w)).publish();
  assert.deepEqual(titles(published(w.slack)[0] as Json).sort(), [
    ":raised_hand:  *Add retry to the uploader*",
    ":x:  *Fix the footer*",
  ]);
});

test("a cross over a kept status shows the cross", async (t) => {
  // An answer that never reached Slack: the root shows ❌ while crash repair still holds ⏳.
  const w = world(t);
  w.state.setStatusPending(CHANNEL, OLD_THREAD, Status.WORKING, Status.ERROR);
  await makeHome(w, listing(w)).publish();
  assert.ok(titles(published(w.slack)[0] as Json).includes(":x:  *Fix the footer*"));
});

test("a thread with no reply is dated by its root", async (t) => {
  const w = world(t);
  w.roots[NEW_THREAD] = root(NEW_THREAD, { reply_count: null, latest_reply: null });
  const home = makeHome(w, listing(w));
  await home.choose(homeFilter({ date: null })); // the root is older than the page's 48 hours
  assert.ok(
    notes(published(w.slack)[0] as Json)
      .join("")
      .includes(`waiting for you · started <!date^${Math.trunc(Number(NEW_THREAD))}^{ago}|`),
  );
});

test("a session claude code does not list yet shows its id", async (t) => {
  const w = world(t);
  const none: Listing = new Map([
    [join(w.directory, "project"), []],
    [join(w.directory, "other"), []],
  ]);
  await makeHome(w, none).publish();
  assert.equal(
    titles(published(w.slack)[0] as Json)[0],
    `:raised_hand:  *${texts.fill(texts.HOME_UNTITLED, { id: NEW.slice(0, 8) })}*`,
  );
});

test("a folder that cannot be listed does not stop the page", async (t) => {
  const w = world(t);
  const warned = warnings(t);
  const home = new Home(w.slack, {
    ownerUserId: OWNER,
    teamId: TEAM,
    state: w.state,
    sessionsOf: () => {
      throw new OSError("gone");
    },
    clock: w.clock,
  });
  await home.publish();
  assert.equal(cards(published(w.slack)[0] as Json).length, 2);
  assert.ok(warned().includes("OSError"));
});

test("a channel slack no longer has is left out with its threads and asked once", async (t) => {
  // The first bound channel is there, the second is gone.
  const w = world(t);
  w.slack.responses["conversations.info"] = [
    slackPayload("api-conversations-info"),
    rejected("channel_not_found"),
  ];
  const home = makeHome(w, listing(w));
  await home.publish();
  const page = published(w.slack)[0] as Json;
  assert.deepEqual(headers(page), [`*<#${CHANNEL}>*`]);
  assert.deepEqual(titles(page), [":white_check_mark:  *Fix the footer*"]);
  assert.equal(control(page, CHANNEL_ACTION).options.length, 2); // All channels, and the one left
  // Nothing is asked about a thread of a channel that is gone.
  assert.deepEqual(
    w.slack.callsTo("conversations.replies").map((a) => a.channel),
    [CHANNEL],
  );
  assert.deepEqual(
    w.slack.callsTo("chat.getPermalink").map((a) => a.channel),
    [CHANNEL],
  );
  await home.publish();
  assert.equal(w.slack.callsTo("conversations.info").length, 2); // each asked once per run
});

test("a channel forgotten and bound again is asked about again", async (t) => {
  const w = world(t);
  w.slack.responses["conversations.info"] = [
    slackPayload("api-conversations-info"),
    rejected("channel_not_found"),
    slackPayload("api-conversations-info"),
  ];
  const home = makeHome(w, listing(w));
  await home.publish();
  assert.deepEqual(headers(published(w.slack).at(-1) as Json), [`*<#${CHANNEL}>*`]);
  w.state.removeChannel(OTHER_CHANNEL); // the cleanup forgets it
  await home.publish();
  w.state.bind(OTHER_CHANNEL, join(w.directory, "other")); // the bot is back in it, and `!bind` again
  await home.publish();
  assert.deepEqual(headers(published(w.slack).at(-1) as Json), [
    `*<#${CHANNEL}>*`,
    `*<#${OTHER_CHANNEL}>*`,
  ]);
});

test("a thread whose root is gone is left out and asked once", async (t) => {
  // Measured 2026-10-01: a deleted root answers `thread_not_found`.
  const w = world(t);
  w.roots[OLD_THREAD] = rejected("thread_not_found");
  const home = makeHome(w, listing(w));
  await home.publish();
  const page = published(w.slack)[0] as Json;
  assert.deepEqual(titles(page), [":raised_hand:  *Add retry to the uploader*"]);
  assert.deepEqual(headers(page), [`*<#${OTHER_CHANNEL}>*`, `*<#${CHANNEL}>*`]); // the channel stays
  assert.deepEqual(
    w.slack.callsTo("chat.getPermalink").map((a) => a.channel),
    [OTHER_CHANNEL],
  );
  // Slack's refusal stands for the run (a deleted root stays deleted): not asked again.
  await home.publish();
  assert.equal(w.slack.callsTo("conversations.replies").length, 2);
});

test("a thread slack did not answer about keeps what was read", async (t) => {
  const w = world(t);
  const home = makeHome(w, listing(w));
  toldByState(home, w.state);
  await home.publish();
  w.roots[OLD_THREAD] = new OSError("network down");
  w.state.setStatusPending(CHANNEL, OLD_THREAD, Status.WORKING);
  await home.publish();
  const page = published(w.slack).at(-1) as Json;
  // The status is the daemon's.
  assert.ok(titles(page).includes(":hourglass_flowing_sand:  *Fix the footer*"));
  assert.ok(notes(page).some((n) => n.startsWith("working · 19 replies"))); // as read before
  // Asked again at the next publish: nothing was learned.
  w.roots[OLD_THREAD] = root(OLD_THREAD, { reply_count: 21, latest_reply: `${EPOCH - 5}.000300` });
  await home.publish();
  assert.ok(
    notes(published(w.slack).at(-1) as Json).some((n) => n.startsWith("working · 21 replies")),
  );
});

test("a root slack returns in a shape it cannot read does not stop the page", async (t) => {
  const w = world(t);
  w.roots[OLD_THREAD] = root(OLD_THREAD, { latest_reply: "not-a-number" });
  await makeHome(w, listing(w)).publish();
  assert.deepEqual(titles(published(w.slack)[0] as Json), [
    ":raised_hand:  *Add retry to the uploader*",
  ]);
});

for (const method of ["conversations.info", "conversations.replies", "chat.getPermalink"]) {
  for (const code of ["internal_error", "ratelimited", "service_unavailable"]) {
    test(`an error that is not a refusal is asked about again [${method}-${code}]`, async (t) => {
      // Only `channel_not_found`, `message_not_found` and `thread_not_found` say a thing is gone.
      // Anything else is Slack having a bad moment: kept as final, one such answer would hide a
      // channel or a thread until the daemon restarts.
      const w = world(t);
      const working = w.slack.responses[method] as Scripted;
      w.slack.responses[method] = rejected(code);
      const home = makeHome(w, listing(w));
      await home.publish();
      w.slack.responses[method] = working;
      await home.publish();
      assert.equal(cards(published(w.slack).at(-1) as Json).length, 2);
    });
  }
}

test("a page slack did not fully answer for is tried again and a whole one is not", async (t) => {
  const w = world(t);
  const working = w.slack.responses["chat.getPermalink"] as Scripted;
  w.slack.responses["chat.getPermalink"] = rejected("ratelimited");
  const home = makeHome(w, listing(w));
  await home.publish();
  assert.deepEqual(cards(published(w.slack)[0] as Json), []);
  w.slack.responses["chat.getPermalink"] = working;
  await w.clock.advance(RETRY_SECONDS); // the retry asks for a publish, which waits out its debounce
  await w.clock.advance(0.01);
  assert.equal(published(w.slack).length, 2);
  assert.equal(cards(published(w.slack)[1] as Json).length, 2);
  await w.clock.advance(RETRY_SECONDS * 3); // the page is whole now: nothing is tried again
  assert.equal(published(w.slack).length, 2);
});

test("no channel answered about keeps the page and does not say none is bound", async (t) => {
  const w = world(t);
  w.slack.responses["conversations.info"] = rejected("internal_error");
  await makeHome(w, listing(w)).publish();
  assert.deepEqual(published(w.slack), []); // nothing false is written over the page that is there
});

test("a chosen channel slack did not answer about stays chosen", async (t) => {
  const w = world(t);
  w.slack.responses["conversations.info"] = [
    slackPayload("api-conversations-info"),
    rejected("ratelimited"),
  ];
  const home = makeHome(w, listing(w));
  await home.choose(homeFilter({ channel: OTHER_CHANNEL }));
  assert.equal(home.chosen.channel, OTHER_CHANNEL);
  // One Slack says is gone is dropped.
  w.slack.responses["conversations.info"] = rejected("channel_not_found");
  await home.publish();
  assert.equal(home.chosen.channel, null);
});

/**
 * A permalink as `chat.getPermalink` forms it for the message asked about; a reply's carries its
 * thread and channel in the query (docs.slack.dev/reference/methods/chat.getPermalink, read
 * 2026-10-05).
 */
function replyLink(args: { channel: Wire; message_ts: Wire }): {
  ok: true;
  channel: Wire;
  permalink: string;
} {
  const link = `https://example.slack.com/archives/${String(args.channel)}/p${String(args.message_ts)}`;
  return { ok: true, channel: args.channel, permalink: link.replaceAll(".", "") };
}

test("open links to the thread s last reply and to the root while it has none", async (t) => {
  const w = world(t);
  w.roots[NEW_THREAD] = root(NEW_THREAD, { reply_count: null, latest_reply: null });
  w.slack.responses["chat.getPermalink"] = (args) =>
    replyLink({ channel: args.channel as Wire, message_ts: args.message_ts as Wire });
  const home = makeHome(w, listing(w));
  toldByState(home, w.state);
  await home.publish();
  const last = `${EPOCH - 3600}.000200`;
  assert.deepEqual(
    w.slack.callsTo("chat.getPermalink").map((a) => [a.channel, a.message_ts]),
    [
      [CHANNEL, last],
      [OTHER_CHANNEL, NEW_THREAD],
    ],
  );
  // The link on the page is the one Slack answered with for that message.
  let opens = `<${replyLink({ channel: CHANNEL, message_ts: last }).permalink}|`;
  assert.ok(notes(published(w.slack).at(-1) as Json).some((note) => note.includes(opens)));

  // A reply arrives in one thread: its link alone is asked again, for the new last reply.
  const newer = `${EPOCH - 5}.000300`;
  w.roots[OLD_THREAD] = root(OLD_THREAD, { reply_count: 20, latest_reply: newer });
  w.state.setStatusPending(CHANNEL, OLD_THREAD, Status.WORKING);
  await home.publish();
  await home.publish();
  assert.deepEqual(
    w.slack
      .callsTo("chat.getPermalink")
      .map((a) => a.message_ts)
      .slice(2),
    [newer],
  );
  opens = `<${replyLink({ channel: CHANNEL, message_ts: newer }).permalink}|`;
  assert.ok(notes(published(w.slack).at(-1) as Json).some((note) => note.includes(opens)));
});

test("a reply s permalink is written as mrkdwn takes it", () => {
  // `&` is markup in mrkdwn (docs.slack.dev/messaging/formatting-message-text, read 2026-10-05).
  const link = `${LINK}?thread_ts=1789990000.000100&cid=${CHANNEL}`;
  const page = view([row("Refactor the feed parser", { permalink: link })], homeFilter(), {
    channels: named({ [CHANNEL]: "cc-articles" }),
  });
  assert.ok(
    (notes(page).at(-1) as string).endsWith(
      ` · <${LINK}?thread_ts=1789990000.000100&amp;cid=${CHANNEL}|${texts.HOME_OPEN}>`,
    ),
  );
});

test("a permalink slack refuses leaves the thread out for the run", async (t) => {
  const w = world(t);
  const home = makeHome(w, listing(w));
  await home.publish();
  await home.publish();
  assert.equal(w.slack.callsTo("chat.getPermalink").length, 2); // one per thread, not per publish

  w.slack.apiCalls.length = 0;
  w.slack.responses["chat.getPermalink"] = rejected("message_not_found");
  const refused = makeHome(w, listing(w));
  await refused.publish();
  assert.deepEqual(cards(published(w.slack)[0] as Json), []); // a thread that cannot be opened is not listed
  await refused.publish();
  assert.equal(w.slack.callsTo("chat.getPermalink").length, 2);
});

test("a permalink that failed without an answer is asked again", async (t) => {
  const w = world(t);
  w.slack.responses["chat.getPermalink"] = new OSError("network down");
  const home = makeHome(w, listing(w));
  await home.publish();
  assert.deepEqual(cards(published(w.slack)[0] as Json), []);
  w.slack.responses["chat.getPermalink"] = { ok: true, permalink: LINK };
  await home.publish();
  assert.equal(cards(published(w.slack)[1] as Json).length, 2);
});

test("choose publishes at once with the filter", async (t) => {
  const w = world(t);
  const home = makeHome(w, listing(w), { debounce: 60 });
  await home.choose(homeFilter({ status: Status.WAITING }));
  const page = published(w.slack)[0] as Json;
  assert.deepEqual(titles(page), [":raised_hand:  *Add retry to the uploader*"]);
  assert.equal(control(page, STATUS_ACTION).initial_option.value, Status.WAITING);
  // The filter stays for the pages a later change of the sessions publishes.
  await home.publish();
  assert.deepEqual(titles(published(w.slack)[1] as Json), [
    ":raised_hand:  *Add retry to the uploader*",
  ]);
});

test("a channel that is not bound is no filter", async (t) => {
  const w = world(t);
  const home = makeHome(w, listing(w));
  await home.choose(homeFilter({ channel: "C000NOPE", search: "footer" }));
  assert.deepEqual(home.chosen, homeFilter({ search: "footer" }));
});

test("a filter chosen while a page is on its way is the page that stays", async (t) => {
  // The first page is held inside its views.publish; a filter is chosen meanwhile. Published
  // side by side, the filtered page would land first and the held one would replace it.
  const w = world(t);
  const held = new AsyncEvent();
  w.slack.gate = held;
  w.slack.gateMethod = "views.publish";
  const home = makeHome(w, listing(w));
  const slow = home.publish();
  await w.slack.gated.wait();
  w.slack.gate = null; // a call that arrives now is not held: only the first page is
  const choosing = home.choose(homeFilter({ status: Status.DONE }));
  await settle(w.clock); // time enough for the filtered page to land first, were it not queued
  held.set();
  await Promise.all([slow, choosing]);
  assert.deepEqual(titles(published(w.slack).at(-1) as Json), [
    ":white_check_mark:  *Fix the footer*",
  ]);
});

test("a disabled home tab is logged once and never asked again", async (t) => {
  // views.publish reference, read 2026-10-01: `not_enabled`, "Error returned if a home view is
  // published but the Home tab isn't enabled for the app." Measured live the same day.
  const w = world(t);
  const warned = warnings(t);
  w.slack.responses["views.publish"] = rejected("not_enabled");
  const home = makeHome(w, listing(w));
  await home.publish();
  await home.publish();
  home.request();
  await w.clock.advance(1);
  assert.equal(w.slack.callsTo("views.publish").length, 1);
  assert.equal(warned().split("Home tab").length - 1, 1);
  assert.ok(warned().includes("docs/setup.md"));
});

test("a failed publish is swallowed and the next one tries again", async (t) => {
  const w = world(t);
  const warned = warnings(t);
  w.slack.responses["views.publish"] = [rejected("internal_error"), { ok: true }];
  const home = makeHome(w, listing(w));
  await home.publish(); // never raises: the page must not break a turn
  assert.ok(warned().includes("internal_error"));
  await home.publish();
  assert.equal(w.slack.callsTo("views.publish").length, 2);
});

test("a burst of requests publishes once and a later one publishes again", async (t) => {
  const w = world(t);
  const home = makeHome(w, listing(w));
  for (let i = 0; i < 5; i += 1) home.request();
  await w.clock.advance(0.01);
  assert.equal(published(w.slack).length, 1);
  home.request();
  await w.clock.advance(0.01);
  assert.equal(published(w.slack).length, 2);
});

test("a change during a publish is not lost", async (t) => {
  // The first page is built and held inside its views.publish when the change lands.
  const w = world(t);
  const release = new AsyncEvent();
  w.slack.gate = release;
  w.slack.gateMethod = "views.publish";
  const home = makeHome(w, listing(w));
  home.request();
  await w.clock.advance(0.01);
  await w.slack.gated.wait();
  w.state.setStatusPending(CHANNEL, OLD_THREAD, Status.WORKING);
  home.request();
  release.set();
  await settle(w.clock); // the loop sees the request and waits out its debounce
  await w.clock.advance(0.01);
  const last = published(w.slack).at(-1) as Json;
  assert.ok(titles(last).includes(":hourglass_flowing_sand:  *Fix the footer*"));
});

test("close publishes what a pending request still owed", async (t) => {
  const w = world(t);
  const home = makeHome(w, listing(w), { debounce: 60 });
  home.request();
  await home.close();
  assert.equal(published(w.slack).length, 1);
  home.request(); // after the close: nothing is scheduled any more
  await w.clock.advance(61);
  assert.equal(published(w.slack).length, 1);
});

test("close cut in the middle of a publish still publishes", async (t) => {
  const w = world(t);
  const release = new AsyncEvent();
  w.slack.gate = release;
  w.slack.gateMethod = "conversations.info";
  const home = makeHome(w, listing(w));
  home.request();
  await w.clock.advance(0.01); // past the debounce, inside the first Slack call
  await w.slack.gated.wait();
  const closing = home.close();
  release.set();
  await closing;
  assert.equal(published(w.slack).length, 1);
});

test("close gives up on a slack that does not answer", async (t) => {
  const w = world(t);
  const hold = new AsyncEvent();
  w.slack.gate = hold;
  w.slack.gateMethod = "conversations.info";
  const home = makeHome(w, listing(w));
  home.request();
  const closing = home.close();
  await w.slack.gated.wait();
  await w.clock.advance(CLOSE_SECONDS);
  await closing; // returns: a stop never hangs on the page
  assert.deepEqual(published(w.slack), []);
  // Slack answers late: the publish that was given up on sends nothing.
  hold.set();
  await settle(w.clock);
  assert.deepEqual(published(w.slack), []);
  assert.equal(w.slack.callsTo("conversations.info").length, 1); // and asks nothing more
});

function buttons(page: Json, actionId: string): Json[] {
  const found: Json[] = page.blocks
    .filter((b: Json) => b.type === "section")
    .map((b: Json) => b.accessory ?? {});
  return found.filter((a) => a.action_id === actionId);
}

test("a page that cannot delete has no edit button even when asked to edit", () => {
  const channels = named({ [CHANNEL]: "cc-articles" });
  const plain = view([row()], homeFilter(), { channels });
  const asked = view([row()], homeFilter(), { channels, editing: true });
  assert.deepEqual(asked, plain);
  assert.deepEqual(buttons(plain, EDIT_ACTION), []);
  assert.deepEqual(buttons(plain, DELETE_ACTION), []);
  assert.ok((notes(plain)[0] as string).includes(texts.HOME_HEADER.split(" · ")[0] as string)); // the header stays a small line
});

test("a page that can delete carries edit on its header line", () => {
  const page = view([row()], homeFilter(), {
    channels: named({ [CHANNEL]: "cc-articles" }),
    canEdit: true,
  });
  const edit = buttons(page, EDIT_ACTION);
  assert.equal(edit.length, 1);
  assert.deepEqual([edit[0]?.text.text, edit[0]?.value], [texts.HOME_EDIT, EDIT_ON]);
  assert.ok(!("style" in (edit[0] as Json)));
  // Out of edit mode nothing else changes: New thread stays, no session carries a button.
  assert.equal(buttons(page, NEW_THREAD_ACTION).length, 1);
  assert.deepEqual(buttons(page, DELETE_ACTION), []);
  assert.equal(cards(page).length, 1);
});

test("edit mode shows delete on each session and no new thread", () => {
  const rows = [
    row("Fix the footer", { threadTs: "1789990000.000100", status: Status.DONE, replies: 19 }),
    row("Still running", { threadTs: "1789990000.000200", status: Status.WORKING }),
    row("Asked you", { threadTs: "1789990000.000300", status: Status.WAITING }),
    row("Never replied", { threadTs: "1789990000.000400", status: Status.ERROR, replies: 0 }),
  ];
  const page = view(rows, homeFilter(), {
    channels: named({ [CHANNEL]: "cc-articles" }),
    canEdit: true,
    editing: true,
  });
  const done = buttons(page, EDIT_ACTION);
  assert.equal(done.length, 1);
  assert.deepEqual(
    [done[0]?.text.text, done[0]?.value, done[0]?.style],
    [texts.HOME_DONE, EDIT_OFF, "primary"],
  );
  assert.deepEqual(buttons(page, NEW_THREAD_ACTION), []);
  // A thread that is working or waiting for the owner is in use: no Delete.
  const deletes = buttons(page, DELETE_ACTION);
  assert.deepEqual(
    deletes.map((d) => d.value),
    [`${CHANNEL}:1789990000.000100`, `${CHANNEL}:1789990000.000400`],
  );
  const first = deletes[0] as Json;
  assert.ok(first.style === "danger" && first.text.text === texts.HOME_DELETE);
  // The button element and its confirm dialog: docs.slack.dev block-elements/button-element
  // and composition-objects/confirmation-dialog-object, read 2026-10-05.
  assert.deepEqual(first.confirm, {
    title: { type: "plain_text", text: "Delete this thread?" },
    text: {
      type: "plain_text",
      text: `“Fix the footer” in #cc-articles, 19 replies. ${texts.HOME_DELETE_TEXT}`,
    },
    confirm: { type: "plain_text", text: "Delete thread" },
    deny: { type: "plain_text", text: "Cancel" },
    style: "danger",
  });
  const second = deletes[1] as Json;
  assert.ok(second.confirm.text.text.startsWith("“Never replied” in #cc-articles. "));
});

test("a confirmation never passes the dialog s limit", () => {
  const long = row("word ".repeat(100), { replies: 1234, status: Status.DONE });
  const page = view([long], homeFilter(), {
    channels: named({ [CHANNEL]: "c".repeat(80) }),
    canEdit: true,
    editing: true,
  });
  const deletes = buttons(page, DELETE_ACTION);
  assert.equal(deletes.length, 1);
  const text: string = deletes[0]?.confirm.text.text;
  assert.ok(Array.from(text).length <= CONFIRM_TEXT);
  assert.ok(text.endsWith(texts.HOME_DELETE_TEXT));
});

test("a notice shows under the header", () => {
  const said = Array.from({ length: 7 }, (_, n) => `notice ${n}`);
  const page = view([row()], homeFilter(), {
    channels: named({ [CHANNEL]: "cc-articles" }),
    canEdit: true,
    notices: said,
  });
  // The five latest, then the session.
  assert.deepEqual(notes(page).slice(0, 6), [...said.slice(0, 5), notes(page)[5]]);
  assert.ok(!notes(page).includes("notice 5"));
});

test("edit and delete publish the page and keep it in edit mode", async (t) => {
  const w = world(t);
  const asked: [string, string][] = [];
  const answers: (string | null)[] = [texts.HOME_DELETE_BUSY, null];
  const home = makeHome(w, listing(w), {
    delete: async (channelId, threadTs) => {
      asked.push([channelId, threadTs]);
      const answer = answers.shift() ?? null;
      if (answer === null) w.state.removeThread(channelId, threadTs);
      return answer;
    },
  });
  // A delete that arrives out of edit mode is not one the page offered.
  await home.delete(CHANNEL, OLD_THREAD);
  assert.deepEqual(asked, []);
  assert.deepEqual(published(w.slack), []);

  await home.edit(true);
  // The other one waits for you.
  assert.equal(buttons(published(w.slack).at(-1) as Json, DELETE_ACTION).length, 1);
  await home.delete(CHANNEL, OLD_THREAD);
  let page = published(w.slack).at(-1) as Json;
  // It names the thread.
  assert.ok(notes(page).includes(`“Fix the footer”: ${texts.HOME_DELETE_BUSY}`));
  assert.equal(buttons(page, DELETE_ACTION).length, 1); // still listed, still in edit mode

  await home.delete(CHANNEL, OLD_THREAD);
  page = published(w.slack).at(-1) as Json;
  assert.deepEqual(asked, [
    [CHANNEL, OLD_THREAD],
    [CHANNEL, OLD_THREAD],
  ]);
  assert.ok(!notes(page).some((note) => note.includes(texts.HOME_DELETE_BUSY)));
  assert.deepEqual(buttons(page, DELETE_ACTION), []); // gone from the page
  assert.equal(buttons(page, EDIT_ACTION)[0]?.value, EDIT_OFF);

  await home.edit(false);
  assert.equal(buttons(published(w.slack).at(-1) as Json, EDIT_ACTION)[0]?.value, EDIT_ON);
});

test("a home with no deleter never enters edit mode", async (t) => {
  const w = world(t);
  const home = makeHome(w, listing(w));
  await home.edit(true);
  await home.delete(CHANNEL, OLD_THREAD);
  const page = published(w.slack).at(-1) as Json;
  assert.deepEqual(buttons(page, EDIT_ACTION), []);
  assert.deepEqual(buttons(page, DELETE_ACTION), []);
  assert.equal(buttons(page, NEW_THREAD_ACTION).length, 2);
});

test("a thread being deleted says so from the click on and takes no second click", async (t) => {
  const w = world(t);
  const started: [string, string][] = [];
  const begun = new AsyncEvent();
  const finish = new AsyncEvent();
  const home = makeHome(w, listing(w), {
    delete: async (channelId, threadTs) => {
      started.push([channelId, threadTs]);
      begun.set();
      await finish.wait(); // Slack's rate limit: a delete takes from seconds to minutes
      w.state.removeThread(channelId, threadTs);
      return null;
    },
  });
  await home.edit(true);
  const running = home.delete(CHANNEL, OLD_THREAD);
  await begun.wait();
  const page = published(w.slack).at(-1) as Json;
  // Published before the delete ends: the row reads `deleting…` and offers no Delete.
  assert.ok(notes(page).some((note) => note.startsWith(`${texts.HOME_DELETING} · 19 replies`)));
  assert.deepEqual(buttons(page, DELETE_ACTION), []);
  // And the page says it in full size under the header, with how many are on their way.
  assert.ok(titles(page).includes(texts.HOME_DELETING_ONE));
  await home.delete(CHANNEL, OLD_THREAD); // a second click while it runs
  assert.deepEqual(started, [[CHANNEL, OLD_THREAD]]);
  // Leaving edit mode does not hide what is going on.
  await home.edit(false);
  assert.ok(
    notes(published(w.slack).at(-1) as Json).some((note) => note.startsWith(texts.HOME_DELETING)),
  );
  finish.set();
  await running;
  const last = published(w.slack).at(-1) as Json;
  assert.ok(!titles(last).includes(texts.HOME_DELETING_ONE));
  assert.ok(!titles(last).some((title) => title.includes("Fix the footer")));
});

test("done brings back the filters the page had before edit", async (t) => {
  const w = world(t);
  const home = makeHome(w, listing(w), { delete: async () => null });
  const before = homeFilter({ status: Status.DONE, date: LAST_7 });
  await home.choose(before);
  await home.edit(true);
  await home.edit(true); // a click sent twice does not take the filters of edit mode as "before"
  // Show all on the channel being cleaned chooses that channel: it lasts while edit mode does.
  await home.choose(homeFilter({ channel: CHANNEL }));
  assert.equal(buttons(published(w.slack).at(-1) as Json, EDIT_ACTION)[0]?.value, EDIT_OFF); // still editing
  await home.edit(false);
  assert.deepEqual(home.chosen, before);
  // The menus show the filters that are back (their block follows what is chosen).
  assert.equal(
    control(published(w.slack).at(-1) as Json, STATUS_ACTION).initial_option.value,
    before.status,
  );
  // Out of edit mode a filter is the owner's choice again, and a second Done changes nothing.
  await home.choose(homeFilter({ channel: CHANNEL }));
  await home.edit(false);
  assert.deepEqual(home.chosen, homeFilter({ channel: CHANNEL }));
});

test("the page counts the threads being deleted", () => {
  const channels = named({ [CHANNEL]: "cc-articles" });
  const page = view([row()], homeFilter(), { channels, canEdit: true, deleting: 3 });
  assert.ok(titles(page).includes(texts.fill(texts.HOME_DELETING_MANY, { count: 3 })));
  assert.ok(
    !JSON.stringify(view([row()], homeFilter(), { channels, canEdit: true })).includes("Deleting"),
  );
});

test("a channel s header counts its threads being deleted shown or not", () => {
  const rows = Array.from({ length: PER_CHANNEL + 2 }, (_, n) =>
    row(`Session ${n}`, {
      threadTs: `17899900${String(n).padStart(2, "0")}.000100`,
      lastActivity: EPOCH - n,
    }),
  );
  // The two oldest are the ones the channel does not show out of a filter.
  rows[rows.length - 1] = { ...(rows.at(-1) as HomeRow), deleting: true };
  rows[rows.length - 2] = { ...(rows.at(-2) as HomeRow), deleting: true };
  const other = row("Elsewhere", { channelId: OTHER_CHANNEL, deleting: true });
  const page = view([...rows, other], homeFilter(), {
    channels: named({ [CHANNEL]: "cc-articles", [OTHER_CHANNEL]: "cc-shop" }),
  });
  assert.deepEqual(headers(page), [
    `*<#${CHANNEL}>*   :hourglass_flowing_sand: deleting 2 threads…`,
    `*<#${OTHER_CHANNEL}>*   :hourglass_flowing_sand: deleting 1 thread…`,
  ]);
  assert.ok(
    !notes(page)
      .slice(1, PER_CHANNEL + 1)
      .some((note) => note.startsWith(texts.HOME_DELETING)),
  );
  // A channel with none says nothing.
  assert.deepEqual(
    headers(view([row()], homeFilter(), { channels: named({ [CHANNEL]: "cc-articles" }) })),
    [`*<#${CHANNEL}>*`],
  );
});

test("edit mode offers clean up beside each channel when it can", () => {
  const channels = named({ [CHANNEL]: "cc-articles" });
  assert.deepEqual(
    buttons(view([row()], homeFilter(), { channels, canEdit: true, editing: true }), CLEAN_ACTION),
    [],
  );
  assert.deepEqual(
    buttons(view([row()], homeFilter(), { channels, canEdit: true, canClean: true }), CLEAN_ACTION),
    [],
  );
  const page = view([row()], homeFilter(), {
    channels,
    canEdit: true,
    canClean: true,
    editing: true,
  });
  const found = buttons(page, CLEAN_ACTION);
  assert.equal(found.length, 1);
  const clean = found[0] as Json;
  assert.deepEqual([clean.text.text, clean.value], [texts.HOME_CLEAN, CHANNEL]);
  assert.deepEqual(clean.confirm, {
    title: { type: "plain_text", text: "Clean up this channel?" },
    text: {
      type: "plain_text",
      text: texts.fill(texts.HOME_CLEAN_TEXT, { channel: "cc-articles" }),
    },
    confirm: { type: "plain_text", text: "Clean up" },
    deny: { type: "plain_text", text: "Cancel" },
    style: "danger",
  });
  const long = view([row()], homeFilter(), {
    channels: named({ [CHANNEL]: "c".repeat(80) }),
    canEdit: true,
    canClean: true,
    editing: true,
  });
  assert.ok(
    Array.from(buttons(long, CLEAN_ACTION)[0]?.confirm.text.text as string).length <= CONFIRM_TEXT,
  );
});

test("a channel being cleaned says so and has no button", () => {
  const page = view([row()], homeFilter(), {
    channels: named({ [CHANNEL]: "cc-articles" }),
    canEdit: true,
    canClean: true,
    editing: true,
    cleaning: new Set([CHANNEL]),
  });
  assert.deepEqual(buttons(page, CLEAN_ACTION), []);
  assert.ok(titles(page).includes(`*<#${CHANNEL}>*   ${texts.HOME_CHANNEL_CLEANING}`));
  assert.ok(titles(page).includes(texts.HOME_CLEANING_ONE));
});

test("clean publishes at once takes no second click and shows a failure", async (t) => {
  const w = world(t);
  const started: string[] = [];
  const begun = new AsyncEvent();
  const finish = new AsyncEvent();
  const home = makeHome(w, listing(w), {
    delete: async () => null,
    clean: async (channelId) => {
      started.push(channelId);
      begun.set();
      await finish.wait();
      return texts.fill(texts.HOME_CLEAN_FAILED, { error: "ratelimited" });
    },
  });
  await home.clean(CHANNEL); // out of edit mode: not a click the page offered
  assert.deepEqual(started, []);
  await home.edit(true);
  await home.clean("C000NOPE"); // not a bound channel
  assert.deepEqual(started, []);
  const running = home.clean(CHANNEL);
  await begun.wait();
  let page = published(w.slack).at(-1) as Json;
  assert.deepEqual(
    buttons(page, CLEAN_ACTION).map((b) => b.value),
    [OTHER_CHANNEL],
  );
  assert.ok(titles(page).includes(texts.HOME_CLEANING_ONE));
  await home.clean(CHANNEL);
  assert.deepEqual(started, [CHANNEL]);
  finish.set();
  await running;
  page = published(w.slack).at(-1) as Json;
  const name = (slackPayload("api-conversations-info").channel as Json).name;
  const failed = texts.fill(texts.HOME_CLEAN_FAILED, { error: "ratelimited" });
  assert.ok(notes(page).includes(`#${name}: ${failed}`)); // it names the channel
  assert.equal(buttons(page, CLEAN_ACTION).length, 2);
  assert.ok(!titles(page).includes(texts.HOME_CLEANING_ONE));
});

test("one delete s notice is not erased by the next one s result", async (t) => {
  const w = world(t);
  w.state.setStatusPending(OTHER_CHANNEL, NEW_THREAD, null, Status.DONE);
  const answers: Record<string, string | null> = {
    [OLD_THREAD]: texts.fill(texts.HOME_DELETE_FAILED, { error: "ratelimited" }),
    [NEW_THREAD]: null,
  };
  const home = makeHome(w, listing(w), {
    delete: async (channelId, threadTs) => {
      const answer = answers[threadTs] ?? null;
      if (answer === null) w.state.removeThread(channelId, threadTs);
      return answer;
    },
  });
  await home.edit(true);
  await home.delete(CHANNEL, OLD_THREAD); // fails
  await home.delete(OTHER_CHANNEL, NEW_THREAD); // succeeds
  const failed = `“Fix the footer”: ${answers[OLD_THREAD]}`;
  assert.ok(notes(published(w.slack).at(-1) as Json).includes(failed)); // still said: the row is still there
  // A new try at the same thread takes its line away while it runs, and Done clears them all.
  answers[OLD_THREAD] = texts.HOME_DELETE_BUSY;
  await home.delete(CHANNEL, OLD_THREAD);
  assert.ok(!notes(published(w.slack).at(-1) as Json).includes(failed));
  await home.edit(false);
  assert.ok(
    !notes(published(w.slack).at(-1) as Json).some((note) => note.includes(texts.HOME_DELETE_BUSY)),
  );
});
