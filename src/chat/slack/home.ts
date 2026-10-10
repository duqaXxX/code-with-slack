/**
 * The app's Home tab: the owner's index of sessions. Two lines per thread that holds a session,
 * grouped by channel, each group and each session ordered by the thread's last reply, with a link
 * that opens the thread: a channel lists its threads by when they started, and Slack's
 * Threads view by unread replies (Help Center, "Use threads to organize discussions", read
 * 2026-10-01), so neither finds the thread worked in last.
 *
 * A card is built from what the daemon already keeps, what Claude Code knows and what Slack shows:
 * the thread and its root's reaction from `state.json`, the title from the session's transcript,
 * as `!resume` shows it, and from Slack the thread's number of replies and the time of its last
 * one, the same the channel shows under the root. Nothing is stored for the page: the filters the
 * owner chooses live in memory and start again at their defaults with the daemon.
 *
 * `conversations.replies` with the root's `ts` and `limit=1` returns the root alone, carrying
 * `reply_count`, `latest_reply` and `reactions` (measured 2026-10-01 on a free workspace with
 * `groups:history`, slack-sdk 3.44.1; a deleted root answers `thread_not_found`). The root's own
 * reaction fills in the status of a thread that ended before the daemon kept it.
 *
 * `views.publish` (docs.slack.dev/reference/methods/views.publish, read 2026-10-01) takes no scope
 * and may be called at any time, with no event from the owner ("Home tab updates can happen when a
 * user isn't interacting with Slack or the app", docs.slack.dev/surfaces/app-home), so the page is
 * rewritten when the index changes, with no event subscribed. A view holds 100 blocks. Ages are
 * Slack's own `{ago}` date token (docs.slack.dev/messaging/formatting-message-text, seen rendered
 * in a Home view on 2026-10-01), so they do not go stale between two publishes.
 *
 * What reaches the app from the page is a `block_actions` payload per use of a control
 * (the app's listeners own them): a filter, which carries the state of every control in
 * `view.state.values`, and the **New thread** link button, which Slack follows itself and still
 * reports (button element reference, read 2026-10-01). A session's **Open** is a plain link in its
 * line of details, which reports nothing: the permalink of the thread's last reply, which Slack
 * opens the thread on (seen in the Mac app and on iOS, 2026-10-05), or of its root while it has
 * none. **New thread** is the documented deep link to the channel
 * (docs.slack.dev/interactivity/deep-linking; it opened the channel in the desktop app on
 * 2026-10-01): the owner's own top-level message there starts the session, so the thread is one
 * the owner started and its replies notify.
 *
 * Port of `home.py`. How Python's asyncio became JavaScript here:
 * - `publish` runs under a FIFO `Mutex`, so the publishes run one at a time and each is built
 *   after the one before it landed. A task cancelled by its owner is an `AbortSignal` that
 *   `publish` looks at before each Slack call and before `views.publish`: a call in flight is
 *   awaited and its answer used, never cut short, and a cancelled publish never sends its page.
 * - `asyncio.create_task` of the debounced loop and of the retry is a `Task`; `asyncio.timeout`
 *   in `close` is a race against the clock's sleep that aborts the publish when the sleep wins.
 * - `asyncio.Semaphore` is the small `Semaphore` below.
 * - Every wait goes through the injected `Clock`.
 */
import { createHash } from "node:crypto";
import { WebAPIPlatformError, type WebClient } from "@slack/web-api";
import type { ListedSession } from "../../agent/seam.ts";
import { type Clock, systemClock } from "../../clock.ts";
import { oneLine } from "../../core/reply/words.ts";
import type { StateStore, ThreadKey } from "../../core/state.ts";
import * as texts from "../../core/texts.ts";
import { getLogger } from "../../log.ts";
import { contextBlock, plainTextObject } from "./reply/blocks.ts";
import { take } from "./reply/chars.ts";
import { describe } from "./reply/errors.ts";
import { mrkdwnEscape, shownAsWritten } from "./reply/escape.ts";
import { Status } from "./reply/status.ts";
import { Cancelled, Mutex, Task } from "./reply/tasks.ts";
import { ID_SHOWN, TITLE_LIMIT } from "./resume.ts";

export const logger = getLogger("awaydesk.chat.slack.home");

export const HOME_BLOCKS = 100;
// What a channel shows while no filter is chosen; a filter shows every session it matches.
export const PER_CHANNEL = 5;
// A turn changes its root's reaction several times in a row (⏳, ✋ at an approval, ⏳ again):
// one publish covers the burst.
export const DEBOUNCE_SECONDS = 2.0;
// How long a stop waits for its last publish: the page must never hold the daemon's exit.
export const CLOSE_SECONDS = 10.0;
// How many threads Slack is asked about at once on the first publish of a run.
export const THREADS_AT_ONCE = 8;
export const NOT_ENABLED = "not_enabled";
// What Slack answers about a channel or a message it no longer has: the only refusals the page
// takes as final. Any other failure (a rate limit, a server error, the network) is no answer.
export const GONE: ReadonlySet<string> = new Set([
  "channel_not_found",
  "message_not_found",
  "thread_not_found",
]);
// How long after a page that lacked an answer the next one is tried.
export const RETRY_SECONDS = 60.0;
// A select menu holds 100 options (select menu reference): "All channels" and 99 channels.
export const CHANNEL_OPTIONS = 99;

export const FILTERS_BLOCK = "home_filters";
export const SEARCH_BLOCK = "home_search";
export const CHANNEL_ACTION = "home_channel";
export const STATUS_ACTION = "home_status";
export const DATE_ACTION = "home_date";
export const SEARCH_ACTION = "home_search_text";
export const SHOW_ALL_ACTION = "home_show_all";
export const NEW_THREAD_ACTION = "home_new_thread";
export const EDIT_ACTION = "home_edit";
export const DELETE_ACTION = "home_delete";
export const CLEAN_ACTION = "home_clean";
// How many lines about deletes that did not end the page shows at once, the latest ones.
export const NOTICES_SHOWN = 5;
// What the Edit button carries: the mode a click asks for, so a click sent twice asks the same.
export const EDIT_ON = "edit";
export const EDIT_OFF = "done";
// A confirmation dialog's text holds 300 characters (confirmation dialog object reference).
export const CONFIRM_TEXT = 300;
export const FILTER_ACTIONS = [CHANNEL_ACTION, STATUS_ACTION, DATE_ACTION, SEARCH_ACTION] as const;
// A blank row between two sessions: a context line that holds a zero-width space, since Slack
// sets the distance between blocks itself and takes no empty text.
export const SPACER = "​";
// A session is a title, a line of details and the blank row above it.
export const CARD_BLOCKS = 3;
export const ALL = "all";
export const SEARCH_LIMIT = 80;
// A select option's text holds 75 characters (option object reference).
export const OPTION_TEXT = 75;

