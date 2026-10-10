/**
 * Port of `tests/test_previews.py`, in the file's order.
 *
 * Python's `preview` read `UserMessage.tool_use_result` itself. Here the Claude back end reads
 * that shape into the seam's `FileChange` (`fileChange` in `src/agent/claude/translate.ts`) and
 * `preview` reads the change: a recorded result reaches a test through the translator, and a
 * result a Python test wrote by hand is that same object through `fileChange`.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import { fileChange } from "../../../src/agent/claude/translate.ts";
import type { FileChange } from "../../../src/agent/seam.ts";
import { answered, folded, preview } from "../../../src/core/reply/previews.ts";
import { recordedEvents } from "../../support/replay.ts";

const CWD = "/home/dev/project";
const RED = "\u{1f7e5}";
const GREEN = "\u{1f7e9}";

/** Each tool call of a recording: its tool, what it did to a file, and whether it failed. */
function results(name: string): [tool: string, change: FileChange | null, failed: boolean][] {
  const tools = new Map<string, string>();
  const out: [string, FileChange | null, boolean][] = [];
  for (const event of recordedEvents(name)) {
    if (event.type === "call_started") tools.set(event.callId, event.toolName);
    if (event.type === "call_ended") {
      const tool = tools.get(event.callId);
      assert.ok(tool !== undefined, event.callId);
      out.push([tool, event.fileChange, event.isError]);
    }
  }
  return out;
}

function lines(body: string | undefined): string[] {
  return (body ?? "").split("\n");
}

test("a new file shows its first ten lines as the terminal", () => {
  const [first] = results("edit-write");
  assert.ok(first !== undefined);
  const [name, change] = first;
  assert.equal(name, "Write");
  const numbered = Array.from({ length: 10 }, (_, i) => `${String(i + 1).padStart(2)} ${i + 1}`);
  assert.deepEqual(preview(name, change, CWD), {
    title: "Write(new.txt)",
    summary: "Wrote 15 lines to new.txt",
    body: `${numbered.join("\n")}\n… +5 lines`,
    language: "",
    plain: false,
  });
});

test("an edit shows its diff numbered as the terminal", () => {
  const edits = results("edit-write").filter(([name]) => name === "Edit");
  const done = edits.find(([, , failed]) => !failed);
  assert.ok(done !== undefined);
  const shown = preview("Edit", done[1], CWD);
  assert.ok(shown !== null);
  assert.deepEqual(
    [shown.title, shown.summary],
    ["Update(notes.txt)", "Added 1 line, removed 1 line"],
  );
  assert.deepEqual(lines(shown.body), [
    "    1 alpha",
    `-${RED} 2 beta`,
    `+${GREEN} 2 gamma`,
    "    3 delta",
  ]);
});

test("a write over a file shows the whole diff", () => {
  const second = results("edit-write").filter(([name]) => name === "Write")[1];
  assert.ok(second !== undefined);
  const shown = preview(second[0], second[1], CWD);
  assert.ok(shown !== null);
  assert.deepEqual(
    [shown.title, shown.summary],
    ["Write(notes.txt)", "Added 2 lines, removed 3 lines"],
  );
  assert.deepEqual(lines(shown.body), [
    `-${RED} 1 alpha`,
    `-${RED} 2 gamma`,
    `-${RED} 3 delta`,
    `+${GREEN} 1 one`,
    `+${GREEN} 2 two`,
  ]);
});

test("hunks are separated as the terminal separates them", () => {
  const patch = [
    { oldStart: 1, oldLines: 1, newStart: 1, newLines: 1, lines: ["-a", "+b"] },
    { oldStart: 40, oldLines: 1, newStart: 40, newLines: 1, lines: [" c"] },
  ];
  const change = fileChange({ filePath: "/elsewhere/f.txt", structuredPatch: patch });
  const shown = preview("Edit", change, CWD);
  assert.ok(shown !== null);
  assert.equal(shown.title, "Update(/elsewhere/f.txt)"); // outside the folder: the full path
  assert.deepEqual(lines(shown.body), [`-${RED}  1 a`, `+${GREEN}  1 b`, "...", "    40 c"]);
});

const OTHER_SHAPES: readonly (readonly [string, unknown])[] = [
  ["Read", { filePath: "/home/dev/project/a", structuredPatch: [] }],
  ["Edit", null],
  ["Edit", { filePath: "/home/dev/project/a" }],
  ["Edit", { filePath: "/home/dev/project/a", structuredPatch: [{ lines: ["-a"] }] }],
  ["Write", { filePath: "/home/dev/project/a", type: "create", content: 3 }],
  ["Edit", { filePath: 7, structuredPatch: [] }],
];

for (const [name, result] of OTHER_SHAPES) {
  const shape = `${name}, ${JSON.stringify(result)}`;
  test(`any other tool or shape falls back to the generic line [${shape}]`, () => {
    assert.equal(preview(name, fileChange(result), CWD), null);
  });
}

test("folded calls read as the terminal for bash and read only", () => {
  assert.equal(folded("Bash", 1), "Ran 1 shell command");
  assert.equal(folded("Bash", 3), "Ran 3 shell commands");
  assert.equal(folded("Read", 2), "Read 2 files");
  assert.equal(folded("WebFetch", 1), "WebFetch");
  assert.equal(folded("WebFetch", 2), "WebFetch ×2");
});

test("a long diff shows whole", () => {
  const patch = [
    {
      oldStart: 1,
      oldLines: 30,
      newStart: 1,
      newLines: 0,
      lines: Array.from({ length: 30 }, (_, i) => `-line ${i + 1}`),
    },
  ];
  const change = fileChange({ filePath: "/home/dev/project/f", structuredPatch: patch });
  const shown = preview("Edit", change, CWD);
  assert.ok(shown !== null);
  // As the terminal shows it: every line, since the diff is collapsed until the owner opens it.
  const body = lines(shown.body);
  assert.ok(body.length === 30 && body[29] === `-${RED} 30 line 30`);
  assert.equal(shown.summary, "Removed 30 lines");
});

