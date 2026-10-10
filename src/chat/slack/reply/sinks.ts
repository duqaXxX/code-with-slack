/**
 * Write a rendered reply to Slack as a native stream: `chat.startStream` with the reply's first
 * content, `chat.appendStream` as it grows, `chat.stopStream` at its end, in the session's thread.
 * A stream in a thread the owner started notifies once, when it stops, with its first text as the
 * banner, and never at its start (measured 2026-09-29). Slack closes a stream 5 minutes after it
 * started (measured), so a reply that runs longer stops its stream at STREAM_SECONDS and goes on in
 * the same message with `chat.update`, which never notifies; its end then posts a closing message.
 * Every `chat.appendStream` and `chat.update` also waits its turn on an `UpdateLimiter` shared by
 * every reply in the process: a token bucket that paces writes evenly under the app's own budget,
 * rather than letting several busy threads exhaust it together and then freeze until it resets.
 *
 * How asyncio's cancellation is said here. Whoever stops a sink (`!stop`, a close, a restart)
 * follows the same rule:
 *
 * 1. A pass is never cancelled. A pass is `flush`: it takes the lock, brings every message to
 *    the model and ends the reply if it has ended. Once asked for it runs to its end, whoever
 *    stops waiting for it, and the next pass queues behind it on the lock. `@slack/web-api` cannot
 *    abort a call in flight (the only signal `WebClient.makeRequest` gives `fetch` is the client's
 *    own timeout), and a call given up after Slack took it loses a message's ts or posts an
 *    ending twice. Python shielded its passes for that reason wherever a task could be cancelled
 *    (`_later`, `_retry_final`, `close_out`); here the shield is the rule, so no wait inside a
 *    pass (the lock, the limiter, a Slack call, a read-back) takes a signal.
 * 2. What can be cancelled is a wait outside a pass, and it is cancelled with an `AbortSignal`:
 *    the debounce before a write, the pause before the end's one retry, the stream's 280 seconds
 *    (each a `Task` with its own signal, aborted by `Task.cancel`), and a caller's own wait for a
 *    pass. `finish`, `closeOut`, `waitLanded` and `settle` take an optional signal: aborting it
 *    rejects that call with the signal's reason and leaves the pass running. To stop a sink,
 *    abort the signal you gave it, then `settle()` to wait for what is in flight.
 * 3. A cancelled wait rejects with the signal's reason; a `Task` aborts with `Cancelled`. A task
 *    that was cancelled while its shielded pass ran sees `signal.aborted` when the pass returns
 *    and does nothing more, as a Python task cancelled inside `asyncio.shield` did.
 * 4. The lock is `Mutex`: first come, first served, as `asyncio.Lock`.
 * 5. State a Python task carried by being alive is explicit: `pending` is null whenever no
 *    debounce waits (cleared by the task itself as it returns, and by whoever cancels it), and
 *    `landedValue` says whether and how the end resolved.
 */
import { isDeepStrictEqual } from "node:util";
import type { WebClient } from "@slack/web-api";
import { type Clock, systemClock } from "../../../clock.ts";
import { Fold, shownPreview } from "../../../core/reply/fold.ts";
import type { FooterFields, ReplySink as Sink, TaskUpdate } from "../../seam.ts";
import { formatFooter } from "../footer.ts";
import {
  BANNER_LIMIT,
  BLOCKS_LIMIT,
  type Block,
  type BlocksChunk,
  blocksCount,
  blocksSizes,
  blockText,
  CARD_TEXT_FIELDS,
  type CardChunk,
  type CardHeld,
  type Chunk,
  cardAddition,
  cardBlock,
  cardChunk,
  cardFields,
  contextBlock,
  MESSAGE_LIMIT,
  PREVIEW_CUT,
  plainLines,
  plainText,
  previewBlocks,
  previewContainers,
  split,
  TERMINAL,
  ZERO_WIDTH_SPACE,
} from "./blocks.ts";
import {
  at,
  blank,
  lastNewline,
  len,
  lstripNewlines,
  rstripNewlines,
  strip,
  stripNewlines,
  take,
} from "./chars.ts";
import {
  logger as defaultLogger,
  describe,
  describeRefusal,
  type Logger,
  NOT_STREAMING,
  REFUSED_CONTENT,
  STILL_STREAMING,
  TOO_LONG,
  tooManyBlocks,
  unknownOutcome,
} from "./errors.ts";
import { mrkdwnEscape } from "./escape.ts";
import type { Limiter } from "./limiter.ts";
import { bannerText, markdownBlocks, markdownCut, plainWords } from "./markdown.ts";
import { cancellable, Mutex, Task } from "./tasks.ts";

export * from "../../../clock.ts";
export * from "./blocks.ts";
export * from "./clients.ts";
export * from "./errors.ts";
export * from "./limiter.ts";
export * from "./markdown.ts";
export { Cancelled, cancellable, Mutex, Task } from "./tasks.ts";

export const DEBOUNCE_SECONDS = 1.0;
// The final write has no next rewrite to fix it: one that fails for any reason but its content
// is tried once more after this pause.
export const FINAL_RETRY_SECONDS = 10.0;
// A stream is closed by Slack 5 minutes after `chat.startStream` (measured 2026-09-28: refused at
// 300.3 s and 305 s); stopped by the daemon at this age, it leaves a margin for the round trip.
export const STREAM_SECONDS = 280.0;
// A stream past this age is over, whatever the daemon did (refused at 300.3 s).
export const STREAM_LIFE = 300.0;
// How far back a create of unknown outcome is looked for: the attempt's own clock, less this.
export const ADOPT_SKEW_SECONDS = 2.0;
// How much of a message's first words is compared to know it is the one that was lost.
export const ADOPT_WORDS = 40;

/** (part, offset): a UTF-16 index into a text part, elements into a tool. */
export type Cursor = readonly [part: number, offset: number];

/** `a <= b`, as Python orders two tuples. */
function upTo(a: Cursor, b: Cursor): boolean {
  return a[0] < b[0] || (a[0] === b[0] && a[1] <= b[1]);
}

function sameCursor(a: Cursor | null, b: Cursor | null): boolean {
  if (a === null || b === null) return a === b;
  return a[0] === b[0] && a[1] === b[1];
}

/** A preview piece of a tool, as a key: (part, piece). */
function pieceKey(index: number, piece: number): string {
  return `${index}:${piece}`;
}

class Text {
  text: string;
  /** A line of the daemon's own: never the banner while Claude has words. */
  notice: boolean;
  /** A notice that says how a reply was cut short (`endingCursor`). */
  ending: boolean;
  /** Bumped when it changes: a message whose parts kept theirs is not rendered again. */
  rev: number;

  constructor(text: string, notice: boolean, ending: boolean, rev: number) {
    this.text = text;
    this.notice = notice;
    this.ending = ending;
    this.rev = rev;
  }
}

export class Tool {
  update!: TaskUpdate;
  rev = 0;
  private shownPieces: string[] = [];
  private sizes: number[] = [];
  private carded = false;

  constructor(update: TaskUpdate) {
    this.set(update);
  }

  /** A new state of the call. What follows its card is worked out once, here. */
  set(update: TaskUpdate): void {
    this.update = update;
    const view = shownPreview(update);
    if (view === null || !view.body) {
      this.shownPieces = [];
    } else if (view.plain) {
      // One context block, as it is drawn: a piece counts for what the message holds.
      this.shownPieces = [plainLines(view.body)];
    } else {
      this.shownPieces = split(view.body).filter((chunk) => chunk !== "");
    }
    this.sizes = this.shownPieces.map(len);
    if (view === null || view.plain || this.shownPieces.length === 0) this.carded = true;
  }

  /**
   * Whether the tool is its preview alone: an Edit or a Write that reached the reply already
   * ended well. A state that needs a card (running, failed, stopped, a preview with no body)
   * gives it one for good, since a stream cannot take a card back.
   */
  get cardless(): boolean {
    return !this.carded;
  }

  /**
   * What follows the tool's card: the terminal's preview of a call that ended well, cut into
   * pieces that fit a message each. None until the call ends, and for any other call.
   */
  pieces(): readonly string[] {
    return this.shownPieces;
  }

  /** Piece `piece` (1 is the first). */
  piece(piece: number): string {
    return this.shownPieces[piece - 1] ?? "";
  }

  /** The characters of piece `piece`, as Python's `len` counts them. */
  pieceSize(piece: number): number {
    return this.sizes[piece - 1] ?? 0;
  }

  /**
   * The elements of the tool: its card (counted even when `cardless` draws none, so a cursor
   * reads the same either way), then its preview pieces.
   */
  get extent(): number {
    return 1 + this.shownPieces.length;
  }
}

type Part = Text | Tool;

/** A part of the model inside a span, with the first and the last element the span takes of it. */
interface Spanned {
  readonly index: number;
  readonly part: Part;
  readonly floor: number;
  readonly ceil: number | null;
}

/**
 * Piece `index` (1 is the first) of a tool's preview as blocks. With no card: a collapsed
 * container titled with the call's line. Under a card: a collapsed container for a diff, code
 * blocks for a new file's first lines, a context block for lines of words (a question's
 * answers), each indented under its card and shown as written.
 */
export function pieceBlocks(tool: Tool, index: number): Block[] {
  const view = shownPreview(tool.update);
  if (view === null) throw new Error("a piece of a tool with no preview");
  const body = tool.piece(index);
  if (view.plain) return [contextBlock(body)];
  if (!pieceCollapsed(tool)) return previewBlocks(body);
  if (tool.cardless) {
    return previewContainers(view.title, body, {
      subtitle: view.summary,
      language: view.language,
      asCode: true,
    });
  }
  return previewContainers(view.summary, body);
}

