/**
 * The picker `!open` shows: the message with its button, the modal with its search field and its
 * rows, what Slack sends back from it, and the order its updates are written in.
 */
import { createHash } from "node:crypto";
import type { KnownBlock, ModalView, Option } from "@slack/web-api";
import * as texts from "../../../core/texts.ts";
import { fill } from "../../../core/texts.ts";
import { contextBlock, plainTextObject } from "../reply/blocks.ts";
import { len, take } from "../reply/chars.ts";
import { shownAsWritten } from "../reply/escape.ts";
import { Gate } from "./waiting.ts";

// The button that opens the picker's modal (in the `!open` message and in the one of several
// matches), the modal's callback id, and the ids of its two input blocks. Slack keeps the state
// of an input block whose ids do not change across `views.update` (reference, read 2026-10-05
// and 2026-10-06), so the search field keeps its ids, to keep what was typed, and the rows' block
// id, which is CHOICE_BLOCK and a mark of the rows it holds, changes with them, to drop a row
// chosen among others.
export const OPEN_BUTTON_ACTION = "open_choose";
export const OPEN_FORM = "open_form";
export const QUERY_BLOCK = "open_query_block";
export const QUERY_ACTION = "open_query";
export const CHOICE_BLOCK = "open_choice_block";
export const CHOICE_ACTION = "open_choice";
// Block Kit's limits, read 2026-10-05: a radio button group holds 10 options (radio button group
// element); an option's `text` and its `description` 75 characters, its `value` 150 (option
// object); a modal's `private_metadata` 3000 characters, its title, submit and close text 24, and
// it holds 100 blocks (modal views).
export const ROW_LIMIT = 10;
export const TEXT_LIMIT = 75;
export const DESCRIPTION_LIMIT = 75;
export const VALUE_LIMIT = 150;
// The words a search carries: typed in the field, or in a button's value from `!open <words>`.
export const QUERY_LIMIT = 200;
// A click waits until this long (seconds) after it arrived for the rows before the modal opens
// without them: the click's `trigger_id` lives 3 seconds (views.open reference, read 2026-10-05),
// which the channel check, this wait and `views.open` share, and the rows are put in by an update
// when they come later.
export const OPEN_WAIT = 1.0;
// MODALS_KEPT modals are tracked at most (`ModalUpdates`).
export const MODALS_KEPT = 16;

type Row = Option;

/** `text` cut in its middle with `…` to `room` characters, its end kept longer than its start so an extension stays. */
function middle(text: string, room: number): string {
  const characters = Array.from(text);
  if (characters.length <= room) return text;
  const end = Math.floor((room - 1) / 2) + ((room - 1) % 2);
  return `${characters.slice(0, room - 1 - end).join("")}…${characters.slice(characters.length - end).join("")}`;
}

/** Whether a path can be an option's value; the file stays reachable by `!open <path>`. */
export function fits(path: string): boolean {
  return len(path) <= VALUE_LIMIT;
}

/**
 * A row for a path: the file's name as plain text, so that nothing in it is read as markup or as
 * an emoji (shortened in its middle past TEXT_LIMIT), its folder below it (shortened from the left
 * past DESCRIPTION_LIMIT, left out for a file at the root of the session's folder), the path
 * itself as the value. Null when the value cannot fit.
 */
export function option(path: string): Row | null {
  if (!fits(path)) return null;
  const slash = path.lastIndexOf("/");
  const folder = slash < 0 ? "" : path.slice(0, slash);
  const name = path.slice(slash + 1);
  const row: Row = {
    text: plainTextObject(middle(name, TEXT_LIMIT), false),
    value: path,
  };
  if (folder) {
    const shown =
      len(folder) <= DESCRIPTION_LIMIT
        ? folder
        : `…${Array.from(folder)
            .slice(-(DESCRIPTION_LIMIT - 1))
            .join("")}`;
    row.description = plainTextObject(shown);
  }
  return row;
}

/** The first ROW_LIMIT paths that make a row. */
export function options(paths: Iterable<string>): Row[] {
  const found: Row[] = [];
  for (const path of paths) {
    if (found.length >= ROW_LIMIT) break;
    const shown = option(path);
    if (shown !== null) found.push(shown);
  }
  return found;
}

/** Metadata that `Target.dump` did not write. */
export class BadTarget extends Error {
  constructor(reason: string) {
    super(reason);
    this.name = "BadTarget";
  }
}

/**
 * The thread a modal belongs to, carried by the modal itself (`private_metadata`). It comes back
 * from Slack and is untrusted: the handlers resolve it to the folder of that thread's own session
 * and refuse anything else.
 */
export class Target {
  readonly channel: string;
  readonly threadTs: string;

  constructor(channel: string, threadTs: string) {
    this.channel = channel;
    this.threadTs = threadTs;
  }

  dump(): string {
    return JSON.stringify({ c: this.channel, t: this.threadTs });
  }

