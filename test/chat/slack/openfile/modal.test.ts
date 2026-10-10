import assert from "node:assert/strict";
import { test } from "node:test";
import {
  BadTarget,
  CHOICE_ACTION,
  CHOICE_BLOCK,
  chosenIn,
  ModalUpdates,
  matchesBlocks,
  modalView,
  OPEN_BUTTON_ACTION,
  option,
  options,
  pickerBlocks,
  QUERY_ACTION,
  QUERY_BLOCK,
  Target,
  typedIn,
} from "../../../../src/chat/slack/openfile/modal.ts";
import * as texts from "../../../../src/core/texts.ts";
import { fill } from "../../../../src/core/texts.ts";

type Path = Array<string | number>;

/** What a payload holds at `path`: undefined where the shape stops. */
function at(value: unknown, ...path: Path): unknown {
  let found = value;
  for (const key of path) {
    if (typeof found !== "object" || found === null) return undefined;
    found = (found as Record<string | number, unknown>)[key];
  }
  return found;
}

// --- options ---

test("a row names the file with its folder below and carries the path", () => {
  assert.deepEqual(option("src/app/main.py"), {
    text: { type: "plain_text", text: "main.py", emoji: false },
    description: { type: "plain_text", text: "src/app" },
    value: "src/app/main.py",
  });
});

test("a file at the root of the folder has no description", () => {
  // A text object is never empty: there is no folder to say.
  assert.deepEqual(option("README.md"), {
    text: { type: "plain_text", text: "README.md", emoji: false },
    value: "README.md",
  });
});

for (const name of [
  "__init__.py",
  "*starred*.md",
  "~struck~.txt",
  "a<b>&c.txt",
  "x`y`.md",
  "_a_.py",
  ":tada:.md",
]) {
  test(`a name that reads as formatting is shown exactly [${name}]`, () => {
    // Plain text: nothing in a name is markup, so nothing is escaped, bolded or reinterpreted.
    const shown = option(`pkg/${name}`);
    assert.equal(at(shown, "text", "text"), name);
    assert.equal(at(shown, "text", "type"), "plain_text");
    assert.equal(at(shown, "text", "emoji"), false);
  });
}

test("a long file name is shortened in its middle and keeps its end", () => {
  const name = `${"a".repeat(60)}-middle-${"b".repeat(30)}.test.py`;
  const shown = option(`src/${name}`);
  const text = String(at(shown, "text", "text"));
  assert.equal(text.length, 75);
  assert.ok(text.startsWith("aaa") && text.endsWith("b.test.py"));
  assert.ok(text.slice(1, -1).includes("…"));
  assert.equal(shown?.value, `src/${name}`);
  assert.equal(at(option("n".repeat(75)), "text", "text"), "n".repeat(75)); // the limit itself: nothing cut
  assert.equal(at(option("n".repeat(76)), "text", "text"), `${"n".repeat(37)}…${"n".repeat(37)}`);
});

test("a long folder is shortened from the left", () => {
  const folder = "dir/".repeat(30); // 120 characters
  const shown = option(`${folder}x.py`);
  const description = String(at(shown, "description", "text"));
  assert.equal(description.length, 75);
  assert.equal(description, `…${folder.replace(/\/+$/, "").slice(-74)}`);
  assert.equal(shown?.value, `${folder}x.py`);
});

test("a path whose value cannot fit is left out", () => {
  assert.notEqual(option("d".repeat(150)), null);
  assert.equal(option("d".repeat(151)), null);
});

test("a group holds ten rows and skips the paths that cannot fit", () => {
  const paths = ["z".repeat(151), ...Array.from({ length: 30 }, (_, i) => `f${i}.py`)];
  assert.deepEqual(
    options(paths).map((row) => row.value),
    Array.from({ length: 10 }, (_, i) => `f${i}.py`),
  );
});

