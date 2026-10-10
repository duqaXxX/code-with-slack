import assert from "node:assert/strict";
import { test } from "node:test";
import { parseReset, parseUsage } from "../../../src/agent/claude/usage.ts";
import { type Json, sdkRecords } from "../../support/fixtures.ts";

// 2026-09-23 21:00 in Europe/Berlin (UTC+2 in September).
const NOW = Date.UTC(2026, 8, 23, 19, 0);

function recordedUsageText(): string {
  const results = sdkRecords("usage").filter((record) => record.type === "result");
  const text: Json | undefined = results.at(-1)?.result;
  assert.equal(typeof text, "string");
  assert.ok(text);
  return text as string;
}

test("parses the recorded usage", () => {
  const usage = parseUsage(recordedUsageText(), NOW);
  assert.ok(usage.session !== null && usage.session.percent >= 0 && usage.session.percent <= 100);
  assert.ok(usage.week !== null && usage.week.percent >= 0 && usage.week.percent <= 100);
  assert.ok(usage.session.resetsAt !== null && Number.isFinite(usage.session.resetsAt));
});

test("parses the measured wording", () => {
  const text = [
    "Current session: 3% used · resets Sep 24 at 1:10am (Europe/Berlin)",
    "Current week (all models): 25% used · resets Sep 27 at 6pm (Europe/Berlin)",
    "Current week (Fable): 0% used · resets Sep 27 at 6pm (Europe/Berlin)",
  ].join("\n");
  const usage = parseUsage(text, NOW);
  assert.deepEqual(usage.session, { percent: 3, resetsAt: Date.UTC(2026, 8, 23, 23, 10) });
  assert.deepEqual(usage.week, { percent: 25, resetsAt: Date.UTC(2026, 8, 27, 16, 0) });
});

test("a reset in january seen in december is next year", () => {
  const december = Date.UTC(2026, 11, 31, 22, 0);
  const usage = parseUsage("Current session: 1% used · resets Jan 1 at 12am (UTC)", december);
  assert.deepEqual(usage.session, { percent: 1, resetsAt: Date.UTC(2027, 0, 1, 0, 0) });
});

test("unknown wording fails soft", () => {
  assert.deepEqual(parseUsage("Session budget: plenty", NOW), { session: null, week: null });
  const usage = parseUsage("Current session: 9% used · resets sometime (Nowhere/Zone)", NOW);
  assert.deepEqual(usage.session, { percent: 9, resetsAt: null });
});

// What the Python tests do not pin and `Intl` has to reproduce from `zoneinfo`.

test("a zone the runtime does not know gives no reset", () => {
  assert.equal(parseReset("Sep 24 at 1:10am (Nowhere/Zone)", NOW), null);
  assert.equal(parseReset("Sep 24 at 1:10am (+02:00)", NOW), null);
});

test("a date that does not exist gives no reset", () => {
  assert.equal(parseReset("Feb 30 at 1:10am (UTC)", NOW), null);
  assert.equal(parseReset("Sep 24 at 1:99am (UTC)", NOW), null);
  assert.equal(parseReset("Foo 24 at 1:10am (UTC)", NOW), null);
  assert.equal(parseReset("Sep 24 at 1:10 (UTC)", NOW), null);
});

test("twelve am is midnight and twelve pm is noon", () => {
  assert.equal(parseReset("Sep 24 at 12am (UTC)", NOW), Date.UTC(2026, 8, 24, 0, 0));
  assert.equal(parseReset("Sep 24 at 12pm (UTC)", NOW), Date.UTC(2026, 8, 24, 12, 0));
  assert.equal(parseReset("Sep 24 at 11:59pm (UTC)", NOW), Date.UTC(2026, 8, 24, 23, 59));
});

test("a reset a little in the past stays in this year", () => {
  // The cut is one day: a limit that reset a few hours ago is not a year ahead.
  assert.equal(parseReset("Sep 23 at 6pm (UTC)", NOW), Date.UTC(2026, 8, 23, 18, 0));
  assert.equal(parseReset("Sep 21 at 6pm (UTC)", NOW), Date.UTC(2027, 8, 21, 18, 0));
});

test("the offset is the zone's on the day of the reset, not on the day of now", () => {
  // Berlin is UTC+1 in January and UTC+2 in September.
  assert.equal(
    parseReset("Jan 5 at 9am (Europe/Berlin)", Date.UTC(2026, 0, 2)),
    Date.UTC(2026, 0, 5, 8, 0),
  );
  assert.equal(
    parseReset("Jul 5 at 9am (Europe/Berlin)", Date.UTC(2026, 0, 2)),
    Date.UTC(2026, 6, 5, 7, 0),
  );
});

test("a wall time the zone repeats is its first occurrence and one it skips takes the old offset", () => {
  // zoneinfo, fold=0 (PEP 495): Berlin 2026-10-25 02:30 happens twice (the first is UTC+2), and
  // 2026-03-29 02:30 never happens (read with the offset before the jump, UTC+1).
  const winter = Date.UTC(2026, 0, 2);
  assert.equal(
    parseReset("Oct 25 at 2:30am (Europe/Berlin)", winter),
    Date.UTC(2026, 9, 25, 0, 30),
  );
  assert.equal(
    parseReset("Mar 29 at 2:30am (Europe/Berlin)", winter),
    Date.UTC(2026, 2, 29, 1, 30),
  );
});
