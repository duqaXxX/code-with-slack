/**
 * The dependency rule of the three layers, read off the imports: `core` knows neither
 * library, `agent/claude` is the only importer of the Agent SDK and `chat/slack` of the Slack
 * packages.
 */
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, posix, relative, sep } from "node:path";
import { test } from "node:test";

const SRC = join(import.meta.dirname, "..", "src");
const AGENT_SDK = "@anthropic-ai/claude-agent-sdk";
const SLACK = "@slack/";

const IMPORT =
  /\b(?:import|export)\s+(?:type\s+)?(?:[^'"`;]*?\sfrom\s+)?["']([^"']+)["']|\b(?:import|require)\s*\(\s*["']([^"']+)["']\s*\)/g;

/** The module specifiers a source text imports, re-exports or loads. */
export function imported(source: string): string[] {
  return [...source.matchAll(IMPORT)].map((match) => (match[1] ?? match[2]) as string);
}

function sources(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return sources(path);
    return entry.name.endsWith(".ts") ? [path] : [];
  });
}

/** `file`, a path under `src/` with forward slashes, may import `specifier`: null, or why not. */
export function refusal(file: string, specifier: string): string | null {
  const target = specifier.startsWith(".")
    ? posix.normalize(posix.join(posix.dirname(file), specifier))
    : specifier;
  if (target.startsWith(AGENT_SDK) && !file.startsWith("agent/claude/")) {
    return "only agent/claude imports the Agent SDK";
  }
  if (target.startsWith(SLACK) && !file.startsWith("chat/slack/")) {
    return "only chat/slack imports the Slack packages";
  }
  if (file.startsWith("core/") && /^(agent\/claude|chat\/slack)\//.test(target)) {
    return "core imports the seams, never a back end or a provider";
  }
  if (file.startsWith("agent/") && /^(chat|core)\//.test(target)) {
    return "the agent module imports neither the chat module nor the core";
  }
  if (file.startsWith("chat/") && /^agent\/claude\//.test(target)) {
    return "the chat module never imports a back end";
  }
  return null;
}

test("the scan reads every form of import", () => {
  const source = [
    'import a from "one";',
    'import type { B } from "two";',
    'import { c,\n  d } from "three";',
    'import "four";',
    'export { e } from "five";',
    'export * from "six";',
    'const f = await import("seven");',
    'const g = require("eight");',
    'const text = "import nothing";',
  ].join("\n");
  assert.deepEqual(imported(source), [
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
  ]);
});

test("the rule refuses a library outside its module", () => {
  assert.ok(refusal("core/sessions.ts", AGENT_SDK));
  assert.ok(refusal("core/sessions.ts", "@slack/web-api"));
  assert.ok(refusal("chat/slack/app.ts", AGENT_SDK));
  assert.ok(refusal("agent/claude/session.ts", "@slack/bolt"));
  assert.ok(refusal("main.ts", `${AGENT_SDK}/sdk-tools`));
  assert.ok(refusal("core/sessions.ts", "../agent/claude/session.ts"));
  assert.ok(refusal("core/reply/renderer.ts", "../../chat/slack/reply/sinks.ts"));
  assert.ok(refusal("agent/claude/session.ts", "../../core/state.ts"));
  assert.ok(refusal("chat/slack/app.ts", "../../agent/claude/session.ts"));
});

test("the rule lets each module import its own library and the seams", () => {
  assert.equal(refusal("agent/claude/session.ts", AGENT_SDK), null);
  assert.equal(refusal("chat/slack/app.ts", "@slack/bolt"), null);
  assert.equal(refusal("core/sessions.ts", "../agent/seam.ts"), null);
  assert.equal(refusal("core/sessions.ts", "../chat/seam.ts"), null);
  assert.equal(refusal("chat/slack/requests.ts", "../../agent/seam.ts"), null);
  assert.equal(refusal("chat/slack/app.ts", "../../core/state.ts"), null);
  assert.equal(refusal("main.ts", "./agent/claude/backend.ts"), null);
  assert.equal(refusal("core/state.ts", "node:fs"), null);
});

test("every source file keeps the dependency rule", () => {
  const files = sources(SRC);
  assert.ok(files.length > 0);
  const broken: string[] = [];
  for (const path of files) {
    const file = relative(SRC, path).split(sep).join("/");
    for (const specifier of imported(readFileSync(path, "utf8"))) {
      const why = refusal(file, specifier);
      if (why !== null) broken.push(`${file} imports ${specifier}: ${why}`);
    }
  }
  assert.deepEqual(broken, []);
});

test("a relative import names its file with the .ts extension", () => {
  // Node runs the sources as they are, and resolves no extension by itself.
  const bare: string[] = [];
  for (const path of [...sources(SRC), ...sources(dirname(import.meta.filename))]) {
    for (const specifier of imported(readFileSync(path, "utf8"))) {
      if (specifier.startsWith(".") && !/\.(ts|json)$/.test(specifier)) {
        bare.push(`${relative(SRC, path)}: ${specifier}`);
      }
    }
  }
  assert.deepEqual(bare, []);
});
