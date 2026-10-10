import assert from "node:assert/strict";
import { test } from "node:test";
import type { Preview, TaskStatus, TaskUpdate } from "../../../src/chat/seam.ts";
import { Fold, shownPreview } from "../../../src/core/reply/fold.ts";
import { STOPPED } from "../../../src/core/reply/words.ts";

function call(
  id: string,
  name: string,
  status: TaskStatus = "in_progress",
  fields: Partial<TaskUpdate> = {},
): TaskUpdate {
  return {
    id,
    title: `${name}: ${id}`,
    status,
    details: null,
    output: null,
    name,
    task: false,
    calls: 0,
    preview: null,
    folded: null,
    ...fields,
  };
}

function preview(title: string, summary: string, body: string, language: "" | "diff"): Preview {
  return { title, summary, body, language, plain: false };
}

function shown(cards: TaskUpdate[]): [string, string, string][] {
  return cards.map((card) => [card.id, card.status, card.title]);
}

test("a single call is one card that updates in place", () => {
  const fold = new Fold();
  assert.deepEqual(shown(fold.task(call("a", "Bash"))), [["fold:a", "in_progress", "Bash: a"]]);
  assert.deepEqual(shown(fold.task(call("a", "Bash", "complete"))), [
    ["fold:a", "complete", "Bash: a"],
  ]);
});

test("the first card turns into the counts when a second call is shown", () => {
  const fold = new Fold();
  fold.task(call("a", "Bash"));
  fold.task(call("a", "Bash", "complete"));
  assert.deepEqual(shown(fold.task(call("b", "Read"))), [
    ["fold:a", "complete", "Ran 1 shell command"],
    ["now:b", "in_progress", "Read: b"],
  ]);
});

test("a call that ended stays whole until another takes its place", () => {
  const fold = new Fold();
  for (const [id, name] of [
    ["a", "Bash"],
    ["b", "Read"],
  ] as const) {
    fold.task(call(id, name));
    fold.task(call(id, name, "complete"));
  }
  // b ended and was still shown whole: it joins the counts when c takes its place.
  assert.deepEqual(shown(fold.task(call("c", "Read"))), [
    ["fold:a", "complete", "Ran 1 shell command · Read 1 file"],
    ["now:b", "in_progress", "Read: c"],
  ]);
  // The first card comes back with the same title: only the line it folds to has changed.
  assert.deepEqual(shown(fold.task(call("c", "Read", "complete"))), [
    ["fold:a", "complete", "Ran 1 shell command · Read 1 file"],
    ["now:b", "complete", "Read: c"],
  ]);
});

test("a failed call says why while shown and is counted after the cross", () => {
  const fold = new Fold();
  fold.task(call("a", "Read"));
  fold.task(call("a", "Read", "complete"));
  fold.task(call("b", "Bash"));
  assert.deepEqual(shown(fold.task(call("b", "Bash", "error", { output: "Exit code 1" }))), [
    ["fold:a", "complete", "Read 1 file"],
    ["now:b", "error", "Bash: b · Exit code 1"],
  ]);
  assert.deepEqual(shown(fold.task(call("c", "Read"))), [
    ["fold:a", "complete", "Read 1 file · ✗ Ran 1 shell command"],
    ["now:b", "in_progress", "Read: c"],
  ]);
});

test("counts of failures alone show as an error card", () => {
  const fold = new Fold();
  fold.task(call("a", "Bash", "error", { output: "Exit code 1" }));
  const cards = fold.task(call("b", "Read"));
  assert.deepEqual(shown(cards)[0], ["fold:a", "error", "✗ Ran 1 shell command"]);
});

test("no card of a run carries details or output", () => {
  const fold = new Fold();
  const cards = fold.task(call("a", "Bash", "in_progress", { details: "doing" }));
  cards.push(...fold.task(call("a", "Bash", "error", { output: "Exit code 1" })));
  cards.push(...fold.task(call("b", "Read")));
  assert.ok(cards.every((card) => card.details === null && card.output === null));
});