/**
 * Whether `pieceBlocks` draws the tool's preview as collapsed containers, whose text a
 * `chat.update` does not count toward MESSAGE_LIMIT. A context block and code blocks (markdown)
 * always count.
 */
export function pieceCollapsed(tool: Tool): boolean {
  const view = shownPreview(tool.update);
  if (view === null) throw new Error("a piece of a tool with no preview");
  return !view.plain && (tool.cardless || view.language === "diff");
}

/**
 * The same piece for a stream: a `blocks` chunk (measured 2026-09-28 for a diff's container and
 * 2026-09-29 for a markdown block, which reads back as rich text; a context block in one was
 * seen drawn on the daemon on 2026-10-03).
 */
export function pieceChunk(tool: Tool, index: number): BlocksChunk {
  return { type: "blocks", blocks: pieceBlocks(tool, index) };
}

/** What a stream still has to be told, and where the reply goes on if it does not all fit. */
class Plan {
  /** Characters the message holds once these chunks are in. */
  size: number;
  /** Elements it holds. */
  count: number;
  /** What its cards count toward MESSAGE_LIMIT beside `size`. */
  cardCost: number;
  chunks: Chunk[] = [];
  /** Where the next message starts, if the rest does not fit. */
  overflow: Cursor | null = null;
  /** Part to the offset sent up to. */
  text = new Map<number, number>();
  pieces = new Set<string>();
  cards = new Map<string, CardChunk>();
  cardHeld = new Map<string, CardHeld>();
  /** Part to the blocks its text makes. */
  textBlocks = new Map<number, number>();

  constructor(size: number, count: number, cardCost: number) {
    this.size = size;
    this.count = count;
    this.cardCost = cardCost;
  }
}

/**
 * What the reply's last message showed when its end was written: nothing of it is removed by a
 * late update.
 */
class Held {
  /** Part to the offset its text was shown up to. */
  text = new Map<number, number>();
  cards = new Set<string>();
  pieces = new Set<string>();

  /** The blocks they take, a card or a piece counting one. */
  blocks(): number {
    return this.text.size + this.cards.size + this.pieces.size;
  }
}

/** The characters of card text a plan's chunks carry. */
function planCardText(plan: Plan): number {
  let total = 0;
  for (const chunk of plan.chunks) {
    if (chunk.type !== "task_update") continue;
    for (const key of CARD_TEXT_FIELDS) total += len(chunk[key] ?? "");
  }
  return total;
}

/**
 * One Slack message of a reply: the span of the reply's model from `start` to the next message's
 * start, first written as a stream (a reply's first message, and one that continues a stream
 * that is still open) or as a post (one that continues a stopped message). While it streams,
 * what it has been sent is remembered here, since a stream only grows.
 */
class Message {
  readonly start: Cursor;
  readonly mode: "stream" | "post";
  ts: string | null = null;
  streaming = false;
  textSent = new Map<number, number>();
  piecesSent = new Set<string>();
  /** What each card was told. */
  cards = new Map<string, CardChunk>();
  /** The details and the output Slack holds per card: those of every chunk sent, joined. */
  cardHeld = new Map<string, CardHeld>();
  // Characters of card text (title, details, output) in every chunk sent, for the log, and
  // what the cards count toward MESSAGE_LIMIT (`cardAddition`); `size` counts none of it.
  cardText = 0;
  cardCost = 0;
  size = 0;
  count = 0;
  /** Part to the blocks its text makes. */
  textBlocks = new Map<number, number>();
  // The blocks the message may hold: BLOCKS_LIMIT, until Slack refuses a write of it for
  // counting more of them than the daemon did.
  blocksRoom = BLOCKS_LIMIT;
  // Stopped: whether what it shows is its stream as sent, which needs no write while the
  // model still says the same; else `shown` is the blocks of its last post or update.
  exact = false;
  shown: Block[] | null = null;
  /** The footer it shows. */
  footer: Block[] = [];
  deadline: Task<void> | null = null;
  // An append whose outcome is unknown: the stream is no longer told anything, it is stopped
  // and the message goes on by update, from the model.
  blind = false;
  // Its stream was refused as too long (`refused`), and an update refused in turn left it
  // showing less than the model (`short`), until an update passes: while a message is short
  // the reply's end has not landed.
  refused = false;
  short = false;
  /** When its stream started, by the clock: a stream is over at 5 minutes. */
  started = 0;
  // (span revision, end) of the last write of a stopped message that has a successor: while it
  // holds, nothing in the message changed and it is not rendered again.
  checked: { readonly rev: number; readonly end: Cursor } | null = null;
  // The footer a stop of unknown outcome carried (a footerless stop records nothing): if the
  // next stop finds the stream over, that stop is the one that landed.
  stopUnknown: Block[] | null = null;
  // Slack refused an update of this message whose containers were not counted: from then on
  // they count toward MESSAGE_LIMIT, as in a post.
  containersCounted = false;

  constructor(start: Cursor, mode: "stream" | "post") {
    this.start = start;
    this.mode = mode;
  }
}

/** (written, where the reply goes on past the message). */
type Step = readonly [ok: boolean, overflow: Cursor | null];

export interface ReplySinkOptions {
  readonly channel: string;
  readonly threadTs: string;
  // A stream is addressed to a user of a workspace (as the recordings passed them).
  readonly teamId: string;
  readonly userId: string;
  /** The daemon's own bot: whose messages are read back to find one a create left unknown. */
  readonly botUserId: string;
  readonly limiter: Limiter;
  /** Wall-clock seconds, and every pause of the sink. */
  readonly clock?: Clock;
  /**
   * Crash repair: `(old ts, new ts)`, this sink's own transition in the thread's open-replies
   * list (more than one sink can be open at once: a background task's own reply can outlive the
   * turn that started it, so each sink owns exactly one entry and must never touch another's).
   * A plain synchronous callback, never awaited here.
   */
  readonly onOpenReply?: (old: string | null, fresh: string | null) => void;
  /**
   * Called after every pass that wrote this reply, or tried to: Slack clears a thread's status
   * line when the app replies. Synchronous, never awaited here.
   */
  readonly onWrite?: () => void;
  /**
   * The client the calls that create or grow a message go through (`creatingClient`), which
   * never sends a call twice. The sink's own client when left out.
   */
  readonly creating?: WebClient;
  readonly logger?: Logger;
  /** DEBOUNCE_SECONDS, unless a test shortens it. */
  readonly debounceSeconds?: number;
  /** FINAL_RETRY_SECONDS, unless a test shortens it. */
  readonly finalRetrySeconds?: number;
}

/**
 * One reply in a Slack thread, as a native stream: Claude's text as it is written, and task
 * cards for its tools, updated in place, in the order things happen: two per run of calls
 * (`core/reply/fold`), which a silent update folds into a line of counts once the stream has
 * stopped at the reply's end. The stream starts with the first content (never a placeholder) and
 * stops with the reply's end, the footer at the bottom.
 * It stops on its own at STREAM_SECONDS, since Slack closes a stream at 5 minutes, and when
 * Slack refuses an append as too long, which it would refuse again: from then on the same
 * message grows by `chat.update`, and the end posts the reply's ending (the text after its last
 * call) with the footer as a new message.
 * A reply past MESSAGE_LIMIT or BLOCKS_LIMIT continues in a new message (a new stream while the
 * message still streams, else a post). `finish` ends the body only: a task that outlives the turn
 * keeps updating its own card after that, in place. Never throws for a write that fails: it is
 * sent again with the next one, the final one once more after FINAL_RETRY_SECONDS.
 */
export class ReplySink implements Sink {
  private readonly slack: WebClient;
  private readonly creating: WebClient;
  private readonly channel: string;
  private readonly threadTs: string;
  private readonly teamId: string;
  private readonly userId: string;
  private readonly botUserId: string;
  private readonly limiter: Limiter;
  private readonly clock: Clock;
  private readonly onOpenReply: ((old: string | null, fresh: string | null) => void) | null;
  private readonly onWrite: (() => void) | null;
  private readonly logger: Logger;
  private readonly debounceSeconds: number;
  private readonly finalRetrySeconds: number;
  /** A Slack write was attempted since `onWrite` was last called. */
  private wrote = false;
  /** This sink's own entries in the open-replies list: the messages a crash would leave unfinished. */
  private readonly tracked = new Set<string>();
  /** The end landed: nothing is left for a repair to close. */
  private ended = false;
  /** The last message's body is whole, and only the closing message is owed. */
  private bodyLanded = false;
  private readonly parts: Part[] = [];
  /** By the id of the card that shows them. */
  private readonly tools = new Map<string, Tool>();
  /** Which cards show the calls: two per run of calls. */
  private readonly fold = new Fold();
  private readonly messages: Message[] = [];
  private pending: Task<void> | null = null;
  private retry: Task<void> | null = null;
  private ending: Task<boolean> | null = null;
  private readonly lock = new Mutex();
  private finished = false;
  private footer: string | null = null;
  private running = "";
  private latest = true;
  /** Whether closeOut has run; a second call is a no-op. */
  private closedOut = false;
  // Where the footer went once the reply ended: "inline", on the stream's own stop (and on the
  // message's updates after it); "moved", under the reply's ending in its last message (posted
  // for it, or the continuation that opened on the line on how the reply ended); or "post", in
  // a closing message that holds nothing else.
  private endMode: "inline" | "moved" | "post" | null = null;
  // Set once the end has landed on Slack (`endLanded`): the reply's messages are then a fixed
  // set, and a late update only edits them, keeping what its last message showed (`blocks`).
  // Not `closedOut`, nor `endMode` alone: a close whose write failed still owes messages, and
  // its retry must open them.
  private held: Held | null = null;
  /** The ts of the closing message, once posted. */
  private closing: string | null = null;
  private closingShown: Block[] = [];
  // Resolves once the reply is known to have ended on Slack (true) or its one retry failed too
  // (false): what the session waits on before it shows a checkmark.
  private readonly landed: Promise<boolean>;
  private landedValue: boolean | null = null;
  private landedResolve: (landed: boolean) => void = () => {};
  // Bumped by `changed`: `later` compares it before and after a pass to notice a change that
  // arrived while the pass wrote or waited its turn, and runs another pass for it.
  private version = 0;
  private rev = 0;