test("names count code points where Python counted them", () => {
  // Extra: an emoji is one character of the 75, and a cut never lands inside it.
  const name = `${"😀".repeat(76)}.md`;
  const text = String(at(option(name), "text", "text"));
  assert.equal(Array.from(text).length, 75);
  assert.ok(!text.includes("�"));
  const path = `${"😀".repeat(150)}`; // 150 code points, 300 UTF-16 units: it fits
  assert.notEqual(option(path), null);
  assert.equal(option(`${path}x`), null);
});

// --- the picker and its modal ---

const TARGET = new Target("C000CHAN", "1790000000.000001");

function inputBlocks(view: unknown): unknown[] {
  return (Array.isArray(at(view, "blocks")) ? (at(view, "blocks") as unknown[]) : []).filter(
    (block) => at(block, "type") === "input",
  );
}

function choiceId(view: unknown): string {
  const [block, ...others] = inputBlocks(view).filter((b) => at(b, "block_id") !== QUERY_BLOCK);
  assert.equal(others.length, 0);
  return String(at(block, "block_id"));
}

test("the message of open alone is a title one button and the way to open by name", () => {
  const [section, actions, context] = pickerBlocks();
  assert.deepEqual(section, { type: "section", text: { type: "mrkdwn", text: "*Open a file*" } });
  const elements = at(actions, "elements") as unknown[];
  assert.equal(elements.length, 1);
  assert.deepEqual(elements[0], {
    type: "button",
    action_id: OPEN_BUTTON_ACTION,
    text: { type: "plain_text", text: "Choose a file" },
  });
  assert.deepEqual(context, {
    type: "context",
    elements: [{ type: "mrkdwn", text: "Or type `!open setup` to open a file by name." }],
  });
});

test("several matches are counted and the button carries the words", () => {
  const [section, actions] = matchesBlocks("set<up", ["a/setup.py", "b/setup.md"]);
  assert.equal(at(section, "text", "text"), "*2 files match* `set&lt;up`");
  const button = at(actions, "elements", 0);
  assert.equal(at(button, "text", "text"), "Choose a file");
  assert.equal(at(button, "value"), "set<up");
  // The words a button carries are bounded: its value is at most 2000 characters in Slack.
  const long = matchesBlocks("w".repeat(5000), ["a.py", "b.py"])[1];
  assert.equal(String(at(long, "elements", 0, "value")).length, 200);
});

test("one match is said in the singular and a cut listing is said last", () => {
  const [section] = matchesBlocks("set", ["a/setup.py"]);
  assert.equal(at(section, "text", "text"), "*1 file matches* `set`");
  const cut = matchesBlocks("set", ["a/setup.py", "b/setup.md"], false);
  assert.deepEqual(
    cut.map((block) => block.type),
    ["section", "actions", "context"],
  );
  assert.equal(at(cut[2], "elements", 0, "text"), texts.OPEN_PARTIAL);
  const tooLong = matchesBlocks("zz", ["e".repeat(200)], false);
  assert.deepEqual(
    tooLong.map((block) => block.type),
    ["section", "context", "context"],
  );
});

test("matches none of which fits a row are said and get no button", () => {
  const blocks = matchesBlocks("zz", [`${"d".repeat(151)}/a.py`, "e".repeat(200)]);
  assert.deepEqual(
    blocks.map((block) => block.type),
    ["section", "context"],
  );
  assert.ok(String(at(blocks[0], "text", "text")).includes("2 files match"));
  assert.equal(at(blocks[1], "elements", 0, "text"), texts.OPEN_MATCHES_TOO_LONG);
  // The control: one that fits is a button again.
  const fits = matchesBlocks("zz", ["d".repeat(151), "ok.py"]);
  assert.deepEqual(
    fits.map((block) => block.type),
    ["section", "actions"],
  );
});