// The reaction names a session can show and their words, in the order the status menu lists.
const WORDS: ReadonlyMap<string, string> = new Map<string, string>([
  [Status.WAITING, texts.HOME_WAITING],
  [Status.WORKING, texts.HOME_WORKING],
  [Status.DONE, texts.HOME_ENDED],
  [Status.ERROR, texts.HOME_ERROR],
]);
// A session in one of these states is not offered for deleting: its thread is in use.
const IN_USE: ReadonlySet<string> = new Set([Status.WAITING, Status.WORKING]);
export const LAST_48 = "48h";
export const TODAY = "today";
export const YESTERDAY = "yesterday";
export const LAST_7 = "7";
export const LAST_30 = "30";
// How far back each rolling period reaches, in seconds.
const REACH: ReadonlyMap<string, number> = new Map<string, number>([
  [LAST_48, 48 * 3600],
  [LAST_7, 7 * 86400],
  [LAST_30, 30 * 86400],
]);
const DATES: ReadonlyMap<string, string> = new Map<string, string>([
  [LAST_48, texts.HOME_LAST_48],
  [TODAY, texts.HOME_TODAY],
  [YESTERDAY, texts.HOME_YESTERDAY],
  [LAST_7, texts.HOME_LAST_7],
  [LAST_30, texts.HOME_LAST_30],
]);

/** A block of the page. */
export type Block = { type: string; [key: string]: unknown };

/** The Home tab's view, as `views.publish` takes it. */
export interface HomeView {
  type: "home";
  blocks: Block[];
}

export interface HomeRow {
  readonly channelId: string;
  readonly threadTs: string;
  readonly title: string;
  /** The root's reaction name (`Status`); null for a thread that ended before the daemon kept
   * its last reaction. */
  readonly status: string | null;
  readonly replies: number;
  /** The thread's last reply, or its root while it has none: epoch seconds. */
  readonly lastActivity: number;
  readonly permalink: string;
  /** Its thread is being deleted, or waits for the delete before it: the row says so. */
  readonly deleting?: boolean;
}

/** What Slack shows of a thread's root message. */
export interface ThreadFacts {
  readonly replies: number;
  /** Epoch seconds. */
  readonly latestReply: number | null;
  /** The last reply's own `ts`, as Slack wrote it: what a permalink to that message is asked by. */
  readonly latestTs: string | null;
  /** The root's status reaction (`Status`), when it carries one. */
  readonly reaction: string | null;
}

/** Python's `float(text)` for what a root carries: a ValueError for anything that is no number. */
function pyFloat(value: unknown): number {
  if (typeof value === "number") return value;
  const text = String(value).trim();
  if (!/^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/.test(text)) {
    throw new TypeError("not a number");
  }
  return Number(text);
}

/** Python's `int(value)` for a count: the number cut toward zero, or a string of digits. */
function pyInt(value: unknown): number {
  if (typeof value === "number" && Number.isFinite(value)) return Math.trunc(value);
  if (typeof value === "string" && /^\s*[+-]?\d+\s*$/.test(value)) return Number(value);
  throw new TypeError("not a whole number");
}

/**
 * Read a root message as `conversations.replies` returns it. A root nobody replied to has no
 * `reply_count` and no `latest_reply`. Throws on a root in a shape that cannot be read.
 */
export function threadFacts(root: Readonly<Record<string, unknown>>): ThreadFacts {
  const latest = root.latest_reply;
  const reactions = Array.isArray(root.reactions) ? (root.reactions as unknown[]) : [];
  const names = reactions
    .filter(
      (r): r is Record<string, unknown> => typeof r === "object" && r !== null && !Array.isArray(r),
    )
    .map((r) => String(r.name));
  return {
    replies: root.reply_count ? pyInt(root.reply_count) : 0,
    latestReply: latest ? Math.trunc(pyFloat(latest)) : null,
    latestTs: latest ? String(latest) : null,
    reaction: names.find((name) => WORDS.has(name)) ?? null,
  };
}

/**
 * What the owner chose in the page's controls. The page starts on the last 48 hours (the
 * maintainer, 2026-10-01); `date` null is any time, and the other fields unset match all.
 */
export interface HomeFilter {
  readonly channel: string | null;
  readonly status: string | null;
  readonly date: string | null;
  readonly search: string;
}

/** A `HomeFilter` with every field not given at its default. */
export function homeFilter(fields: Partial<HomeFilter> = {}): HomeFilter {
  return { channel: null, status: null, date: LAST_48, search: "", ...fields };
}

/**
 * Whether a channel, a status or a search is chosen: the page then shows every session that
 * matches. The period alone keeps the page's shape, each channel cut to its newest.
 */
export function narrowed(filter: HomeFilter): boolean {
  return Boolean(filter.channel || filter.status || filter.search);
}

/** The day of the machine's own calendar an instant falls on, as a day number. */
function localDay(epochSeconds: number): number {
  const at = new Date(epochSeconds * 1000);
  return Math.floor(Date.UTC(at.getFullYear(), at.getMonth(), at.getDate()) / 86_400_000);
}

/** Python's `str.casefold()`, which `toLowerCase` alone lacks for `ß`, final sigma and the like. */
function casefold(text: string): string {
  return text.toUpperCase().toLowerCase();
}

/**
 * Whether `row` passes every chosen filter. The date is the thread's last reply: `today` and
 * `yesterday` are calendar days of the machine's own time zone, each instant read with the
 * offset in force at that instant (a day a clock change makes 23 or 25 hours long stays one day);
 * the others reach back from now.
 */
