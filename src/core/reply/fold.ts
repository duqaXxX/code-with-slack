/**
 * A run of tool calls as two cards: what ended, folded into counts, and under it the call that
 * runs now. A stream can only append and a card is what Slack updates in place, so the fold the
 * channel model drew as two lines of text is drawn here as two cards whose titles change.
 *
 * A run is the calls between two pieces of text. Its first card shows the first call; once a call
 * has ended and another is shown, that card turns into the counts (`Ran 2 shell commands · Read 1
 * file · ✗ Ran 1 shell command`) and a second card takes the call shown. The call shown is the last
 * one started that still runs, else the last one shown: it joins the counts when another takes its
 * place, as the terminal folds a call once the next one follows.
 *
 * A call with a view of its own never folds: an Edit or a Write that ended well (its preview), a
 * subagent or a background command (its task card), a stopped call. It keeps the card that shows
 * it, or gets one, and the run takes no new call after it. An Edit or a Write reaches the fold only
 * once it has ended (the renderer holds it until then): one that failed joins the run as a call
 * that ended.
 *
 * Neither card carries `details` or `output`: Slack appends both to what a card already holds
 * (measured 2026-10-01), so a card that is reused keeps its text in its title.
 */
import type { Preview, TaskStatus, TaskUpdate } from "../../chat/seam.ts";
import { folded } from "./previews.ts";
import { STOPPED } from "./words.ts";

export const ENDED: readonly TaskStatus[] = ["complete", "error"];
export const SEPARATOR = " · ";
export const OK = "✓";
export const FAILED = "✗";

/**
 * The preview, on a call that finished well and only there: a failed, running or task card keeps
 * its own view whatever it carries, so no path can show a preview over an error.
 */
export function shownPreview(update: TaskUpdate): Preview | null {
  return update.status === "complete" && !update.task ? update.preview : null;
}

/** Whether a call is drawn in its run's two cards rather than on a card of its own. */
export function folds(update: TaskUpdate): boolean {
  return (
    update.name !== "" && !update.task && shownPreview(update) === null && update.output !== STOPPED
  );
}

/**
 * Ended calls as counts in the terminal's words, the failed ones after `✗`; `check` puts `✓`
 * before the ones that ended well (a card draws that icon itself).
 */
export function counts(calls: readonly TaskUpdate[], options: { check: boolean }): string {
  // In the order each name first ended, as the terminal lists them.
  const complete = new Map<string, number>();
  const error = new Map<string, number>();
  for (const call of calls) {
    const group = call.status === "error" ? error : complete;
    group.set(call.name, (group.get(call.name) ?? 0) + 1);
  }
  const parts: string[] = [];
  for (const [icon, group] of [
    [options.check ? `${OK} ` : "", complete],
    [`${FAILED} `, error],
  ] as const) {
    if (group.size > 0) {
      parts.push(icon + [...group].map(([name, n]) => folded(name, n)).join(SEPARATOR));
    }
  }
  return parts.join(SEPARATOR);
}

interface Run {
  /** In the order they started. */
  calls: Map<string, TaskUpdate>;
  /** In the order they ended. */
  ended: string[];
  /** The call shown whole. */
  shown: string | null;
  /** The first card's id. */
  summary: string | null;
  /** The second card's id. */
  now: string | null;
  /** Whether the first card has turned into the counts. */
  split: boolean;
  /** Whether a new call still joins it. */
  open: boolean;
}

function newRun(): Run {
  return {
    calls: new Map(),
    ended: [],
    shown: null,
    summary: null,
    now: null,
    split: false,
    open: true,
  };
}

function samePreview(a: Preview | null, b: Preview | null): boolean {
  if (a === null || b === null) return a === b;
  return (
    a.title === b.title &&
    a.summary === b.summary &&
    a.body === b.body &&
    a.language === b.language &&
    a.plain === b.plain
  );
}

/** Whether two states of a line say the same, field by field. */
function sameUpdate(a: TaskUpdate | undefined, b: TaskUpdate): boolean {
  return (
    a !== undefined &&
    a.id === b.id &&
    a.title === b.title &&
    a.status === b.status &&
    a.details === b.details &&
    a.output === b.output &&
    a.name === b.name &&
    a.task === b.task &&
    a.calls === b.calls &&
    a.folded === b.folded &&
    samePreview(a.preview, b.preview)
  );
}

function remove(list: string[], item: string): void {
  const index = list.indexOf(item);
  if (index !== -1) list.splice(index, 1);
}

/**
 * Turns the calls of one reply into the cards that show them. `task` returns the cards a call's
 * new state changes, in the order they are to appear; `text` ends the run.
 */
