import assert from "node:assert/strict";
import { test } from "node:test";
import type { FooterFields } from "../../../src/chat/seam.ts";
import { formatFooter } from "../../../src/chat/slack/footer.ts";
import { formatStatusFields } from "../../../src/core/footer.ts";

// 2026-09-23 21:00 in Europe/Berlin (UTC+2 in September).
const NOW = Date.UTC(2026, 8, 23, 19, 0);
const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** A footer that knows nothing; a test sets what it is about. */
function fields(known: Partial<FooterFields> = {}): FooterFields {
  return {
    bypass: false,
    model: null,
    effort: null,
    folder: null,
    branch: null,
    changes: null,
    sessionTokens: null,
    contextPercent: null,
    sessionLimit: null,
    weekLimit: null,
    ...known,
  };
}

test("full footer", () => {
  // The owner's order: bypass, model, effort, folder, branch, changes, tokens, context, limits.
  const data = fields({
    bypass: true,
    branch: "main",
    model: "claude-opus-5-5",
    contextPercent: 6.4,
    sessionTokens: 12_345,
    sessionLimit: { percent: 3, resetsAt: NOW + 2 * HOUR + 10 * MINUTE },
    weekLimit: { percent: 25, resetsAt: null },
  });
  assert.equal(
    formatFooter(data, NOW),
    "⚡ bypass · claude-opus-5-5 · main · 12.3k *tok* · *ctx* 6% · *5h* 3% ↻ 2h · *7d* 25%",
  );
});

test("minimal footer hides what it does not know", () => {
  assert.equal(formatFooter(fields(), NOW), "");
});

test("the weekly limit shows its reset in the footer and the status", () => {
  const data = fields({
    weekLimit: { percent: 45, resetsAt: NOW + 3 * DAY + 4 * HOUR + 12 * MINUTE },
  });
  assert.equal(formatFooter(data, NOW), "*7d* 45% ↻ 3d 4h");
  assert.deepEqual(formatStatusFields(data, NOW), ["7d limit: `45% ↻ 3d 4h`"]);
});

test("the changes follow the branch in the footer and the status", () => {
  const data = fields({ branch: "main", model: "claude-opus-5-5", changes: [42, 10] });
  assert.equal(formatFooter(data, NOW), "claude-opus-5-5 · main · (+42,-10)");
  assert.deepEqual(formatStatusFields(data, NOW), [
    "Model: `claude-opus-5-5`",
    "Branch: `main`",
    "Uncommitted: `(+42,-10)`",
  ]);
});

test("status fields hold the values the footer shows", () => {
  const data = fields({
    branch: "main",
    model: "claude-opus-5-5",
    contextPercent: 6.4,
    sessionTokens: 12_345,
    sessionLimit: { percent: 3, resetsAt: NOW + 2 * HOUR + 10 * MINUTE },
    weekLimit: { percent: 25, resetsAt: null },
    effort: "high",
  });
  const shown = formatFooter(data, NOW);
  const lines = formatStatusFields(data, NOW);
  assert.ok(lines.length > 0);
  for (const line of lines) {
    assert.ok(shown.includes(line.split("`")[1] as string));
  }
});

test("the footer shows the effort after the model", () => {
  const data = fields({ branch: "main", model: "claude-opus-5-5", effort: "high" });
  assert.equal(formatFooter(data, NOW), "claude-opus-5-5 · *effort* high · main");
});

for (const [directory, shown] of [
  ["/srv/alice/code/app", "app"],
  ["/app", "app"],
  ["/", null],
  ["code/app", "app"],
] as const) {
  test(`the folder s name comes before the branch [${directory}]`, () => {
    // The project's name alone, not its path (the maintainer, 2026-09-27).
    const data = fields({ branch: "main", folder: directory });
    assert.equal(
      formatFooter(data, NOW),
      [shown, "main"].filter((part) => part !== null).join(" · "),
    );
  });
}

test("the folder and the branch are shown as written", () => {
  const data = fields({ branch: "fix/<a>&b", folder: "/srv/alice/R&D <x>" });
  assert.equal(formatFooter(data, NOW), "R&amp;D &lt;x&gt; · fix/&lt;a&gt;&amp;b");
});
