/**
 * Text counted and trimmed the way the Python daemon counted it, which is what the limits of a
 * reply were measured against.
 *
 * Python's `len` and its slices count code points; a JavaScript string counts UTF-16 units, two
 * for an emoji or any other character past U+FFFF (the red and green squares of every diff
 * preview are such). The rule the reply's code follows:
 *
 * - an offset into a text (a cursor, what a stream was sent up to, a block's start) is a UTF-16
 *   index, and offsets are only ever added to `.length` values and to each other;
 * - a size held against a limit (`MESSAGE_LIMIT`, a card's cost, a title's 150 characters) is
 *   `len`, in code points;
 * - a cut "after N characters" goes through `at` or `take`, which turn the count into the index
 *   that ends that many code points, so a cut never lands inside a surrogate pair.
 */
function isHigh(code: number): boolean {
  return code >= 0xd800 && code <= 0xdbff;
}

function isLow(code: number): boolean {
  return code >= 0xdc00 && code <= 0xdfff;
}

/** Python's `len(text)`: the code points of a text. */
export function len(text: string): number {
  let count = text.length;
  for (let i = 0; i < text.length - 1; i += 1) {
    if (isHigh(text.charCodeAt(i)) && isLow(text.charCodeAt(i + 1))) {
      count -= 1;
      i += 1;
    }
  }
  return count;
}

/** The UTF-16 index that ends the first `count` code points of `text`: where `text[:count]` cuts. */
export function at(text: string, count: number): number {
  if (count <= 0) return 0;
  // A text has at most as many code points as units: nothing to scan when it is that short.
  if (count >= text.length) return text.length;
  let index = 0;
  for (let taken = 0; taken < count && index < text.length; taken += 1) {
    index +=
      isHigh(text.charCodeAt(index)) && index + 1 < text.length && isLow(text.charCodeAt(index + 1))
        ? 2
        : 1;
  }
  return index;
}

/** Python's `text[:count]`. */
export function take(text: string, count: number): string {
  return text.slice(0, at(text, count));
}

// The same set as `PY_SPACE`, by code: a pattern that ends on `$` would start again from every
// run of spaces, which is quadratic on a long text.
function isSpace(code: number): boolean {
  return (
    (code >= 0x09 && code <= 0x0d) ||
    (code >= 0x1c && code <= 0x20) ||
    code === 0x85 ||
    code === 0xa0 ||
    code === 0x1680 ||
    (code >= 0x2000 && code <= 0x200a) ||
    code === 0x2028 ||
    code === 0x2029 ||
    code === 0x202f ||
    code === 0x205f ||
    code === 0x3000
  );
}

/** Python's `text.strip()`: its whitespace is not `String.prototype.trim`'s. */
export function strip(text: string): string {
  let start = 0;
  let end = text.length;
  while (start < end && isSpace(text.charCodeAt(start))) start += 1;
  while (end > start && isSpace(text.charCodeAt(end - 1))) end -= 1;
  return text.slice(start, end);
}

/** Python's `not text.strip()`. */
export function blank(text: string): boolean {
  for (let i = 0; i < text.length; i += 1) {
    if (!isSpace(text.charCodeAt(i))) return false;
  }
  return true;
}

/** Python's `text.lstrip("\n")`. */
export function lstripNewlines(text: string): string {
  let start = 0;
  while (text.charCodeAt(start) === 0x0a) start += 1;
  return text.slice(start);
}

/** Python's `text.rstrip("\n")`. */
export function rstripNewlines(text: string): string {
  let end = text.length;
  while (end > 0 && text.charCodeAt(end - 1) === 0x0a) end -= 1;
  return text.slice(0, end);
}

/** Python's `text.strip("\n")`. */
export function stripNewlines(text: string): string {
  return rstripNewlines(lstripNewlines(text));
}

/** Python's `text.rfind("\n", 0, end)`, `end` a UTF-16 index: -1 when there is none. */
export function lastNewline(text: string, end: number): number {
  return end <= 0 ? -1 : text.lastIndexOf("\n", end - 1);
}