test("a missing final newline is not a line of the file", () => {
  const patch = [
    {
      oldStart: 1,
      oldLines: 1,
      newStart: 1,
      newLines: 2,
      lines: ["-a", "\\ No newline at end of file", "+a", "+b"],
    },
  ];
  const change = fileChange({ filePath: "/home/dev/project/f", structuredPatch: patch });
  const shown = preview("Edit", change, CWD);
  assert.ok(shown !== null);
  assert.deepEqual(lines(shown.body), [`-${RED} 1 a`, `+${GREEN} 1 a`, `+${GREEN} 2 b`]);
});

test("an unknown line shape falls back to the generic line", () => {
  const patch = [{ oldStart: 1, oldLines: 1, newStart: 1, newLines: 1, lines: ["~a"] }];
  const change = fileChange({ filePath: "/home/dev/project/f", structuredPatch: patch });
  assert.equal(preview("Edit", change, CWD), null);
});

const QUESTIONS = [{ text: "Colour?" }, { text: "Sizes?" }];

test("an answered question shows each answer as the terminal", () => {
  // The answers as the core sends them back: a label, or a list for a multi-select.
  assert.deepEqual(answered(QUESTIONS, { "Colour?": "blue", "Sizes?": ["s", "xl"] }), {
    title: "User answered Claude's questions:",
    summary: "",
    body: "· Colour? → blue\n· Sizes? → s, xl",
    language: "",
    plain: true,
  });
});

test("a question with no answer has no line and none has no preview", () => {
  const shown = answered(QUESTIONS, { "Sizes?": "s" });
  assert.ok(shown !== null && shown.body === "· Sizes? → s");
  assert.equal(answered(QUESTIONS, {}), null);
});

test("a question written on several lines keeps one line", () => {
  const shown = answered([{ text: "Colour?\nPick one." }], { "Colour?\nPick one.": "dark\nblue" });
  assert.ok(shown !== null && shown.body === "· Colour? Pick one. → dark blue");
});

test("a question s result alone gives no preview", () => {
  // The answers come from the daemon's own data, never from the undocumented result.
  const all = results("ask-answered");
  assert.equal(all.length, 1);
  const [name, change] = all[0] ?? ["", null];
  assert.ok(name === "AskUserQuestion" && preview(name, change, CWD) === null);
});

// Not in the Python file: what `preview` decides on the seam's `FileChange`, where Python
// decided on the raw result.

const EDITED: FileChange = {
  path: "/home/dev/project/notes.txt",
  kind: "edited",
  hunks: [{ oldStart: 3, newStart: 3, lines: ["-a", "+b"] }],
  content: null,
};
const CREATED: FileChange = {
  path: "/home/dev/project/new.txt",
  kind: "created",
  hunks: [],
  content: "one\n",
};

test("a file change under a tool with no preview falls back to the generic line", () => {
  for (const name of ["Read", "Bash", "NotebookEdit", ""]) {
    assert.equal(preview(name, EDITED, CWD), null, name);
    assert.equal(preview(name, CREATED, CWD), null, name);
  }
});

test("an edit that created its file shows the hunks it holds, or nothing", () => {
  // Python read `type: "create"` for a Write alone: under any other name the patch speaks.
  assert.equal(preview("Edit", CREATED, CWD), null);
  const shown = preview("Edit", { ...CREATED, hunks: EDITED.hunks }, CWD);
  assert.deepEqual(
    [shown?.title, shown?.summary, shown?.language],
    ["Update(new.txt)", "Added 1 line, removed 1 line", "diff"],
  );
});

test("a created file with no content has no preview", () => {
  assert.equal(preview("Write", { ...CREATED, content: null }, CWD), null);
});

test("an empty new file is a preview with no line", () => {
  assert.deepEqual(preview("Write", { ...CREATED, content: "" }, CWD), {
    title: "Write(new.txt)",
    summary: "Wrote 0 lines to new.txt",
    body: "",
    language: "",
    plain: false,
  });
});

test("a line under a sign no hunk has falls back to the generic line", () => {
  const hunks = [{ oldStart: 1, newStart: 1, lines: ["-a", "~b"] }];
  assert.equal(preview("Edit", { ...EDITED, hunks }, CWD), null);
  const empty = [{ oldStart: 1, newStart: 1, lines: [""] }];
  assert.equal(preview("Edit", { ...EDITED, hunks: empty }, CWD), null);
});

test("a path is named from the session's folder only when it is inside it", () => {
  const title = (path: string, cwd: string | null) =>
    preview("Edit", { ...EDITED, path }, cwd)?.title;
  assert.equal(title("/home/dev/project/a/b.txt", CWD), "Update(a/b.txt)");
  assert.equal(title("/home/dev/project/a/b.txt", `${CWD}/`), "Update(a/b.txt)");
  // A folder whose name only starts the same is another folder.
  assert.equal(title("/home/dev/project-two/b.txt", CWD), "Update(/home/dev/project-two/b.txt)");
  assert.equal(title("/home/dev/b.txt", CWD), "Update(/home/dev/b.txt)");
  assert.equal(title("/home/dev/project/b.txt", null), "Update(/home/dev/project/b.txt)");
  assert.equal(title("/home/dev/project/b.txt", ""), "Update(/home/dev/project/b.txt)");
  assert.equal(title("b.txt", CWD), "Update(b.txt)");
});
