/**
 * Deterministic checks over every markdown file in the repository.
 *
 * Reads whole files, so it can skip headings, fenced blocks and backtick spans. The changelog is
 * exempt from the prose checks: it quotes history by design.
 */
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { basename, dirname, join, relative, resolve } from "node:path";
import { test } from "node:test";
import { features } from "../probe/features.ts";
import { WORD } from "../src/core/commands.ts";
import { REQUIRED } from "../src/core/config.ts";
import { sdkJson } from "./support/fixtures.ts";

const ROOT = join(import.meta.dirname, "..");
const SRC = join(ROOT, "src");
const README = join(ROOT, "README.md");
const SETUP = join(ROOT, "docs", "setup.md");
const LIMITS = join(ROOT, "docs", "limits.md");
// A feature's row is a name to scan: what it does in full is in docs/setup.md or under Details.
// Raise this when a name cannot be said in fewer characters.
const FEATURE_NAME_LIMIT = 300;
// The README is the landing page: a feature's detail goes in docs/setup.md. The limit sits a
// quarter above the page's length when it was set (677 words, as `str.split` counts them). It is
// a prompt to ask where new text belongs: raise it when the page needs the words.
const README_WORD_LIMIT = 846;

const LINK = /\[[^\]]*\]\(([^)\s]+)\)/g;
const LINE_POINTER = /\b[\w./-]+\.(?:ts|py|md|sh|ya?ml|json|toml):\d+/;
const REPO_PATH = /`((?:src|test|tests|docs|probe|\.github)\/[\w./-]+)`/g;
const BACKTICKS = /`[^`]*`/g;
// A code span that is nothing but a dotted name: `texts.GUIDE`, `ClaudeSession.query`.
const SYMBOL = /`([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)`/g;
// `state.json` and `config.ts` are files, not attributes of the modules of the same name.
const FILE_SUFFIXES = new Set([
  "ts",
  "js",
  "mjs",
  "py",
  "md",
  "json",
  "jsonl",
  "toml",
  "sh",
  "yml",
  "yaml",
  "txt",
  "plist",
  "log",
  "lock",
  "sock",
]);
// Dotted names that start like a module or a class of ours and belong to Slack or to a library:
// `state.values` is a key of an interaction payload.
const NOT_OURS = new Set(["state.values"]);
// A label of the decision log, which no file in this repository defines.
const DECISION_LABEL = /\bD\d{1,2}\b/;

/** Every markdown file of the repository, hidden folders and installed packages left out. */
function markdownFiles(directory: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    if (entry.name.startsWith(".") || entry.name === "node_modules" || entry.name === "dist") {
      continue;
    }
    const path = join(directory, entry.name);
    if (entry.isDirectory()) found.push(...markdownFiles(path));
    else if (entry.name.endsWith(".md")) found.push(path);
  }
  return found.sort();
}

const DOCS = markdownFiles(ROOT);
const PROSE_DOCS = DOCS.filter((doc) => basename(doc) !== "CHANGELOG.md");
// `docs/sdk-surface.md` is the table of the Python package's use of the Python SDK, kept until
// the Python tree is removed: its names are Python's, so the symbol check leaves it out.
const SYMBOL_DOCS = PROSE_DOCS.filter((doc) => docId(doc) !== join("docs", "sdk-surface.md"));

function read(path: string): string {
  return readFileSync(path, "utf8");
}

function docId(path: string): string {
  return relative(ROOT, path);
}

/** Lines outside fenced blocks and headings, with backtick spans removed. */
function proseLines(text: string): [number, string][] {
  const out: [number, string][] = [];
  let fenced = false;
  text.split(/\r?\n/).forEach((line, index) => {
    if (line.trimStart().startsWith("```") || line.trimStart().startsWith("~~~")) {
      fenced = !fenced;
      return;
    }
    if (fenced || line.startsWith("#")) return;
    out.push([index + 1, line.replace(BACKTICKS, "")]);
  });
  return out;
}

function slug(heading: string): string {
  return heading
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}_\s-]/gu, "")
    .replace(/\s+/g, "-");
}