export function matches(filter: HomeFilter, row: HomeRow, now: Date): boolean {
  if (filter.channel && row.channelId !== filter.channel) return false;
  if (filter.status && row.status !== filter.status) return false;
  if (filter.search && !casefold(row.title).includes(casefold(filter.search))) return false;
  if (filter.date === TODAY || filter.date === YESTERDAY) {
    const days = localDay(now.getTime() / 1000) - localDay(row.lastActivity);
    return days === (filter.date === TODAY ? 0 : 1);
  }
  const reach = filter.date === null ? undefined : REACH.get(filter.date);
  if (reach !== undefined) return now.getTime() / 1000 - row.lastActivity <= reach;
  return true;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * The filter a use of a control leaves chosen, from the `view.state.values` its payload carries:
 * every control's state rides on it, so nothing is tracked per control. Untrusted like every
 * click, and never throwing on a shape Slack does not send: a status or a date that is not one of
 * the page's own keeps the current one, and the channel is checked by `Home.publish`, which knows
 * the page's own.
 */
export function readFilter(values: unknown, current: HomeFilter): HomeFilter {
  // Found by action id, whatever the block: the blocks' ids change with the choice.
  const controls = new Map<string, unknown>();
  if (isObject(values)) {
    for (const block of Object.values(values)) {
      if (isObject(block))
        for (const [id, control] of Object.entries(block)) controls.set(id, control);
    }
  }

  const picked = (
    actionId: string,
    nowChosen: string | null,
    known: ReadonlyMap<string, string> | null,
  ): string | null => {
    const control = controls.get(actionId);
    if (!isObject(control)) return nowChosen;
    const option = control.selected_option;
    const value = isObject(option) ? option.value : undefined;
    if (value === ALL) return null;
    if (typeof value !== "string" || (known !== null && !known.has(value))) return nowChosen;
    return value;
  };

  let search = current.search;
  const box = controls.get(SEARCH_ACTION);
  if (isObject(box)) {
    const typed = box.value;
    search = typeof typed === "string" ? oneLine(typed, SEARCH_LIMIT) : "";
  }
  return {
    channel: picked(CHANNEL_ACTION, current.channel, null),
    status: picked(STATUS_ACTION, current.status, WORDS),
    date: picked(DATE_ACTION, current.date, DATES),
    search,
  };
}

/** Slack's date markup; the fallback is what a client that cannot render it shows. */
function dateToken(epoch: number, token: string): string {
  const iso = new Date(epoch * 1000).toISOString();
  const fallback = `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC`;
  return `<!date^${epoch}^{${token}}|${fallback}>`;
}

function linkButton(text: string, url: string, actionId: string): Block {
  return { type: "button", action_id: actionId, text: { type: "plain_text", text }, url };
}

/** A menu that starts on `chosen`, or on its `ALL` option when nothing is chosen. */
function select(
  actionId: string,
  options: readonly (readonly [text: string, value: string])[],
  chosen: string | null,
): Block {
  const built = options.map(([text, value]) => ({
    text: { type: "plain_text", text },
    value,
  }));
  const byValue = new Map(built.map((option) => [option.value, option]));
  // A choice the menu does not hold (a channel Slack did not answer about) reads as "all".
  const initial = byValue.get(chosen || ALL) ?? byValue.get(ALL);
  return { type: "static_select", action_id: actionId, options: built, initial_option: initial };
}

/** Python's `repr` of a string: the quote it picks and the escapes it writes. */
function pyStr(text: string): string {
  const quote = text.includes("'") && !text.includes('"') ? '"' : "'";
  let out = quote;
  for (const char of text) {
    const code = char.codePointAt(0) as number;
    if (char === quote || char === "\\") out += `\\${char}`;
    else if (char === "\t") out += "\\t";
    else if (char === "\n") out += "\\n";
    else if (char === "\r") out += "\\r";
    else if (code >= 0x20 && code < 0x7f) out += char;
    else if (code > 0x7f && !/[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}\p{Zs}]/u.test(char)) {
      out += char;
    } else if (code <= 0xff) out += `\\x${code.toString(16).padStart(2, "0")}`;
    else if (code <= 0xffff) out += `\\u${code.toString(16).padStart(4, "0")}`;
    else out += `\\U${code.toString(16).padStart(8, "0")}`;
  }
  return out + quote;
}

/** What Python's dataclass `repr` of the filter read, which the controls' block ids hash. */
function filterRepr(chosen: HomeFilter): string {
  const field = (value: string | null) => (value === null ? "None" : pyStr(value));
  return (
    `HomeFilter(channel=${field(chosen.channel)}, status=${field(chosen.status)}, ` +
    `date=${field(chosen.date)}, search=${pyStr(chosen.search)})`
  );
}

function controlsOf(channels: ReadonlyMap<string, string>, chosen: HomeFilter): Block[] {
  // Slack keeps what a control shows for as long as its block keeps its id, whatever
  // `initial_option` a later page carries (seen 2026-10-01: after a restart the menus still
  // showed the choices of the run before). An id that follows the choice makes the controls
  // show what the page was built with: a restart, Show all and a dropped channel included.
  const mark = createHash("sha256").update(filterRepr(chosen)).digest("hex").slice(0, 8);
  const search: Block = {
    type: "plain_text_input",
    action_id: SEARCH_ACTION,
    placeholder: { type: "plain_text", text: texts.HOME_SEARCH_HINT },
    dispatch_action_config: { trigger_actions_on: ["on_enter_pressed"] },
  };
  if (chosen.search) search.initial_value = chosen.search;
  return [
    {
      type: "actions",
      block_id: `${FILTERS_BLOCK}:${mark}`,
      elements: [
        select(
          CHANNEL_ACTION,
          [
            [texts.HOME_ALL_CHANNELS, ALL],
            ...[...channels]
              .slice(0, CHANNEL_OPTIONS)
              .map(([id, name]): [string, string] => [oneLine(name, OPTION_TEXT), id]),
          ],
          chosen.channel,
        ),
        select(
          STATUS_ACTION,
          [
            [texts.HOME_ALL_STATUSES, ALL],
            ...[...WORDS].map(([name, word]): [string, string] => [capitalize(word), name]),
          ],
          chosen.status,
        ),
        select(
          DATE_ACTION,
          [
            ...[...DATES].map(([key, text]): [string, string] => [text, key]),
            [texts.HOME_ANY_TIME, ALL],
          ],
          chosen.date,
        ),
      ],
    },
    {
      type: "input",
      block_id: `${SEARCH_BLOCK}:${mark}`,
      dispatch_action: true,
      label: { type: "plain_text", text: texts.HOME_SEARCH_LABEL },
      element: search,
    },
  ];
}