  constructor(slack: WebClient, options: ReplySinkOptions) {
    this.slack = slack;
    this.creating = options.creating ?? slack;
    this.channel = options.channel;
    this.threadTs = options.threadTs;
    this.teamId = options.teamId;
    this.userId = options.userId;
    this.botUserId = options.botUserId;
    this.limiter = options.limiter;
    this.clock = options.clock ?? systemClock;
    this.onOpenReply = options.onOpenReply ?? null;
    this.onWrite = options.onWrite ?? null;
    this.logger = options.logger ?? defaultLogger;
    this.debounceSeconds = options.debounceSeconds ?? DEBOUNCE_SECONDS;
    this.finalRetrySeconds = options.finalRetrySeconds ?? FINAL_RETRY_SECONDS;
    this.landed = new Promise((resolve) => {
      this.landedResolve = resolve;
    });
  }

  /**
   * One transition of this sink's own entries in the thread's open-replies list: add `fresh`,
   * or drop `old`; every other sink's entry is left alone. Best-effort: a failed state write is
   * logged (ids only) and swallowed, since the reply itself must never fail over crash-repair
   * bookkeeping. False then: the caller keeps its set as it was, or it would believe a ts no
   * longer needs removing and orphan it in state.json forever, since nothing else ever asks to
   * remove a ts this sink no longer remembers.
   */
  private track(old: string | null, fresh: string | null): boolean {
    if (this.onOpenReply === null) return true;
    try {
      this.onOpenReply(old, fresh);
    } catch (error) {
      this.logger.warning(
        `could not update the open-reply tracking for ${this.channel}/${this.threadTs}: ` +
          describe(error),
      );
      return false;
    }
    return true;
  }

  /**
   * Whether a crash would leave the message unfinished: its stream still open, or a card of it
   * still running (a stopped message stores such a card as an error until it is updated), or,
   * for the reply's last message, the end not yet landed. Once the body is whole and only the
   * closing message is owed, there is nothing of the answer left to fix.
   */
  private keepsOpen(message: Message, end: Cursor | null): boolean {
    if (message.ts === null) return false;
    if (message.streaming) return true;
    for (const { part, floor, ceil } of this.span(message.start, end)) {
      if (!(part instanceof Tool)) continue;
      const [hasCard] = ReplySink.toolElements(part, floor, ceil);
      if (hasCard && !TERMINAL.includes(part.update.status)) return true;
    }
    return message === this.messages.at(-1) && !this.ended && !this.bodyLanded;
  }

  /**
   * Bring this sink's open-reply entries to the messages that are unfinished now: one is added
   * the moment it is written, and dropped once nothing is left for a repair to close.
   */
  private retrack(): void {
    const wanted = new Set<string>();
    for (const [index, message] of this.messages.entries()) {
      const following = this.messages[index + 1];
      const end = following === undefined ? null : following.start;
      if (message.ts !== null && this.keepsOpen(message, end)) wanted.add(message.ts);
    }
    for (const ts of [...wanted].filter((ts) => !this.tracked.has(ts)).sort()) {
      if (this.track(null, ts)) this.tracked.add(ts);
    }
    for (const ts of [...this.tracked].filter((ts) => !wanted.has(ts)).sort()) {
      if (this.track(ts, null)) this.tracked.delete(ts);
    }
  }

  /**
   * The reply's end landed: from then on nothing is left open for a repair to close, but a card
   * still running.
   */
  private settleOpenReply(): void {
    this.ended = true;
    this.retrack();
  }

  /**
   * Claude's words, or with `notice` a line of the daemon's own (never a banner while Claude
   * has words). With `ending` that line says how the reply was cut short: a part of its own,
   * never joined to an earlier notice, so it can move whole (`endingCursor`).
   */
  async text(
    markdown: string,
    options: { readonly notice?: boolean; readonly ending?: boolean } = {},
  ): Promise<void> {
    const ending = options.ending ?? false;
    const notice = (options.notice ?? false) || ending;
    if (!blank(markdown)) this.fold.text();
    const last = this.parts.at(-1);
    if (last instanceof Text && last.notice === notice && last.ending === ending) {
      last.text += markdown;
      last.rev = this.nextRev();
    } else {
      this.parts.push(new Text(markdown, notice, ending, this.nextRev()));
    }
    this.changed();
  }

  /**
   * A call's new state, on the cards that show it: a run of calls shares two cards
   * (`core/reply/fold`), a call with a view of its own has one.
   */
  async task(update: TaskUpdate): Promise<void> {
    const cards = this.fold.task(update);
    for (const card of cards) {
      let tool = this.tools.get(card.id);
      if (tool === undefined) {
        tool = new Tool(card);
        this.tools.set(card.id, tool);
        this.parts.push(tool);
      } else {
        tool.set(card);
      }
      tool.rev = this.nextRev();
    }
    if (cards.length > 0) this.changed();
  }

  private nextRev(): number {
    this.rev += 1;
    return this.rev;
  }

  /**
   * Show what still runs in this thread (`⏳ 1 shell · 1 agent`) after the footer, or on a line
   * of its own; empty removes it. Only the thread's latest reply shows one.
   */
  async setRunning(counts: string): Promise<void> {
    if (counts === this.running) return;
    this.running = counts;
    if (this.closedOut) this.changed();
  }

  /**
   * Whether the reply's end has landed on Slack: its footer is then what says the running
   * counts, and no status line under it repeats them.
   */
  get footerShown(): boolean {
    return this.ended && this.messages.length > 0;
  }

  /**
   * Only the thread's latest reply shows the running counts, at the bottom of the thread; an
   * older one drops them and keeps its footer, a record of how its turn ended.
   */
  async setLatest(latest: boolean): Promise<void> {
    if (latest === this.latest) return;
    this.latest = latest;
    if (this.closedOut) this.changed();
  }

  /**
   * End the reply's body: what was written is sent now, not at the next debounce. A card still
   * in progress is a task that outlives the turn: `task` keeps updating it after this. The
   * stream stays open: it ends with `closeOut`, once the caller knows nothing more is coming (a
   * task can still outlive this very turn).
   */
  async finish(closing: readonly TaskUpdate[], signal?: AbortSignal): Promise<void> {
    for (const update of closing) await this.task(update);
    this.pending?.cancel();
    this.pending = null;
    this.finished = true;
    // What a card of a run of calls shows changes here, to its folded line: its message is
    // rendered again even when nothing else in it changed.
    for (const tool of this.tools.values()) {
      if (tool.update.folded !== null) tool.rev = this.nextRev();
    }
    await cancellable(this.flush(), signal);
  }

  /** `closeOutFormatted`, with the footer's fields formatted as one line of mrkdwn. */
  closeOut(footer: FooterFields | null, signal?: AbortSignal): Promise<boolean> {
    const line = footer === null ? null : formatFooter(footer, this.clock.time() * 1000) || null;
    return this.closeOutFormatted(line, signal);
  }

  /**
   * End the reply, once: stop its stream with the footer at the bottom (one push), or, when the
   * stream already stopped at STREAM_SECONDS, post its ending and the footer as a new message
   * (the second push). `footer` is the line as mrkdwn. True when the reply ended on Slack (or
   * nothing was owed); false when a write failed and one retry is scheduled: `waitLanded` then
   * says how it ended. A second call is a no-op: the reply has already ended.
   */
  async closeOutFormatted(footer: string | null, signal?: AbortSignal): Promise<boolean> {
    if (this.closedOut) return this.landedValue === true;
    this.closedOut = true;
    this.footer = footer;
    // The end is its own task, shielded from the caller: a caller that stops waiting while Slack
    // takes the write (a timer, a turn starting, a shutdown) must not leave `landed` unresolved
    // with no retry, which no later `closeOut` could repair.
    this.ending = new Task((ending) => this.endOut(ending));
    return cancellable(this.ending.result(), signal);
  }

  /**
   * The end's first flush: whatever happens, `landed` resolves (an end that began always does),
   * with the retry when Slack refused the write.
   */
  private async endOut(signal: AbortSignal): Promise<boolean> {
    let ok: boolean;
    try {
      ok = await cancellable(this.flush(), signal);
    } catch (error) {
      this.resolve(false);
      throw error;
    }
    if (ok) {
      this.resolve(true);
      return true;
    }
    if (this.landedValue === null) {
      // `settle` may have decided it already
      this.retry = new Task((retry) => this.retryFinal(retry));
    }
    return false;
  }

  private resolve(landed: boolean): void {
    if (landed) this.settleOpenReply();
    if (this.landedValue === null) {
      this.landedValue = landed;
      this.landedResolve(landed);
    }
  }

  /** Whether the reply ends up on Slack: waits for the retry a failed end schedules. */
  waitLanded(signal?: AbortSignal): Promise<boolean> {
    return cancellable(this.landed, signal);
  }

