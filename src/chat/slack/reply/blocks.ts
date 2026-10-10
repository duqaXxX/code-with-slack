/**
 * The blocks and the stream chunks a reply is written with, and the limits they are held to.
 *
 * The shapes are the ones the Python daemon sent and the recordings read back; the pinned
 * `@slack/types` 3.2.0 declares them as `MarkdownBlock`, `ContextBlock`, `DividerBlock`,
 * `TaskCardBlock`, `ContainerBlock`, `RichTextBlock` and the chunks of `AnyChunk`.
 */
import { shownPreview } from "../../../core/reply/fold.ts";
import { STOPPED } from "../../../core/reply/words.ts";
import { NESTED } from "../../../core/texts.ts";
import type { TaskStatus, TaskUpdate } from "../../seam.ts";
import { at, lastNewline, len, take } from "./chars.ts";
import { shownAsWritten } from "./escape.ts";
import { markdownBlocks } from "./markdown.ts";

// A message holds at most 12,000 characters and 50 blocks or task cards (measured 2026-09-28),
// a text counting the blocks Slack makes of it (`markdownStarts`);
// the margins keep a preview that arrives after its card, and the footer, inside them. A stream
// and a post count the text of a collapsed container toward that cap; `chat.update` does not
// (measured 2026-10-06, slack-sdk 3.44.1: 50 containers of 10,000 characters taken, nothing ever
// refused). A message written by update therefore counts a container as its block only: at most
// BLOCKS_LIMIT containers of MESSAGE_LIMIT characters, 495,000, which is what was measured in the
// shape of a call with no card (45 of 11,000, taken).
export const MESSAGE_LIMIT = 11_000;
export const BLOCKS_LIMIT = 45;
export const FALLBACK_LIMIT = 3_000;
// What a message's `text` says: the banner of a notification, short, since a `chat.update` whose
// `text` is long fails `msg_too_long` (measured 2026-09-28).
export const BANNER_LIMIT = 300;
// A task card's details and output are text of a rich text element; a context block's text
// holds at most 3,000 characters.
export const CARD_TEXT_LIMIT = 2_900;
export const CARD_TITLE_LIMIT = 150;
export const CARD_TEXT_FIELDS = ["title", "details", "output"] as const;
// A stream keeps the details and the output of every `task_update` of a card, each added to what
// the card holds; an update that carries neither leaves them, and its title replaces the title
// (measured 2026-10-01 and 2026-10-08).
export const CARD_APPENDED = ["details", "output"] as const;
// What a card costs a streamed message besides its characters: the card, each of the two texts
// it holds, and each line of them. Slack stores a card's text as rich text, a line an element,
// and its cap follows what it stores. Replayed over the 15 streams of 2026-10-01 (slack-sdk
// 3.44.1), text and cards counted this way came to 13,514 at most in an accepted append and to
// 13,801 at least in a refused one; MESSAGE_LIMIT sits under both.
export const CARD_COST = 100;
export const CARD_FIELD_COST = 150;
export const CARD_LINE_COST = 50;
// What stands where a preview arrived too late for its message.
export const PREVIEW_CUT = "Preview left out: it did not fit this message.";
export const TERMINAL: readonly TaskStatus[] = ["complete", "error"];
// A container's title and subtitle (Block Kit reference).
const CONTAINER_TITLE_LIMIT = 150;

export interface TextElement {
  type: "text";
  text: string;
  style?: { code: boolean };
}

export interface RichText {
  type: "rich_text";
  elements: {
    type: "rich_text_section" | "rich_text_preformatted";
    language?: string;
    elements: TextElement[];
  }[];
}

export interface PlainTextObject {
  type: "plain_text";
  text: string;
  emoji?: boolean;
}

export interface MarkdownBlock {
  type: "markdown";
  text: string;
}

export interface ContextBlock {
  type: "context";
  elements: { type: "mrkdwn"; text: string }[];
}

export interface DividerBlock {
  type: "divider";
}