/** Python's `str.capitalize()`: the first character upper case, the rest lower case. */
function capitalize(text: string): string {
  const [first = "", ...rest] = Array.from(text);
  return first.toUpperCase() + rest.join("").toLowerCase();
}

function section(text: string): Block {
  return { type: "section", text: { type: "mrkdwn", text } };
}

function context(text: string): Block {
  return { ...contextBlock(text) };
}

function deletingLine(count: number): string {
  return count === 1 ? texts.HOME_DELETING_ONE : texts.fill(texts.HOME_DELETING_MANY, { count });
}

function cleaningLine(count: number): string {
  return count === 1 ? texts.HOME_CLEANING_ONE : texts.fill(texts.HOME_CLEANING_MANY, { count });
}

/**
 * The line above the sessions. It carries the Edit button when threads can be deleted: a context
 * block holds no button (context block reference), so the line is a section then.
 */
function header(time: string, editing: boolean, canEdit: boolean): Block {
  const text = texts.fill(texts.HOME_HEADER, { time });
  if (!canEdit) return context(text);
  const button: Block = {
    type: "button",
    action_id: EDIT_ACTION,
    text: plainTextObject(editing ? texts.HOME_DONE : texts.HOME_EDIT),
    value: editing ? EDIT_OFF : EDIT_ON,
  };
  if (editing) button.style = "primary";
  return { type: "section", text: { type: "mrkdwn", text }, accessory: button };
}

/** A channel's Clean up, with a confirmation dialog that says what it deletes. */
function cleanButton(channelId: string, channelName: string): Block {
  const room = CONFIRM_TEXT - Array.from(texts.fill(texts.HOME_CLEAN_TEXT, { channel: "" })).length;
  return {
    type: "button",
    action_id: CLEAN_ACTION,
    text: plainTextObject(texts.HOME_CLEAN),
    value: channelId,
    confirm: {
      title: plainTextObject(texts.HOME_CLEAN_TITLE),
      text: plainTextObject(
        texts.fill(texts.HOME_CLEAN_TEXT, { channel: oneLine(channelName, room) }),
      ),
      confirm: plainTextObject(texts.HOME_CLEAN_CONFIRM),
      deny: plainTextObject(texts.HOME_DELETE_DENY),
      style: "danger",
    },
  };
}

interface ChannelHeaderOptions {
  editing: boolean;
  deleting?: number;
  name?: string;
  canClean?: boolean;
  cleaning?: boolean;
}

/**
 * A channel's name, with how many of its threads are being deleted beside it: the rows that say
 * so can be among those the channel does not show. Its button is New thread, and in edit mode
 * Clean up, which gives way to a note while the channel is being cleaned.
 */
function channelHeader(teamId: string, channelId: string, options: ChannelHeaderOptions): Block {
  const { editing, deleting = 0, name = "", canClean = false, cleaning = false } = options;
  let text = `*<#${channelId}>*`;
  if (cleaning) text += `   ${texts.HOME_CHANNEL_CLEANING}`;
  if (deleting === 1) text += `   ${texts.HOME_CHANNEL_DELETING_ONE}`;
  else if (deleting) {
    text += `   ${texts.fill(texts.HOME_CHANNEL_DELETING_MANY, { count: deleting })}`;
  }
  const head: Block = { type: "section", text: { type: "mrkdwn", text } };
  if (!editing) {
    head.accessory = linkButton(
      texts.HOME_NEW_THREAD,
      `slack://channel?team=${teamId}&id=${channelId}`,
      NEW_THREAD_ACTION,
    );
  } else if (canClean && !cleaning) {
    head.accessory = cleanButton(channelId, name);
  }
  return head;
}

function repliesOf(row: HomeRow): string {
  return row.replies === 1
    ? texts.HOME_REPLY
    : texts.fill(texts.HOME_REPLIES, { count: row.replies });
}

/**
 * The Delete of a session's row, with Slack's own confirmation dialog, which names the thread.
 * The dialog shows before the click is sent (confirmation dialog object reference).
 */
function deleteButton(row: HomeRow, channelName: string): Block {
  const replies = repliesOf(row);
  const named = row.replies ? texts.HOME_DELETE_NAMED : texts.HOME_DELETE_NAMED_BARE;
  let room = CONFIRM_TEXT - Array.from(texts.HOME_DELETE_TEXT).length;
  room -= Array.from(texts.fill(named, { title: "", channel: channelName, replies })).length;
  const title = oneLine(row.title, Math.max(room, 1));
  return {
    type: "button",
    action_id: DELETE_ACTION,
    style: "danger",
    text: plainTextObject(texts.HOME_DELETE),
    value: `${row.channelId}:${row.threadTs}`,
    confirm: {
      title: plainTextObject(texts.HOME_DELETE_TITLE),
      text: plainTextObject(
        texts.fill(named, { title, channel: channelName, replies }) + texts.HOME_DELETE_TEXT,
      ),
      confirm: plainTextObject(texts.HOME_DELETE_CONFIRM),
      deny: plainTextObject(texts.HOME_DELETE_DENY),
      style: "danger",
    },
  };
}

function card(row: HomeRow, channelName: string, editing: boolean): Block[] {
  const icon = row.status ? `:${row.status}:  ` : "";
  const title = shownAsWritten(oneLine(row.title, TITLE_LIMIT));
  const when = row.replies ? texts.HOME_LAST_REPLY : texts.HOME_STARTED;
  let word: string | null = null;
  if (row.deleting) word = texts.HOME_DELETING;
  else if (row.status) word = WORDS.get(row.status) ?? null;
  const details = [
    word,
    row.replies ? repliesOf(row) : null,
    texts.fill(when, { when: dateToken(row.lastActivity, "ago") }),
    // Open is a link in the small line, not a button: a button sits at the far right of the
    // title's row, and a row that carries one cannot be small (the maintainer, 2026-10-01).
    // A reply's permalink carries a query (`?thread_ts=…&cid=…`), and mrkdwn reads `&` as markup.
    `<${mrkdwnEscape(row.permalink)}|${texts.HOME_OPEN}>`,
  ];
  const head = section(`${icon}*${title}*`);
  if (editing && !row.deleting && !(row.status !== null && IN_USE.has(row.status))) {
    head.accessory = deleteButton(row, channelName);
  }
  return [head, context(details.filter((d) => d).join(" · "))];
}