  private async retryFinal(signal: AbortSignal): Promise<void> {
    await this.clock.sleep(this.finalRetrySeconds, signal);
    // Shielded: a shutdown's `settle` cancels this task, and a write given up after Slack took
    // it would lose what it did.
    const ok = await this.flush();
    if (signal.aborted) return;
    this.resolve(ok);
  }

  /**
   * Force this reply to its current, true form right now, cancelling any debounce still
   * pending: a shutdown's very last chance. Cheap when nothing changed since the last write. A
   * retry still waiting is tried now. True when the reply is on Slack (or nothing was owed): a
   * shutdown that gets false has lost it.
   */
  async settle(signal?: AbortSignal): Promise<boolean> {
    this.pending?.cancel();
    this.pending = null;
    if (this.ending !== null && !this.ending.done) {
      // The end in flight may still schedule its retry: let it, so the cancel below sees it.
      // `settled` hands back neither its error nor its cancellation: only this call's own
      // signal ends the wait early.
      await cancellable(this.ending.settled(), signal);
    }
    this.retry?.cancel();
    const ok = await cancellable(this.flush(), signal);
    if (this.closedOut && this.landedValue === null) this.resolve(ok);
    return ok || this.landedValue === true;
  }

  /**
   * A background task or subagent can still update its own card after the reply itself ended
   * (rare, and possibly during shutdown): debounced through `later`, like any other change,
   * rather than flushed here and now, which would spend a limiter wait (real time, under a busy
   * process's shared budget) inline on the caller, the SDK reader loop among them.
   */
  private changed(): void {
    this.version += 1;
    this.schedule();
  }

  private schedule(): void {
    if (this.pending === null) this.pending = new Task((signal) => this.later(signal));
  }

  private async later(signal: AbortSignal): Promise<void> {
    try {
      for (;;) {
        await this.clock.sleep(this.debounceSeconds, signal);
        const version = this.version;
        // Shielded: `finish` cancels a pending write, and a write given up after Slack took it
        // would lose the message's ts. `finish` waits for the lock instead.
        const ok = await this.flush();
        if (signal.aborted || !ok) return;
        if (this.version === version) return;
        // The reply changed again while that call wrote or waited its turn, in a way it never
        // saw: another debounce, then flush again, so it still catches up rather than waiting
        // for the next unrelated event to notice. The debounce stays inside the loop: while
        // text keeps streaming, the version moves during every round trip, and without it here
        // a reply would be written after every round trip instead of at most once per
        // DEBOUNCE_SECONDS.
      }
    } finally {
      // In the same step as the decision to return: a change that arrives next starts a
      // debounce of its own.
      if (this.pending?.signal === signal) this.pending = null;
    }
  }

  // The reply's model: the parts in order, cut into messages at cursors.

  private spanRev(message: Message, end: Cursor | null): number {
    return this.span(message.start, end).reduce((rev, { part }) => Math.max(rev, part.rev), 0);
  }

  private hasContent(): boolean {
    return this.parts.some((part) => part instanceof Tool || !blank(part.text));
  }

  /**
   * The parts from `start` to `end` (the model's end when null), each with the first and last
   * element the span takes of it.
   */
  private span(start: Cursor, end: Cursor | null): Spanned[] {
    const last = end === null ? this.parts.length : Math.min(this.parts.length, end[0] + 1);
    const spanned: Spanned[] = [];
    for (let index = start[0]; index < last; index += 1) {
      const part = this.parts[index];
      if (part === undefined) continue;
      spanned.push({
        index,
        part,
        floor: index === start[0] ? start[1] : 0,
        ceil: end !== null && index === end[0] ? end[1] : null,
      });
    }
    return spanned;
  }

  /** Whether a span takes a tool's card, if it has one, and which of its preview pieces. */
  private static toolElements(
    tool: Tool,
    floor: number,
    ceil: number | null,
  ): [hasCard: boolean, pieces: number[]] {
    const extent = tool.extent;
    const top = ceil === null ? extent : Math.min(extent, ceil);
    const pieces: number[] = [];
    for (let piece = Math.max(floor, 1); piece < top; piece += 1) pieces.push(piece);
    return [floor === 0 && top > 0 && !tool.cardless, pieces];
  }

  /**
   * The footer below a divider, and after it, on the thread's latest reply only, what still
   * runs; empty when there is nothing to show.
   */
  private closingBlocks(): Block[] {
    const lastLine = [this.footer, this.latest ? this.running : ""]
      .filter((piece) => piece)
      .join(" · ");
    if (!lastLine) return [];
    return [{ type: "divider" }, contextBlock(lastLine)];
  }

  /**
   * The footer a message shows: only the reply's last, when the footer rode on its stream's
   * stop or when it is the reply's ending, in a message of its own (`end`).
   */
  private footerOf(message: Message): Block[] {
    if (
      message === this.messages.at(-1) &&
      (this.endMode === "inline" || this.endMode === "moved")
    ) {
      return this.closingBlocks();
    }
    return [];
  }

  /**
   * Where the reply's ending starts in its last message: the text Claude wrote after its last
   * call, whole (a part of the model is never cut: half a list or a heading without its body is
   * no ending), with whatever follows it. Null when the message holds no such text from its
   * start, or would keep nothing of the answer before it (an answer that is text alone): the
   * ending is then the footer alone, unless the turn was cut short. The ending is then the
   * daemon's line on how the reply ended (`Text.ending`), with whatever follows it: the message
   * that notifies says so, where it would show empty or hold a footer alone. That line moves
   * from under a line of the daemon's too, which stays.
   */
  private endingCursor(message: Message): Cursor | null {
    const cursor = this.cutBefore(message, this.lastText(message, false));
    if (cursor !== null) return cursor;
    return this.cutBefore(message, this.lastText(message, true), true);
  }

  /**
   * The last part, from where `message` starts, that holds words of Claude's, or with `ending`
   * the daemon's line on how the reply was cut short; null when there is none.
   */
  private lastText(message: Message, ending: boolean): number | null {
    for (let i = this.parts.length - 1; i >= message.start[0]; i -= 1) {
      const part = this.parts[i];
      if (part instanceof Text && (ending ? part.ending : !part.notice) && !blank(part.text)) {
        return i;
      }
    }
    return null;
  }

  /**
   * The cursor at the start of part `index`, when cutting `message` there leaves it something of
   * the answer, or with `notices` any line at all; null when the part opens the message or
   * nothing would stay.
   */
  private cutBefore(message: Message, index: number | null, notices = false): Cursor | null {
    if (index === null || upTo([index, 0], message.start)) return null;
    const cursor: Cursor = [index, 0];
    // Something of the answer must stay before it, and show: a card that draws (a folded run's
    // second card draws nothing), a preview, or words of Claude's. A message left with a line
    // of the daemon's alone, or with nothing, is no answer: cutting Claude's text there would
    // move the whole answer out. The line on how the reply ended is no part of the answer, so
    // a line of the daemon's is enough to stay above it.
    for (const { part, floor, ceil } of this.span(message.start, cursor)) {
      if (part instanceof Tool) {
        const [hasCard, pieces] = ReplySink.toolElements(part, floor, ceil);
        if ((hasCard && this.cardBlocks(part.update).length > 0) || pieces.length > 0) {
          return cursor;
        }
      } else if ((notices || !part.notice) && !blank(part.text.slice(floor, ceil ?? undefined))) {
        return cursor;
      }
    }
    return null;
  }

  /**
   * Whether `message`, posted as a continuation, starts at the daemon's line on how the reply
   * ended, inside it or in what follows it: it rang when it was posted, and is the reply's
   * ending as it stands.
   */
  private opensOnEnding(message: Message): boolean {
    let said: number | null = null;
    for (let i = this.parts.length - 1; i >= 0; i -= 1) {
      const part = this.parts[i];
      if (part instanceof Text && part.ending && !blank(part.text)) {
        said = i;
        break;
      }
    }
    return message.mode === "post" && said !== null && upTo([said, 0], message.start);
  }

  /**
   * The notification's text, plain: the first paragraph of Claude's own words in the span; else
   * the daemon's line on how the reply was cut short; else the first tool's title; else any
   * line of the daemon's. Never a daemon line while there are words of Claude's to show. Cut to
   * BANNER_LIMIT.
   */
  private banner(span: Spanned[] | null = null): string {
    const parts = span ?? this.span([0, 0], null);
    const first = (words: string): string => {
      const end = words.indexOf("\n\n");
      const paragraph = end === -1 ? words : words.slice(0, end);
      return bannerText(paragraph, { limit: BANNER_LIMIT }) || "…";
    };
    let notice = "";
    let said = "";
    for (const { part, floor, ceil } of parts) {
      if (!(part instanceof Text)) continue;
      const words = strip(part.text.slice(floor, ceil ?? undefined));
      if (words && !part.notice) return first(words);
      notice = notice || words;
      said = said || (part.ending ? words : "");
    }
    if (said) return first(said);
    for (const { part } of parts) {
      if (part instanceof Tool) {
        return bannerText(cardFields(part.update).title, { limit: BANNER_LIMIT }) || "…";
      }
    }
    return first(notice);
  }

  // What a stream is told.

  /**
   * What the message's stream lacks: the cards that changed since they were sent, then what the
   * model has past what it was sent, in order, as far as the message holds.
   */
  private plan(message: Message): Plan {
    const plan = new Plan(message.size, message.count, message.cardCost);
    for (const [toolId, sent] of message.cards) {
      const tool = this.tools.get(toolId);
      if (tool === undefined) continue;
      const chunk = cardChunk(tool.update);
      if (!isDeepStrictEqual(chunk, sent)) ReplySink.planCard(message, plan, chunk);
    }
    for (const { index, part, floor } of this.span(message.start, null)) {
      const more =
        part instanceof Text
          ? this.planText(message, plan, index, part, floor)
          : this.planTool(message, plan, index, part, floor);
      if (!more) break;
    }
    return plan;
  }