export interface TaskCardBlock {
  type: "task_card";
  task_id: string;
  title: string;
  status: TaskStatus;
  details?: RichText;
  output?: RichText;
}

export interface ContainerBlock {
  type: "container";
  title: PlainTextObject;
  rich_text_title?: RichText;
  subtitle?: PlainTextObject;
  width: "full";
  is_collapsible: boolean;
  default_collapsed: boolean;
  child_blocks: RichText[];
}

/** A block of a message, as a reply writes it. */
export type Block = MarkdownBlock | ContextBlock | DividerBlock | TaskCardBlock | ContainerBlock;

export interface CardChunk {
  type: "task_update";
  id: string;
  title: string;
  status: TaskStatus;
  details?: string;
  output?: string;
}

export interface TextChunk {
  type: "markdown_text";
  text: string;
}

export interface BlocksChunk {
  type: "blocks";
  blocks: Block[];
}

/** A chunk of a stream, as a reply writes it. */
export type Chunk = CardChunk | TextChunk | BlocksChunk;

/** What a card says: its title and state, and the one text it carries, if any. */
export interface CardFields {
  title: string;
  status: TaskStatus;
  details?: string;
  output?: string;
}

/** The details and the output Slack holds for a card. */
export type CardHeld = { details?: string; output?: string };

export function contextBlock(text: string): ContextBlock {
  return { type: "context", elements: [{ type: "mrkdwn", text }] };
}

/**
 * A `plain_text` text object. `emoji` false says `:name:` in `text` is not to be turned into an
 * emoji (text object reference, read 2026-10-06); left out when null.
 */
export function plainTextObject(text: string, emoji: boolean | null = null): PlainTextObject {
  const shown: PlainTextObject = { type: "plain_text", text };
  if (emoji !== null) shown.emoji = emoji;
  return shown;
}

// A text object's limit, as a context block's mrkdwn element holds one (Block Kit reference).
export const CONTEXT_LIMIT = 3000;

/** A daemon notice fitted into one context element: cut with `…` past its limit. */
export function noticeText(text: string): string {
  const end = at(text, CONTEXT_LIMIT);
  return end >= text.length ? text : `${take(text, CONTEXT_LIMIT - 1)}…`;
}

/**
 * A preview's lines of words as one context element's text: each indented under its card and
 * shown as written, the whole cut with `…` past the element's limit.
 */
export function plainLines(body: string): string {
  const lines = body.split("\n").map((line) => NESTED + shownAsWritten(line));
  return noticeText(lines.join("\n"));
}

// Invisible, so a block that holds only this one shows no text of its own; never pasted as a
// literal character in source, always by its code.
export const ZERO_WIDTH_SPACE = String.fromCodePoint(0x200b);

/**
 * A new file's first lines as code blocks, split where a block would pass its limit. A fence
 * inside the file must not close the block early: every run of three or more backticks is
 * broken up, as `shownAsWritten` breaks every one.
 */
export function previewBlocks(body: string): MarkdownBlock[] {
  const broken = body.replace(/`{3,}/g, (run) => run.split("").join(ZERO_WIDTH_SPACE));
  return split(broken)
    .filter((chunk) => chunk !== "")
    .map((chunk) => ({ type: "markdown", text: `\`\`\`\n${chunk}\n\`\`\`` }));
}

/**
 * A preview's body, collapsed: a full-width container per MESSAGE_LIMIT piece of it, closed
 * until the owner opens it. A call with no card is titled with its line, in code style as a tool
 * line is (`asCode`), and says the preview's sentence under it; under a card, which is the
 * call's line, the title is the sentence alone. The body sits in the message itself, so it opens
 * after a restart too.
 */