export interface HomeViewOptions {
  readonly teamId: string;
  readonly chosen: HomeFilter;
  readonly now: Date;
  readonly canEdit?: boolean;
  readonly editing?: boolean;
  readonly notices?: Iterable<string>;
  readonly deleting?: number;
  readonly canClean?: boolean;
  readonly cleaning?: ReadonlySet<string>;
}

/**
 * The Home tab's view. `rows` are newest first and `channels` maps each bound channel Slack still
 * has to its name. Only the sessions of the chosen period are shown. With no channel, status or
 * search chosen, every channel is a group, the ones with sessions first by their newest, each
 * showing its `PER_CHANNEL` newest and a button to see them all (which chooses that channel);
 * otherwise only what matches, with no such cut. Never past Slack's 100 blocks: the page says
 * when it stops short. With `canEdit` the header line carries Edit; in `editing` each session
 * that is not in use carries Delete and no channel carries New thread. `notices` are the lines
 * the deletes and clean-ups that did not end left, under the header; `deleting` is how many
 * threads are being deleted or wait for it, said in a line of full size under the header, since a
 * row that says so can be one of those a channel does not show.
 */
export function homeView(
  rows: readonly HomeRow[],
  channels: ReadonlyMap<string, string>,
  options: HomeViewOptions,
): HomeView {
  const { teamId, chosen, now } = options;
  const { canEdit = false, deleting = 0, canClean = false } = options;
  const cleaning = options.cleaning ?? new Set<string>();
  const editing = (options.editing ?? false) && canEdit;
  const blocks: Block[] = [
    ...controlsOf(channels, chosen),
    header(dateToken(Math.trunc(now.getTime() / 1000), "time"), editing, canEdit),
    ...(deleting ? [section(deletingLine(deleting))] : []),
    ...(cleaning.size > 0 ? [section(cleaningLine(cleaning.size))] : []),
    ...[...(options.notices ?? [])].slice(0, NOTICES_SHOWN).map(context),
  ];
  if (channels.size === 0) return { type: "home", blocks: [...blocks, context(texts.HOME_EMPTY)] };
  const groups = new Map<string, HomeRow[]>();
  const group = (channelId: string): HomeRow[] => {
    let found = groups.get(channelId);
    if (found === undefined) {
      found = [];
      groups.set(channelId, found);
    }
    return found;
  };
  for (const row of rows) {
    if (matches(chosen, row, now)) group(row.channelId).push(row);
  }
  if (chosen.channel) group(chosen.channel);
  else if (!narrowed(chosen)) {
    for (const channelId of channels.keys()) group(channelId);
  }
  if (groups.size === 0) blocks.push(context(texts.HOME_NO_MATCH));
  const withSessions = new Set(rows.map((row) => row.channelId));
  // Counted over every session of the channel, shown or not, whatever the filters.
  const going = new Map<string, number>();
  for (const row of rows) {
    if (row.deleting) going.set(row.channelId, (going.get(row.channelId) ?? 0) + 1);
  }
  const wanted = new Map(
    [...groups].map(([id, found]) => [id, narrowed(chosen) ? found : found.slice(0, PER_CHANNEL)]),
  );
  let shown = 0;
  for (const [channelId, found] of groups) {
    const cards = wanted.get(channelId) ?? [];
    // One block is kept for the line that says the page stops short; a group is a divider, a
    // header, `CARD_BLOCKS` a card at most, and one closing block at most.
    const room = Math.floor((HOME_BLOCKS - 1 - blocks.length - 3) / CARD_BLOCKS);
    if (room < Math.min(1, cards.length) || HOME_BLOCKS - 1 - blocks.length < 3) break;
    const name = channels.get(channelId) ?? "";
    blocks.push(
      { type: "divider" },
      channelHeader(teamId, channelId, {
        editing,
        deleting: going.get(channelId) ?? 0,
        name,
        canClean,
        cleaning: cleaning.has(channelId),
      }),
    );
    cards.slice(0, room).forEach((row, index) => {
      if (index) blocks.push(context(SPACER));
      blocks.push(...card(row, name, editing));
    });
    shown += Math.min(cards.length, room);
    if (cards.length > room) break;
    if (found.length === 0) {
      // A channel with sessions, none of them in the period or under the filters.
      const hidden = withSessions.has(channelId);
      blocks.push(context(hidden ? texts.HOME_NO_MATCH : texts.HOME_NO_SESSIONS));
    } else if (found.length > cards.length) {
      const showAll = {
        type: "button",
        action_id: SHOW_ALL_ACTION,
        text: {
          type: "plain_text",
          text: texts.fill(texts.HOME_SHOW_ALL, { count: found.length }),
        },
        value: channelId,
      };
      blocks.push({ type: "actions", elements: [showAll] });
    }
  }
  // Said only when a session is left out: channels with none can fall off the end unsaid.
  let wantedCards = 0;
  for (const cards of wanted.values()) wantedCards += cards.length;
  if (shown < wantedCards) blocks.push(context(texts.fill(texts.HOME_MORE, { rows: shown })));
  return { type: "home", blocks };
}

/** Whether Slack answered that the channel or the message is not there any more. */
function gone(error: unknown): boolean {
  return error instanceof WebAPIPlatformError && GONE.has(describe(error));
}

/** A root that `conversations.replies` did not return among its messages. */
class RootNotReturned extends Error {
  constructor() {
    super("the root is not among the messages");
    this.name = "RootNotReturned";
  }
}

/** `asyncio.Semaphore`: at most `limit` holders, the waiters served in the order they came. */
class Semaphore {
  private free: number;
  private readonly waiters: Array<() => void> = [];

  constructor(limit: number) {
    this.free = limit;
  }