export class Fold {
  /** Call id to its run. */
  private readonly runs = new Map<string, Run>();
  /** Call id to the card that is its own. */
  private readonly own = new Map<string, string>();
  private current: Run | null = null;
  private readonly sent = new Map<string, TaskUpdate>();
  /** Every card id given out: none is used twice. */
  private readonly cards = new Set<string>();

  /**
   * A new card's id, named after the call it first shows. A card a call took for its own keeps
   * its id, so a second card named after the same call gets a longer one.
   */
  private card(kind: string, call: string): string {
    let card = `${kind}:${call}`;
    while (this.cards.has(card)) card += "+";
    this.cards.add(card);
    return card;
  }

  /** Text was written: the calls after it are a new run. */
  text(): void {
    if (this.current !== null) this.current.open = false;
  }

  task(update: TaskUpdate): TaskUpdate[] {
    const own = this.own.get(update.id);
    if (own !== undefined) return this.changed([{ ...update, id: own }]);
    let run = this.runs.get(update.id);
    if (run === undefined) {
      if (!folds(update)) {
        this.text();
        this.own.set(update.id, update.id);
        return this.changed([update]);
      }
      if (this.current === null || !this.current.open) this.current = newRun();
      run = this.current;
      this.runs.set(update.id, run);
    } else if (!folds(update)) {
      return this.changed([this.leave(run, update), ...this.draw(run)]);
    }
    run.calls.set(update.id, update);
    if (!ENDED.includes(update.status)) {
      remove(run.ended, update.id);
    } else if (!run.ended.includes(update.id)) {
      run.ended.push(update.id);
      // The call that just ended is the one shown, unless another still runs.
      run.shown = update.id;
    }
    return this.changed(this.draw(run));
  }

  /**
   * A call that no longer folds: it keeps the card that shows it, which is its own from now on,
   * or gets one. Its run takes no new call.
   */
  private leave(run: Run, update: TaskUpdate): TaskUpdate {
    run.calls.delete(update.id);
    this.runs.delete(update.id);
    remove(run.ended, update.id);
    run.open = false;
    let card = update.id;
    if (run.shown === update.id) {
      run.shown = null;
      if (run.split && run.now !== null) {
        card = run.now;
        run.now = null;
      } else if (!run.split && run.summary !== null) {
        card = run.summary;
        run.summary = null;
      }
    }
    this.own.set(update.id, card);
    return { ...update, id: card };
  }

  /** The run's cards as they stand: the counts of what ended, then the call shown. */
  private draw(run: Run): TaskUpdate[] {
    const running = [...run.calls.values()]
      .filter((call) => !ENDED.includes(call.status))
      .map((call) => call.id);
    const last = running.at(-1);
    if (last !== undefined) run.shown = last;
    const shown = run.shown === null ? undefined : run.calls.get(run.shown);
    const ended = run.ended.flatMap((id) => run.calls.get(id) ?? []);
    const counted = ended.filter((call) => call.id !== run.shown);
    // What replaces the run's cards once the reply's body has ended: every call that ended.
    const line = counts(ended, { check: true });
    const cards: TaskUpdate[] = [];
    const first = counted[0];
    if (first !== undefined) {
      run.split = true;
      run.summary = run.summary || this.card("fold", first.id);
      const status: TaskStatus = counted.some((call) => call.status === "complete")
        ? "complete"
        : "error";
      cards.push(card(run.summary, counts(counted, { check: false }), status, "", line));
    } else if (shown !== undefined && (run.summary !== null || !run.split)) {
      // Nothing to count: the first card shows the call whole. Before the run has two calls,
      // and when a call the card counted alone stopped folding.
      run.summary = run.summary || this.card("fold", shown.id);
      cards.push(whole(run.summary, shown, line));
    }
    if (shown !== undefined && run.split) {
      run.now = run.now || this.card("now", shown.id);
      cards.push(whole(run.now, shown, ""));
    }
    return cards;
  }

  private changed(cards: TaskUpdate[]): TaskUpdate[] {
    const out = cards.filter((card) => !sameUpdate(this.sent.get(card.id), card));
    for (const card of out) this.sent.set(card.id, card);
    return out;
  }
}

/** A card of a run: a title and a state, and what it folds to. */
function card(
  id: string,
  title: string,
  status: TaskStatus,
  name: string,
  line: string | null,
): TaskUpdate {
  return {
    id,
    title,
    status,
    details: null,
    output: null,
    name,
    task: false,
    calls: 0,
    preview: null,
    folded: line,
  };
}

/**
 * A call shown whole on a card of its run: its words, and why it failed. `line` is what replaces
 * the card once the reply's body has ended; a call still running then has no count yet and stays
 * a card.
 */
function whole(id: string, call: TaskUpdate, line: string): TaskUpdate {
  let title = call.title;
  if (call.status === "error" && call.output) title += SEPARATOR + call.output;
  return card(id, title, call.status, call.name, ENDED.includes(call.status) ? line : null);
}