export function previewContainers(
  title: string,
  body: string,
  options: { subtitle?: string; language?: string; asCode?: boolean } = {},
): ContainerBlock[] {
  const { subtitle = "", language = "diff", asCode = false } = options;
  const shown = take(title, CONTAINER_TITLE_LIMIT);
  return split(body)
    .filter((chunk) => chunk !== "")
    .map((chunk) => {
      const block: ContainerBlock = {
        type: "container",
        title: { type: "plain_text", text: shown },
        width: "full",
        is_collapsible: true,
        default_collapsed: true,
        // A `markdown` block is not allowed in a container: rich text is, and its text is
        // literal, so no fence to break. Slack desktop colours `diff`; mobile colours nothing.
        child_blocks: [
          {
            type: "rich_text",
            elements: [
              {
                type: "rich_text_preformatted",
                ...(language ? { language } : {}),
                elements: [{ type: "text", text: chunk }],
              },
            ],
          },
        ],
      };
      // The plain title is the fallback of a client that does not draw the rich one.
      if (asCode) {
        block.rich_text_title = {
          type: "rich_text",
          elements: [
            {
              type: "rich_text_section",
              elements: [{ type: "text", text: shown, style: { code: true } }],
            },
          ],
        };
      }
      if (subtitle) {
        block.subtitle = { type: "plain_text", text: take(subtitle, CONTAINER_TITLE_LIMIT) };
      }
      return block;
    });
}

export function blockText(block: Block): string {
  switch (block.type) {
    case "markdown":
      return block.text;
    case "container":
      return block.child_blocks
        .flatMap((child) => child.elements.flatMap((pre) => pre.elements.map((e) => e.text)))
        .join("");
    case "context":
      return block.elements.map((element) => element.text).join("");
    default:
      return "";
  }
}

/**
 * What a refused write sent, for the log, never its content: the characters of text, the blocks,
 * and how many of them are task cards.
 */
export function blocksSizes(
  blocks: readonly Block[],
): [text: number, blocks: number, cards: number] {
  const cards = blocks.filter((block) => block.type === "task_card").length;
  const text = blocks.reduce((sum, block) => sum + len(blockText(block)), 0);
  return [text, blocks.length, cards];
}

/**
 * A message's text with no block: what a refused final write is retried with. Slack caps a
 * text-only message at 4,000 characters (chat.update reference, 2026-09-25).
 */
export function plainText(blocks: readonly Block[]): string {
  let body = blocks
    .flatMap((block) => (block.type === "markdown" ? [block.text] : []))
    .join("\n\n");
  const end = at(body, FALLBACK_LIMIT);
  if (end < body.length) body = `${body.slice(0, end)}…`;
  return body || "…";
}

/** Cut a body into messages of at most MESSAGE_LIMIT characters, at line breaks if possible. */
export function split(body: string): string[] {
  const chunks: string[] = [];
  let rest = body;
  for (;;) {
    // The index that ends MESSAGE_LIMIT characters: the end of the text when it has no more.
    const end = at(rest, MESSAGE_LIMIT);
    if (end >= rest.length) break;
    const cut = lastNewline(rest, end);
    if (cut <= 0) {
      chunks.push(rest.slice(0, end));
      rest = rest.slice(end);
    } else {
      chunks.push(rest.slice(0, cut));
      rest = rest.slice(cut + 1);
    }
  }
  chunks.push(rest);
  return chunks;
}

/**
 * What a tool's card says, in the terminal's words: the title, the state, what it is doing now
 * while it runs, and its output when it failed, was stopped, or has a preview's sentence. A
 * running call, a task and a stopped call keep the words their tool line had.
 */
export function cardFields(update: TaskUpdate): CardFields {
  const view = shownPreview(update);
  let title = view ? view.title : update.title;
  if (update.calls) {
    // How much a subagent has done: its latest call alone does not say.
    title += ` · ${update.calls} call${update.calls === 1 ? "" : "s"}`;
  }
  const fields: CardFields = { title: take(title, CARD_TITLE_LIMIT), status: update.status };
  const output = update.output;
  if (view) {
    if (view.summary) fields.output = take(view.summary, CARD_TEXT_LIMIT);
  } else if (output && (update.status === "error" || output === STOPPED)) {
    fields.output = take(output, CARD_TEXT_LIMIT);
  } else if (update.status === "in_progress" && update.details) {
    fields.details = take(update.details, CARD_TEXT_LIMIT);
  }
  return fields;
}