  /** Resolves with the function that gives the place back, once there is one. */
  async acquire(): Promise<() => void> {
    if (this.free > 0) this.free -= 1;
    else await new Promise<void>((resolve) => this.waiters.push(resolve));
    let released = false;
    return () => {
      if (released) return;
      released = true;
      // Handed straight to the next in line.
      const next = this.waiters.shift();
      if (next === undefined) this.free += 1;
      else next();
    };
  }
}

const keyOf = (channelId: string, threadTs: string): string => `${channelId}:${threadTs}`;

export interface HomeOptions {
  readonly ownerUserId: string;
  readonly teamId: string;
  readonly state: StateStore;
  /** The sessions Claude Code lists for a folder (the seam's `listSessions`). */
  readonly sessionsOf: (
    directory: string,
  ) => readonly ListedSession[] | Promise<readonly ListedSession[]>;
  /**
   * Deletes a thread and answers the line to show when it could not (`ThreadDeleter.delete`).
   * Absent: no Edit button, the page as it always was.
   */
  readonly delete?: (channelId: string, threadTs: string) => Promise<string | null>;
  /** Cleans a channel up (`ThreadDeleter.clean`), and answers the line to show when it could not. */
  readonly clean?: (channelId: string) => Promise<string | null>;
  readonly debounce?: number;
  readonly clock?: Clock;
}

/**
 * Publishes the owner's Home tab. `request` asks for a publish soon and returns at once (a state
 * write calls it, naming the threads it changed); `choose` sets the filters and publishes now (a
 * control was used); `publish` builds the page and sends it, one at a time, and never throws: the
 * page must never break a turn. Always published to the configured owner, whoever opens the app.
 * `not_enabled` (the Home tab is off in the Slack app's settings) is logged once and ends the
 * publishing for this run. Any other failure is logged by its code, and a page that Slack did not
 * fully answer for is tried again after `RETRY_SECONDS`.
 */
export class Home {
  private readonly slack: WebClient;
  private readonly owner: string;
  private readonly team: string;
  private readonly state: StateStore;
  private readonly sessionsOf: HomeOptions["sessionsOf"];
  private readonly deleteThread: HomeOptions["delete"];
  private readonly cleanChannel: HomeOptions["clean"];
  private readonly debounce: number;
  private readonly clock: Clock;
  // Kept in memory like the filters: a restart starts out of edit mode.
  private editing = false;
  // The filters as they were when edit mode was entered: Done brings them back.
  private beforeEdit: HomeFilter | null = null;
  // What each delete or clean-up that did not end left to say, by the thread (`channel:thread`)
  // or the channel it was for, the latest first: one's result never erases another's. A new try
  // at the same one, and leaving or entering edit mode, clear it.
  private notices = new Map<string, string>();
  // Each listed thread's title, for the line that names it.
  private labels = new Map<string, string>();
  // The threads a delete was asked for and has not ended: Slack's rate limit makes one take from
  // seconds to minutes, and the page says so from the click on.
  private readonly deleting = new Set<string>();
  // The channels a clean-up is on its way for.
  private readonly cleaning = new Set<string>();
  private task: Task<void> | null = null;
  // Whether the debounced loop is running: set the moment it ends, which `Task.done` is a turn
  // of the microtask queue late in saying.
  private settling = false;
  private retry: Task<void> | null = null;
  private dirty = false;
  private off = false;
  // Whether the page being built lacks something Slack gave no answer about.
  private incomplete = false;
  private current: HomeFilter = homeFilter();
  // One publish at a time, each built inside the lock: the one that lands last was built last,
  // so a filter just chosen is never overwritten by an older page.
  private readonly publishing = new Mutex();
  private readonly asking = new Semaphore(THREADS_AT_ONCE);
  // Each thread's permalink, with the `ts` of the message it opens: the thread's last reply, or
  // its root while it has none. Asked again only once that `ts` changed. A null link: Slack no
  // longer has that message (`GONE`).
  private readonly links = new Map<string, [ts: string, link: string | null]>();
  // A channel's name, asked once per run. Null: Slack no longer has the channel, or the bot left
  // it (`GONE`): the channel and its threads are left out of the page.
  private readonly names = new Map<string, string | null>();
  // What Slack showed of each thread's root. Null: the root is gone (`GONE`).
  private readonly facts = new Map<string, ThreadFacts | null>();
  // The threads a state write changed since their root was read: read again at the next
  // publish, which every turn causes at its start and at its end.
  private readonly stale = new Set<string>();

  constructor(slack: WebClient, options: HomeOptions) {
    this.slack = slack;
    this.owner = options.ownerUserId;
    this.team = options.teamId;
    this.state = options.state;
    this.sessionsOf = options.sessionsOf;
    this.deleteThread = options.delete;
    this.cleanChannel = options.clean;
    this.debounce = options.debounce ?? DEBOUNCE_SECONDS;
    this.clock = options.clock ?? systemClock;
  }

  /** The filters the page is built with. */
  get chosen(): HomeFilter {
    return this.current;
  }

  /** Ask for a publish soon; `changed` are the (channel, thread) a state write touched. */
  request(changed: Iterable<ThreadKey> = []): void {
    if (this.off) return;
    for (const [channelId, threadTs] of changed) this.stale.add(keyOf(channelId, threadTs));
    this.dirty = true;
    if (!this.settling) {
      this.settling = true;
      this.task = new Task((signal) => this.publishWhenSettled(signal));
    }
  }

  private async publishWhenSettled(signal: AbortSignal): Promise<void> {
    try {
      while (this.dirty && !this.off) {
        await this.clock.sleep(this.debounce, signal);
        this.dirty = false;
        await this.publish(signal);
      }
    } finally {
      this.settling = false;
    }
  }

  private async requestLater(signal: AbortSignal): Promise<void> {
    await this.clock.sleep(RETRY_SECONDS, signal);
    this.request();
  }

  /**
   * Set the filters and publish at once. A channel that is not one of the page's own (not bound,
   * or gone from Slack) is no filter: `publish` drops it.
   */
  async choose(chosen: HomeFilter): Promise<void> {
    this.current = chosen;
    await this.publish();
  }

