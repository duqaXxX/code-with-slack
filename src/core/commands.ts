/**
 * Commands typed as messages: `!word`. A few words are awaydesk's own; any other
 * `!name args` is a Claude Code command, so a command a new Claude Code release adds works at once.
 *
 * Slack never delivers a message that starts with `/` to the bot (measured 2026-09-23), so
 * Claude Code's own slash is replaced by `!`, as the official Claude app for Slack does with its
 * `@Claude !word` commands.
 */
import type { CommandInfo } from "../agent/seam.ts";
import * as texts from "./texts.ts";

export interface Help {
  readonly kind: "help";
  /** Lowercase; empty lists everything. */
  readonly query: string;
}

export interface Bind {
  readonly kind: "bind";
  readonly path: string;
}

export interface Bypass {
  readonly kind: "bypass";
  readonly on: boolean;
}

export interface Status {
  readonly kind: "status";
}

export interface Stop {
  readonly kind: "stop";
}

export interface Resume {
  readonly kind: "resume";
  /** A session id or name; empty lists the sessions. */
  readonly target: string;
}

export interface Open {
  readonly kind: "open";
  /** A path or words in one; empty offers a picker. */
  readonly target: string;
}

export interface Guide {
  readonly kind: "guide";
}

export interface Passthrough {
  readonly kind: "passthrough";
  readonly text: string;
}

export interface Invalid {
  readonly kind: "invalid";
}

// The daemon's own words, answered without Claude Code; a Passthrough goes to the session.
// `WORD` names each one, and test/core/commands.test.ts checks that the guide (`!guide`) and
// `!help` explain every one: a new word cannot ship without its line in both.
export type Word = Help | Guide | Bind | Bypass | Status | Stop | Resume | Open | Invalid;
export type Command = Word | Passthrough;

/** The word each of the daemon's own commands is typed as; a new kind of `Word` fails to compile here. */
export const WORD = {
  help: "help",
  guide: "guide",
  bind: "bind",
  bypass: "bypass",
  status: "status",
  stop: "stop",
  resume: "resume",
  open: "open",
} as const satisfies Record<Exclude<Word["kind"], "invalid">, string>;

export const DESCRIPTION_LIMIT = 100;

// Claude Code's `/clear` starts a new session under any of its names (aliases `/reset` and `/new`:
// commands reference, code.claude.com/docs/en/commands, read 2026-10-02, and the list SDK 0.2.163
// reports), and one thread is one session: the daemon refuses each of them inside a thread, which
// is also the only place a session's commands are listed. Named here because a session rebuilt
// after a restart lists no command until it connects (`refusedInThread`).
export const NEW_SESSION_NAMES: ReadonlySet<string> = new Set(["clear", "reset", "new"]);

// Claude Code's `/login` and `/logout` act on the host's own login, which the daemon and every
// session run on: neither is ever sent to Claude Code, from the channel or from a thread, and
// each is answered with where it is done instead (`hostOnly`).
export const HOST_ONLY: ReadonlyMap<string, string> = new Map([
  ["login", texts.LOGIN_ON_HOST],
  ["logout", texts.LOGOUT_ON_HOST],
]);

// Python's `str.isspace`, which `strip` and `split` follow, is not JavaScript's `\s`: it adds the
// separators U+001C to U+001F and U+0085, and leaves out U+FEFF.
const SPACE =
  "\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const LEADING = new RegExp(`^[${SPACE}]+`);
const TRAILING = new RegExp(`[${SPACE}]+$`);
const STARTS_WITH_SPACE = new RegExp(`^[${SPACE}]`);
const WORD_AND_REST = new RegExp(`^([^${SPACE}]+)(?:[${SPACE}]+([\\s\\S]*))?$`);
const RUNS_OF_SPACE = new RegExp(`[${SPACE}]+`);

function strip(text: string): string {
  return text.replace(LEADING, "").replace(TRAILING, "");
}

/** Python's `sorted` on strings: by code point, where `Array.prototype.sort` goes by UTF-16 unit. */
function compareCodePoints(a: string, b: string): number {
  const left = Array.from(a);
  const right = Array.from(b);
  for (let i = 0; i < Math.min(left.length, right.length); i += 1) {
    const x = left[i] as string;
    const y = right[i] as string;
    if (x !== y) return (x.codePointAt(0) as number) - (y.codePointAt(0) as number);
  }
  return left.length - right.length;
}

/**
 * Map a `!word` message to the daemon's own command or a Claude Code passthrough; null when the
 * message is not a command at all.
 */
export function parseBang(text: string): Command | null {
  const stripped = strip(text);
  if (!stripped.startsWith("!")) return null;
  const body = stripped.slice(1);
  if (body === "" || STARTS_WITH_SPACE.test(body)) return null;
  // The word ends at any whitespace: a line break after it (a code block that follows the
  // word, a second line) separates it from its arguments as a space does.
  const split = WORD_AND_REST.exec(body);
  const word = split?.[1] ?? "";
  const rest = strip(split?.[2] ?? "");
  switch (word.toLowerCase()) {
    case "help":
      return { kind: "help", query: rest.toLowerCase() };
    case "guide":
      if (rest === "") return { kind: "guide" };
      break;
    case "bind":
      return { kind: "bind", path: rest }; // alone, it lists the folders a session can start in
    case "bypass":
      switch (rest.toLowerCase()) {
        case "on":
          return { kind: "bypass", on: true };
        case "off":
          return { kind: "bypass", on: false };
        default:
          return { kind: "invalid" };
      }
    case "status":
      if (rest === "") return { kind: "status" };
      break;
    case "stop":
      if (rest === "") return { kind: "stop" };
      break;
    case "resume":
      return { kind: "resume", target: rest };
    case "open":
      return { kind: "open", target: rest };
  }
  return { kind: "passthrough", text: strip(`${word} ${rest}`) };
}