test("the modal is a search field and a row for each changed file", () => {
  const view = modalView(TARGET, "", ["docs/guide.md", "notes.txt"], { opening: true });
  assert.equal(view.type, "modal");
  assert.equal(view.callback_id, "open_form");
  assert.deepEqual(view.title, { type: "plain_text", text: "Open a file" });
  assert.deepEqual(view.close, { type: "plain_text", text: "Close" });
  assert.deepEqual(view.submit, { type: "plain_text", text: "Open" });
  const [query, choice, ...rest] = view.blocks;
  assert.equal(rest.length, 0);
  assert.deepEqual(query, {
    type: "input",
    block_id: QUERY_BLOCK,
    dispatch_action: true,
    optional: true,
    label: { type: "plain_text", text: "Search any file" },
    element: {
      type: "plain_text_input",
      action_id: QUERY_ACTION,
      max_length: 200,
      focus_on_load: true,
      placeholder: { type: "plain_text", text: "Type part of a name" },
      dispatch_action_config: { trigger_actions_on: ["on_character_entered"] },
    },
  });
  assert.equal(at(choice, "type"), "input");
  assert.equal(at(choice, "block_id"), choiceId(view));
  assert.equal(at(choice, "label", "text"), "Changed in this session (2), newest first");
  assert.equal(at(choice, "dispatch_action") ?? false, false); // a row chosen sends nothing
  const radio = at(choice, "element");
  assert.equal(at(radio, "type"), "radio_buttons");
  assert.equal(at(radio, "action_id"), CHOICE_ACTION);
  const shown = at(radio, "options") as Array<{ value: string }>;
  assert.deepEqual(
    shown.map((row) => row.value),
    ["docs/guide.md", "notes.txt"],
  );
  assert.deepEqual(shown[0], option("docs/guide.md"));
});

test("the search field holds the words only in the opening view", () => {
  // An update keeps what was typed through the field's ids; restating a value there could
  // put an older text back (views.update reference: "Preserving input entry").
  const opening = modalView(TARGET, "setup", ["a/setup.py"], { opening: true });
  const update = modalView(TARGET, "setup", ["a/setup.py"]);
  assert.equal(at(opening.blocks[0], "element", "initial_value"), "setup");
  assert.equal(at(update.blocks[0], "element", "initial_value"), undefined);
  assert.equal(at(update.blocks[0], "element", "focus_on_load"), undefined);
  const empty = modalView(TARGET, "", [], { opening: true });
  assert.equal(at(empty.blocks[0], "element", "initial_value"), undefined);
});

test("the search field keeps its ids from one view to the next", () => {
  // Slack keeps what was typed in an input block whose ids do not change (views.update).
  const ids = (view: unknown): [unknown, unknown] => {
    const [field, ...others] = inputBlocks(view).filter((b) => at(b, "block_id") === QUERY_BLOCK);
    assert.equal(others.length, 0);
    return [at(field, "block_id"), at(field, "element", "action_id")];
  };
  const first = modalView(TARGET, "", ["a.py"], { opening: true });
  for (const later of [modalView(TARGET, "a", ["a.py", "b/a.py"]), modalView(TARGET, "zz", [])]) {
    assert.deepEqual(ids(later), ids(first));
    assert.deepEqual(ids(first), [QUERY_BLOCK, QUERY_ACTION]);
  }
});

test("the rows get an id that follows them so a selection is not kept across other rows", () => {
  // Slack keeps the state of an input block whose ids stay, a chosen row included, even when
  // the rows it chose from are gone (views.update, "Preserving input entry"): the id follows
  // the rows, as the Home tab's controls follow their choice.
  const first = modalView(TARGET, "", ["a.py", "b.py"]);
  assert.ok(choiceId(first).startsWith(`${CHOICE_BLOCK}:`));
  assert.equal(choiceId(modalView(TARGET, "a", ["a.py", "b.py"])), choiceId(first)); // same rows
  assert.notEqual(choiceId(modalView(TARGET, "", ["a.py", "c.py"])), choiceId(first));
  assert.notEqual(choiceId(modalView(TARGET, "", ["b.py", "a.py"])), choiceId(first));
  assert.ok(choiceId(first).length <= 255);
  const [group] = inputBlocks(first).filter((b) => at(b, "block_id") !== QUERY_BLOCK);
  assert.equal(at(group, "element", "action_id"), CHOICE_ACTION);
  // With no row there is no radio group (Slack wants an option in it), and the field stays.
  assert.deepEqual(
    inputBlocks(modalView(TARGET, "zz", [])).map((b) => at(b, "block_id")),
    [QUERY_BLOCK],
  );
});