  /**
   * Enter or leave edit mode and publish at once. Edit mode is a parenthesis: a filter chosen
   * inside it (Show all on the channel being cleaned, most often) lasts while it does, and
   * leaving it brings back the filters the page had on entering.
   */
  async edit(on: boolean): Promise<void> {
    const wanted = on && this.deleteThread !== undefined;
    if (wanted && !this.editing) {
      this.beforeEdit = this.current;
    } else if (!wanted && this.editing && this.beforeEdit !== null) {
      this.current = this.beforeEdit;
      this.beforeEdit = null;
    }
    this.editing = wanted;
    this.notices.clear();
    await this.publish();
  }

  /**
   * Delete a thread the page lists. Publishes at once with the row marked as being deleted, then
   * again when the delete ended: without the row, or with the line that says why it is still
   * there. A second click on the same thread while it runs does nothing. The page stays in edit
   * mode.
   */
  async delete(channelId: string, threadTs: string): Promise<void> {
    const key = keyOf(channelId, threadTs);
    if (this.deleteThread === undefined || !this.editing || this.deleting.has(key)) return;
    this.deleting.add(key);
    this.notices.delete(key);
    await this.publish();
    let notice: string | null;
    try {
      notice = await this.deleteThread(channelId, threadTs);
    } finally {
      this.deleting.delete(key);
    }
    if (notice) {
      const label = this.labels.get(key);
      this.say(key, label ? `“${label}”: ${notice}` : notice);
    }
    // A delete that stopped half way changed the thread: its root is read again.
    this.stale.add(key);
    await this.publish();
  }

  /** Keep a notice under `name`, ahead of the ones already kept. */
  private say(name: string, notice: string): void {
    this.notices = new Map([
      [name, notice],
      ...[...this.notices].filter(([kept]) => kept !== name),
    ]);
  }

  /**
   * Clean up a bound channel, as `delete` deletes a thread: the page says so at once, and again
   * when it ended; a second click while it runs does nothing.
   */
  async clean(channelId: string): Promise<void> {
    if (this.cleanChannel === undefined || !this.editing || this.cleaning.has(channelId)) return;
    if (!this.state.channels().includes(channelId)) return; // the value of a click is untrusted
    this.cleaning.add(channelId);
    this.notices.delete(channelId);
    await this.publish();
    let notice: string | null;
    try {
      notice = await this.cleanChannel(channelId);
    } finally {
      this.cleaning.delete(channelId);
    }
    if (notice) {
      const label = this.names.get(channelId);
      this.say(channelId, label ? `#${label}: ${notice}` : notice);
    }
    await this.publish();
  }

  /**
   * Publish what a pending request still owed, then stop: the page a stop leaves behind shows the
   * sessions as the stop left them. Gives up after `CLOSE_SECONDS`.
   */
  async close(): Promise<void> {
    const [task, retry] = [this.task, this.retry];
    this.task = null;
    this.retry = null;
    // Waiting out its debounce or cut in the middle of a publish: owed either way.
    const owed = this.dirty || this.settling;
    for (const pending of [task, retry]) {
      if (pending !== null) {
        pending.cancel();
        await pending.settled();
      }
    }
    if (owed) {
      const limit = new AbortController();
      const publishing = this.publish(limit.signal).catch(() => undefined);
      const expired = this.clock.sleep(CLOSE_SECONDS, limit.signal).catch(() => undefined);
      // A Slack that does not answer is not waited for: the publish is told to stop and the
      // daemon goes on, since a stop never hangs on the page.
      await Promise.race([publishing, expired]);
      limit.abort(new Cancelled());
    }
    this.off = true;
  }

  /** Build the page and send it. Never throws, unless `signal` aborted it. */
  async publish(signal?: AbortSignal): Promise<void> {
    if (this.off) return;
    const release = await this.publishing.acquire(signal);
    try {
      this.incomplete = false;
      try {
        await this.publishPage(signal);
      } catch (error) {
        if (signal?.aborted) throw signal.reason;
        const code = describe(error);
        if (code === NOT_ENABLED) {
          this.off = true;
          logger.warning(
            "the Home tab is not enabled in the Slack app (docs/setup.md): the session index " +
              "is not published this run",
          );
          return;
        }
        logger.warning(`could not publish the session index: ${code}`);
        this.incomplete = true;
      }
      if (this.incomplete && (this.retry === null || this.retry.done)) {
        this.retry = new Task((retrySignal) => this.requestLater(retrySignal));
      }
    } finally {
      release();
    }
  }

  private async publishPage(signal?: AbortSignal): Promise<void> {
    const bound = this.state.channels();
    const channels = await this.channelsOf(bound, signal);
    if (bound.length > 0 && channels.size === 0 && this.incomplete) {
      return; // Slack answered about no channel: the page stays as it is until it does
    }
    const rows = await this.rowsOf(channels, signal);
    // Untrusted when it came from a click, and a chosen channel can go away. One Slack merely
    // did not answer about is still the owner's choice.
    const chosen = this.current.channel;
    if (chosen && (!bound.includes(chosen) || this.names.get(chosen) === null)) {
      this.current = { ...this.current, channel: null };
    }
    const view = homeView(rows, channels, {
      teamId: this.team,
      chosen: this.current,
      now: new Date(this.clock.time() * 1000),
      canEdit: this.deleteThread !== undefined,
      editing: this.editing,
      notices: [...this.notices.values()],
      deleting: this.deleting.size,
      canClean: this.cleanChannel !== undefined,
      cleaning: new Set(this.cleaning),
    });
    signal?.throwIfAborted();
    // The shape is the one the Python daemon sent; `@slack/types` declares it as `HomeView`.
    await this.slack.views.publish({
      user_id: this.owner,
      view: view as unknown as Parameters<WebClient["views"]["publish"]>[0]["view"],
    });
  }