test("calls running together show the last one started that still runs", () => {
  const fold = new Fold();
  fold.task(call("a", "Read"));
  assert.deepEqual(shown(fold.task(call("b", "Read"))), [["fold:a", "in_progress", "Read: b"]]);
  // b ends first: it is counted, and a, still running, is the call shown.
  assert.deepEqual(shown(fold.task(call("b", "Read", "complete"))), [
    ["fold:a", "complete", "Read 1 file"],
    ["now:a", "in_progress", "Read: a"],
  ]);
  assert.deepEqual(shown(fold.task(call("a", "Read", "complete"))), [
    ["fold:a", "complete", "Read 1 file"],
    ["now:a", "complete", "Read: a"],
  ]);
});

test("text ends the run", () => {
  const fold = new Fold();
  fold.task(call("a", "Bash", "complete"));
  fold.text();
  assert.deepEqual(shown(fold.task(call("b", "Bash"))), [["fold:b", "in_progress", "Bash: b"]]);
  // A call of the earlier run still updates that run's card.
  assert.deepEqual(fold.task(call("a", "Bash", "complete")), []);
});

test("the folded line counts every call that ended and removes the second card", () => {
  const fold = new Fold();
  fold.task(call("a", "Bash", "complete"));
  fold.task(call("b", "Read", "complete"));
  const cards = fold.task(call("c", "Bash", "error", { output: "Exit code 1" }));
  assert.deepEqual(
    cards.map((card) => [card.id, card.folded]),
    [
      ["fold:a", "✓ Ran 1 shell command · Read 1 file · ✗ Ran 1 shell command"],
      ["now:b", ""],
    ],
  );
});

test("a single call folds to its count and a running one stays a card", () => {
  const fold = new Fold();
  assert.equal(fold.task(call("a", "Bash"))[0]?.folded, null);
  assert.equal(fold.task(call("a", "Bash", "complete"))[0]?.folded, "✓ Ran 1 shell command");
});

test("a call with a card of its own passes through and ends the run", () => {
  const fold = new Fold();
  fold.task(call("a", "Bash", "complete"));
  const agent = call("t", "Agent", "in_progress", { task: true, details: "Read: x" });
  assert.deepEqual(fold.task(agent), [agent]);
  assert.deepEqual(shown(fold.task(call("b", "Bash"))), [["fold:b", "in_progress", "Bash: b"]]);
});

test("an edit that ends with a preview keeps the card that showed it", () => {
  const fold = new Fold();
  fold.task(call("e", "Edit"));
  const view = preview("Update(notes.txt)", "Added 1 line", "1 +x", "diff");
  const cards = fold.task(call("e", "Edit", "complete", { preview: view }));
  assert.deepEqual(
    cards.map((card) => [card.id, shownPreview(card), card.folded]),
    [["fold:e", view, null]],
  );
  // The run is over: the next call starts one of its own.
  assert.deepEqual(shown(fold.task(call("b", "Bash"))), [["fold:b", "in_progress", "Bash: b"]]);
});

test("an edit that arrives ended ends the run and is not counted", () => {
  // Issue #136: the renderer sends an Edit only once it has ended.
  const fold = new Fold();
  fold.task(call("a", "Bash"));
  fold.task(call("a", "Bash", "complete"));
  const view = preview("Update(notes.txt)", "Added 1 line", "1 +x", "diff");
  const done = call("e", "Edit", "complete", { preview: view });
  assert.deepEqual(fold.task(done), [done]); // as it came: nothing of the run changes
  assert.deepEqual(shown(fold.task(call("b", "Bash"))), [["fold:b", "in_progress", "Bash: b"]]);
});

test("an edit that arrives failed joins the run as a call that ended", () => {
  const fold = new Fold();
  fold.task(call("a", "Bash"));
  fold.task(call("a", "Bash", "complete"));
  const cards = fold.task(call("e", "Edit", "error", { output: "No such file" }));
  assert.deepEqual(shown(cards), [
    ["fold:a", "complete", "Ran 1 shell command"],
    ["now:e", "error", "Edit: e · No such file"],
  ]);
  assert.equal(cards[0]?.folded, "✓ Ran 1 shell command · ✗ Edit");
});