test("what the rows are is said above them", () => {
  const heading = (words: string, paths: string[]) =>
    at(modalView(TARGET, words, paths).blocks[1], "label", "text");
  assert.equal(heading("", ["a.py"]), "Changed in this session (1), newest first");
  assert.equal(heading("a", ["a.py", "b/a.py", "c/a.py"]), "3 files match");
  assert.equal(heading("a", ["a.py"]), "1 file matches");
});

test("with nothing to list a line says what to do and there are no rows", () => {
  const nothing = modalView(TARGET, "", []);
  assert.deepEqual(
    nothing.blocks.map((block) => block.type),
    ["input", "context"],
  );
  assert.equal(
    at(nothing.blocks[1], "elements", 0, "text"),
    "No changed files to show. Type part of a name to find a file.",
  );
  const noneMatch = modalView(TARGET, "zz", []);
  assert.equal(at(noneMatch.blocks[1], "elements", 0, "text"), "0 files match");
});

test("while the files are listed the modal says so", () => {
  const loading = modalView(TARGET, "", null, { opening: true });
  assert.deepEqual(
    loading.blocks.map((block) => block.type),
    ["input", "context"],
  );
  assert.equal(at(loading.blocks[1], "elements", 0, "text"), "Looking for files…");
});

test("more than ten matches list ten and say how many there are", () => {
  const paths = Array.from({ length: 137 }, (_, i) => `data/part${String(i).padStart(3, "0")}.csv`);
  const [, choice, capped] = modalView(TARGET, "part", paths).blocks;
  assert.equal((at(choice, "element", "options") as unknown[]).length, 10);
  assert.equal(at(choice, "label", "text"), "137 files match");
  assert.equal(
    at(capped, "elements", 0, "text"),
    fill(texts.OPEN_MATCHES_CAPPED, { shown: 10, count: 137 }),
  );
});

test("the count past the rows is the one given and a cut listing is said", () => {
  const paths = Array.from({ length: 10 }, (_, i) => `data/part${String(i).padStart(3, "0")}.csv`);
  const view = modalView(TARGET, "part", paths, { count: 137 });
  const [, choice, capped] = view.blocks;
  assert.equal(at(choice, "label", "text"), "137 files match");
  assert.equal(
    at(capped, "elements", 0, "text"),
    fill(texts.OPEN_MATCHES_CAPPED, { shown: 10, count: 137 }),
  );
  const cut = modalView(TARGET, "part", paths.slice(0, 1), { complete: false });
  assert.deepEqual(
    cut.blocks.map((block) => block.type),
    ["input", "input", "context"],
  );
  assert.equal(at(cut.blocks[2], "elements", 0, "text"), texts.OPEN_PARTIAL);
  const nothing = modalView(TARGET, "zz", [], { complete: false });
  assert.deepEqual(
    nothing.blocks.map((block) => block.type),
    ["input", "context", "context"],
  );
  assert.equal(at(nothing.blocks[2], "elements", 0, "text"), texts.OPEN_PARTIAL);
  assert.deepEqual(modalView(TARGET, "zz", []), modalView(TARGET, "zz", [], { complete: true }));
});

