import assert from "node:assert/strict";
import { test } from "node:test";
import type { CommandInfo } from "../../src/agent/seam.ts";
import {
  type Command,
  commandParts,
  helpText,
  hostOnly,
  parseBang,
  refusedInThread,
  unformatted,
  WORD,
} from "../../src/core/commands.ts";
import * as texts from "../../src/core/texts.ts";
import { sdkJson } from "../support/fixtures.ts";

/**
 * The chat provider's markdown escape, which the core takes as a parameter: the same characters
 * as `MARKDOWN_INLINE` in the Python `render/escape.py` (Slack's markdown block reference, read
 * 2026-09-25).
 */
function markdownEscape(text: string): string {
  return text.replace(/([\\`*_{}[\]()&~])/g, "\\$1");
}

function helpOf(commands: readonly CommandInfo[] | null, query = ""): string {
  return helpText(commands, query, markdownEscape);
}

/** A command as the session's list holds it: what the agent module makes of the SDK's raw entry. */
function command(
  name: string,
  description: string,
  extra: Partial<Omit<CommandInfo, "name" | "description">> = {},
): CommandInfo {
  return { name, description, argumentHint: "", aliases: [], ...extra };
}

/** The recorded server info's commands (SDK 0.2.163), as the session's list holds them. */
function recordedCommands(): CommandInfo[] {
  const info = sdkJson("server-info") as { commands: Record<string, unknown>[] };
  return info.commands.map((raw) =>
    command(String(raw.name), String(raw.description ?? ""), {
      argumentHint: String(raw.argumentHint ?? ""),
      aliases: Array.isArray(raw.aliases) ? raw.aliases.map(String) : [],
    }),
  );
}

const parseCases: [string, Command | null][] = [
  ["!help", { kind: "help", query: "" }],
  ["!help Comp", { kind: "help", query: "comp" }],
  ["!bind ~/code/app", { kind: "bind", path: "~/code/app" }],
  ["!bind  /a b", { kind: "bind", path: "/a b" }],
  ["!bind", { kind: "bind", path: "" }], // alone: the folders a session can start in
  ["!bypass on", { kind: "bypass", on: true }],
  ["!bypass OFF", { kind: "bypass", on: false }],
  ["!open", { kind: "open", target: "" }], // alone: the picker
  ["!OPEN  setup ", { kind: "open", target: "setup" }],
  ["!open docs/my file.md", { kind: "open", target: "docs/my file.md" }],
  ["!bypass", { kind: "invalid" }],
  ["!bypass maybe", { kind: "invalid" }],
  ["!status", { kind: "status" }],
  ["  !stop", { kind: "stop" }],
  ["!compact", { kind: "passthrough", text: "compact" }],
  ["!model opus", { kind: "passthrough", text: "model opus" }],
  ["!status now", { kind: "passthrough", text: "status now" }],
  ["!important: read this", { kind: "passthrough", text: "important: read this" }],
  ["hello", null],
  ["!", null],
  ["! compact", null],
  ["!compact\nnotes", { kind: "passthrough", text: "compact notes" }], // the word ends at a line break too
  ["!stop\n", { kind: "stop" }],
  ["!model  opus ", { kind: "passthrough", text: "model opus" }],
];

for (const [text, expected] of parseCases) {
  test(`parse bang [${JSON.stringify(text)}]`, () => {
    assert.deepEqual(parseBang(text), expected);
  });
}

// A composer message's blocks, as Slack stores them. Read back on 2026-10-07: a section with a
// `text` element in inline code, and a `rich_text_preformatted` part. The `bold` style and the
// `rich_text_quote` and `rich_text_list` part types are named by the Block Kit reference and were
// not read from a real message.
type Part = Record<string, unknown>;

function richText(...parts: Part[]): Part[] {
  return [{ type: "rich_text", block_id: "Rd58H", elements: parts }];
}

function part(leaves: Part[], kind = "rich_text_section"): Part {
  return { type: kind, elements: leaves };
}

function leaf(text: string, style: Record<string, boolean> = {}): Part {
  return { type: "text", text, ...(Object.keys(style).length > 0 ? { style } : {}) };
}

const CODE_BLOCK = { ...part([leaf("!goal tick")], "rich_text_preformatted"), border: 0 };

const unformattedCases: [string, unknown, string][] = [
  // The two shapes read back from Slack: inline code, and a code block.
  ["`!goal tick`", richText(part([leaf("!goal tick", { code: true })])), "!goal tick"],
  ["```!goal tick```\n", richText(CODE_BLOCK), "!goal tick"],
  // Only the first run is read in the blocks: what follows it comes from the text, with its own
  // formatting and its links as the owner sent them.
  [
    "`!goal` what is it?",
    richText(part([leaf("!goal", { code: true }), leaf(" what is it?")])),
    "!goal what is it?",
  ],
  [
    "*!goal* run `make test` see https://example.com/1",
    richText(
      part([leaf("!goal", { bold: true }), leaf(" run "), leaf("make test", { code: true })]),
    ),
    "!goal run `make test` see https://example.com/1",
  ],
  [
    "`!compact`\n```notes```",
    richText(
      part([leaf("!compact", { code: true })]),
      part([leaf("notes")], "rich_text_preformatted"),
    ),
    "!compact\n```notes```",
  ],
  // Anything before the `!` keeps the message a prompt, formatted or not.
  ["`\\!stop`", richText(part([leaf("\\!stop", { code: true })])), ""],
  ["say `!stop`", richText(part([leaf("say "), leaf("!stop", { code: true })])), ""],
  ["!stop", richText(part([leaf("!stop")])), ""], // no marks: `parseBang` reads the text
  // A quote and a list are not read through.
  ["> !stop", richText(part([leaf("!stop")], "rich_text_quote")), ""],
  ["• !stop", richText(part([part([leaf("!stop")])], "rich_text_list")), ""],
  // A text that does not read as marks, the run, the marks again: left alone.
  ["`!stop`", richText(part([leaf("!status", { code: true })])), ""],
  ["`!stop", richText(part([leaf("!stop", { code: true })])), ""],
  ["x`!stop`", richText(part([leaf("!stop", { code: true })])), ""],
  // No composer block: nothing tells a mark from a character the owner typed.
  ["`!stop`", [{ type: "section", text: { type: "mrkdwn", text: "`!stop`" } }], ""],
  ["`!stop`", [], ""],
  ["`!stop`", null, ""],
];

unformattedCases.forEach(([text, blocks, expected], index) => {
  test(`unformatted [${index + 1}: ${JSON.stringify(text)}]`, () => {
    assert.equal(unformatted(text, blocks), expected);
  });
});

test("help lists the daemon words and every session command", () => {
  const commands = recordedCommands();
  const text = helpOf(commands);
  for (const word of ["!help", "!status", "!stop", "!bind", "!bypass", "!open"]) {
    assert.ok(text.includes(`\`${word}`), word);
  }
  assert.ok(commands.filter((c) => c.name !== "clear").every((c) => text.includes(`\`!${c.name}`)));
});

test("help says how a command is told from a text", () => {
  const commands = recordedCommands();
  for (const listed of [null, commands]) {
    // in the channel and inside a session's thread
    assert.equal(helpOf(listed).split("\n")[1], texts.HELP_RULE);
    assert.ok(!helpOf(listed, "status").includes(texts.HELP_RULE)); // a search lists matches only
  }
  assert.ok(texts.HELP_RULE.includes("`\\!goal`"));
});

test("help leaves out clear which a thread refuses", () => {
  // Issue #76: a session's commands are listed inside its thread only, where `!clear` is
  // refused. The recorded list (SDK 0.2.163) does carry it.
  const commands = recordedCommands();
  assert.ok(commands.some((c) => c.name === "clear"));
  assert.ok(!helpOf(commands).includes("`!clear"));
  assert.ok(helpOf(commands).includes("`!compact"));
});

test("a thread refuses clear and its aliases before the session lists them", () => {
  // A session rebuilt after a restart has listed no command until it connects.
  for (const listed of [[], null]) {
    const refused = refusedInThread(listed);
    for (const name of ["clear", "reset", "new"]) assert.ok(refused.has(name), name);
  }
});

test("a thread refuses an alias of clear the session adds", () => {
  const commands = [
    command("clear", "d", { aliases: ["reset", "new", "wipe"] }),
    command("rename", "d", { aliases: ["name"] }),
  ];
  const refused = refusedInThread(commands);
  assert.ok(refused.has("wipe"));
  assert.ok(!refused.has("name") && !refused.has("rename"));
});

test("help before binding says where the commands come from", () => {
  const text = helpOf(null);
  assert.ok(text.includes("`!bind") && !text.includes("`!compact"));
});

test("help filters by name or description", () => {
  const commands = [
    command("compact", "Free up context"),
    command("model", "Set the model", { argumentHint: "[model]" }),
  ];
  const text = helpOf(commands, "CONTEXT");
  assert.ok(text.includes("`!compact`") && !text.includes("`!model"));
  assert.ok(!text.includes("`!status`")); // daemon words are filtered too
  assert.ok(helpOf(commands, "stop").includes("`!stop`"));
});

test("help says when nothing matches", () => {
  const text = helpOf([command("compact", "x")], "zzz");
  assert.ok(text.includes(texts.fill(texts.HELP_NO_MATCH, { query: "zzz" })));
});

test("the guide and the help explain every word of the daemon", () => {
  // A word added without its line in the guide and in !help fails here, so neither goes stale.
  const words: string[] = Object.values(WORD);
  for (const expected of ["help", "guide", "bind", "bypass", "status", "stop", "resume", "open"]) {
    assert.ok(words.includes(expected), expected);
  }
  // A WORD the parser does not answer would document a word that reaches Claude instead.
  for (const word of words) assert.notEqual(parseBang(`!${word}`)?.kind, "passthrough", word);
  const helpLines = texts.HELP_WORDS.join("\n");
  for (const word of words) {
    assert.ok(texts.GUIDE.includes(`\`!${word}`), word);
    assert.ok(helpLines.includes(`\`!${word}`), word);
  }
});

test("guide parses", () => {
  assert.deepEqual(parseBang("!guide"), { kind: "guide" });
  assert.deepEqual(parseBang("!GUIDE"), { kind: "guide" });
});

test("the help titles are bold in a markdown block", () => {
  // `!help` goes out as a markdown block, where bold is **text** and *text* is italic
  // (markdown block reference, read 2026-09-25).
  const text = helpOf([], "");
  assert.ok(text.startsWith("**awaydesk**") && text.includes("**Claude Code**"));
});

// /doctor's description in the bundled CLI 2.1.280, read 2026-09-25: the 100-character cut falls
// inside its code span.
const DOCTOR =
  "Health-check the user's Claude Code setup and fix issues: diagnose installation health " +
  "— what the `claude doctor` terminal diagnostics cover — from local data";

test("a description cannot leave formatting open", () => {
  const commands = [command("doctor", DOCTOR), command("model", "*x_")];
  const lines = helpOf(commands).split("\n");
  const doctor = lines.find((line) => line.startsWith("`!doctor`")) as string;
  const unescaped = doctor.slice("`!doctor`".length).match(/(?<!\\)[`*_]/g) ?? [];
  assert.deepEqual(unescaped, []); // shown as the terminal's menu shows it: plain text
  assert.equal(
    lines.find((line) => line.startsWith("`!model`")),
    "`!model` \\*x\\_",
  );
});

test("the filter reads the description as written", () => {
  const commands = [command("remote", "run a_b")];
  assert.ok(helpOf(commands, "a_b").includes("`!remote`"));
});

test("a hint with a backtick cannot break the line", () => {
  // A code span cannot hold a backtick: such a usage is shown escaped, as plain text.
  const text = helpOf([command("x", "d", { argumentHint: "[`file`]" })]);
  assert.ok(text.split("\n").includes("!x \\[\\`file\\`\\] d"));
});

// What the Python tests reach through `help_text` and `host_only` callers, checked on their own.

test("a description is cut by code points, never inside an emoji", () => {
  const [, description] = commandParts(command("x", `${"a".repeat(98)}😀😀😀`), markdownEscape);
  assert.equal(description, `${"a".repeat(98)}😀…`);
});

test("login and logout are answered on the host, whatever their case", () => {
  assert.equal(hostOnly({ kind: "passthrough", text: "login" }), texts.LOGIN_ON_HOST);
  assert.equal(hostOnly({ kind: "passthrough", text: "LOGOUT now" }), texts.LOGOUT_ON_HOST);
  assert.equal(hostOnly({ kind: "passthrough", text: "compact" }), null);
});