/**
 * What a tool's card says, as a `task_update` chunk. A stream is sent `cardAddition` of it: the
 * card is updated in place by its id, and its details and output only grow.
 */
export function cardChunk(update: TaskUpdate): CardChunk {
  return { type: "task_update", id: update.id, ...cardFields(update) };
}

/**
 * What to add to the text a card holds so that it ends with `wanted`: the lines of `wanted` past
 * those the card already ends with, a line break first. Empty when it lacks nothing. Ten equal
 * lines followed by an eleventh read as nothing new.
 */
export function lacking(held: string, wanted: string): string {
  if (!held) return wanted;
  const has = held.split("\n");
  const wants = wanted.split("\n");
  let shared = 0;
  for (let n = Math.min(has.length, wants.length); n > 0; n -= 1) {
    const tail = has.slice(-n);
    if (tail.every((line, i) => line === wants[i])) {
      shared = n;
      break;
    }
  }
  return wants
    .slice(shared)
    .map((line) => `\n${line}`)
    .join("");
}

function newlines(text: string): number {
  let count = 0;
  for (let i = text.indexOf("\n"); i !== -1; i = text.indexOf("\n", i + 1)) count += 1;
  return count;
}

/**
 * The chunk that brings a stream's card to `chunk`, what the card holds after it, and what it
 * costs the message. `sent` is the card's last chunk (undefined for a new card) and `held` the
 * details and the output Slack keeps for it. A card already in the message leaves out the
 * details that do not fit `room`, and keeps those it holds; its output, which says how the call
 * ended, is sent whatever the room.
 */
export function cardAddition(
  chunk: CardChunk,
  sent: CardChunk | undefined,
  held: CardHeld,
  room: number | null,
): [told: CardChunk, after: CardHeld, cost: number] {
  const told: CardChunk = {
    type: chunk.type,
    id: chunk.id,
    title: chunk.title,
    status: chunk.status,
  };
  const after: CardHeld = { ...held };
  let cost =
    sent === undefined
      ? CARD_COST + len(chunk.title)
      : Math.max(0, len(chunk.title) - len(sent.title));
  for (const key of CARD_APPENDED) {
    const wanted = chunk[key];
    if (wanted === undefined) continue; // nothing to say: the card keeps what it holds
    const has = held[key];
    const more = lacking(has ?? "", wanted);
    if (!more) continue;
    let price = len(more) + CARD_LINE_COST * (newlines(more) + (has === undefined ? 1 : 0));
    if (has === undefined) price += CARD_FIELD_COST;
    if (key === "details" && sent !== undefined && room !== null && cost + price > room) continue;
    told[key] = more;
    after[key] = (has ?? "") + more;
    cost += price;
  }
  return [told, after, cost];
}

export function richText(text: string): RichText {
  return {
    type: "rich_text",
    elements: [{ type: "rich_text_section", elements: [{ type: "text", text }] }],
  };
}

/**
 * The `task_card` block of a tool, for a message that is no longer a stream (recorded shape:
 * `details` and `output` are rich text).
 */
export function cardBlock(update: TaskUpdate): TaskCardBlock {
  const fields = cardFields(update);
  const block: TaskCardBlock = {
    type: "task_card",
    task_id: update.id,
    title: fields.title,
    status: fields.status,
  };
  for (const key of CARD_APPENDED) {
    const value = fields[key];
    if (value !== undefined) block[key] = richText(value);
  }
  return block;
}

/** The blocks of a message as Slack counts them. */
export function blocksCount(blocks: readonly Block[]): number {
  return blocks.reduce(
    (sum, block) => sum + (block.type === "markdown" ? markdownBlocks(block.text) : 1),
    0,
  );
}