  /**
   * Add a card's chunk to the plan as a stream takes it; false when the card is new and the
   * message has no room for it. A message that holds nothing yet takes any card.
   */
  private static planCard(message: Message, plan: Plan, chunk: CardChunk): boolean {
    const toolId = chunk.id;
    const room = MESSAGE_LIMIT - plan.size - plan.cardCost;
    const [told, held, cost] = cardAddition(
      chunk,
      message.cards.get(toolId),
      message.cardHeld.get(toolId) ?? {},
      room,
    );
    if (!message.cards.has(toolId) && cost > room && plan.size + plan.cardCost > 0) return false;
    plan.chunks.push(told);
    plan.cards.set(toolId, chunk);
    plan.cardHeld.set(toolId, held);
    plan.cardCost += cost;
    return true;
  }

  /** Add the unsent tail of a text part to the plan; false when the plan is complete. */
  private planText(
    message: Message,
    plan: Plan,
    index: number,
    part: Text,
    floor: number,
  ): boolean {
    const sent = Math.max(message.textSent.get(index) ?? 0, floor);
    let tail = part.text.slice(sent);
    let lead = 0;
    if (plan.size === 0) {
      // a message opens on its words, not on the blank lines before them
      lead = tail.length - lstripNewlines(tail).length;
      tail = tail.slice(lead);
    }
    if (blank(tail)) {
      if (index === this.parts.length - 1 && !this.finished) {
        return false; // more of it may come: the blanks go with it
      }
      plan.text.set(index, part.text.length);
      return true;
    }
    const had = message.textBlocks.get(index) ?? 0;
    const others = plan.count - had; // the blocks of everything else in the message
    if (!had && others >= message.blocksRoom) {
      plan.overflow = [index, sent];
      return false;
    }
    // Characters, held against the limit; `fits` is where that many of them end in the tail.
    const room = Math.max(0, MESSAGE_LIMIT - plan.size - plan.cardCost);
    const fits = at(tail, room);
    let piece: string;
    let upto: number;
    if (fits >= tail.length || blank(tail.slice(fits))) {
      // All of it, or all but blanks, which go with what follows if anything does.
      piece = tail.slice(0, fits);
      upto = sent + lead + piece.length;
    } else {
      const cut = room > 0 ? lastNewline(tail, fits) : -1;
      const end = cut <= 0 ? fits : cut;
      piece = tail.slice(0, end);
      upto = sent + lead + end + (cut > 0 ? 1 : 0);
      plan.overflow = [index, upto];
    }
    // The part's text in this message, as the update that follows the stream will write it:
    // cut where the blocks Slack makes of it would pass the message's room.
    const whole = part.text.slice(floor, sent + lead + piece.length);
    const opening = floor + whole.length - lstripNewlines(whole).length;
    let words = stripNewlines(whole);
    // Text already sent can gain a block when its next line arrives (a line that turns out to
    // head a table): the cut is then the next block's start, never inside a line.
    const over = markdownCut(
      words,
      Math.max(1, message.blocksRoom - others),
      Math.max(0, sent + lead - opening),
    );
    if (over !== null) {
      const stop = opening + over;
      piece = part.text.slice(sent + lead, stop);
      upto = stop;
      plan.overflow = [index, stop];
      words = stripNewlines(part.text.slice(opening, stop));
    }
    plan.text.set(index, upto);
    if (!blank(piece)) {
      plan.chunks.push({ type: "markdown_text", text: piece });
      plan.size += len(piece);
      const made = markdownBlocks(words);
      plan.textBlocks.set(index, made);
      plan.count = others + made;
    }
    return plan.overflow === null;
  }

  /**
   * Add a tool's card, if this message holds it and has not sent it, and the pieces of its
   * preview it has not sent; false when the plan is complete.
   */
  private planTool(
    message: Message,
    plan: Plan,
    index: number,
    tool: Tool,
    floor: number,
  ): boolean {
    const update = tool.update;
    const [hasCard, pieces] = ReplySink.toolElements(tool, floor, null);
    if (hasCard && !message.cards.has(update.id) && !plan.cards.has(update.id)) {
      if (
        plan.count >= message.blocksRoom ||
        !ReplySink.planCard(message, plan, cardChunk(update))
      ) {
        plan.overflow = [index, 0];
        return false;
      }
      plan.count += 1;
    }
    for (const piece of pieces) {
      if (message.piecesSent.has(pieceKey(index, piece))) continue;
      const size = tool.pieceSize(piece);
      if (plan.count >= message.blocksRoom || plan.size + plan.cardCost + size > MESSAGE_LIMIT) {
        plan.overflow = [index, piece];
        return false;
      }
      plan.chunks.push(pieceChunk(tool, piece));
      plan.pieces.add(pieceKey(index, piece));
      plan.size += size;
      plan.count += 1;
    }
    return true;
  }

  private static sent(message: Message, plan: Plan): void {
    for (const [index, upto] of plan.text) message.textSent.set(index, upto);
    for (const piece of plan.pieces) message.piecesSent.add(piece);
    for (const [id, chunk] of plan.cards) message.cards.set(id, chunk);
    for (const [id, held] of plan.cardHeld) message.cardHeld.set(id, held);
    message.cardText += planCardText(plan);
    message.size = plan.size;
    message.count = plan.count;
    message.cardCost = plan.cardCost;
    for (const [index, made] of plan.textBlocks) message.textBlocks.set(index, made);
  }

  /**
   * Whether the message's stream, as sent, shows what the model says for its span: every card
   * as it is now, and ended (a card left in progress in a stopped stream is stored as an error
   * until it is updated: measured 2026-09-28), every preview piece, all the text. Never once
   * the body has ended, for a message with cards of a run of calls: the model then says the
   * folded line, which a stream cannot show; without `folding` that is let pass, and the answer
   * is whether the stream holds everything the span says.
   */
  private streamShows(message: Message, end: Cursor | null, folding = true): boolean {
    for (const [toolId, sent] of message.cards) {
      const update = this.tools.get(toolId)?.update;
      if (update === undefined) return false;
      if (folding && this.finished && update.folded !== null) return false;
      if (!TERMINAL.includes(update.status) || !isDeepStrictEqual(cardChunk(update), sent)) {
        return false;
      }
    }
    for (const { index, part, floor, ceil } of this.span(message.start, end)) {
      if (part instanceof Text) {
        const sentTo = Math.max(message.textSent.get(index) ?? 0, floor);
        if (!blank(part.text.slice(sentTo, ceil ?? undefined))) return false;
      } else {
        const [hasCard, pieces] = ReplySink.toolElements(part, floor, ceil);
        if (hasCard && !message.cards.has(part.update.id)) return false;
        if (pieces.some((piece) => !message.piecesSent.has(pieceKey(index, piece)))) return false;
      }
    }
    return true;
  }

  // What a message shows once it is no longer a stream: blocks.

  /**
   * The blocks of the message's span: Claude's text as markdown, the tools' task cards
   * (`cardBlocks`), the preview blocks after a card. With no `end` (the reply's last message)
   * only as many as fit MESSAGE_LIMIT and BLOCKS_LIMIT, and where the reply goes on if it does
   * not all fit. Once the reply's end has landed nothing opens a message after it: what arrives
   * late is shown when it all fits, and else not at all, the last message keeping what it showed
   * (`Held`) and no cursor being returned. The blocks are counted for a `chat.update`, which
   * takes a collapsed container's text without counting it; `posting` counts it, as a
   * `chat.postMessage` does.
   */
  private blocks(
    message: Message,
    end: Cursor | null,
    posting = false,
  ): [blocks: Block[], overflow: Cursor | null] {
    const [blocks, overflow] = this.render(message, end, null, posting);
    if (overflow === null || end !== null || this.held === null) return [blocks, overflow];
    return [this.render(message, null, this.held)[0], null];
  }