test("a shown call that becomes a task takes the second card", () => {
  const fold = new Fold();
  fold.task(call("a", "Read", "complete"));
  fold.task(call("b", "Bash"));
  const background = call("b", "Bash", "in_progress", {
    task: true,
    details: "Running in background",
  });
  const cards = fold.task(background);
  assert.deepEqual(
    cards.map((card) => [card.id, card.task, card.details]),
    [["now:b", true, "Running in background"]],
  );
  // Later states of that call keep going to the card it took.
  assert.deepEqual(
    fold.task(call("b", "Bash", "complete", { task: true })).map((card) => card.id),
    ["now:b"],
  );
});

test("a hidden call that stops folding gets a card of its own", () => {
  const fold = new Fold();
  fold.task(call("a", "Agent"));
  fold.task(call("b", "Read"));
  const cards = fold.task(call("a", "Agent", "in_progress", { task: true, calls: 1 }));
  assert.deepEqual(
    cards.map((card) => card.id),
    ["a"],
  );
});

test("a stopped call keeps its card and its word", () => {
  const fold = new Fold();
  fold.task(call("a", "Bash"));
  const cards = fold.task(call("a", "Bash", "complete", { output: STOPPED }));
  assert.deepEqual(
    cards.map((card) => [card.id, card.output, card.folded]),
    [["fold:a", STOPPED, null]],
  );
});

test("the calls left running when the first card is taken get a new one", () => {
  const fold = new Fold();
  fold.task(call("a", "Read"));
  fold.task(call("b", "Agent"));
  const cards = fold.task(call("b", "Agent", "in_progress", { task: true, calls: 1 }));
  assert.deepEqual(
    cards.map((card) => card.title),
    ["Agent: b", "Read: a"],
  );
  const [taken, fresh] = cards.map((card) => card.id);
  assert.ok(taken === "fold:a" && fresh !== taken); // a card given away is never used again
  // Each call keeps to its own card from here on.
  assert.deepEqual(
    fold.task(call("b", "Agent", "complete", { task: true })).map((c) => c.id),
    [taken],
  );
  assert.deepEqual(
    fold.task(call("a", "Read", "complete")).map((c) => c.id),
    [fresh],
  );
});

test("the call left running when the second card is taken gets a new one", () => {
  const fold = new Fold();
  fold.task(call("x", "Read", "complete"));
  fold.task(call("a", "Read"));
  fold.task(call("b", "Bash"));
  const cards = fold.task(
    call("b", "Bash", "in_progress", { task: true, details: "Running in background" }),
  );
  assert.deepEqual([cards[0]?.id, cards[0]?.title], ["now:a", "Bash: b"]);
  const last = cards.at(-1);
  assert.ok(last?.title === "Read: a" && !["now:a", "fold:x"].includes(last.id));
  assert.equal(new Set(cards.map((card) => card.id)).size, cards.length);
});

test("two agents started together never share a card", () => {
  const fold = new Fold();
  fold.task(call("a", "Agent"));
  fold.task(call("b", "Agent"));
  const first = fold.task(call("b", "Agent", "in_progress", { task: true, calls: 1 }));
  const second = fold.task(call("a", "Agent", "in_progress", { task: true, calls: 1 }));
  assert.ok(first[0]?.title === "Agent: b" && second[0]?.title === "Agent: a");
  assert.notEqual(first[0]?.id, second[0]?.id);
});

test("a counted call that stops folding leaves the line to the calls that remain", () => {
  const fold = new Fold();
  fold.task(call("a", "Agent", "complete"));
  fold.task(call("b", "Read"));
  fold.task(call("a", "Agent", "complete", { task: true, calls: 1 }));
  const cards = fold.task(call("b", "Read", "complete"));
  assert.deepEqual(
    cards.map((card) => [card.title, card.folded]),
    [
      ["Read: b", "✓ Read 1 file"],
      ["Read: b", ""],
    ],
  );
});
