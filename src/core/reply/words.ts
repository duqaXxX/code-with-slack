/** The words a reply's lines share, whoever writes them: the renderer, the fold, a chat provider. */

export const BACKGROUND = "Running in background";
export const STOPPED = "Stopped";

// What Python's `str.split()`, `str.strip()` and `\s` take for whitespace (`str.isspace`), as
// ranges of code points: JavaScript's `\s` lacks U+001C to U+001F and U+0085, and adds U+FEFF.
const SPACE_RANGES: readonly (readonly [number, number])[] = [
  [0x09, 0x0d],
  [0x1c, 0x20],
  [0x85, 0x85],
  [0xa0, 0xa0],
  [0x1680, 0x1680],
  [0x2000, 0x200a],
  [0x2028, 0x2029],
  [0x202f, 0x202f],
  [0x205f, 0x205f],
  [0x3000, 0x3000],
];

function escaped(code: number): string {
  return `\\u${code.toString(16).padStart(4, "0")}`;
}

/** The body of a regular expression class that matches Python's whitespace: `[${PY_SPACE}]`. */
export const PY_SPACE = SPACE_RANGES.map(([first, last]) =>
  first === last ? escaped(first) : `${escaped(first)}-${escaped(last)}`,
).join("");

const SPACES = new RegExp(`[${PY_SPACE}]+`);

/** `value` on one line, its runs of whitespace as one space, cut with `…` past `limit` characters. */
export function oneLine(value: string, limit: number): string {
  const text = value
    .split(SPACES)
    .filter((word) => word !== "")
    .join(" ");
  // Counted in code points, as Python's `len` counts: an emoji is one character.
  const chars = Array.from(text);
  return chars.length <= limit ? text : `${chars.slice(0, limit - 1).join("")}…`;
}
