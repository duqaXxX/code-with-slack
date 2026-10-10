/**
 * Text counted as Python counted it. Two halves: the helpers of `chars.ts` on their own, and the
 * sink over texts full of characters past U+FFFF (the squares of a diff, an emoji), whose every
 * limit must fall where Python's fell.
 *
 * The recordings never cross a limit with such a character, so the golden files cannot tell a
 * count in UTF-16 units from one in code points. The second half can: each scenario was replayed
 * on the Python `ReplySink` (the source of 2026-10-10, over `tests/fakes.FakeSlack`, with the
 * golden files' discipline and `FakeClock.advance` for the 280 seconds), and `PYTHON` holds what
 * it sent: per Slack call, the method and the sizes in code points of what the call carried.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import type { Preview, TaskUpdate } from "../../../../src/chat/seam.ts";
import {
  at,
  blank,
  lastNewline,
  len,
  lstripNewlines,
  rstripNewlines,
  strip,
  stripNewlines,
  take,
} from "../../../../src/chat/slack/reply/chars.ts";
import { ReplySink, UpdateLimiter } from "../../../../src/chat/slack/reply/sinks.ts";
import { oneLine } from "../../../../src/core/reply/words.ts";
import {
  BOT,
  CHANNEL,
  FakeClock,
  FakeSlack,
  OWNER,
  TEAM,
  THREAD,
} from "../../../support/fake-slack.ts";
import type { Json, JsonObject } from "../../../support/fixtures.ts";

const R = "🟥";
const G = "🟩";
// Characters Python's `str.strip` and `str.split` take for whitespace and JavaScript's do not,
// and the one JavaScript takes and Python does not: built by code, never pasted.
const FILE_SEPARATOR = String.fromCodePoint(0x1c);
const NEXT_LINE = String.fromCodePoint(0x85);
const BYTE_ORDER_MARK = String.fromCodePoint(0xfeff);

test("a length is counted in code points", () => {
  assert.equal(len(""), 0);
  assert.equal(len("abc"), 3);
  assert.equal(len(`a${R}b${G}`), 4);
  assert.equal(`a${R}b${G}`.length, 6);
  // A half pair is one character, as Python counts a lone surrogate.
  assert.equal(len(R.slice(0, 1)), 1);
  assert.equal(len(R.slice(1)), 1);
});

test("a cut after n characters never lands inside a pair", () => {
  const text = `${R}${R}a${G}`;
  assert.deepEqual(
    [0, 1, 2, 3, 4, 5].map((count) => take(text, count)),
    ["", R, R + R, `${R}${R}a`, text, text],
  );
  assert.equal(at(text, 2), 4);
  assert.equal(at(text, 99), text.length);
  assert.equal(at(text, -1), 0);
});

test("stripping takes python's whitespace", () => {
  assert.equal(strip(` \t\n${FILE_SEPARATOR}${NEXT_LINE}x y${NEXT_LINE}\r\n`), "x y");
  // Not whitespace in Python: it stays.
  assert.equal(strip(`${BYTE_ORDER_MARK}x`), `${BYTE_ORDER_MARK}x`);
  assert.equal(blank(` ${FILE_SEPARATOR}\n`), true);
  assert.equal(blank(BYTE_ORDER_MARK), false);
  assert.equal(blank(""), true);
});

test("newlines are stripped alone", () => {
  assert.equal(lstripNewlines("\n\n a\n"), " a\n");
  assert.equal(rstripNewlines("\n a \n\n"), "\n a ");
  assert.equal(stripNewlines("\n\n"), "");
  assert.equal(lastNewline("a\nb\nc", 3), 1);
  assert.equal(lastNewline("a\nb\nc", 4), 3);
  assert.equal(lastNewline("abc", 3), -1);
  assert.equal(lastNewline("\nabc", 0), -1);
});

test("one line is cut in code points and split on python's whitespace", () => {
  assert.equal(oneLine(`a ${FILE_SEPARATOR} b${NEXT_LINE}c`, 80), "a b c");
  assert.equal(oneLine(`${R}${R}${R}${R}`, 3), `${R}${R}…`);
  assert.equal(oneLine(`${R}${R}${R}`, 3), `${R}${R}${R}`);
});

type Op =
  | ["text", string, ("notice" | "ending")?]
  | ["task", TaskUpdate]
  | ["settle"]
  | ["advance", number]
  | ["finish"]
  | ["close", string | null];

function upd(
  id: string,
  title: string,
  status: TaskUpdate["status"],
  fields: Partial<TaskUpdate> = {},
): TaskUpdate {
  return {
    id,
    title,
    status,
    details: null,
    output: null,
    name: "",
    task: false,
    calls: 0,
    preview: null,
    folded: null,
    ...fields,
  };
}

function view(
  title: string,
  summary: string,
  body: string,
  language: "" | "diff" = "",
  plain = false,
): Preview {
  return { title, summary, body, language, plain };
}

function lines(count: number, line: (i: number) => string, first = 0): string[] {
  return Array.from({ length: count }, (_, i) => line(i + first));
}

const DIFF = lines(899, (i) => `+${G} ${i} ${"x".repeat(40)}${R}`, 1).join("\n");
const HEADS = lines(70, (i) => `## ${R} heading ${i}\n\nbody ${G} ${i}\n\n`).join("");

const SCENARIOS: Record<string, Op[]> = {
  // text past the limit, in a stream
  "text-stream": [
    ["text", lines(3000, (i) => `${R}${G} line ${i} é中\n`).join("")],
    ["settle"],
    ["finish"],
    ["close", "footer"],
  ],
  // text in pieces, no line break to cut at
  "text-nolines": [
    ["text", `${R}a`.repeat(4000)],
    ["settle"],
    ["text", `${G}bc`.repeat(4000)],
    ["settle"],
    ["finish"],
    ["close", "footer"],
  ],
  // previews and cards: titles, details that grow, a context block, code blocks, an error
  previews: [
    [
      "task",
      upd("e", "Edit: e", "complete", {
        name: "Edit",
        preview: view(`Update(${R.repeat(200)})`, G.repeat(200), DIFF, "diff"),
      }),
    ],
    ["settle"],
    [
      "task",
      upd("a", `Agent: ${R.repeat(200)}`, "in_progress", {
        name: "Agent",
        task: true,
        calls: 3,
        details: lines(30, (i) => `Read: ${G}${i}`).join("\n"),
      }),
    ],
    ["settle"],
    [
      "task",
      upd("a", `Agent: ${R.repeat(200)}`, "in_progress", {
        name: "Agent",
        task: true,
        calls: 4,
        details: lines(35, (i) => `Read: ${G}${i}`, 25).join("\n"),
      }),
    ],
    ["settle"],
    [
      "task",
      upd("q", "AskUserQuestion: q", "complete", {
        name: "AskUserQuestion",
        preview: view(
          "User answered",
          "",
          lines(40, () => `· ${R.repeat(50)} → ${G.repeat(50)}\`x\`<b>`).join("\n"),
          "",
          true,
        ),
      }),
    ],
    ["settle"],
    ["task", upd("w", "Write: w", "in_progress", { name: "Write" })],
    ["settle"],
    [
      "task",
      upd("w", "Write: w", "complete", {
        name: "Write",
        preview: view(
          "Write(n.txt)",
          "Wrote lines",
          lines(2499, (i) => `${i} ${R}\`\`\`${G}`, 1).join("\n"),
        ),
      }),
    ],
    ["settle"],
    ["task", upd("b", `Bash: ${R.repeat(100)}`, "error", { name: "Bash", output: G.repeat(4000) })],
    ["settle"],
    ["finish"],
    ["close", "footer"],
  ],
  // after the 280 seconds: the update path, the banner, continuation posts
  window: [
    ["text", `${R.repeat(400)}\n\nsecond`],
    ["settle"],
    ["advance", 281],
    ["text", lines(3000, (i) => `${R}${G} line ${i}\n`).join("")],
    ["settle"],
    [
      "task",
      upd("e", "Edit: e", "complete", {
        name: "Edit",
        preview: view("Update(a)", "Added", DIFF, "diff"),
      }),
    ],
    ["settle"],
    ["text", `The end ${G.repeat(500)} & <done>\n\nmore ${R}`],
    ["settle"],
    ["finish"],
    ["close", `footer ${R}`],
  ],
  // headings: the blocks Slack makes of a text, in a stream and by update
  "headings-stream": [["text", HEADS], ["settle"], ["finish"], ["close", null]],
  "headings-update": [
    ["text", `intro ${R}`],
    ["settle"],
    ["advance", 281],
    ["text", `\n\n${HEADS}`],
    ["settle"],
    ["finish"],
    ["close", "f"],
  ],
  // a reply cut short whose first message is full to the character
  ending: [
    ["text", `${R}w`.repeat(5500)],
    ["settle"],
    ["advance", 281],
    ["text", `\n\nClaude Code reported an error: ${G.repeat(30)}`, "ending"],
    ["text", `\n\n_3 messages were not sent_ ${R}`, "notice"],
    ["finish"],
    ["close", null],
  ],
  // cards whose details fill a streamed message
  "cards-fill": [
    ...Array.from({ length: 12 }, (_, i) => i).map(
      (i): Op => [
        "task",
        upd(`t${i}`, `Agent: ${R}${i}`, "in_progress", {
          name: "Agent",
          task: true,
          details: lines(30, () => G.repeat(60)).join("\n"),
        }),
      ],
    ),
    ["settle"],
    ["text", R.repeat(3000)],
    ["settle"],
    ["finish"],
    ["close", "footer"],
  ],
};

// What the Python sink sent for each scenario: [method, sizes], the sizes in code points.
const PYTHON: Record<string, [string, number[]][]> = {
  "cards-fill": [
    ["startStream", [9, 1829, 0, 9, 1829, 0, 9, 1829, 0]],
    ["stopStream", []],
    ["startStream", [9, 1829, 0, 9, 1829, 0, 9, 1829, 0]],
    ["stopStream", []],
    ["startStream", [9, 1829, 0, 9, 1829, 0, 9, 1829, 0]],
    ["stopStream", []],
    ["startStream", [9, 1829, 0, 10, 1829, 0, 10, 1829, 0]],
    ["update", [9, 9, 9, 9]],
    ["update", [9, 9, 9, 9]],
    ["update", [9, 9, 9, 9]],
    ["appendStream", [234]],
    ["stopStream", []],
    ["startStream", [2766]],
    ["update", [234, 9, 10, 10, 234]],
    ["stopStream", [0, 6]],
  ],
  ending: [
    ["startStream", [11000]],
    ["stopStream", []],
    ["update", [300, 11000]],
    ["postMessage", [61, 61, 28]],
  ],
  "headings-stream": [
    ["startStream", [596]],
    ["stopStream", []],
    ["startStream", [616]],
    ["stopStream", []],
    ["startStream", [616]],
    ["stopStream", []],
    ["startStream", [112]],
    ["stopStream", []],
  ],
  "headings-update": [
    ["startStream", [7]],
    ["stopStream", []],
    ["update", [7, 603]],
    ["postMessage", [12, 614]],
    ["postMessage", [12, 614]],
    ["postMessage", [12, 110]],
    ["postMessage", [7, 0, 1]],
  ],
  previews: [
    ["startStream", [10965]],
    ["stopStream", []],
    ["startStream", [10975]],
    ["stopStream", []],
    ["startStream", [10975]],
    ["stopStream", []],
    ["startStream", [10975]],
    ["stopStream", []],
    ["startStream", [48]],
    ["appendStream", [150, 289, 0]],
    ["appendStream", [150, 300, 0]],
    ["appendStream", [13, 0, 0, 3000]],
    ["appendStream", [8, 0, 0]],
    ["appendStream", [12, 0, 11]],
    ["stopStream", []],
    ["startStream", [10999, 2208]],
    ["stopStream", []],
    ["startStream", [11005, 2009]],
    ["stopStream", []],
    ["startStream", [5194]],
    ["update", [150, 48, 150, 13, 3000, 12]],
    ["appendStream", [150, 0, 0]],
    ["stopStream", [0, 6]],
    ["update", [12, 5194, 21, 0, 6]],
  ],
  "text-nolines": [
    ["startStream", [8000]],
    ["appendStream", [3000]],
    ["stopStream", []],
    ["startStream", [9000]],
    ["stopStream", [0, 6]],
  ],
  "text-stream": [
    ["startStream", [10989]],
    ["stopStream", []],
    ["startStream", [10987]],
    ["stopStream", []],
    ["startStream", [10991]],
    ["stopStream", []],
    ["startStream", [10991]],
    ["stopStream", []],
    ["startStream", [2928]],
    ["stopStream", [0, 6]],
  ],
  window: [
    ["startStream", [408]],
    ["stopStream", []],
    ["update", [300, 10989]],
    ["postMessage", [300, 10992]],
    ["postMessage", [300, 10997]],
    ["postMessage", [300, 5316]],
    ["update", [300, 5316, 10965, 10975, 10975, 10975, 48]],
    ["update", [300, 5316, 10965, 10975, 10975, 10975, 48, 525]],
    ["postMessage", [300, 525, 0, 8]],
    ["update", [300, 5316, 10965, 10975, 10975, 10975, 48]],
  ],
};

/** Python's `len`, counted here on its own and not by the code under test. */
function points(text: Json | undefined): number {
  return typeof text === "string" ? Array.from(text).length : 0;
}