  /**
   * `blocks` for one reading of the span: with `held`, only what the message showed when the
   * reply's end landed, as the span is now, and the note for a preview of a card of it that was
   * left out, if a block is free for it.
   */
  private render(
    message: Message,
    end: Cursor | null,
    held: Held | null,
    posting = false,
  ): [blocks: Block[], overflow: Cursor | null] {
    const blocks: Block[] = [];
    let size = 0;
    let count = 0; // the blocks as Slack counts them, `markdownBlocks` a text
    const limit = message.blocksRoom;
    const fixed = end !== null || held !== null;
    // A message whose span is fixed: what its cards and text take, and what previews took.
    const [baseBlocks, baseSize] = end !== null ? this.base(message, end) : [0, 0];
    const leftOut =
      end !== null ? this.cutPieces(message, end, baseBlocks, baseSize) : new Set<string>();
    const noteRoom = held !== null && held.blocks() < BLOCKS_LIMIT;
    let noted = false;
    for (const { index, part, floor, ceil: spanCeil } of this.span(message.start, end)) {
      if (part instanceof Text) {
        const ceil = held !== null ? (held.text.get(index) ?? floor) : spanCeil;
        const raw = part.text.slice(floor, ceil ?? undefined);
        const lead = raw.length - lstripNewlines(raw).length;
        const words = stripNewlines(raw.slice(lead));
        if (!words) continue;
        if (!fixed && count >= limit) return [blocks, [index, floor + lead]];
        if (!fixed) {
          let stop: number | null = null;
          let skip = 0;
          // Where the characters the message still has room for end in the words.
          const fits = at(words, MESSAGE_LIMIT - size);
          if (fits < words.length) {
            const cut = fits > 0 ? lastNewline(words, fits) : -1;
            [stop, skip] = cut <= 0 ? [fits, 0] : [cut, 1];
          }
          const over = markdownCut(words, limit - count);
          if (over !== null && (stop === null || over < stop)) {
            // a line's start: the next message opens on it
            stop = over;
            skip = 0;
          }
          if (stop !== null) {
            const shown = words.slice(0, stop);
            if (stripNewlines(shown)) {
              blocks.push({ type: "markdown", text: rstripNewlines(shown) });
            }
            return [blocks, [index, floor + lead + stop + skip]];
          }
        }
        blocks.push({ type: "markdown", text: words });
        size += len(words);
        count += markdownBlocks(words);
      } else {
        const [hasCard, pieces] = ReplySink.toolElements(part, floor, spanCeil);
        if (hasCard && (held === null || held.cards.has(part.update.id))) {
          if (!fixed && count >= limit) return [blocks, [index, 0]];
          const card = this.cardBlocks(part.update);
          blocks.push(...card);
          count += card.length;
        }
        for (const piece of pieces) {
          const length = ReplySink.weight(message, part, piece, posting);
          if (held !== null && !held.pieces.has(pieceKey(index, piece))) {
            // Late: left out, with the note when its card was shown, once.
            if (noteRoom && !noted && held.cards.has(part.update.id)) {
              blocks.push(contextBlock(PREVIEW_CUT));
              noted = true;
              count += 1;
            }
            continue;
          }
          if (!fixed) {
            if (count + 1 > limit || size + length > MESSAGE_LIMIT) {
              return [blocks, [index, piece]];
            }
          } else if (leftOut.has(pieceKey(index, piece))) {
            // A preview that arrives after its card, in a message whose span is fixed and
            // full: it is cut, and says so once, rather than pass the limit (Slack would
            // refuse the whole update, the card with it).
            if (!noted) {
              blocks.push(contextBlock(PREVIEW_CUT));
              noted = true;
              count += 1;
            }
            continue;
          }
          const shown = pieceBlocks(part, piece);
          if (!fixed && count + blocksCount(shown) > limit) return [blocks, [index, piece]];
          blocks.push(...shown);
          size += length;
          count += blocksCount(shown);
        }
      }
    }
    return [blocks, null];
  }

  /**
   * A tool's card, or once the reply's body has ended what a card of a run of calls folds to: a
   * line of counts, as the channel model drew it, or nothing.
   */
  private cardBlocks(update: TaskUpdate): Block[] {
    if (!this.finished || update.folded === null) return [cardBlock(update)];
    return update.folded ? [contextBlock(mrkdwnEscape(update.folded))] : [];
  }

  /**
   * What the message shows of the model as it is now: as much of it as fits. Words can reach
   * the model while the end is being written; past the limits they were never shown, and
   * holding them would have every later edit of the message refused.
   */
  private hold(message: Message): Held {
    const held = new Held();
    const fits = this.render(message, null, null)[1];
    for (const { index, part, floor, ceil } of this.span(message.start, fits)) {
      if (part instanceof Text) {
        if (stripNewlines(part.text.slice(floor, ceil ?? undefined))) {
          held.text.set(index, ceil === null ? part.text.length : ceil);
        }
      } else {
        const [hasCard, pieces] = ReplySink.toolElements(part, floor, ceil);
        if (hasCard) held.cards.add(part.update.id);
        for (const piece of pieces) held.pieces.add(pieceKey(index, piece));
      }
    }
    return held;
  }

  /**
   * The characters a preview piece counts toward MESSAGE_LIMIT in a message: its text, unless
   * it is drawn as collapsed containers, the message is written by `chat.update` and Slack has
   * not refused an update of it for them.
   */
  private static weight(message: Message, tool: Tool, piece: number, posting: boolean): number {
    if (!posting && !message.containersCounted && pieceCollapsed(tool)) return 0;
    return tool.pieceSize(piece);
  }

  /**
   * The preview pieces a fixed span has no room for, once the text and the cards, and the one
   * note that says pieces were left out, have theirs. Pieces are taken in order.
   */
  private cutPieces(
    message: Message,
    end: Cursor,
    baseBlocks: number,
    baseSize: number,
  ): Set<string> {
    let cut = new Set<string>();
    // a note is owed as soon as one piece is cut
    for (const reserved of [0, 1]) {
      cut = new Set<string>();
      let usedBlocks = 0;
      let usedSize = 0;
      for (const { index, part, floor, ceil } of this.span(message.start, end)) {
        if (!(part instanceof Tool)) continue;
        for (const piece of ReplySink.toolElements(part, floor, ceil)[1]) {
          const length = ReplySink.weight(message, part, piece, false);
          if (
            baseBlocks + usedBlocks + 1 + reserved > message.blocksRoom ||
            baseSize + usedSize + length > MESSAGE_LIMIT
          ) {
            cut.add(pieceKey(index, piece));
          } else {
            usedBlocks += 1;
            usedSize += length;
          }
        }
      }
      if (cut.size === 0) break;
    }
    return cut;
  }

  /**
   * The blocks and characters the text and the cards of a fixed span take: what is left of the
   * limits is what its previews may.
   */
  private base(message: Message, end: Cursor): [blocks: number, size: number] {
    let blocks = 0;
    let size = 0;
    for (const { part, floor, ceil } of this.span(message.start, end)) {
      if (part instanceof Text) {
        const words = stripNewlines(part.text.slice(floor, ceil ?? undefined));
        if (words) {
          blocks += markdownBlocks(words);
          size += len(words);
        }
      } else if (ReplySink.toolElements(part, floor, ceil)[0]) {
        blocks += 1;
      }
    }
    return [blocks, size];
  }

  /**
   * Bring a stopped message to what the model says, with a `chat.update` when it shows something
   * else: (written, where the reply goes on past the message). An update never notifies
   * (measured 2026-09-29). Nothing is written for a message whose stream already shows the
   * model. An update refused for content that held a container, which counted for nothing, is
   * tried once more with the containers counted, as a post counts them. With `split`, for a
   * caller that opens the next message at the cursor returned: an update Slack refuses for too
   * many blocks is tried again with less of the span (`tighten`); `room` is the room the message
   * had before the first of those tries.
   */
  private async updateStep(
    message: Message,
    end: Cursor | null,
    split = false,
    room = 0,
  ): Promise<Step> {
    const ts = message.ts;
    if (ts === null) throw new Error("an update of a message that was never written");
    let [blocks, overflow] = this.blocks(message, end);
    let footer = this.footerOf(message);
    blocks = [...blocks, ...footer];
    if (blocks.length === 0 || this.current(message, blocks, footer, end)) return [true, overflow];
    try {
      await this.limiter.acquire();
      // A change can arrive while this write waits its turn: send what the reply looks like
      // right now rather than the snapshot taken before the wait.
      let fresh: Block[];
      [fresh, overflow] = this.blocks(message, end);
      footer = this.footerOf(message);
      fresh = [...fresh, ...footer];
      if (fresh.length === 0 || this.current(message, fresh, footer, end)) {
        await this.limiter.refund(); // caught up: no write follows
        return [true, overflow];
      }
      blocks = fresh;
      this.wrote = true;
      await this.slack.chat.update({
        channel: this.channel,
        ts,
        text: this.banner(this.span(message.start, end)),
        blocks,
      });
    } catch (error) {
      const code = describe(error);
      const [text, elements, cards] = blocksSizes(blocks);
      this.logger.warning(
        `chat.update failed (${describeRefusal(error)}) with text ${text}, ` +
          `elements ${elements}, cards ${cards}`,
      );
      if (code === STILL_STREAMING) {
        // The daemon's stop never reached Slack: stopped now, the next write passes.
        await this.stop(message, null, end);
      } else if (REFUSED_CONTENT.has(code)) {
        if (!message.containersCounted && blocks.some((block) => block.type === "container")) {
          message.containersCounted = true;
          return this.updateStep(message, end, split, room);
        }
        if (split && end === null && this.held === null && tooManyBlocks(error)) {
          const before = room || message.blocksRoom;
          if (ReplySink.tighten(message, blocks)) {
            return this.updateStep(message, end, true, before);
          }
          // Refused down to one block: nothing was written, so nothing is cut.
          message.blocksRoom = before;
          [blocks, overflow] = this.blocks(message, end);
          blocks = [...blocks, ...footer];
        }
        // The message already shows what it showed: never replaced by a plainer one. The
        // change is dropped; the next one is tried. Nothing else would say the message is
        // short of the model, so that is kept until an update passes. A stream never
        // rewritten (`shown` is null) that was sent all of its span lacks only how the update
        // would have drawn it, its cards folded: it is not short.
        const whole = message.shown === null && this.streamShows(message, end, false);
        message.shown = blocks;
        message.exact = false;
        message.footer = footer;
        message.short = message.short || !whole;
        return [true, overflow];
      }
      return [false, null];
    }
    message.shown = blocks;
    message.exact = false;
    message.footer = footer;
    message.short = false;
    return [true, overflow];
  }

  /**
   * Slack counted more blocks in a write of the message than the daemon did: halve the room the
   * message has, so that its next write holds less and the reply goes on in a new message.
   * False, with the room as it was, when one block was already too many.
   */
  private static tighten(message: Message, blocks: readonly Block[]): boolean {
    if (message.blocksRoom <= 1) return false;
    message.blocksRoom = Math.max(
      1,
      Math.floor(Math.min(message.blocksRoom, blocksCount(blocks)) / 2),
    );
    return true;
  }