  /** Every bound channel Slack still has, with its name, in the order they were bound. */
  private async channelsOf(bound: string[], signal?: AbortSignal): Promise<Map<string, string>> {
    const found = new Map<string, string>();
    // A channel forgotten and bound again later is asked about again.
    for (const unbound of [...this.names.keys()]) {
      if (!bound.includes(unbound)) this.names.delete(unbound);
    }
    for (const channelId of bound) {
      signal?.throwIfAborted();
      if (!this.names.has(channelId)) {
        try {
          const info = await this.slack.conversations.info({ channel: channelId });
          const name = info.channel?.name;
          if (name === undefined) throw new TypeError("the channel has no name");
          this.names.set(channelId, String(name));
        } catch (error) {
          logger.warning(`could not read channel ${channelId}: ${describe(error)}`);
          if (!gone(error)) {
            this.incomplete = true;
            continue;
          }
          this.names.set(channelId, null);
        }
      }
      const name = this.names.get(channelId);
      if (name !== null && name !== undefined) found.set(channelId, name);
    }
    return found;
  }

  /**
   * One row per thread that holds a session in one of `channels` and that Slack still has (a
   * thread whose root is gone cannot be opened: it is left out), last reply first.
   */
  private async rowsOf(
    channels: ReadonlyMap<string, string>,
    signal?: AbortSignal,
  ): Promise<HomeRow[]> {
    const threads = this.state.threads();
    // What is kept per thread goes with the thread: a pruned one leaves nothing behind.
    const existing = new Set(threads.map(([channelId, threadTs]) => keyOf(channelId, threadTs)));
    for (const key of [...this.stale]) if (!existing.has(key)) this.stale.delete(key);
    for (const cache of [this.links, this.facts]) {
      for (const key of [...cache.keys()]) if (!existing.has(key)) cache.delete(key);
    }
    const held = threads.filter(
      ([channelId, , thread]) => thread.sessionId !== null && channels.has(channelId),
    );
    const titles = await this.titlesOf(new Set(held.map(([, , thread]) => thread.directory)));
    const seen = await Promise.all(
      held.map(([channelId, threadTs]) => this.inSlack(channelId, threadTs, signal)),
    );
    const rows: HomeRow[] = [];
    held.forEach(([channelId, threadTs, thread], index) => {
      const [link, facts] = seen[index] as [string | null, ThreadFacts | null];
      if (link === null || facts === null) return;
      const sessionId = String(thread.sessionId);
      rows.push({
        channelId,
        threadTs,
        title:
          titles.get(sessionId) ||
          texts.fill(texts.HOME_UNTITLED, { id: take(sessionId, ID_SHOWN) }),
        // ❌ over a kept ⏳ or ✋ (an answer that never reached Slack) shows ❌. A thread that
        // ended before the daemon kept its reaction shows the root's.
        status: thread.ended || thread.status || facts.reaction,
        replies: facts.replies,
        lastActivity: facts.latestReply || Math.trunc(pyFloat(threadTs)),
        permalink: link,
        deleting: this.deleting.has(keyOf(channelId, threadTs)),
      });
    });
    rows.sort((a, b) => b.lastActivity - a.lastActivity);
    this.labels = new Map(
      rows.map((row) => [
        keyOf(row.channelId, row.threadTs),
        shownAsWritten(oneLine(row.title, TITLE_LIMIT)),
      ]),
    );
    return rows;
  }

  /**
   * The title Claude Code gives each session of `directories`, by session id. A folder that
   * cannot be listed is logged and skipped: its sessions show their id.
   */
  private async titlesOf(directories: ReadonlySet<string>): Promise<Map<string, string>> {
    const found = new Map<string, string>();
    for (const directory of directories) {
      try {
        const listed = await this.sessionsOf(directory);
        for (const session of listed) found.set(session.id, session.title);
      } catch (error) {
        logger.warning(
          `could not list a folder's sessions for the session index: ${describe(error)}`,
        );
      }
    }
    return found;
  }

  /**
   * A thread's permalink and what Slack shows of its root; either is null for a thread the page
   * leaves out.
   */
  private async inSlack(
    channelId: string,
    threadTs: string,
    signal?: AbortSignal,
  ): Promise<[string | null, ThreadFacts | null]> {
    const release = await this.asking.acquire();
    try {
      signal?.throwIfAborted();
      const facts = await this.root(channelId, threadTs);
      if (facts === null) return [null, null];
      signal?.throwIfAborted();
      return [await this.permalink(channelId, threadTs, facts.latestTs ?? threadTs), facts];
    } finally {
      release();
    }
  }

  private async root(channelId: string, threadTs: string): Promise<ThreadFacts | null> {
    const key = keyOf(channelId, threadTs);
    if (this.facts.has(key) && !this.stale.has(key)) return this.facts.get(key) ?? null;
    let facts: ThreadFacts;
    try {
      const answer = await this.slack.conversations.replies({
        channel: channelId,
        ts: threadTs,
        limit: 1,
      });
      const messages: unknown[] = Array.isArray(answer.messages) ? answer.messages : [];
      const root = messages.find((m) => isObject(m) && String(m.ts) === threadTs);
      if (!isObject(root)) throw new RootNotReturned();
      facts = threadFacts(root);
    } catch (error) {
      // A root Slack returns in a shape this cannot read counts as no answer too.
      logger.warning(`could not read thread ${channelId}/${threadTs}: ${describe(error)}`);
      if (gone(error)) {
        this.stale.delete(key);
        this.facts.set(key, null);
        return null;
      }
      // No answer: what was read before stands, and the thread is asked about again.
      this.incomplete = true;
      return this.facts.get(key) ?? null;
    }
    this.stale.delete(key);
    this.facts.set(key, facts);
    return facts;
  }

  /**
   * The permalink that opens the thread on `messageTs`: Slack scrolls a thread to the reply its
   * permalink names (seen in the Mac app and on iOS, 2026-10-05).
   */
  private async permalink(
    channelId: string,
    threadTs: string,
    messageTs: string,
  ): Promise<string | null> {
    const key = keyOf(channelId, threadTs);
    const kept = this.links.get(key);
    if (kept !== undefined && kept[0] === messageTs) return kept[1];
    let link: string | null = null;
    try {
      const answer = await this.slack.chat.getPermalink({
        channel: channelId,
        message_ts: messageTs,
      });
      if (answer.permalink === undefined) throw new TypeError("no permalink");
      link = String(answer.permalink);
    } catch (error) {
      logger.warning(`could not get a permalink for ${channelId}/${messageTs}: ${describe(error)}`);
      if (!gone(error)) {
        this.incomplete = true;
        return null;
      }
    }
    this.links.set(key, [messageTs, link]);
    return link;
  }
}