  /** Throws `BadTarget` for anything `dump` did not write. */
  static load(text: unknown): Target {
    if (typeof text !== "string") throw new BadTarget("no metadata");
    let data: unknown;
    try {
      data = JSON.parse(text);
    } catch {
      throw new BadTarget("not json");
    }
    if (!isRecord(data)) throw new BadTarget("not an object");
    const { c: channel, t: threadTs } = data;
    if (typeof channel !== "string" || typeof threadTs !== "string") {
      throw new BadTarget("no thread");
    }
    return new Target(channel, threadTs);
  }
}

type Loose = Record<string, unknown>;

function isRecord(value: unknown): value is Loose {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function own(record: Loose, key: string): unknown {
  return Object.hasOwn(record, key) ? record[key] : undefined;
}

/** The state of one element in a view's `state.values` (block id, then action id), `{}` for any shape Slack does not send. */
function field(values: unknown, block: string, action: string): Loose {
  const found = isRecord(values) ? own(values, block) : undefined;
  const element = isRecord(found) ? own(found, action) : undefined;
  return isRecord(element) ? element : {};
}

/** The text in the search field, from a view's `state.values`; `""` when there is none. */
export function typedIn(values: unknown): string {
  const value = field(values, QUERY_BLOCK, QUERY_ACTION).value;
  return typeof value === "string" ? value : "";
}

/** The block of rows in a view's own `blocks`; null when the view shows none. */
export function choiceBlock(view: unknown): Loose | null {
  const blocks = isRecord(view) ? view.blocks : undefined;
  for (const block of Array.isArray(blocks) ? blocks : []) {
    if (
      isRecord(block) &&
      typeof block.block_id === "string" &&
      block.block_id.startsWith(`${CHOICE_BLOCK}:`)
    ) {
      return block;
    }
  }
  return null;
}

/**
 * The value of the row chosen in a view as Slack sends it back (`blocks` and `state.values`), null
 * when none is. Only a row of the block the view shows counts: the state of a block with an id
 * that is gone, or a value that is not among its options, is a choice made among other rows.
 */
export function chosenIn(view: unknown): string | null {
  const block = choiceBlock(view);
  if (!isRecord(view) || block === null) return null;
  const state = view.state;
  const values = isRecord(state) ? state.values : undefined;
  const chosen = field(values, block.block_id as string, CHOICE_ACTION).selected_option;
  const value = isRecord(chosen) ? chosen.value : undefined;
  const element = block.element;
  const shown = isRecord(element) ? element.options : undefined;
  const offered = new Set(
    Array.isArray(shown) ? shown.filter(isRecord).map((offer) => offer.value) : [],
  );
  return typeof value === "string" && offered.has(value) ? value : null;
}

function chooseButton(words: string): KnownBlock {
  return {
    type: "actions",
    elements: [
      {
        type: "button",
        action_id: OPEN_BUTTON_ACTION,
        text: plainTextObject(texts.OPEN_BUTTON),
        ...(words ? { value: take(words, QUERY_LIMIT) } : {}),
      },
    ],
  };
}

/** What `!open` alone posts: the button that opens the modal, and the way to open by name. */
export function pickerBlocks(): KnownBlock[] {
  return [
    { type: "section", text: { type: "mrkdwn", text: texts.OPEN_TITLE } },
    chooseButton(""),
    contextBlock(texts.OPEN_BY_NAME),
  ];
}

/**
 * Files match `words`: how many, and the button that opens the modal with `words` in its field;
 * or, when no path is short enough to be a row, the line that says so. With `complete` false the
 * line that says the folder could not be listed in full comes last.
 */
export function matchesBlocks(
  words: string,
  found: readonly string[],
  complete = true,
): KnownBlock[] {
  const heading: KnownBlock = {
    type: "section",
    text: {
      type: "mrkdwn",
      text: fill(found.length === 1 ? texts.OPEN_MATCHES_ONE : texts.OPEN_MATCHES, {
        count: found.length,
        words: shownAsWritten(words),
      }),
    },
  };
  const blocks: KnownBlock[] = [
    heading,
    found.some(fits) ? chooseButton(words) : contextBlock(texts.OPEN_MATCHES_TOO_LONG),
  ];
  return complete ? blocks : [...blocks, contextBlock(texts.OPEN_PARTIAL)];
}

/** What the block of these rows is told apart by: the same rows, the same mark. */
function mark(rows: readonly Row[]): string {
  const values = rows.map((row) => row.value).join("\0");
  return createHash("sha256").update(values, "utf8").digest("hex").slice(0, 8);
}

export interface ModalViewOptions {
  /** How many files there are when `paths` holds only some of them (its length when absent). */
  readonly count?: number;
  /** False adds the line that says the folder could not be listed in full. */
  readonly complete?: boolean;
  /** The view `views.open` takes. */
  readonly opening?: boolean;
}

/**
 * The picker's modal for `target`: the search field holding `words`, and one row for each of the
 * first ROW_LIMIT of `paths`, which are the session's changed files (newest first) while `words`
 * is empty and the files matching it otherwise; null while they are still being listed. `opening`
 * is the view `views.open` takes: it alone sets the field's initial value and focus, since an
 * update keeps what was typed through the field's ids and must not restate it.
 */
export function modalView(
  target: Target,
  words: string,
  paths: readonly string[] | null,
  { count, complete = true, opening = false }: ModalViewOptions = {},
): ModalView {
  const query = {
    type: "plain_text_input" as const,
    action_id: QUERY_ACTION,
    max_length: QUERY_LIMIT,
    placeholder: plainTextObject(texts.OPEN_QUERY_HINT),
    dispatch_action_config: { trigger_actions_on: ["on_character_entered" as const] },
    ...(opening ? { focus_on_load: true, ...(words ? { initial_value: words } : {}) } : {}),
  };
  const blocks: KnownBlock[] = [
    {
      type: "input",
      block_id: QUERY_BLOCK,
      dispatch_action: true,
      optional: true,
      label: plainTextObject(texts.OPEN_QUERY_LABEL),
      element: query,
    },
  ];
  if (paths === null) {
    blocks.push(contextBlock(texts.OPEN_LOADING));
  } else {
    const rows = options(paths);
    const total = count ?? paths.length;
    let heading: string;
    if (words) {
      heading = fill(total === 1 ? texts.OPEN_ROWS_MATCH_ONE : texts.OPEN_ROWS_MATCH, {
        count: total,
      });
    } else if (total) {
      heading = fill(texts.OPEN_ROWS_CHANGED, { count: total });
    } else {
      heading = texts.OPEN_TYPE_A_NAME;
    }
    if (rows.length > 0) {
      blocks.push({
        type: "input",
        block_id: `${CHOICE_BLOCK}:${mark(rows)}`,
        optional: true,
        label: plainTextObject(heading),
        element: { type: "radio_buttons", action_id: CHOICE_ACTION, options: rows },
      });
    } else {
      blocks.push(contextBlock(heading));
    }
    if (total > rows.length) {
      blocks.push(
        contextBlock(
          rows.length > 0
            ? fill(texts.OPEN_MATCHES_CAPPED, { shown: rows.length, count: total })
            : texts.OPEN_MATCHES_TOO_LONG,
        ),
      );
    }
    if (!complete) blocks.push(contextBlock(texts.OPEN_PARTIAL));
  }
  return {
    type: "modal",
    callback_id: OPEN_FORM,
    private_metadata: target.dump(),
    title: plainTextObject(texts.OPEN_MODAL_TITLE),
    submit: plainTextObject(texts.OPEN_MODAL_SUBMIT),
    close: plainTextObject(texts.OPEN_MODAL_CLOSE),
    blocks,
  };
}

/**
 * Which update of an open modal may still be written, so that an older one never overwrites a
 * newer one. Each keystroke reaches the daemon as its own `block_actions` event, handled on its
 * own, and answers can finish in any order. The daemon is the only writer of its modals, so an
 * update carries no `hash` (optional in `views.update`, reference read 2026-10-06): what orders
 * the updates is a `key` (the event's `action_ts`; 0 for the first fill), and:
 *
 * - `claim` refuses a key that is not newer than one already taken, and any key of a view that was
 *   forgotten;
 * - `lock` lets one update of a view run at a time, and `current` says, once its turn has come and
 *   again before it writes, whether it is still the newest;
 * - `forget` drops a view whose modal was submitted, for good: nothing of it is tracked again.
 *
 * At most `limit` modals are tracked, the oldest dropped first (it is tracked again by its next
 * update), and at most `limit` forgotten ones are remembered.
 */
export class ModalUpdates {
  readonly #limit: number;
  readonly #newest = new Map<string, number>();
  readonly #locks = new Map<string, Gate>();
  readonly #gone = new Set<string>();

  constructor(limit: number = MODALS_KEPT) {
    this.#limit = limit;
  }

  /** Takes `key` as the newest of the view; false when one as new was taken already. */
  claim(viewId: string, key: number): boolean {
    if (this.#gone.has(viewId) || key <= (this.#newest.get(viewId) ?? Number.NEGATIVE_INFINITY)) {
      return false;
    }
    this.#newest.delete(viewId);
    this.#newest.set(viewId, key);
    while (this.#newest.size > this.#limit) {
      this.#drop(this.#newest.keys().next().value as string);
    }
    return true;
  }

  /** Whether `key` is still the newest taken for the view. */
  current(viewId: string, key: number): boolean {
    return this.#newest.get(viewId) === key;
  }

  /** The view's lock, one holder at a time; a forgotten view gets a lock that is kept nowhere. */
  lock(viewId: string): Gate {
    if (this.#gone.has(viewId)) return new Gate(1);
    let held = this.#locks.get(viewId);
    if (held === undefined) {
      held = new Gate(1);
      this.#locks.set(viewId, held);
    }
    return held;
  }

  /** The view's modal was submitted: nothing of it is tracked again. */
  forget(viewId: string): void {
    this.#drop(viewId);
    this.#gone.add(viewId);
    while (this.#gone.size > this.#limit) {
      this.#gone.delete(this.#gone.values().next().value as string);
    }
  }

  #drop(viewId: string): void {
    this.#newest.delete(viewId);
    this.#locks.delete(viewId);
  }
}