test("matches none of which fits a row get the line and no group", () => {
  const view = modalView(TARGET, "e", ["e".repeat(200)]);
  assert.deepEqual(
    view.blocks.map((block) => block.type),
    ["input", "context", "context"],
  );
  assert.equal(at(view.blocks[1], "elements", 0, "text"), "1 file matches");
  assert.equal(at(view.blocks[2], "elements", 0, "text"), texts.OPEN_MATCHES_TOO_LONG);
});

test("a view stays inside what slack allows", () => {
  const paths = Array.from({ length: 20 }, (_, i) => [
    `x${i}/${"d".repeat(40)}/${"e".repeat(40)}/${"y".repeat(40)}.py`, // a folder too long
    `x${i}/${"y".repeat(80)}.py`, // a name too long
  ]).flat();
  const view = modalView(TARGET, "x", paths, { opening: true });
  assert.ok(view.blocks.length <= 100);
  assert.ok((view.private_metadata ?? "").length <= 3000);
  for (const key of ["title", "submit", "close"] as const) {
    assert.ok((view[key]?.text ?? "").length <= 24);
  }
  for (const row of at(view.blocks[1], "element", "options") as Array<{
    text: { text: string };
    value: string;
    description: { text: string };
  }>) {
    assert.ok(Array.from(row.text.text).length <= 75 && Array.from(row.value).length <= 150);
    assert.ok(Array.from(row.description.text).length <= 75);
  }
});

test("the thread of a modal comes back from its metadata", () => {
  const view = modalView(TARGET, "", []);
  assert.deepEqual(Target.load(view.private_metadata), TARGET);
});

const NOT_OURS: Array<[string, unknown]> = [
  ["None", null],
  ["5", 5],
  ["empty", ""],
  ["not json", "not json"],
  ["[]", "[]"],
  ["null", "null"],
  ['{"c": "C1"}', '{"c": "C1"}'],
  ['{"c": 1, "t": "2"}', '{"c": 1, "t": "2"}'],
  ['{"c": "C", "t": []}', '{"c": "C", "t": []}'],
];
for (const [name, text] of NOT_OURS) {
  test(`metadata that is not ours is refused [${name}]`, () => {
    assert.throws(() => Target.load(text), BadTarget);
  });
}

/** The state of a radio group as form-submit.json records it: `selected_option` is null when nothing was chosen. */
function picked(blockId: string, value: string | null): Record<string, unknown> {
  const chosen = value === null ? null : { text: { type: "plain_text", text: value }, value };
  return { [blockId]: { [CHOICE_ACTION]: { type: "radio_buttons", selected_option: chosen } } };
}

function viewWith(blocks: unknown, values: unknown): unknown {
  return { blocks, state: { values } };
}

test("the typed text is read from the views state", () => {
  const values = {
    [QUERY_BLOCK]: { [QUERY_ACTION]: { type: "plain_text_input", value: "setup" } },
  };
  assert.equal(typedIn(values), "setup");
  const untouched = {
    [QUERY_BLOCK]: { [QUERY_ACTION]: { type: "plain_text_input", value: null } },
  };
  assert.equal(typedIn(untouched), "");
});

test("the row chosen is read from the views state", () => {
  const shown = modalView(TARGET, "", ["docs/a.md", "b.md"]);
  const block = choiceId(shown);
  assert.equal(chosenIn(viewWith(shown.blocks, picked(block, "docs/a.md"))), "docs/a.md");
  assert.equal(chosenIn(viewWith(shown.blocks, picked(block, null))), null);
});

test("a row that the view no longer shows is never chosen", () => {
  // Slack kept the state of a radio group across an update; the rows have changed since.
  const older = modalView(TARGET, "", ["old.md", "other.md"]);
  const newer = modalView(TARGET, "n", ["new.md"]);
  // The selection sits under the id the older rows had: no such block is in this view.
  assert.equal(chosenIn(viewWith(newer.blocks, picked(choiceId(older), "old.md"))), null);
  // Under the id of the rows shown, but for a row that is not among them.
  assert.equal(chosenIn(viewWith(newer.blocks, picked(choiceId(newer), "old.md"))), null);
  // Under the id of the rows shown, and one of them: the control.
  assert.equal(chosenIn(viewWith(newer.blocks, picked(choiceId(newer), "new.md"))), "new.md");
});