function anchors(path: string): Set<string> {
  return new Set([...read(path).matchAll(/^#+\s+(.*)$/gm)].map((m) => slug(m[1] ?? "")));
}

function targetsOf(text: string): string[] {
  return [...text.matchAll(LINK)].map((m) => m[1] ?? "");
}

for (const doc of DOCS) {
  test(`relative links resolve [${docId(doc)}]`, () => {
    for (const target of targetsOf(read(doc))) {
      if (/^[a-z]+:/.test(target)) continue;
      const [filePart = "", ...rest] = target.split("#");
      const anchor = rest.join("#");
      const dest = filePart ? resolve(dirname(doc), filePart) : doc;
      assert.ok(existsSync(dest), `${basename(doc)}: broken link ${target}`);
      if (anchor && dest.endsWith(".md")) {
        assert.ok(anchors(dest).has(anchor), `${basename(doc)}: missing anchor ${target}`);
      }
    }
  });
}

// The changelog names files that a later change removed: it quotes history by design.
for (const doc of PROSE_DOCS) {
  test(`repo paths exist [${docId(doc)}]`, () => {
    for (const [, path = ""] of read(doc).matchAll(REPO_PATH)) {
      assert.ok(
        existsSync(join(ROOT, path.replace(/\/+$/, ""))),
        `${basename(doc)}: ${path} does not exist`,
      );
    }
  });
}

for (const doc of DOCS) {
  test(`no line number pointers [${docId(doc)}]`, () => {
    const hits = proseLines(read(doc))
      .filter(([, line]) => LINE_POINTER.test(line))
      .map(([number]) => number);
    assert.deepEqual(
      hits,
      [],
      `${basename(doc)}: line-number pointer on lines ${hits}; name the symbol`,
    );
  });
}

for (const doc of PROSE_DOCS) {
  test(`no em dash in prose [${docId(doc)}]`, () => {
    const hits = proseLines(read(doc))
      .filter(([, line]) => line.includes("—"))
      .map(([number]) => number);
    assert.deepEqual(hits, [], `${basename(doc)}: em dash in prose on lines ${hits}`);
  });
}

test("no orphan doc", () => {
  const linked = new Set<string>();
  for (const doc of DOCS) {
    for (const target of targetsOf(read(doc))) {
      const filePart = target.split("#")[0] ?? "";
      if (filePart && !/^[a-z]+:/.test(filePart)) linked.add(resolve(dirname(doc), filePart));
    }
  }
  const orphans = readdirSync(join(ROOT, "docs"))
    .filter((name) => name.endsWith(".md"))
    .filter((name) => !linked.has(join(ROOT, "docs", name)));
  assert.deepEqual(orphans, [], `docs nobody links to: ${orphans}`);
});

/** The README's text under one `##` heading, up to the next one. */
function readmeSection(title: string): string {
  const escaped = title.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`^## ${escaped}\\n([\\s\\S]*?)(?=^## |$(?![\\s\\S]))`, "m").exec(
    read(README),
  );
  assert.ok(match, `README.md has no '## ${title}' section`);
  return match[1] ?? "";
}

test("the readme lists every word of the daemon and no other", () => {
  // A word added, renamed or removed without its row in the README's table fails here.
  const words = Object.values(WORD).sort();
  const rows = [
    ...new Set([...readmeSection("Commands").matchAll(/^\| `!(\w+)/gm)].map((m) => m[1] ?? "")),
  ].sort();
  assert.deepEqual(rows, words);
});

test("the readme states the version and the node the package declares", () => {
  const manifest = JSON.parse(read(join(ROOT, "package.json"))) as {
    version: string;
    engines: { node: string };
  };
  const text = read(README);
  assert.ok(text.includes(`version ${manifest.version}`));
  // `>=22.18.0` is written `Node 22.18 or later`: a patch of 0 is left out.
  const node = manifest.engines.node.replace(/^>=/, "");
  assert.ok(
    text.includes(`Node ${node}`) || text.includes(`Node ${node.replace(/\.0$/, "")}`),
    `README.md does not name Node ${node}`,
  );
});

// ---- the source scan behind the dotted-name check ------------------------------------------
//
// The compiler API is not a dependency, so the sources are read as text. `blank` turns comments,
// the insides of strings and of regular expressions into spaces, which leaves the code's own
// shape: Biome formats every file, so a top-level declaration starts at column 0, ends at the
// next line that starts with `}`, and a member of a class, an interface or an object sits on a
// line indented by two spaces.

/** `source` with comments, string contents and regular expression bodies blanked to spaces. */
function blank(source: string): string {
  let out = "";
  let i = 0;
  const space = (ch: string): string => (ch === "\n" ? "\n" : " ");

  function quoted(quote: string): void {
    out += quote;
    i += 1;
    while (i < source.length && source.charAt(i) !== quote) {
      if (source.charAt(i) === "\\") {
        out += " ";
        i += 1;
      }
      out += space(source.charAt(i));
      i += 1;
    }
    out += quote;
    i += 1;
  }

  function template(): void {
    out += "`";
    i += 1;
    while (i < source.length && source.charAt(i) !== "`") {
      if (source.charAt(i) === "\\") {
        out += " ";
        i += 1;
        out += space(source.charAt(i));
        i += 1;
      } else if (source.charAt(i) === "$" && source.charAt(i + 1) === "{") {
        out += "${";
        i += 2;
        code(true);
      } else {
        out += space(source.charAt(i));
        i += 1;
      }
    }
    out += "`";
    i += 1;
  }

  function regex(): void {
    out += "/";
    i += 1;
    let inClass = false;
    while (i < source.length && (source.charAt(i) !== "/" || inClass)) {
      const ch = source.charAt(i);
      if (ch === "\\") {
        out += " ";
        i += 1;
      } else if (ch === "[") inClass = true;
      else if (ch === "]") inClass = false;
      out += space(source.charAt(i));
      i += 1;
    }
    out += "/";
    i += 1;
  }

  // Code up to the end of the file, or, inside a template's `${`, up to its closing brace.
  function code(nested: boolean): void {
    let depth = 0;
    let last = "";
    while (i < source.length) {
      const ch = source.charAt(i);
      const after = source.charAt(i + 1);
      if (ch === "/" && after === "/") {
        while (i < source.length && source.charAt(i) !== "\n") {
          out += " ";
          i += 1;
        }
      } else if (ch === "/" && after === "*") {
        const end = source.indexOf("*/", i + 2);
        const stop = end < 0 ? source.length : end + 2;
        for (; i < stop; i += 1) out += space(source.charAt(i));
      } else if (ch === '"' || ch === "'") {
        quoted(ch);
        last = ch;
      } else if (ch === "`") {
        template();
        last = ch;
      } else if (
        ch === "/" &&
        (last === "" || "(,=:[!&|?{};+-*%<>~^".includes(last) || out.trimEnd().endsWith("return"))
      ) {
        regex();
        last = ")";
      } else {
        if (ch === "{") depth += 1;
        if (ch === "}") {
          if (nested && depth === 0) {
            out += "}";
            i += 1;
            return;
          }
          depth -= 1;
        }
        out += ch;
        i += 1;
        if (ch.trim() !== "") last = ch;
      }
    }
  }

  code(false);
  return out;
}

/** What an export holds that can be named after a dot: null when it has no such members. */
interface Declared {
  members: Set<string> | null;
}

const MEMBER =
  /^ {2}(?:(?:public|private|protected|readonly|static|abstract|override|declare|async|get|set|accessor)\s+)*\*?\s*(#?[A-Za-z_$][\w$]*)\s*[?!]?\s*(?=[(:=<;,])/gm;
const EXPORT_DECLARATION =
  /^export\s+(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?(function|class|interface|type|const|let|var|enum)\s*\*?\s*([A-Za-z_$][\w$]*)/gm;

function membersOf(blanked: string, at: number): Set<string> | null {
  const lineEnd = blanked.indexOf("\n", at);
  const first = blanked.slice(at, lineEnd < 0 ? blanked.length : lineEnd).trimEnd();
  if (first.endsWith("}")) return new Set();
  if (!first.endsWith("{")) return null;
  const close = blanked.indexOf("\n}", at);
  const body = blanked.slice(lineEnd, close < 0 ? blanked.length : close);
  return new Set([...body.matchAll(MEMBER)].map((m) => m[1] ?? ""));
}

/** The names a module's text exports, and for a class, an interface or an object, its members. */
export function exportsOf(source: string): Map<string, Declared> {
  const blanked = blank(source);
  const found = new Map<string, Declared>();
  for (const match of blanked.matchAll(EXPORT_DECLARATION)) {
    const name = match[2] ?? "";
    const kind = match[1];
    const members = kind === "function" ? null : membersOf(blanked, match.index);
    found.set(name, { members });
  }
  for (const match of blanked.matchAll(/^export\s+(?:type\s+)?\{([^}]*)\}/gm)) {
    for (const item of (match[1] ?? "").split(",")) {
      const name =
        item
          .trim()
          .replace(/^type\s+/, "")
          .split(/\s+as\s+/)
          .pop() ?? "";
      if (name) found.set(name, { members: null });
    }
  }
  for (const match of blanked.matchAll(/^export\s+\*\s+as\s+([A-Za-z_$][\w$]*)/gm)) {
    found.set(match[1] ?? "", { members: null });
  }
  return found;
}

function sourceFiles(directory: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) found.push(...sourceFiles(path));
    else if (entry.name.endsWith(".ts")) found.push(path);
  }
  return found.sort();
}

function mergeDeclared(into: Map<string, Declared>, name: string, declared: Declared): void {
  const there = into.get(name);
  if (!there) {
    into.set(name, { members: declared.members ? new Set(declared.members) : null });
  } else if (declared.members) {
    there.members = new Set([...(there.members ?? []), ...declared.members]);
  }
}

interface Owners {
  /** By the base name of a file under `src/`: several files may share one. */
  modules: Map<string, Map<string, Declared>>;
  /** By the name of an exported class or interface, wherever it is declared. */
  classes: Map<string, Set<string>>;
}

let owners: Owners | undefined;

/** What a dotted name can start with, and the names found there. */
function ownersOfSource(): Owners {
  if (owners) return owners;
  const modules = new Map<string, Map<string, Declared>>();
  const classes = new Map<string, Set<string>>();
  for (const file of sourceFiles(SRC)) {
    const text = read(file);
    const declared = exportsOf(text);
    const stem = basename(file, ".ts");
    const module = modules.get(stem) ?? new Map<string, Declared>();
    modules.set(stem, module);
    const kinds = new Map<string, string>();
    for (const match of blank(text).matchAll(EXPORT_DECLARATION)) {
      kinds.set(match[2] ?? "", match[1] ?? "");
    }
    for (const [name, one] of declared) {
      mergeDeclared(module, name, one);
      if ((kinds.get(name) === "class" || kinds.get(name) === "interface") && one.members) {
        classes.set(name, new Set([...(classes.get(name) ?? []), ...one.members]));
      }
    }
  }
  owners = { modules, classes };
  return owners;
}

/** True for a dotted name that starts in this package and names something it does not hold. */
function unresolved(span: string): boolean {
  const parts = span.split(".");
  const [first = "", second = "", third] = parts;
  if (FILE_SUFFIXES.has(parts.at(-1) ?? "") || NOT_OURS.has(span)) return false;
  const { modules, classes } = ownersOfSource();
  const exported = modules.get(first);
  const members = classes.get(first);
  if (!exported && !members) return false;
  const declared = exported?.get(second);
  if (declared) {
    return third !== undefined && declared.members !== null && !declared.members.has(third);
  }
  return !members?.has(second);
}

for (const doc of SYMBOL_DOCS) {
  test(`every symbol a doc names exists in the source [${docId(doc)}]`, () => {
    // A function, class or constant renamed in the source and not in the doc fails here.
    const missing = [...new Set([...read(doc).matchAll(SYMBOL)].map((m) => m[1] ?? ""))]
      .filter(unresolved)
      .sort();
    assert.deepEqual(missing, [], `${basename(doc)} names what the source does not define`);
  });
}

test("the scan of the sources reads comments, strings and templates as the compiler does", () => {
  const source = [
    "// export const COMMENTED = 1;",
    "export const TEXT = `a $" + "{ `{` + '}' } b`; // }",
    'export const PATTERN = /["}]/g;',
    "export class Box {",
    "  readonly size: number;",
    "  private static count = 0;",
    "  open(): void {",
    '    const inner = "}";',
    "  }",
    "}",
    "export interface Shape {",
    "  readonly kind: string;",
    "  area?(): number;",
    "}",
    "export const TABLE = {",
    "  first: 1,",
    "  second() {},",
    "} as const;",
    "export { a, type B, c as d } from './x.ts';",
    "export function run(): void {}",
  ].join("\n");
  const found = exportsOf(source);
  assert.deepEqual([...found.keys()].sort(), [
    "B",
    "Box",
    "PATTERN",
    "Shape",
    "TABLE",
    "TEXT",
    "a",
    "d",
    "run",
  ]);
  assert.deepEqual([...(found.get("Box")?.members ?? [])].sort(), ["count", "open", "size"]);
  assert.deepEqual([...(found.get("Shape")?.members ?? [])].sort(), ["area", "kind"]);
  assert.deepEqual([...(found.get("TABLE")?.members ?? [])].sort(), ["first", "second"]);
  assert.equal(found.get("run")?.members, null);
});

test("a dotted name is checked against the module, then the export's members", () => {
  assert.equal(unresolved("texts.GUIDE"), false);
  assert.equal(unresolved("texts.NO_SUCH_TEXT"), true);
  assert.equal(unresolved("commands.WORD.help"), false);
  assert.equal(unresolved("commands.WORD.nothing"), true);
  assert.equal(unresolved("ConfigError.constructor"), false);
  assert.equal(unresolved("ConfigError.nothing"), true);
  assert.equal(unresolved("process.env"), false);
  assert.equal(unresolved("state.json"), false);
  assert.equal(unresolved("state.values"), false);
});

test("every source file's braces balance once comments and strings are blanked", () => {
  for (const file of sourceFiles(SRC)) {
    const text = blank(read(file));
    const open = (text.match(/\{/g) ?? []).length;
    const close = (text.match(/\}/g) ?? []).length;
    assert.equal(open, close, `${docId(file)}: ${open} opening and ${close} closing braces`);
  }
});

for (const doc of PROSE_DOCS) {
  test(`no label of the decision log in prose [${docId(doc)}]`, () => {
    const hits = proseLines(read(doc))
      .filter(([, line]) => DECISION_LABEL.test(line))
      .map(([number]) => number);
    assert.deepEqual(
      hits,
      [],
      `${basename(doc)}: decision label on lines ${hits}; name the behaviour`,
    );
  });
}

test("the setup guide names every variable scope and word", () => {
  const text = read(SETUP);
  const read_ = [
    ...read(join(SRC, "core", "config.ts")).matchAll(/\bvalues(?:\.([A-Z_]+)|\["([A-Z_]+)"\])/g),
  ].map((m) => m[1] ?? m[2] ?? "");
  const manifest = JSON.parse(read(join(ROOT, "slack-app-manifest.json"))) as {
    oauth_config: { scopes: { bot: string[] } };
  };
  for (const name of new Set([...REQUIRED, ...read_, ...manifest.oauth_config.scopes.bot])) {
    assert.ok(text.includes(`\`${name}\``), `docs/setup.md does not name ${name}`);
  }
  const rows = new Set(
    [...(text.split("### Commands")[1] ?? "").matchAll(/^\| `!(\w+)/gm)].map((m) => m[1] ?? ""),
  );
  const missing = Object.values(WORD).filter((word) => !rows.has(word));
  assert.deepEqual(missing, [], `docs/setup.md has no row for ${missing}`);
});

test("the features table can be scanned", () => {
  const long = Object.fromEntries(
    features()
      .filter((f) => f.name.length > FEATURE_NAME_LIMIT)
      .map((f) => [f.name.slice(0, 40), f.name.length]),
  );
  assert.deepEqual(
    long,
    {},
    `docs/features.md: feature names over ${FEATURE_NAME_LIMIT} characters`,
  );
});

test("the readme stays a landing page", () => {
  const words = read(README).split(/\s+/).filter(Boolean).length;
  assert.ok(
    words <= README_WORD_LIMIT,
    `README.md has ${words} words, over ${README_WORD_LIMIT}: detail belongs in docs/setup.md`,
  );
});

/** The cells of each row of the table under `heading` in docs/limits.md. */
function limitsRows(heading: string): string[][] {
  const section = (read(LIMITS).split(`## ${heading}\n`)[1] ?? "").split("\n## ")[0] ?? "";
  return section
    .split("\n")
    .filter((line) => line.startsWith("|"))
    .slice(2)
    .map((row) =>
      row
        .replace(/^\||\|$/g, "")
        .split("|")
        .map((cell) => cell.trim()),
    );
}

test("a command listed as not offered is not in the recorded commands", () => {
  // `server-info.json` is what the SDK's server info returned when the fixtures were recorded:
  // recorded again on a release that offers one of these commands, it makes this fail, and
  // the page loses the row.
  const info = sdkJson("server-info") as { commands: { name: string }[] };
  const offered = new Set(info.commands.map((c) => c.name));
  const named = limitsRows("Limits of the Claude Agent SDK").flatMap((row) =>
    [...(row[1] ?? "").matchAll(/`\/([a-z-]+)`/g)].map((m) => m[1] ?? ""),
  );
  assert.ok(named.length >= 19, `${named}`);
  assert.deepEqual(
    named.filter((name) => offered.has(name)),
    [],
  );
});

test("every limit says whether an issue looks into it", () => {
  const headings = [...read(LIMITS).matchAll(/^## (.+)$/gm)].map((m) => m[1] ?? "");
  const tables = headings.filter((heading) => heading !== "Not checked");
  assert.equal(tables.length, 4, `${headings}`);
  const issue = /^\[#(\d+)\]\(https:\/\/github\.com\/duqaXxX\/awaydesk\/issues\/\1\)$/;
  for (const heading of tables) {
    for (const row of limitsRows(heading)) {
      const last = row.at(-1) ?? "";
      assert.ok(last === "none" || issue.test(last), `${heading}: ${row[0]}: ${last}`);
    }
  }
});
