/**
 * How the handlers read what Slack sent: a payload is wire data, read field by field, and a field
 * of the wrong type is a field that is not there. An owner or channel check never passes on one.
 */
import type { Payload } from "./guards.ts";

export type { Payload };

/**
 * What a view submission answers Slack with besides a plain acknowledgement, which closes the
 * modal: errors under its blocks, or the view that replaces it (`response_action`, the view
 * submission reference).
 */
export type ViewAnswer =
  | { readonly response_action: "errors"; readonly errors: Readonly<Record<string, string>> }
  | { readonly response_action: "update"; readonly view: object };

/** How a listener acknowledges Slack: once, before anything else it does. */
export type Ack = (answer?: ViewAnswer) => Promise<void>;

/** The object a field holds, or an empty one for anything else: Python's `body.get(key) or {}`. */
export function record(value: unknown): Payload {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Payload)
    : {};
}

/** The string a field holds; null for a missing one and for any other type. */
export function text(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

/** The list a field holds, or an empty one: Python's `body.get(key) or []`. */
export function items(value: unknown): readonly unknown[] {
  return Array.isArray(value) ? value : [];
}

/**
 * Python's `str(value)` for what a payload holds: a missing field reads `None`, which names no
 * message, thread or request, as it did there.
 */
export function str(value: unknown): string {
  if (value === undefined || value === null) return "None";
  if (typeof value === "string") return value;
  if (typeof value === "boolean") return value ? "True" : "False";
  if (typeof value === "number" || typeof value === "bigint") return String(value);
  return JSON.stringify(value);
}

/** Python's truth of a parsed JSON value, which `or` goes by: empty and zero are false. */
function truthy(value: unknown): boolean {
  if (value === undefined || value === null) return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return Boolean(value);
}

/** The first action of a click: `body["actions"][0]`, empty when the payload carries none. */
export function firstAction(body: Payload): Payload {
  return record(items(body.actions)[0]);
}

/**
 * The ts of the message a button sits on, which Python read as `body["message"]["ts"]`: a click
 * that names none throws, as the missing key did there, and the listener's failure is logged.
 */
export function messageTs(body: Payload): string {
  const message = body.message;
  if (typeof message !== "object" || message === null || !("ts" in message)) {
    throw new TypeError("the click names no message");
  }
  return str((message as Payload).ts);
}

/**
 * The thread a button or a form sits in: `container.thread_ts`, falling back to
 * `message.thread_ts` then the message's own ts (a button is never trusted, so the thread it
 * answers in is read the same way for every kind of click).
 */
export function clickThread(body: Payload): string {
  const container = record(body.container);
  const message = record(body.message);
  const found = [container.thread_ts, message.thread_ts].find(truthy);
  return str(found === undefined ? message.ts : found);
}

/**
 * When an interaction happened, from its `action_ts`: what orders the updates it causes. `now`,
 * the arrival time in seconds, for a payload that carries none.
 *
 * Python's `float()` also read `inf`, `nan` and digits grouped with `_`, which Slack never
 * writes: here those are a payload that carries no time.
 */
export function actionKey(action: Payload, now: () => number): number {
  const raw = action.action_ts;
  if (typeof raw === "number" && Number.isFinite(raw)) return raw;
  if (typeof raw === "string" && raw.trim() !== "") {
    const parsed = Number(raw);
    if (Number.isFinite(parsed)) return parsed;
  }
  return now();
}