const ODD_VIEWS: Array<[string, unknown]> = [
  ["None", null],
  ["a list", []],
  ["an empty object", {}],
  ["no blocks and no state", { blocks: null, state: null }],
  ["blocks that are not blocks", { blocks: [null, 1, { block_id: 5 }], state: { values: {} } }],
  [
    "a block whose element is a list",
    { blocks: [{ block_id: `${CHOICE_BLOCK}:x`, element: [] }], state: { values: {} } },
  ],
];
for (const [name, view] of ODD_VIEWS) {
  test(`a view of a shape slack does not send chooses nothing [${name}]`, () => {
    assert.equal(chosenIn(view), null);
    assert.equal(typedIn(null), "");
    assert.equal(typedIn([]), "");
    assert.equal(typedIn({ [QUERY_BLOCK]: null }), "");
  });
}

// --- the updates of an open modal ---

test("an update older than one already taken is refused", () => {
  const updates = new ModalUpdates();
  assert.ok(updates.claim("V1", 5.0));
  assert.ok(!updates.claim("V1", 4.0)); // arrived late
  assert.ok(!updates.claim("V1", 5.0)); // the same event, delivered twice
  assert.ok(updates.claim("V1", 6.0));
  assert.ok(!updates.current("V1", 5.0));
  assert.ok(updates.current("V1", 6.0));
});

test("each view has its own newest update", () => {
  const updates = new ModalUpdates();
  assert.ok(updates.claim("V1", 9.0) && updates.claim("V2", 1.0));
  assert.ok(updates.current("V1", 9.0) && updates.current("V2", 1.0));
});

test("the first fill of a view is older than any keystroke", () => {
  const updates = new ModalUpdates();
  assert.ok(updates.claim("V1", 1790000000.5));
  assert.ok(!updates.claim("V1", 0.0));
});

test("only a few views are tracked and the oldest go first", () => {
  const updates = new ModalUpdates(2);
  for (const viewId of ["V1", "V2", "V3"]) updates.claim(viewId, 1.0);
  assert.ok(!updates.current("V1", 1.0));
  assert.ok(updates.current("V2", 1.0));
  // One dropped to make room is not closed: a keystroke of it may still come.
  assert.ok(updates.claim("V1", 2.0));
});

test("a view that was forgotten is never tracked again", () => {
  // The modal was submitted: an update still on its way (an event delivered after the Submit)
  // must not put the view back into the maps.
  const updates = new ModalUpdates();
  updates.claim("V1", 1.0);
  const held = updates.lock("V1");
  updates.forget("V1");
  assert.ok(!updates.current("V1", 1.0));
  assert.ok(!updates.claim("V1", 5.0));
  assert.ok(!updates.current("V1", 5.0));
  assert.notEqual(updates.lock("V1"), held); // a lock of its own...
  assert.notEqual(updates.lock("V1"), updates.lock("V1")); // ...kept nowhere
});

test("only a few forgotten views are remembered", () => {
  const updates = new ModalUpdates(2);
  for (const viewId of ["V1", "V2", "V3"]) updates.forget(viewId);
  // The oldest is let go, the other two are still refused.
  assert.ok(updates.claim("V1", 1.0));
  assert.ok(!updates.claim("V2", 1.0));
  assert.ok(!updates.claim("V3", 1.0));
});

test("a view has one lock", () => {
  const updates = new ModalUpdates();
  assert.equal(updates.lock("V1"), updates.lock("V1"));
  assert.notEqual(updates.lock("V1"), updates.lock("V2"));
});