  /** Whether the message already shows `blocks`. */
  private current(
    message: Message,
    blocks: readonly Block[],
    footer: readonly Block[],
    end: Cursor | null,
  ): boolean {
    if (message.exact) {
      return isDeepStrictEqual(message.footer, footer) && this.streamShows(message, end);
    }
    return message.shown !== null && isDeepStrictEqual(message.shown, blocks);
  }

  /**
   * Slack refused the blocks of a message not yet posted: without this one retry it would never
   * show, so it is posted as text alone. Never used on a message that shows something already,
   * which a plainer form would replace.
   */
  private async writePlain(message: Message, blocks: readonly Block[]): Promise<boolean> {
    let posted: { ts?: string };
    try {
      this.wrote = true;
      posted = await this.creating.chat.postMessage({
        channel: this.channel,
        thread_ts: this.threadTs,
        text: plainText(blocks),
        unfurl_links: false,
        unfurl_media: false,
      });
    } catch (error) {
      this.logger.warning(`could not write a reply to Slack as plain text: ${describe(error)}`);
      return false;
    }
    message.ts = String(posted.ts);
    message.shown = [];
    message.exact = false;
    message.footer = [];
    this.retrack();
    return true;
  }

  /**
   * Post the message that continues a stopped one, with the blocks of its span as a post takes
   * them, then bring it to what an update takes: a container's text counts toward a post's limit
   * and not toward an update's, so what the post left out of its span reaches the same message
   * and no further one is opened for it.
   */
  private async postStep(message: Message, split = false): Promise<Step> {
    let [blocks, overflow] = this.blocks(message, null, true);
    const footer = this.footerOf(message);
    blocks = [...blocks, ...footer];
    if (blocks.length === 0) return [true, overflow];
    const attempted = this.clock.time();
    const banner = this.banner(this.span(message.start, null));
    let posted: { ts?: string };
    try {
      // Claude's text can carry a link built to leak data when Slack fetches it for a preview:
      // no previews for anything the daemon posts.
      this.wrote = true;
      posted = await this.creating.chat.postMessage({
        channel: this.channel,
        thread_ts: this.threadTs,
        text: banner,
        blocks,
        unfurl_links: false,
        unfurl_media: false,
      });
    } catch (error) {
      const [text, elements, cards] = blocksSizes(blocks);
      this.logger.warning(
        `chat.postMessage failed (${describeRefusal(error)}) with text ${text}, ` +
          `elements ${elements}, cards ${cards}`,
      );
      if (split && tooManyBlocks(error) && ReplySink.tighten(message, blocks)) {
        return this.postStep(message, true);
      }
      if (unknownOutcome(error)) {
        const ts = await this.adopt(attempted, false, take(plainWords(banner), ADOPT_WORDS));
        if (ts !== null) {
          message.ts = ts;
          message.shown = blocks;
          message.footer = footer;
          this.retrack();
          return [true, overflow];
        }
      }
      if (
        this.closedOut &&
        REFUSED_CONTENT.has(describe(error)) &&
        (await this.writePlain(message, blocks))
      ) {
        return [true, overflow];
      }
      return [false, null];
    }
    message.ts = String(posted.ts);
    message.shown = blocks;
    message.footer = footer;
    this.retrack();
    return this.updateStep(message, null, split);
  }

  /**
   * The start of what a stream's first chunks say, as plain words: empty when there are none to
   * compare (then nothing is adopted). A stream's `text` reads back with a card's title and with
   * a container's title in it (recorded 2026-09-28, slack-sdk 3.44.1).
   */
  private static firstWords(plan: Plan): string {
    const first = plan.chunks[0];
    let words = "";
    if (first === undefined) {
      words = "";
    } else if (first.type === "blocks") {
      const block = first.blocks[0];
      if (block !== undefined) {
        words = (block.type === "container" ? block.title.text : "") || blockText(block);
      }
    } else if (first.type === "markdown_text") {
      words = first.text;
    } else {
      words = first.title;
    }
    return take(plainWords(words), ADOPT_WORDS);
  }

  /**
   * A create that failed on the connection may have landed. Read the thread back, and return
   * the ts of the daemon's own message newer than the attempt that carries `probe` (a stream:
   * `streaming_state`, and the start of its text), or null. Writing again first would make it
   * twice.
   */
  private async adopt(attempted: number, stream: boolean, probe: string): Promise<string | null> {
    let found: unknown;
    try {
      const read = await this.slack.conversations.replies({
        channel: this.channel,
        ts: this.threadTs,
        oldest: (attempted - ADOPT_SKEW_SECONDS).toFixed(6),
        limit: 200,
      });
      found = read.messages;
    } catch (error) {
      this.logger.warning(`could not read the thread back for a lost write: ${describe(error)}`);
      return null;
    }
    const known = new Set(this.messages.map((message) => message.ts));
    known.add(this.closing);
    for (const item of Array.isArray(found) ? found : []) {
      if (typeof item !== "object" || item === null) continue;
      const message = item as Record<string, unknown>;
      const ts = String(message.ts);
      if (ts === this.threadTs || known.has(ts) || message.user !== this.botUserId) continue;
      if (stream && !("streaming_state" in message)) continue;
      if (probe && plainWords(String(message.text ?? "")).includes(probe)) return ts;
    }
    return null;
  }

  /**
   * Tell the message's stream what it lacks, starting it with the first content: (written,
   * where the reply goes on past the message).
   */
  private async streamStep(message: Message): Promise<Step> {
    let plan = this.plan(message);
    if (plan.chunks.length === 0) return [true, plan.overflow];
    if (message.ts === null) {
      const attempted = this.clock.time();
      let streamTs: string;
      try {
        this.wrote = true;
        const started = await this.creating.chat.startStream({
          channel: this.channel,
          thread_ts: this.threadTs,
          recipient_team_id: this.teamId,
          recipient_user_id: this.userId,
          chunks: plan.chunks,
          task_display_mode: "timeline",
        });
        streamTs = String(started.ts);
      } catch (error) {
        const unknown = unknownOutcome(error);
        this.logger.warning(
          `could not start a reply's stream: ${describe(error)}` +
            (unknown
              ? " (outcome unknown: the thread is read back for a stream it may have made)"
              : ""),
        );
        const ts = unknown ? await this.adopt(attempted, true, ReplySink.firstWords(plan)) : null;
        if (ts === null) return [false, null];
        streamTs = ts; // it landed: this is that stream
      }
      message.ts = streamTs;
      message.streaming = true;
      message.started = attempted;
      ReplySink.sent(message, plan);
      this.retrack();
      message.deadline = new Task((signal) => this.expire(message, signal));
      return [true, plan.overflow];
    }
    try {
      await this.limiter.acquire();
      // A change can arrive while this write waits its turn: tell the stream what it lacks
      // right now, not the snapshot taken before the wait.
      plan = this.plan(message);
      if (plan.chunks.length === 0) {
        await this.limiter.refund(); // caught up: no write follows
        return [true, plan.overflow];
      }
      this.wrote = true;
      await this.creating.chat.appendStream({
        channel: this.channel,
        ts: message.ts,
        chunks: plan.chunks,
      });
    } catch (error) {
      const code = describe(error);
      if (code === TOO_LONG) {
        // Slack's cap follows what it stores, which the plan only estimates, and it would
        // refuse the same append again. The stream is stopped bare, as at STREAM_SECONDS, and
        // the message goes on by update, from the model; the end then posts the closing
        // message. Only this code: any other refusal stays a failed write, which the session
        // shows.
        const cards = new Set([...message.cards.keys(), ...plan.cards.keys()]).size;
        this.logger.warning(
          `chat.appendStream refused (${code}) with text ${plan.size}, elements ${plan.count}, ` +
            `cards ${cards}, card text sent ${message.cardText} and ${planCardText(plan)} more ` +
            "in this append: the stream is stopped and the message goes on by update",
        );
        message.refused = true;
        if ((await this.stop(message, null, null)) === "failed") return [false, null];
        return this.updateStep(message, null, true);
      }
      this.logger.warning(`could not write a reply to Slack: ${code}`);
      if (code === NOT_STREAMING) {
        // Slack ended the stream first: the message goes on by update.
        this.gone(message);
        return this.updateStep(message, null, true);
      }
      if (unknownOutcome(error)) {
        // An append is not idempotent: sent again it may show twice. The stream is told
        // nothing more; the message is stopped and goes on by update, from the model.
        this.logger.warning("a stream append's outcome is unknown: the message goes on by update");
        message.blind = true;
      }
      return [false, null];
    }
    ReplySink.sent(message, plan);
    return [true, plan.overflow];
  }

  /** The message's stream is over, however it ended. */
  private gone(message: Message): void {
    message.streaming = false;
    // Its own deadline may be what stopped it: past its waits, a cancel changes nothing there.
    message.deadline?.cancel();
    message.deadline = null;
  }

