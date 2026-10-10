/**
 * `docs/sdk-surface-typescript.md` against the source under `src/agent/claude/` and the
 * declarations of the installed `@anthropic-ai/claude-agent-sdk` (`sdk.d.ts`): the map of what the
 * daemon depends on is only worth reading while both agree with it.
 *
 * The Python suite keeps `docs/sdk-surface.md` true with `probe/surface.py` and the interpreter's
 * `ast` and `typing`. Neither exists here and the compiler API is not a dependency, so the table
 * parser, the reading of the declarations and the check of the reference are the helpers below,
 * which move to the TypeScript probe when it has a `surface` module of its own. The declarations
 * are read as text: a declared type's block runs from its `declare` line to the `;` that closes it
 * at bracket depth zero (an interface or a class: to the `}` that does), with comments blanked and
 * string literals kept whole.
 */
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { basename, join } from "node:path";
import { test } from "node:test";
import { kindOf, TRANSLATED_KINDS, UNREAD_KINDS } from "../../../src/agent/claude/translate.ts";
import { sdkRecordings, sdkRecords } from "../../support/fixtures.ts";

const ROOT = join(import.meta.dirname, "..", "..", "..");
const SRC = join(ROOT, "src");
const CLAUDE = join(SRC, "agent", "claude");
const PACKAGE = "@anthropic-ai/claude-agent-sdk";
const SURFACE = join(ROOT, "docs", "sdk-surface-typescript.md");
const DECLARATIONS_FILE = join(ROOT, "node_modules", PACKAGE, "sdk.d.ts");
// The claims are read from the Python probe until the TypeScript probe replaces that file.
const CLAIMS_FILE = join(ROOT, "probe", "claims.py");

export const COLUMNS = ["Owner", "Member", "Kind", "Used in", "Source", "Checked by"] as const;
// A `type` or `function` row's owner is the module it is imported from; every other row's owner
// is a type of the SDK, or a dotted path below one for what a declaration leaves open.
export const KINDS = ["type", "function", "field", "method", "option", "key", "value"] as const;
// `reference`: the published reference names it. `package`: `sdk.d.ts` declares it and the
// reference does not. `measured`: neither does; it was read off a real stream.
export const SOURCES = ["reference", "package", "measured"] as const;
const SET_ASIDE = "## Types the daemon does not read";
const UNREAD_HEADING = "### Recorded and left unread";
const UNRECORDED_HEADING = "### Declared and not recorded";

export interface Row {
  readonly owner: string;
  readonly member: string;
  readonly kind: string;
  readonly usedIn: readonly string[]; // `session.ts: ClaudeSession.setModel`
  readonly source: string;
  readonly claims: readonly string[]; // probe claim ids, as `P1`
}

/** The row's name as a report and a test id give it. */
export function nameOf(row: Row): string {
  return row.kind === "type" || row.kind === "function" ? row.member : `${row.owner}.${row.member}`;
}

