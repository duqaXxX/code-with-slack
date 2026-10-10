/**
 * Reading what Claude Code sends as `unknown`. The SDK's types describe less than the wire
 * carries (of 24 recorded kinds, 9 hold fields the types do not declare and one is absent from
 * the union, measured 2026-10-10 on SDK 0.3.296), so nothing here trusts a declared shape: each
 * value is narrowed where it is read.
 */

/** A JSON object off the wire, its values still unread. */
export type WireRecord = Readonly<Record<string, unknown>>;

export function isRecord(value: unknown): value is WireRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** `value` when it is a string, else null. */
export function string(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

/** `value` when it is a string that says something, else null. */
export function words(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

/** `value` when it is a whole number, else null. */
export function integer(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

/** `value` when it is a number, else null. */
export function number(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** The objects of `value` when it is a list, in order; anything else in it is left out. */
export function records(value: unknown): WireRecord[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

// What Python's `str.strip()` removes, which is not what `String.prototype.trim` removes: the
// four separators below U+0020 and U+0085 are white space to Python alone, U+FEFF to JavaScript
// alone. The texts cut here were cut by Python, and the goldens hold its result.
const SPACE =
  "\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const EDGES = new RegExp(`^[${SPACE}]+|[${SPACE}]+$`, "g");

/** `text` without the white space at its ends, as Python's `str.strip()` cuts it. */
export function strip(text: string): string {
  return text.replace(EDGES, "");
}