// The parts of a composer message a word is read through. A quote or a list is left out: what
// opens it is not what the owner typed first.
const READ_THROUGH = new Set(["rich_text_section", "rich_text_preformatted"]);
// What Slack writes in a message's text around code, bold, italic, strikethrough.
const MARKS = "`*_~";

/** The first element of a list, or an empty record for anything else. */
function firstOf(value: unknown): Record<string, unknown> {
  const head: unknown = Array.isArray(value) ? value[0] : undefined;
  return typeof head === "object" && head !== null ? (head as Record<string, unknown>) : {};
}

/**
 * The text a composer message opens with, without its formatting; null for a message that opens
 * with anything else (a quote, a list, an emoji, a mention) or has no composer block.
 */
function firstRun(blocks: unknown): string | null {
  const block = firstOf(blocks);
  const part = block.type === "rich_text" ? firstOf(block.elements) : {};
  const leaf =
    typeof part.type === "string" && READ_THROUGH.has(part.type) ? firstOf(part.elements) : {};
  if (leaf.type !== "text") return null;
  return typeof leaf.text === "string" ? leaf.text : "";
}

/**
 * `text` without the marks around the run it opens with, when that run starts with `!`: the
 * composer's block says what the run is (`firstRun`), the text holds the rest as the owner sent
 * it. Empty when the message does not open with a formatted `!`, or when the text does not read as
 * its marks, that run, then the same marks closing it or the message.
 */
export function unformatted(text: string, blocks: unknown): string {
  const run = firstRun(blocks);
  const stripped = strip(text);
  const at = stripped.indexOf("!");
  const opening = at === -1 ? stripped : stripped.slice(0, at);
  const body = at === -1 ? "" : stripped.slice(at);
  if (!run?.startsWith("!") || !opening || [...opening].some((c) => !MARKS.includes(c))) {
    return "";
  }
  if (!body.startsWith(run)) return "";
  const after = body.slice(run.length);
  const closing = [...opening].reverse().join("");
  if (after.startsWith(closing)) return run + after.slice(closing.length);
  if (after.endsWith(closing)) return run + after.slice(0, after.length - closing.length);
  return "";
}

/**
 * The daemon's words, then every command the session offers now (null: not bound yet) except
 * those its thread refuses (`refusedInThread`), keeping only the lines whose name or description
 * contains `query`, ignoring case. With no query, the rule that tells a command from a text comes
 * first. `escapeMarkdown` is the chat provider's escape for a markdown block.
 */
export function helpText(
  commands: readonly CommandInfo[] | null,
  query: string,
  escapeMarkdown: (text: string) => string,
): string {
  const keep = (line: string): boolean => line.toLowerCase().includes(query.toLowerCase());
  const own = texts.HELP_WORDS.filter(keep);
  const refused = refusedInThread(commands);
  const offered = (commands ?? []).filter((c) => !refused.has(c.name.toLowerCase()));
  const session = offered
    .toSorted((a, b) => compareCodePoints(a.name, b.name))
    .map((command) => commandParts(command, escapeMarkdown))
    .filter(([usage, description]) => keep(`${usage} ${description}`))
    .map(([usage, description]) => `${usage} ${escapeMarkdown(description)}`.replace(TRAILING, ""));
  // The rule heads the whole list; a search shows its matches alone.
  const lines = [texts.HELP_OWN, ...(query ? [] : [texts.HELP_RULE]), ...own];
  if (commands === null) {
    lines.push(texts.HELP_UNBOUND);
  } else {
    lines.push(texts.HELP_CLAUDE, ...session);
  }
  if (query && own.length === 0 && session.length === 0) {
    lines.push(texts.fill(texts.HELP_NO_MATCH, { query }));
  }
  return lines.join("\n");
}

/**
 * The answer to a command that is only ever run on the host (`HOST_ONLY`), or null for any other:
 * the caller sends that answer and nothing to Claude Code.
 */
export function hostOnly(command: Passthrough): string | null {
  return HOST_ONLY.get((command.text.split(" ", 1)[0] as string).toLowerCase()) ?? null;
}

/**
 * The command names a thread refuses, lowercase: `NEW_SESSION_NAMES`, with every alias the
 * session's own list (`commands`, null or empty before it connects) gives one of them.
 */
export function refusedInThread(commands: readonly CommandInfo[] | null): ReadonlySet<string> {
  const aliases = (commands ?? [])
    .filter((command) => NEW_SESSION_NAMES.has(command.name.toLowerCase()))
    .flatMap((command) => command.aliases.map((alias) => alias.toLowerCase()));
  return new Set([...NEW_SESSION_NAMES, ...aliases]);
}

/**
 * `!name hint` and the description as written, cut to one short line; not escaped. The
 * description is third-party text, and a cut can fall inside its own code span: the caller
 * escapes it before showing it (`helpText`).
 */
export function commandParts(
  command: CommandInfo,
  escapeMarkdown: (text: string) => string,
): [usage: string, description: string] {
  const plain = `!${command.name} ${command.argumentHint}`.replace(TRAILING, "");
  // A code span cannot hold a backtick: such a usage is plain text, escaped.
  const usage = plain.includes("`") ? escapeMarkdown(plain) : `\`${plain}\``;
  let description = command.description
    .split(RUNS_OF_SPACE)
    .filter((word) => word !== "")
    .join(" ");
  // The cut counts code points, as Python's `len` did: a unit-wise cut could split an emoji.
  const points = Array.from(description);
  if (points.length > DESCRIPTION_LIMIT) {
    description = `${points.slice(0, DESCRIPTION_LIMIT - 1).join("")}…`;
  }
  return [usage, description];
}