/** The SDK type a row hangs from: `SDKPartialAssistantMessage` for `SDKPartialAssistantMessage.event`. */
export function rootOf(row: Row): string {
  return row.owner.split(/[.[]/)[0] ?? "";
}

function cells(line: string): string[] {
  return line
    .replace(/^\||\|$/g, "")
    .split("|")
    .map((cell) => cell.trim());
}

/**
 * The table's rows. Throws when the table is missing, its header changed, or a row is malformed:
 * a broken row is an error, never a row left out of the checks.
 */
export function surface(text: string, name = basename(SURFACE)): Row[] {
  const table: string[][] = [];
  for (const line of text.split("\n")) {
    const row = line.startsWith("|") ? cells(line) : null;
    if (table.length > 0 && row === null) break; // the table ended: the file may hold others
    if (row !== null && (table.length > 0 || row.join("|") === COLUMNS.join("|"))) table.push(row);
  }
  if (table.length === 0) throw new Error(`${name}: no table with the columns ${COLUMNS}`);
  const rows: Row[] = [];
  for (const row of table.slice(2)) {
    const [owner, member, kind, usedIn, source, checked] = row;
    if (
      row.length !== COLUMNS.length ||
      owner === undefined ||
      member === undefined ||
      kind === undefined ||
      usedIn === undefined ||
      source === undefined ||
      checked === undefined
    ) {
      throw new Error(`${name}: ${row.length} cells, not ${COLUMNS.length}, in ${row.slice(0, 2)}`);
    }
    if (!(KINDS as readonly string[]).includes(kind)) {
      throw new Error(`${name}: kind ${kind} in ${row.slice(0, 2)}`);
    }
    if (!(SOURCES as readonly string[]).includes(source)) {
      throw new Error(`${name}: source ${source} in ${row.slice(0, 2)}`);
    }
    rows.push({
      owner: owner.replaceAll("`", ""),
      member: member.replaceAll("`", ""),
      kind,
      usedIn: usedIn
        .split(";")
        .map((place) => place.trim())
        .filter((place) => place !== ""),
      source,
      claims: checked.match(/\bP\d+\b/g) ?? [],
    });
  }
  const keys = new Set(rows.map((row) => `${row.owner}\n${row.member}\n${row.kind}`));
  if (keys.size !== rows.length) throw new Error(`${name}: a row is listed twice`);
  return rows;
}

// Scanning TypeScript text: comments blanked, strings skipped, brackets matched.

const OPEN = "([{";
const CLOSE = ")]}";

/** Where the string that opens at `start` ends (the index after its closing quote). */
function stringEnd(text: string, start: number): number {
  const quote = text.charAt(start);
  let index = start + 1;
  while (index < text.length) {
    const char = text.charAt(index);
    if (char === "\\") {
      index += 2;
    } else if (quote === "`" && char === "$" && text.charAt(index + 1) === "{") {
      index = groupEnd(text, index + 1);
    } else if (char === quote) {
      return index + 1;
    } else {
      index += 1;
    }
  }
  return text.length;
}

/** The index after the bracket that closes the one at `open`. */
function groupEnd(text: string, open: number): number {
  let depth = 0;
  for (let index = open; index < text.length; index += 1) {
    const char = text.charAt(index);
    if (char === '"' || char === "'" || char === "`") {
      index = stringEnd(text, index) - 1;
    } else if (OPEN.includes(char)) {
      depth += 1;
    } else if (CLOSE.includes(char)) {
      depth -= 1;
      if (depth === 0) return index + 1;
    }
  }
  return text.length;
}

/** `text` with its comments blanked to spaces: every position keeps its place, strings stay. */
export function withoutComments(text: string): string {
  let out = "";
  let index = 0;
  while (index < text.length) {
    const char = text.charAt(index);
    const next = text.charAt(index + 1);
    if (char === '"' || char === "'" || char === "`") {
      const end = stringEnd(text, index);
      out += text.slice(index, end);
      index = end;
    } else if (char === "/" && next === "/") {
      const end = text.indexOf("\n", index);
      const stop = end < 0 ? text.length : end;
      out += " ".repeat(stop - index);
      index = stop;
    } else if (char === "/" && next === "*") {
      const end = text.indexOf("*/", index + 2);
      const stop = end < 0 ? text.length : end + 2;
      out += text.slice(index, stop).replace(/[^\n]/g, " ");
      index = stop;
    } else {
      out += char;
      index += 1;
    }
  }
  return out;
}

// What may follow the `}` that closes a block without the statement being over: an object type in a
// union, a cast, a call, the body of a method after its return type.
const CONTINUES = /^\s*(?:[;|&.,)?:{=]|as\b|satisfies\b)/;

/**
 * Where the declaration or member that starts at `start` ends in comment-free `text`: at the `;`
 * that closes it at bracket depth zero, at the `}` that closes its body when nothing continues the
 * statement, or just before the bracket of the group it sits in.
 */
export function statementEnd(text: string, start: number): number {
  let depth = 0;
  for (let index = start; index < text.length; index += 1) {
    const char = text.charAt(index);
    if (char === '"' || char === "'" || char === "`") {
      index = stringEnd(text, index) - 1;
    } else if (OPEN.includes(char)) {
      depth += 1;
    } else if (CLOSE.includes(char)) {
      depth -= 1;
      if (depth < 0) return index;
      if (depth === 0 && char === "}" && !CONTINUES.test(text.slice(index + 1, index + 40))) {
        return index + 1;
      }
    } else if (char === ";" && depth === 0) {
      return index + 1;
    }
  }
  return text.length;
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** The names a statement is made of, between its brackets: the members of its groups. */
export function groupMembers(text: string): Map<string, string[]> {
  const found = new Map<string, string[]>();
  const stack: string[] = [];
  let segment = "";
  let angles = 0;
  const flush = (): void => {
    const named = /^\s*(?:readonly\s+)?(?:(["'])([^"']+)\1|([\w$]+))\s*\??\s*[(:]/.exec(segment);
    const name = named?.[2] ?? named?.[3];
    if (name !== undefined) found.set(name, [...(found.get(name) ?? []), segment.trim()]);
    segment = "";
    angles = 0;
  };
  for (let index = 0; index < text.length; index += 1) {
    const char = text.charAt(index);
    if (char === '"' || char === "'" || char === "`") {
      const end = stringEnd(text, index);
      if (stack.length > 0) segment += text.slice(index, end);
      index = end - 1;
      continue;
    }
    if (OPEN.includes(char)) {
      stack.push(char);
      if (stack.length === 1) {
        segment = "";
        angles = 0;
        continue;
      }
    } else if (CLOSE.includes(char)) {
      if (stack.length === 1) {
        if (stack[0] !== "[") flush();
        stack.pop();
        continue;
      }
      stack.pop();
    } else if (stack.length === 1) {
      if (char === "<") angles += 1;
      else if (char === ">" && text.charAt(index - 1) !== "=") angles -= 1;
      else if ((char === ";" || char === ",") && angles <= 0 && stack[0] !== "[") {
        flush();
        continue;
      }
    }
    if (stack.length > 0) segment += char;
  }
  return found;
}

/** A member's text without its name: `status?: 'a' | 'b'` is `'a' | 'b'`. */
function typeOf(text: string): string {
  return text.replace(/^\s*(?:readonly\s+)?(?:["'][^"']+["']|[\w$]+)\s*\??\s*:\s*/, "");
}

const NOT_TYPES = new Set(["extends", "keyof", "typeof", "readonly", "infer", "in", "is"]);

/** The names a type expression is made of at its top level: the operands of a union or an extension. */
export function operands(text: string): string[] {
  const found: string[] = [];
  const stack: string[] = [];
  let angles = 0;
  for (let index = 0; index < text.length; index += 1) {
    const char = text.charAt(index);
    if (char === '"' || char === "'" || char === "`") {
      index = stringEnd(text, index) - 1;
    } else if (OPEN.includes(char)) {
      stack.push(char);
    } else if (CLOSE.includes(char)) {
      stack.pop();
    } else if (stack.length === 0 && char === "<") {
      angles += 1;
    } else if (stack.length === 0 && char === ">" && text.charAt(index - 1) !== "=") {
      angles -= 1;
    } else if (stack.length === 0 && angles === 0 && /[A-Za-z_$]/.test(char)) {
      const name = /^[\w$]+(?:\.[\w$]+)*/.exec(text.slice(index))?.[0] ?? char;
      index += name.length - 1;
      if (text.charAt(index + 1) === "<") continue; // a generic wrapper, not an operand
      const last = name.split(".").at(-1) ?? name;
      if (!NOT_TYPES.has(last)) found.push(last);
    }
  }
  return found;
}

/** What the package declares, read from `sdk.d.ts` as text. */
export class Declarations {
  readonly #text: string;
  readonly #starts = new Map<string, number>();

  constructor(source: string) {
    this.#text = withoutComments(source);
    const declared =
      /^(?:export\s+)?declare\s+(?:abstract\s+)?(?:type|interface|function|const|class|enum|namespace)\s+([A-Za-z_$][\w$]*)/gm;
    for (const match of this.#text.matchAll(declared)) {
      const name = match[1];
      if (name !== undefined && !this.#starts.has(name)) this.#starts.set(name, match.index);
    }
  }

  has(name: string): boolean {
    return this.#starts.has(name);
  }

  /** Whether the declarations, comments aside, hold `text` anywhere. */
  contains(text: string): boolean {
    return this.#text.includes(text);
  }

  /** The text of the declaration of `name`, or null when it is not declared. */
  block(name: string): string | null {
    const start = this.#starts.get(name);
    if (start === undefined) return null;
    return this.#text.slice(start, statementEnd(this.#text, start));
  }

  /** The right-hand side of a declaration: after its `=` for a type, after its name otherwise. */
  rightSide(name: string): string {
    const block = this.block(name);
    if (block === null) return "";
    const afterName = block.slice(block.indexOf(name) + name.length);
    if (!/^\s*(?:export\s+)?declare\s+type\b/.test(block)) return afterName;
    const equals = afterName.indexOf("=");
    return equals < 0 ? "" : afterName.slice(equals + 1).replace(/;\s*$/, "");
  }

  /**
   * The members of `name` and of every declared type it extends or is a union of, each with the
   * text of its declaration; null when `name` is not declared.
   */
  members(name: string, seen = new Set<string>()): Map<string, string[]> | null {
    if (this.block(name) === null) return null;
    const found = new Map<string, string[]>();
    if (seen.has(name)) return found;
    seen.add(name);
    const right = this.rightSide(name);
    const own = groupMembers(right);
    this.#merge(found, own);
    for (const operand of operands(right)) {
      const inner = this.members(operand, seen);
      if (inner !== null) this.#merge(found, inner);
    }
    // What a function or a function type takes is its own: `listSessions` has the fields of its
    // options, `CanUseTool` those of the options it is called with.
    if (right.trimStart().startsWith("(")) {
      this.#merge(found, this.membersOfTexts([...own.values()].flat(), seen));
    }
    return found;
  }

  #merge(into: Map<string, string[]>, from: Map<string, string[]>): void {
    for (const [name, texts] of from) into.set(name, [...(into.get(name) ?? []), ...texts]);
  }

  /** The members of what the member texts `texts` declare (an object type, a declared name). */
  membersOfTexts(texts: readonly string[], seen = new Set<string>()): Map<string, string[]> {
    const found = new Map<string, string[]>();
    for (const text of texts) {
      const type = typeOf(text);
      this.#merge(found, groupMembers(type));
      for (const operand of operands(type)) {
        const inner = this.members(operand, seen);
        if (inner !== null) this.#merge(found, inner);
      }
    }
    return found;
  }

  /** Every string literal a type allows at its top level, through the declared names it uses. */
  literals(texts: readonly string[], seen = new Set<string>()): Set<string> {
    const found = new Set<string>();
    for (const text of texts) {
      const type = typeOf(text);
      let depth = 0;
      for (let index = 0; index < type.length; index += 1) {
        const char = type.charAt(index);
        if (char === '"' || char === "'" || char === "`") {
          const end = stringEnd(type, index);
          if (depth === 0) found.add(type.slice(index + 1, end - 1));
          index = end - 1;
        } else if (OPEN.includes(char)) {
          depth += 1;
        } else if (CLOSE.includes(char)) {
          depth -= 1;
        }
      }
      for (const operand of operands(type)) {
        if (!this.has(operand) || seen.has(operand)) continue;
        seen.add(operand);
        for (const value of this.literals([this.rightSide(operand)], seen)) found.add(value);
      }
    }
    return found;
  }
}

export const DECLARATIONS = new Declarations(readFileSync(DECLARATIONS_FILE, "utf8"));

/**
 * Whether the installed package declares what `row` names. False also when the type it hangs from
 * is not declared: gone in this release, or never a type of the SDK (a tool's input). Null when
 * the type is there and cannot say: a key below a field the declarations leave open, a value of a
 * plain `string` field.
 */
export function inPackage(row: Row, declarations: Declarations = DECLARATIONS): boolean | null {
  if (row.kind === "type" || row.kind === "function") return declarations.has(row.member);
  const root = rootOf(row);
  let members = declarations.members(root);
  if (members === null) return false;
  const path = row.owner
    .split(".")
    .slice(1)
    .map((part) => part.replace(/(?:\[\]|\(\))+$/, ""));
  // A value is a literal of the field it hangs from: the last step of the path is that field.
  const fieldOfValue = row.kind === "value" ? path.pop() : undefined;
  for (const step of path) {
    const texts = members.get(step);
    if (texts === undefined) return null;
    members = declarations.membersOfTexts(texts);
  }
  if (row.kind === "value") {
    let allowed: Set<string>;
    if (fieldOfValue === undefined) {
      allowed = declarations.literals([declarations.rightSide(root)]);
    } else {
      const texts = members.get(fieldOfValue);
      if (texts === undefined) return null;
      allowed = declarations.literals(texts);
    }
    return allowed.size > 0 ? allowed.has(row.member.replace(/^"|"$/g, "")) : null;
  }
  if (row.kind === "method") return row.owner === root && members.has(row.member);
  if (members.has(row.member)) return true;
  // A union of aliases (`HookInput`) with no member resolved has nothing to deny it with.
  return members.size > 0 ? false : null;
}

// The published reference.

/**
 * The part of the reference under the heading that names `name`, to the next heading of the same
 * or a higher level.
 */
export function section(page: string, name: string): string | null {
  // The heading that is the name alone (a type's may carry `object` after it), or that ends in it
  // after a colon (`Return type:`): another one may only mention it.
  const heading = new RegExp(
    `^(#{2,4}) (?:[^\`\\n]*: )?\`?${escapeRegExp(name)}(?:\\(\\))?\`?(?: object)?[ \\t]*$`,
    "m",
  );
  const found = heading.exec(page);
  if (found === null) return null;
  const rest = page.slice(found.index + found[0].length);
  const end = new RegExp(`^#{2,${(found[1] ?? "##").length}} `, "m").exec(rest);
  return end === null ? rest : rest.slice(0, end.index);
}

/**
 * Whether the published reference names what `row` names: a heading for a type or a function, and
 * for anything else the member inside the section of the type it hangs from.
 */
export function inReference(row: Row, page: string): boolean {
  if (row.kind === "type" || row.kind === "function") return section(page, row.member) !== null;
  // Below a field the reference describes no keys, and a word match there would prove nothing:
  // only a quoted value of a field is looked for one level down.
  const depth = row.owner.split(/[.[]/).length;
  if (depth > (row.kind === "value" ? 2 : 1)) return false;
  const found = section(page, rootOf(row));
  if (found === null) return false;
  const quoted = /^"(.*)"$/.exec(row.member);
  const word =
    quoted === null
      ? `(?<![\\w])${escapeRegExp(row.member)}(?![\\w])`
      : `["'\`]${escapeRegExp(quoted[1] ?? "")}["'\`]`;
  return new RegExp(word).test(found);
}

// The SDK names the source imports.

interface Import {
  readonly module: string;
  readonly name: string;
  readonly local: string;
}

const IMPORT_CLAUSE = /\b(?:import|export)\s+(?:type\s+)?([^'"`;]*?)\s+from\s+["']([^"']+)["']/g;
const BARE_IMPORT = /\bimport\s*["']([^"']+)["']/g;
const DYNAMIC_IMPORT = /\b(?:import|require)\s*\(\s*["']([^"']+)["']\s*\)/g;

function isSdk(specifier: string): boolean {
  return specifier === PACKAGE || specifier.startsWith(`${PACKAGE}/`);
}

/** Every (module, name) the source of `path` imports from the SDK; the package as a whole is an error. */
export function importsOf(source: string, path: string): Import[] {
  const found: Import[] = [];
  for (const match of source.matchAll(IMPORT_CLAUSE)) {
    const [, clause = "", specifier = ""] = match;
    if (!isSdk(specifier)) continue;
    const named = /^\{([^}]*)\}$/.exec(clause.trim());
    assert.ok(named, `${path} imports ${specifier} as a whole, not by name`);
    for (const entry of (named[1] ?? "").split(",")) {
      const item = entry.trim().replace(/^type\s+/, "");
      if (item === "") continue;
      const [name = "", local = name] = item.split(/\s+as\s+/);
      found.push({ module: specifier, name, local });
    }
  }
  for (const pattern of [BARE_IMPORT, DYNAMIC_IMPORT]) {
    for (const match of source.matchAll(pattern)) {
      assert.ok(!isSdk(match[1] ?? ""), `${path} loads the SDK other than by name`);
    }
  }
  return found;
}

function sources(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return sources(path);
    return entry.name.endsWith(".ts") ? [path] : [];
  });
}

let importedNames: Import[] | undefined;

/** Every SDK name any source file under `src/` imports. */
export function imported(): Import[] {
  importedNames ??= sources(SRC).flatMap((path) => importsOf(readFileSync(path, "utf8"), path));
  return importedNames;
}

// Where a row says it is used.

/**
 * The source of `symbol` in `file` (under `src/agent/claude/`): a function, a class, an interface,
 * a type or a constant, then each further part a member of the one before. Members are looked for
 * at the class's own indentation, so a call that begins a line is not taken for one.
 */
export function body(file: string, symbol: string): string {
  const text = withoutComments(readFileSync(join(CLAUDE, file), "utf8")); // a missing file throws
  let scope = text;
  let indent = -2;
  for (const [position, part] of symbol.split(".").entries()) {
    const name = escapeRegExp(part);
    const pattern =
      position === 0
        ? new RegExp(
            `^([ \\t]*)(?:export\\s+)?(?:declare\\s+)?(?:default\\s+)?(?:abstract\\s+)?(?:async\\s+)?(?:function\\*?\\s+|class\\s+|interface\\s+|type\\s+|const\\s+|let\\s+|var\\s+|enum\\s+)${name}\\b`,
            "m",
          )
        : new RegExp(
            `^( {${indent + 2}})(?:(?:public|private|protected|static|async|override|readonly|get|set|declare)\\s+)*${name}\\s*[(<:?=!]`,
            "m",
          );
    const found = pattern.exec(scope);
    assert.ok(found, `${file} has no ${symbol}`);
    indent = (found[1] ?? "").length;
    scope = scope.slice(found.index, statementEnd(scope, found.index));
  }
  return scope;
}

// The probe's claims.

/** The ids of the claims the probe makes; the TypeScript probe replaces this file's source. */
export function claimIds(): Set<string> {
  const text = readFileSync(CLAIMS_FILE, "utf8");
  return new Set([...text.matchAll(/Claim\(\s*"(P\d+)"/g)].map((match) => match[1] ?? ""));
}

// What the table sets aside.

/** The text of `## Types the daemon does not read`, up to the next `##` heading. */
function setAsideSection(text: string): string {
  if (!text.includes(SET_ASIDE)) throw new Error(`no section ${JSON.stringify(SET_ASIDE)}`);
  return (text.split(SET_ASIDE, 2)[1] ?? "").split("\n## ", 1)[0] ?? "";
}

function bullets(text: string): string[] {
  return [...text.matchAll(/^- `([^`]+)`.*$/gm)].map((match) => match[0]);
}

function under(sectionText: string, heading: string): string {
  const rest = sectionText.split(heading, 2)[1];
  if (rest === undefined) throw new Error(`no heading ${JSON.stringify(heading)}`);
  return rest.split("\n### ", 1)[0] ?? "";
}

export interface Unread {
  readonly kind: string;
  /** The SDK type of the kind, or null for a kind of no type of its own. */
  readonly type: string | null;
}

/** The kinds of record the section names as recorded and left unread, with their SDK types. */
export function unreadKinds(text: string): Unread[] {
  return bullets(under(setAsideSection(text), UNREAD_HEADING)).map((line) => ({
    kind: /^- `([^`]+)`/.exec(line)?.[1] ?? "",
    type: /`(SDK\w+)`/.exec(line)?.[1] ?? null,
  }));
}

/** The declared types the section names as not recorded. */
export function unrecordedTypes(text: string): string[] {
  return bullets(under(setAsideSection(text), UNRECORDED_HEADING)).map(
    (line) => /^- `([^`]+)`/.exec(line)?.[1] ?? "",
  );
}

/**
 * The SDK types that section sets aside: those of the recorded kinds it names, and the declared
 * ones no recording carries. Looked at and left unread on purpose, so that only a type nobody has
 * looked at is reported as new.
 */
export function setAside(text: string = readFileSync(SURFACE, "utf8")): string[] {
  const ofKinds = unreadKinds(text).flatMap((entry) => (entry.type === null ? [] : [entry.type]));
  return [...ofKinds, ...unrecordedTypes(text)];
}

/** The `type` of a message type, with its `subtype` after a colon: the kind a record of it has. */
export function kindOfType(name: string, declarations: Declarations = DECLARATIONS): string | null {
  const block = declarations.block(name) ?? "";
  const type = /\btype: '([^']+)'/.exec(block)?.[1];
  const subtype = /\bsubtype: '([^']+)'/.exec(block)?.[1];
  if (type === undefined) return null;
  return subtype === undefined ? type : `${type}:${subtype}`;
}

/** The declared members of the SDK's message union. */
export function messageTypes(declarations: Declarations): string[] {
  const block = declarations.block("SDKMessage") ?? "";
  return operands(block.slice(block.indexOf("=") + 1));
}

/**
 * Message types the installed package declares in its union that the map neither lists nor sets
 * aside: what a new release added that the daemon has never looked at.
 */
export function unlistedTypes(
  rows: readonly Row[],
  known: readonly string[],
  declarations: Declarations = DECLARATIONS,
): string[] {
  const listed = new Set([...rows.map(rootOf), ...known]);
  return messageTypes(declarations)
    .filter((name) => !listed.has(name))
    .sort();
}

export interface Check {
  readonly missing: readonly Row[]; // the map says the package declares it, and it does not
  readonly undocumented: readonly Row[]; // a `reference` row the reference no longer names
  readonly referenceRead: boolean;
  readonly byHand: readonly Row[]; // known from no reference and covered by no claim
  readonly unlisted: readonly string[];
  readonly broken: boolean;
}

export function check(
  rows: readonly Row[],
  page: string | null,
  known: readonly string[],
  declarations: Declarations = DECLARATIONS,
): Check {
  const missing = rows.filter(
    (row) => row.source !== "measured" && inPackage(row, declarations) === false,
  );
  return {
    missing,
    undocumented: rows.filter(
      (row) => page !== null && row.source === "reference" && !inReference(row, page),
    ),
    referenceRead: page !== null,
    byHand: rows.filter((row) => row.source !== "reference" && row.claims.length === 0),
    unlisted: unlistedTypes(rows, known, declarations),
    broken: missing.length > 0,
  };
}

/**
 * What the probe prints about the map, before its scenes. The rows no reference names and no claim
 * covers are the same from one release to the next, so a run counts them and only `full` lists
 * them: they are where to look when a release misbehaves, not a list to tick.
 */
export function report(result: Check, full = false): string {
  const lines = [`SDK surface (${basename(SURFACE)}):`];
  if (result.missing.length > 0) {
    lines.push("  BROKEN, no longer in the package:");
    for (const row of result.missing) {
      lines.push(`    ${nameOf(row)} (${row.kind}), used in ${row.usedIn.join("; ")}`);
    }
  } else {
    lines.push("  every listed type, field, method and option is in the package");
  }
  if (!result.referenceRead) {
    lines.push("  UNPROVEN, the reference could not be read");
  } else if (result.undocumented.length > 0) {
    lines.push("  UNPROVEN, listed as `reference` and not found in it, read the page:");
    for (const row of result.undocumented) lines.push(`    ${nameOf(row)} (${row.kind})`);
  } else {
    lines.push("  every `reference` row is still named by the reference");
  }
  if (result.unlisted.length > 0) {
    lines.push(`  in the package and not in the map: ${result.unlisted.join(", ")}`);
  }
  if (result.byHand.length > 0 && full) {
    lines.push("  in no reference and under no claim:");
    for (const row of result.byHand) lines.push(`    ${nameOf(row)} (${row.source})`);
  } else if (result.byHand.length > 0) {
    lines.push(
      `  ${result.byHand.length} rows are in no reference and under no claim (\`--surface\` lists them)`,
    );
  }
  return lines.join("\n");
}

// Tests.

const ROWS = surface(readFileSync(SURFACE, "utf8"));
const id = (row: Row): string => `${row.kind}:${nameOf(row)}`.slice(0, 70);

test("every sdk name the source imports has a row and no row is left over", () => {
  const listed = ROWS.filter((row) => row.kind === "type" || row.kind === "function")
    .map((row) => `${row.owner}\n${row.member}`)
    .sort();
  const names = imported()
    .map((entry) => `${entry.module}\n${entry.name}`)
    .sort();
  assert.deepEqual(listed, [...new Set(names)]);
});

for (const row of ROWS) {
  test(`a row s source agrees with the installed package [${id(row)}]`, () => {
    const defined = inPackage(row);
    if (row.source === "package") {
      assert.equal(defined, true, "a `package` row names something the package declares");
    } else if (row.source === "reference") {
      assert.notEqual(defined, false, "the reference names it and the package no longer has it");
    } else {
      assert.notEqual(
        defined,
        true,
        "the package declares it: the row is `package`, or `reference`",
      );
    }
  });
}

for (const row of ROWS) {
  test(`a row is used inside a symbol it names [${id(row)}]`, () => {
    // Not "somewhere in the source": `type`, `name` and `text` are everywhere. The member has to
    // appear in the body of a symbol the row itself points at, and every such symbol has to exist.
    assert.ok(row.usedIn.length > 0, "a row says where it is used");
    const local = imported()
      .filter((entry) => entry.name === row.member)
      .map((entry) => entry.local);
    const names =
      row.kind === "type" || row.kind === "function"
        ? [row.member, ...local]
        : [row.member.replace(/^"|"$/g, "")];
    const bodies: string[] = [];
    for (const place of row.usedIn) {
      const [file = "", symbols = ""] = place.split(/:(.*)/s);
      for (const symbol of symbols.split(",")) bodies.push(body(file.trim(), symbol.trim()));
    }
    const pattern = new RegExp(`(?<![\\w-])(?:${names.map(escapeRegExp).join("|")})(?![\\w-])`);
    assert.ok(
      bodies.some((source) => pattern.test(source)),
      `${nameOf(row)} appears in none of ${row.usedIn.join("; ")}`,
    );
  });
}

test("every claim a row names exists", () => {
  const known = claimIds();
  const named = new Set(ROWS.flatMap((row) => row.claims));
  assert.deepEqual(
    [...named].filter((claim) => !known.has(claim)),
    [],
  );
});

test("the types set aside are exported and not imported", () => {
  const text = readFileSync(SURFACE, "utf8");
  const names = setAside(text);
  const union = messageTypes(DECLARATIONS);
  // Exported: each is a member of the union the package declares.
  assert.ok(names.length > 0 && names.every((name) => union.includes(name)));
  const taken = new Set(imported().map((entry) => entry.name));
  assert.deepEqual(
    names.filter((name) => taken.has(name)),
    [],
  );
  assert.deepEqual(check(ROWS, null, names).unlisted, []); // on the pinned release, nothing is new
  // The first list is the translator's own (`UNREAD_KINDS`), and no kind the translator reads is in it.
  const kinds = unreadKinds(text);
  assert.deepEqual(kinds.map((entry) => entry.kind).sort(), [...UNREAD_KINDS].sort());
  assert.deepEqual(
    kinds.filter((entry) => TRANSLATED_KINDS.includes(entry.kind)),
    [],
  );
  // A kind named with a type is the kind that type declares; a kind with none is in no declaration
  // of a record type.
  for (const { kind, type } of kinds) {
    if (type !== null) assert.equal(kindOfType(type), kind, `${kind} is not the kind of ${type}`);
  }
  assert.ok(!DECLARATIONS.contains("command_lifecycle"));
  // The second list is what no recording carries: a recording that does moves the type out of it.
  const recorded = new Set<string>();
  for (const name of sdkRecordings()) {
    for (const record of sdkRecords(name)) recorded.add(kindOf(record) ?? "");
  }
  for (const type of unrecordedTypes(text)) {
    const kind = kindOfType(type);
    assert.ok(kind !== null && !recorded.has(kind), `${type} has a kind a recording carries`);
  }
});

test("the pinned release defines everything the map lists", () => {
  assert.deepEqual(check(ROWS, null, setAside()).missing, []);
});

const PAGE = [
  "## Choosing between `query()` and `Query`",
  "",
  "Mentions updatedInput and summary, which proves nothing.",
  "",
  "### `Query` object",
  "",
  "#### Methods",
  "",
  "| `interrupt()` | Stops the turn |",
  "",
  "### `SDKTaskNotificationMessage`",
  "",
  "    status: 'completed' | 'failed' | 'stopped'",
  "    summary: string",
  "",
  "#### Return type: `SDKSessionInfo`",
  "",
  "| `sessionId` | `string` |",
  "",
  "## Other",
  "",
].join("\n");

function row(owner: string, member: string, kind: string, source = "reference"): Row {
  return { owner, member, kind, usedIn: ["session.ts: x"], source, claims: [] };
}

test("the reference names a member only in the section of its type", () => {
  assert.ok(inReference(row("Query", "interrupt", "method"), PAGE));
  assert.ok(inReference(row("SDKTaskNotificationMessage", "summary", "field"), PAGE));
  assert.ok(inReference(row("SDKTaskNotificationMessage.status", '"failed"', "value"), PAGE));
  assert.ok(inReference(row("SDKSessionInfo", "sessionId", "field"), PAGE));
  assert.ok(inReference(row(PACKAGE, "SDKTaskNotificationMessage", "type"), PAGE));
  // a word elsewhere on the page, a value the section does not list, a key below a field
  assert.ok(!inReference(row("Query", "summary", "method"), PAGE));
  assert.ok(!inReference(row("SDKTaskNotificationMessage.status", '"killed"', "value"), PAGE));
  assert.ok(!inReference(row("SDKTaskNotificationMessage.usage", "status", "key"), PAGE));
  assert.ok(!inReference(row(PACKAGE, "SDKTaskUpdatedMessage", "type"), PAGE));
});

test("a symbol gone from the package breaks and one gone from the reference does not", () => {
  const gone = row("Query", "noSuchMethod", "method");
  const unnamed = row("SDKTaskNotificationMessage", "task_id", "field"); // declared, not in PAGE
  const result = check([gone, unnamed], PAGE, []);
  assert.deepEqual(result.missing, [gone]);
  assert.ok(result.broken);
  assert.ok(result.undocumented.includes(unnamed));
  assert.ok(!check([unnamed], PAGE, []).broken);
  const text = report(result);
  assert.ok(text.includes("BROKEN") && text.includes("Query.noSuchMethod"));
  assert.ok(text.includes("UNPROVEN") && text.includes("SDKTaskNotificationMessage.task_id"));
});

test("a member whose type left the package is missing too", () => {
  // `ModelUsage` has no `type` row of its own (the source never imports it): were it renamed,
  // only its fields' rows could say so.
  const orphan = row("NoSuchUsage", "inputTokens", "field");
  assert.equal(inPackage(orphan), false);
  assert.deepEqual(check([orphan], null, []).missing, [orphan]);
  const below = row("NoSuchMessage.data", "key", "key", "measured");
  assert.deepEqual(check([below], null, []).missing, []); // a measured row never claimed the package had it
});

test("a reference that cannot be read is said and proves nothing", () => {
  const result = check([row("SDKTaskNotificationMessage", "summary", "field")], null, []);
  assert.ok(!result.referenceRead);
  assert.deepEqual(result.undocumented, []);
  assert.ok(!result.broken);
  assert.ok(report(result).includes("could not be read"));
});

test("rows in no reference and under no claim are listed by hand", () => {
  const measured = row("SDKPartialAssistantMessage.event", "type", "key", "measured");
  const claimed: Row = {
    ...row("SDKSystemMessage", "claude_code_version", "key", "measured"),
    claims: ["P1"],
  };
  const result = check([measured, claimed], PAGE, []);
  assert.deepEqual(result.byHand, [measured]);
  // A run counts them, since they are the same on every release; the list is on request.
  assert.ok(report(result).includes("1 rows are in no reference and under no claim"));
  assert.ok(!report(result).includes("SDKPartialAssistantMessage.event.type"));
  assert.ok(report(result, true).includes("SDKPartialAssistantMessage.event.type (measured)"));
});

test("a changed header or a bad cell is an error not a shorter map", () => {
  assert.throws(() => surface("| Owner | Member |\n|---|---|\n| `A` | `b` |\n"));
  const header =
    "| Owner | Member | Kind | Used in | Source | Checked by |\n|---|---|---|---|---|---|\n";
  assert.throws(() =>
    surface(`${header}| \`A\` | \`b\` | field | session.ts: x | guessed | none |\n`),
  );
  assert.throws(() =>
    surface(`${header}${"| `A` | `b` | field | session.ts: x | measured | P1 |\n".repeat(2)}`),
  );
  const text = `| Column | Meaning |\n|---|---|\n| x | y |\n\n${header}| \`A\` | \`b\` | field | a.ts: x; b.ts: y | measured | P1 |\n`;
  assert.deepEqual(surface(text), [
    {
      owner: "A",
      member: "b",
      kind: "field",
      usedIn: ["a.ts: x", "b.ts: y"],
      source: "measured",
      claims: ["P1"],
    },
  ]);
  assert.equal(basename(SURFACE), "sdk-surface-typescript.md");
});
