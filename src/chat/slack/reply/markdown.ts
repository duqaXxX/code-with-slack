/**
 * Claude's markdown as Slack reads it: how many blocks it makes of a text and where to cut one,
 * and the text with its markers gone, for a notification's banner.
 *
 * Every pattern here is Python's, ported for what differs between the two engines: Python's `\s`
 * and `\w` are Unicode (`PY_SPACE`, `WORD`), its `.` stops at `\n` only, and its `^` under
 * `re.M` starts a line after `\n` only (JavaScript's also after `\r`, U+2028 and U+2029).
 */
import { PY_SPACE } from "../../../core/reply/words.ts";
import { blank, len, strip } from "./chars.ts";
import { mrkdwnEscape } from "./escape.ts";

const S = `[${PY_SPACE}]`;
// Python's `\w` on text: a letter or a digit of any script, or `_`.
const WORD = "[\\p{L}\\p{N}_]";

// Slack stores a `markdown` block, and a stream's `markdown_text`, as several blocks: a header
// per heading, a table per table, a divider per rule, and rich text for each run of anything
// else between them. A write by `chat.update` or `chat.postMessage` is refused when the message
// passes 50 of them; a stream is not, and the update that follows its stop is (measured
// 2026-10-08, slack-sdk 3.45.0). Quotes, lists, images, bold lines and code blocks stay in the
// rich text around them, and a `#` inside a code block is not a heading.
// Read as CommonMark and GFM define them where Slack was not measured (a heading underlined
// with `=`, a fence longer than three marks, a rule with spaces in it): a shape counted that
// Slack keeps in its rich text only ends a message early.
// Sticky, so that it reads a heading at an offset of a whole text as well as at a line's start.
const HEADING = new RegExp(` {0,3}#{1,6}(?:${S}|$)`, "y");
const UNDERLINE = new RegExp(`^ {0,3}=+${S}*$`);
const RULE = new RegExp(`^ {0,3}([-*_])(?:${S}*\\1){2,}${S}*$`);
const TABLE_RULE = new RegExp(`^${S}*\\|?${S}*:?-+:?${S}*(?:\\|${S}*:?-+:?${S}*)*\\|?${S}*$`);
const FENCE = /^ {0,3}(`{3,}|~{3,})([^\n]*)$/;

/** Whether a heading starts at `offset` of `text`. */
export function headingAt(text: string, offset: number): boolean {
  HEADING.lastIndex = offset;
  return HEADING.test(text);
}

/** Python's `line.rstrip("\r")`. */
function rstripReturns(line: string): string {
  let end = line.length;
  while (end > 0 && line.charCodeAt(end - 1) === 0x0d) end -= 1;
  return line.slice(0, end);
}

/** Where each block starts that Slack makes of a markdown text, as offsets into it. */
export function markdownStarts(text: string): number[] {
  const starts: number[] = [];
  const lines = text.split("\n");
  let offset = 0;
  let fence = "";
  let running = false;
  let table = false;
  for (const [number, raw] of lines.entries()) {
    const here = offset;
    offset += raw.length + 1;
    const line = rstripReturns(raw);
    const mark = FENCE.exec(line);
    const run = mark?.[1] ?? "";
    const info = mark?.[2] ?? "";
    if (fence) {
      // Closed by a run of the same mark, at least as long, with nothing after it.
      if (mark !== null && run.startsWith(fence) && blank(info)) fence = "";
    } else if (mark !== null && !info.includes("`")) {
      fence = run;
      table = false;
    } else {
      if (blank(line)) {
        table = false;
        continue;
      }
      if (table && line.includes("|")) continue;
      table = false;
      const following = rstripReturns(lines[number + 1] ?? "");
      if (line.includes("|") && following.includes("|") && TABLE_RULE.test(following)) {
        starts.push(here);
        table = true;
        running = false;
        continue;
      }
      if (headingAt(line, 0) || RULE.test(line) || (running && UNDERLINE.test(line))) {
        starts.push(here);
        running = false;
        continue;
      }
    }
    if (!running) {
      starts.push(here);
      running = true;
    }
  }
  return starts;
}