  /**
   * Stop the message's stream, with `blocks` at its bottom: "stopped", "gone" (Slack had ended
   * it already) or "failed". A stop notifies; what the stream still lacks is left for the
   * update that follows.
   */
  private async stop(
    message: Message,
    blocks: Block[] | null,
    end: Cursor | null,
  ): Promise<"stopped" | "gone" | "failed"> {
    const ts = message.ts;
    if (ts === null) throw new Error("a stop of a stream that never started");
    let footer = blocks !== null && blocks.length > 0 ? blocks : null;
    let result: "stopped" | "gone" = "stopped";
    try {
      this.wrote = true;
      await this.creating.chat.stopStream({
        channel: this.channel,
        ts,
        ...(footer === null ? {} : { blocks: footer }),
      });
    } catch (error) {
      if (describe(error) !== NOT_STREAMING) {
        this.logger.warning(`could not stop a reply's stream: ${describe(error)}`);
        if (unknownOutcome(error) && footer !== null && message.stopUnknown === null) {
          message.stopUnknown = [...footer];
        }
        return "failed";
      }
      if (message.stopUnknown !== null && !this.pastLife(message)) {
        // The stop that failed on the connection is the one that landed, footer and all.
        footer = message.stopUnknown;
        result = "stopped";
      } else {
        // Over without a footer of ours, or past the stream's life, when Slack's own end
        // cannot be told from our stop landing: the end posts a footer of its own.
        result = "gone";
      }
      message.stopUnknown = null;
    }
    this.gone(message);
    message.stopUnknown = null;
    message.exact = this.streamShows(message, end);
    message.footer = result === "stopped" ? [...(footer ?? [])] : [];
    return result;
  }

  private pastLife(message: Message): boolean {
    return this.clock.time() - message.started >= STREAM_LIFE;
  }

  /**
   * STREAM_SECONDS after the stream started: stop it before Slack does, and go on by update.
   * The stop notifies (accepted); the updates after it never do.
   */
  private async expire(message: Message, signal: AbortSignal): Promise<void> {
    await this.clock.sleep(STREAM_SECONDS, signal);
    const release = await this.lock.acquire(signal);
    try {
      if (!message.streaming) return;
      if ((await this.stop(message, null, null)) === "failed") {
        return; // Slack ends it at 5 minutes: the next append finds it over
      }
      await this.sync();
    } finally {
      release();
    }
  }

  // The one place a reply is written.

  /**
   * Write what changed; false when a write failed and the reply is not as it should be. A write
   * can take real time (the limiter, a round trip), during which the reply can change again:
   * what is written is what the reply looks like right now, never the snapshot taken before the
   * wait, so a wait never drops a change. `later` runs this again when `version` moved during
   * it. A pass: never cancelled (see the top of this file).
   */
  private async flush(): Promise<boolean> {
    const release = await this.lock.acquire();
    try {
      const ok = await this.sync();
      if (ok && this.closedOut) this.settleOpenReply();
      return ok;
    } finally {
      release();
    }
  }

  /**
   * Bring every message of the reply to the model, then, once the reply has ended, end it, and
   * follow which messages a crash would leave unfinished. The lock is held.
   */
  private async sync(): Promise<boolean> {
    try {
      return await this.syncMessages();
    } finally {
      this.retrack();
      if (this.wrote) {
        this.wrote = false;
        this.onWrite?.();
      }
    }
  }

  private async syncMessages(): Promise<boolean> {
    if (this.messages.length === 0) {
      if (!this.hasContent()) return this.closedOut ? this.end() : true;
      this.messages.push(new Message([0, 0], "stream"));
    }
    for (let i = 0; i + 1 < this.messages.length; i += 1) {
      const message = this.messages[i];
      const following = this.messages[i + 1];
      if (message === undefined || following === undefined || message.ts === null) continue;
      const rev = this.spanRev(message, following.start);
      if (
        message.checked !== null &&
        message.checked.rev === rev &&
        sameCursor(message.checked.end, following.start)
      ) {
        continue; // nothing in it changed since it was last brought to the model
      }
      if (!(await this.updateStep(message, following.start))[0]) return false;
      message.checked = { rev, end: following.start };
    }
    for (;;) {
      const message = this.messages.at(-1);
      if (message === undefined) return false;
      if (message.streaming && message.blind) {
        // Told nothing more: stopped now (with the footer, if the reply has ended), and the
        // update below writes the whole message from the model.
        const footer = this.closedOut ? this.closingBlocks() : null;
        const result = await this.stop(message, footer, null);
        if (result === "failed") return false;
        if (this.closedOut && result === "stopped") this.endMode = "inline";
        message.exact = false;
      }
      let step: Step;
      if (message.ts === null) {
        step =
          message.mode === "stream"
            ? await this.streamStep(message)
            : await this.postStep(message, true);
      } else if (message.streaming) {
        step = await this.streamStep(message);
      } else {
        step = await this.updateStep(message, null, true);
      }
      const [ok, overflow] = step;
      if (!ok) return false;
      if (overflow === null) break;
      if (sameCursor(overflow, message.start)) {
        this.logger.error("a reply's message cannot hold its first element");
        return false;
      }
      const mode = message.streaming ? "stream" : "post";
      if (message.streaming && (await this.stop(message, null, overflow)) === "failed") {
        return false;
      }
      this.messages.push(new Message(overflow, mode));
    }
    if (!this.closedOut) return true;
    // The end is written either way; a message left short of the model makes it one that did
    // not land, which the session shows.
    const ended = await this.end();
    const last = this.messages.at(-1);
    if (this.held === null && last !== undefined && this.endLanded()) this.held = this.hold(last);
    return ended && !this.messages.some((message) => message.short);
  }

  /**
   * The reply's end, on Slack: the footer on the last stream's stop; once the stream is over,
   * under the reply's ending, posted as a new message, or in a closing message of its own when
   * there is no ending to move (`endingCursor`). A continuation that opened on the line on how
   * the reply ended is the ending as it stands (`opensOnEnding`): nothing is posted, and the
   * footer goes under it by edit.
   */
  private async end(): Promise<boolean> {
    const message = this.messages.at(-1);
    if (message === undefined) return true; // nothing was ever shown: nothing to end
    if (this.endMode === "inline" || this.endMode === "moved") {
      // The stream's stop carried the footer, or the ending posted as the reply's last message
      // does: a later change edits that message (`syncMessages`).
      return true;
    }
    if (message.streaming) {
      const result = await this.stop(message, this.closingBlocks(), null);
      if (result === "failed") return false;
      if (result === "stopped") this.endMode = "inline";
      // A stream cannot fold its cards: the update that follows its stop does, silently. The
      // reply has ended whatever becomes of that write: one that fails is tried once more with
      // the next pass, and the cards stay if that fails too.
      if (!(await this.updateStep(message, null))[0]) {
        this.version += 1;
        this.schedule();
      }
      if (result === "stopped") return true;
    }
    if (this.endMode === null) {
      const cursor = this.endingCursor(message);
      if (cursor !== null) {
        // The reply's ending, with the footer, as a new message: the one that notifies, so the
        // notification says how the work ended. Posted first, then taken out of the message it
        // grew in: for a moment it shows twice, never nowhere.
        const ending = new Message(cursor, "post");
        this.messages.push(ending);
        this.endMode = "moved"; // the footer goes under it, in the same post
        try {
          await this.postStep(ending);
        } finally {
          if (ending.ts === null) {
            // Not posted: as if never tried, so the next pass posts it before it shortens
            // anything.
            this.messages.pop();
            this.endMode = null;
          }
        }
        if (this.endMode === null) return false;
        // its stream said more than its span
        message.exact = false;
        message.checked = null;
        return (await this.updateStep(message, cursor))[0];
      }
      if (this.opensOnEnding(message)) {
        // A full message pushed the line into this one, which notified with it: a closing
        // message after it would ring again with nothing to show. The footer, if there is
        // one, goes under it by a silent edit.
        this.endMode = "moved";
        return (await this.updateStep(message, null))[0];
      }
    }
    this.endMode = "post";
    this.bodyLanded = true; // every message is written: only the closing message is owed
    return this.writeClosing();
  }

  /**
   * Whether the reply's end is on Slack: the footer rode on the last stream's stop ("inline",
   * set after a stop that landed) or sits in the ending posted as the last message ("moved",
   * reset when that post fails), or the closing message was posted. A continuation that is the
   * ending as it stands is "moved" too, from before its footer is written: it rang when it was
   * posted, and an edit that fails is written by the next pass. Not `endMode === "post"`, which
   * `end` sets before the closing message is written.
   */
  private endLanded(): boolean {
    return this.endMode === "inline" || this.endMode === "moved" || this.closing !== null;
  }

  /**
   * Post the closing message of a reply whose stream stopped early and that has no ending to
   * move into a message of its own (`end`), or bring it to the footer as it stands: it stays
   * once posted, since it is what notified. Its text is Claude's own words, as a banner: never
   * a line of the daemon's. With no footer either it holds one zero-width space: the known case
   * is a turn that ended well, text alone, whose footer could not be built.
   */
  private async writeClosing(): Promise<boolean> {
    let blocks = this.closingBlocks();
    if (blocks.length === 0) blocks = [contextBlock(ZERO_WIDTH_SPACE)];
    const attempted = this.clock.time();
    try {
      if (this.closing === null) {
        this.wrote = true;
        const posted = await this.creating.chat.postMessage({
          channel: this.channel,
          thread_ts: this.threadTs,
          text: this.banner(),
          blocks,
          unfurl_links: false,
          unfurl_media: false,
        });
        this.closing = String(posted.ts);
      } else if (!isDeepStrictEqual(blocks, this.closingShown)) {
        // An edit never notifies (measured 2026-09-27): the text stays as posted.
        await this.limiter.acquire();
        this.wrote = true;
        await this.slack.chat.update({
          channel: this.channel,
          ts: this.closing,
          text: this.banner(),
          blocks,
        });
      }
    } catch (error) {
      this.logger.warning(`could not write a reply's closing message: ${describe(error)}`);
      if (this.closing === null && unknownOutcome(error)) {
        this.closing = await this.adopt(
          attempted,
          false,
          take(plainWords(this.banner()), ADOPT_WORDS),
        );
        if (this.closing !== null) {
          this.closingShown = blocks;
          return true;
        }
      }
      return false;
    }
    this.closingShown = blocks;
    return true;
  }
}