function objects(value: Json | undefined): JsonObject[] {
  return Array.isArray(value) ? (value as JsonObject[]) : [];
}

function blockSize(block: JsonObject): number {
  switch (block.type) {
    case "markdown":
      return points(block.text);
    case "container":
      return objects(block.child_blocks)
        .flatMap((child) => objects(child.elements))
        .flatMap((pre) => objects(pre.elements))
        .reduce((sum, element) => sum + points(element.text), 0);
    case "context":
      return objects(block.elements).reduce((sum, element) => sum + points(element.text), 0);
    case "task_card":
      return points(block.title);
    default:
      return 0;
  }
}

/** The sizes of what a call carried: its `text`, each chunk, each block. */
function sizes(args: JsonObject): number[] {
  const out: number[] = [];
  if ("text" in args) out.push(points(args.text));
  for (const chunk of objects(args.chunks)) {
    if (chunk.type === "markdown_text") out.push(points(chunk.text));
    else if (chunk.type === "task_update") {
      out.push(points(chunk.title), points(chunk.details), points(chunk.output));
    } else out.push(...objects(chunk.blocks).map(blockSize));
  }
  out.push(...objects(args.blocks).map(blockSize));
  return out;
}

async function replay(ops: Op[]): Promise<FakeSlack> {
  const slack = new FakeSlack();
  const clock = new FakeClock();
  const sink = new ReplySink(slack, {
    channel: CHANNEL,
    threadTs: THREAD,
    teamId: TEAM,
    userId: OWNER,
    botUserId: BOT,
    limiter: new UpdateLimiter({ limit: 10 ** 9, burst: 10 ** 9, clock }),
    clock,
  });
  for (const op of ops) {
    switch (op[0]) {
      case "text":
        await sink.text(op[1], { notice: op[2] === "notice", ending: op[2] === "ending" });
        break;
      case "task":
        await sink.task(op[1]);
        break;
      case "settle":
        await sink.settle();
        break;
      case "advance":
        await clock.advance(op[1]);
        break;
      case "finish":
        await sink.finish([]);
        break;
      case "close":
        await sink.closeOutFormatted(op[1]);
        break;
    }
  }
  await sink.settle();
  return slack;
}

for (const [name, ops] of Object.entries(SCENARIOS)) {
  test(`a reply full of characters past U+FFFF is cut where python cut it [${name}]`, async () => {
    const slack = await replay(ops);
    const made = slack.apiCalls.map((call) => [call.method.replace("chat.", ""), sizes(call.args)]);
    assert.deepEqual(made, PYTHON[name]);
  });
}