/** How many blocks a markdown text with words in it counts in a message. */
export function markdownBlocks(text: string): number {
  return Math.max(1, markdownStarts(text).length);
}

/**
 * Where to cut a markdown text so that it makes at most `room` blocks, at a block's start no
 * earlier than `floor` (what a stream was already sent); null when nothing is to cut. A heading
 * is never the last block before the cut, nor the last of a text that fills the room: it opens
 * the next message, with the text it heads.
 */
export function markdownCut(text: string, room: number, floor = 0): number | null {
  const starts = markdownStarts(text);
  let index: number | null = starts.length === room ? starts.length : null;
  if (starts.length > room) {
    index = null;
    for (let i = room; i < starts.length; i += 1) {
      if ((starts.at(i) ?? 0) >= floor) {
        index = i;
        break;
      }
    }
  }
  if (index === null) return null;
  for (;;) {
    const before = starts[index - 1];
    if (index <= 1 || before === undefined || before < floor || !headingAt(text, before)) break;
    index -= 1;
  }
  return starts[index] ?? null;
}

// A pattern that reads ahead and then fails is tried again from every later start, which is
// quadratic on a long run of `[`, of blank lines or of `_` inside a word. So the three that read
// ahead also match what they read where no marker is (a `[` no link closes, space no marker
// follows, `_` between two letters), and the replacement puts that back as it was.
const LINK = /\[([^\]]*)(?:\](?:\([^)]*(\))?)?)?/g;
// `(?<![^\n])` is Python's `^` under `re.M`; `\p{Nd}` its `\d`.
const LINE_MARKER = new RegExp(`(?<![^\\n])${S}*((?:#+|>|[-+*]|\\p{Nd}+\\.)${S}+)?`, "gu");
const PAIRED = /\*\*|__|~~|`+/g;
const EMPHASIS = new RegExp(`(?<!${WORD})[*_]+|[*_]+(?!${WORD})|(_+)`, "gu");

/**
 * The markdown markers of `text` removed, nothing escaped, in a time linear in the text: a
 * banner is cut from a paragraph as long as Claude wrote it.
 */
export function stripMarkdown(text: string): string {
  let plain = text.replace(LINK, (all: string, label: string, closed: string | undefined) =>
    closed ? label : all,
  );
  plain = plain.replace(LINE_MARKER, (all: string, marker: string | undefined) =>
    marker ? "" : all,
  );
  plain = plain.replace(PAIRED, "");
  plain = plain.replace(EMPHASIS, (_all: string, kept: string | undefined) => kept ?? "");
  return strip(plain);
}

/**
 * A paragraph of Claude's markdown as the plain `text` of a notification: the markers gone
 * (headings, quotes, list bullets, emphasis, code ticks, link targets) and `&`, `<`, `>`
 * escaped, since Slack reads them as markup there too. With `limit` it is cut before it is
 * escaped, so the result holds at most that many characters and no half an entity.
 */
export function bannerText(paragraph: string, options: { limit?: number } = {}): string {
  const plain = stripMarkdown(paragraph);
  const limit = options.limit;
  if (limit === undefined) return mrkdwnEscape(plain);
  const out: string[] = [];
  let size = 0;
  // By code point, as Python walks a string: an emoji is one character of the limit.
  for (const char of plain) {
    const piece = mrkdwnEscape(char);
    const cost = len(piece);
    if (size + cost > limit) break;
    out.push(piece);
    size += cost;
  }
  return out.join("");
}

const LABELLED = /<[^|>]*\|([^>]*)>/g;
const WORDS = new RegExp(`${WORD}+`, "gu");

/**
 * The words of a text with the markup, the punctuation and the spacing gone: what two
 * renderings of one text (Claude's markdown and Slack's converted read-back of it, with `*b*`
 * for `**b**`, `<url|label>` for a link, `•` for a bullet) have in common.
 */
export function plainWords(text: string): string {
  const words = stripMarkdown(text.replace(LABELLED, "$1")).match(WORDS) ?? [];
  return words.join(" ").toLowerCase();
}
