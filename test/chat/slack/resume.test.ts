/**
 * Port of the part of `tests/test_resume.py` whose subject is the picker's blocks and the
 * Resume button's value. Left to the Claude back end (they read Claude Code's transcript
 * files): `a_session_s_time_is_its_last_message_not_its_file`,
 * `dating_stops_once_the_rest_cannot_enter_the_list` and
 * `dates_falling_back_to_file_times_are_logged`.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import type { ListedSession } from "../../../src/agent/seam.ts";
import {
  matching,
  parseResumeValue,
  RESUME_ROWS,
  resumeBlocks,
  resumeValue,
} from "../../../src/chat/slack/resume.ts";
import * as texts from "../../../src/core/texts.ts";

// A Slack block read in a test, whatever its type.
// biome-ignore lint/suspicious/noExplicitAny: blocks are read by path
type Json = Record<string, any>;

const NOW = new Date(2026, 8, 25, 12, 0);
const THREAD = "1780000000.000001"; // the thread of the owner's `!resume` message
const DIRECTORY = "/srv/dev/app";

/** Session metadata as the seam lists it. */
function info(
  id: string,
  title: string,
  hoursAgo: number,
  fields: Partial<ListedSession> = {},
): ListedSession {
  return {
    id,
    title,
    customTitle: null,
    branch: null,
    size: null,
    lastModified: NOW.getTime() - Math.round(hoursAgo * 3_600_000),
    ...fields,
  };
}

function blocksOf(...args: Parameters<typeof resumeBlocks>): Json[] {
  return resumeBlocks(...args) as unknown as Json[];
}

function rows(blocks: Json[]): Json[] {
  return blocks.filter((b) => String(b.block_id ?? "").startsWith("session-"));
}

test("each session is a row with the picker s columns and a button", () => {
  const sessions = [
    info("68da9311-0000-4000-8000-000000000001", "Fix footer effort", 2, {
      branch: "main",
      size: 412_000,
    }),
    info("68da9311-0000-4000-8000-000000000002", "Add trust gate", 26, {
      branch: "security-fixes",
      size: 1_100_000,
    }),
  ];
  const blocks = blocksOf(DIRECTORY, sessions, 0, NOW, THREAD);
  assert.ok(blocks[0]?.elements[0].text.includes("/srv/dev/app"));
  const [first, second] = rows(blocks) as [Json, Json];
  // The id's first characters close the row, in plain text like the rest of it.
  assert.equal(first.text.text, "Fix footer effort · 2 hours ago · main · 402.3KB · 68da9311");
  assert.equal(second.text.text, "Add trust gate · 1 day ago · security-fixes · 1.0MB · 68da9311");
  const button = first.accessory;
  assert.equal(button.action_id, "session_resume");
  // The list is a top-level post: the button names the thread the session is resumed into.
  assert.equal(button.value, `68da9311-0000-4000-8000-000000000001@${THREAD}`);
  assert.equal(button.text.text, texts.RESUME_BUTTON);
});

test("sessions open in a thread are counted under the list", () => {
  const sessions = [info("68da9311-0000-4000-8000-000000000001", "Now", 0.1)];
  const blocks = blocksOf(DIRECTORY, sessions, 3, NOW, THREAD);
  assert.equal(rows(blocks).length, 1); // the rows are the sessions that can be resumed
  assert.equal(blocks.at(-1)?.elements[0].text, texts.fill(texts.RESUME_OPEN_MANY, { count: 3 }));
  const one = blocksOf(DIRECTORY, sessions, 1, NOW, THREAD);
  assert.equal(one.at(-1)?.elements[0].text, texts.RESUME_OPEN_ONE);
});

test("a folder whose sessions are all open says there is none to resume", () => {
  const [first, second] = blocksOf(DIRECTORY, [], 2, NOW, THREAD) as [Json, Json];
  assert.equal(
    first.elements[0].text,
    texts.fill(texts.RESUME_NONE_LEFT, { directory: "/srv/dev/app" }),
  );
  assert.equal(second.elements[0].text, texts.fill(texts.RESUME_OPEN_MANY, { count: 2 }));
});

test("a free row carries no link", () => {
  const sessions = [info("68da9311-0000-4000-8000-000000000001", "Now", 0.1)];
  const found = rows(blocksOf(DIRECTORY, sessions, 0, NOW, THREAD));
  assert.equal(found.length, 1);
  const [row] = found as [Json];
  assert.ok("accessory" in row);
  assert.equal(row.text.text, "Now · 6 minutes ago · 68da9311");
});

test("the branch reads as the terminal shows it", () => {
  // The terminal's picker shows HEAD for a folder outside git, and the list does too.
  const sessions = [
    info("68da9311-0000-4000-8000-000000000001", "Notes", 50, { branch: "HEAD", size: 976_000 }),
  ];
  const [row] = rows(blocksOf("/srv/dev/notes", sessions, 0, NOW, THREAD)) as [Json];
  assert.equal(row.text.text, "Notes · 2 days ago · HEAD · 953.1KB · 68da9311");
});

test("only the newest sessions are listed", () => {
  const sessions = Array.from({ length: 25 }, (_, i) =>
    info(`68da9311-0000-4000-8000-${String(i).padStart(12, "0")}`, `s${i}`, i),
  );
  const blocks = blocksOf(DIRECTORY, sessions, 0, NOW, THREAD);
  const listed = rows(blocks);
  assert.equal(listed.length, RESUME_ROWS);
  assert.equal(RESUME_ROWS, 20); // the maintainer, 2026-09-25: ten were too few
  const first = parseResumeValue(listed[0]?.accessory.value);
  assert.ok(first?.[0].endsWith("000000000000"));
  assert.equal(
    blocks.at(-1)?.elements[0].text,
    texts.fill(texts.RESUME_MORE, { rows: RESUME_ROWS }),
  );
  // An untitled session has no title to type: `!resume <id>` reaches it when its id is known.
  assert.ok(texts.RESUME_MORE.includes("!resume <id>") && texts.RESUME_MORE.includes("<title>"));
});

test("no more line when every session fits", () => {
  const sessions = Array.from({ length: RESUME_ROWS }, (_, i) =>
    info(`68da9311-0000-4000-8000-${String(i).padStart(12, "0")}`, `s${i}`, i),
  );
  const blocks = blocksOf(DIRECTORY, sessions, 0, NOW, THREAD);
  assert.equal(rows(blocks).length, RESUME_ROWS);
  assert.ok("accessory" in (blocks.at(-1) as Json));
});

test("a title is shown as written", () => {
  const sessions = [info("68da9311-0000-4000-8000-000000000001", "see <http://x|ok> ```", 1)];
  const [row] = rows(blocksOf(DIRECTORY, sessions, 0, NOW, THREAD)) as [Json];
  assert.ok(row.text.text.includes("&lt;http://x|ok&gt;") && !row.text.text.includes("```"));
});

test("no session yet says so", () => {
  const blocks = blocksOf(DIRECTORY, [], 0, NOW, THREAD);
  assert.equal(
    blocks[0]?.elements[0].text,
    texts.fill(texts.RESUME_EMPTY, { directory: "/srv/dev/app" }),
  );
  assert.deepEqual(rows(blocks), []);
});

test("a session is found by id or by its name", () => {
  const named = info("68da9311-0000-4000-8000-000000000001", "trust", 1, { customTitle: "trust" });
  const other = info("68da9311-0000-4000-8000-000000000002", "Other", 2);
  const twin = info("68da9311-0000-4000-8000-000000000003", "twin", 3, { customTitle: "twin" });
  const twin2 = info("68da9311-0000-4000-8000-000000000004", "twin", 4, { customTitle: "twin" });
  const sessions = [named, other, twin, twin2];
  assert.deepEqual(matching(sessions, other.id), [other]);
  assert.deepEqual(matching(sessions, "trust"), [named]);
  assert.deepEqual(matching(sessions, "Other"), []); // a summary with no title (a first prompt) is no name
  assert.deepEqual(matching(sessions, "twin"), [twin, twin2]);
});

test("a session is found by the start of its id the list shows", () => {
  const first = info("1b4b42a0-0000-4000-8000-000000000001", "first", 1);
  const second = info("1b4b42a1-0000-4000-8000-000000000002", "second", 2);
  const added = info("add00000-0000-4000-8000-000000000003", "add", 3, { customTitle: "add" });
  const sessions = [first, second, added];
  assert.deepEqual(matching(sessions, "1b4b42a0"), [first]);
  assert.deepEqual(matching(sessions, "1b4b42a"), []); // shorter than the list shows: not read as an id
  assert.deepEqual(matching(sessions, "add"), [added]); // a short title that is also hex stays a title
  assert.deepEqual(matching(sessions, "1b4b42a0-0000"), [first]);
});

test("the branch and the folder are shown as written", () => {
  // git accepts `<`, `>` and `&` in a branch name; unescaped, `<!here>` would notify the channel.
  const sessions = [
    info("68da9311-0000-4000-8000-000000000001", "t", 1, { branch: "fix/<!here>" }),
  ];
  const blocks = blocksOf("/srv/R&D", sessions, 0, NOW, THREAD);
  assert.ok(blocks[0]?.elements[0].text.includes("R&amp;D"));
  assert.ok(rows(blocks)[0]?.text.text.includes("fix/&lt;!here&gt;"));
});

test("a resume value round trips", () => {
  const sid = "68da9311-0000-4000-8000-000000000001";
  assert.deepEqual(parseResumeValue(resumeValue(sid, THREAD)), [sid, THREAD]);
});

for (const [name, value] of [
  ["", ""],
  ["68da9311", "68da9311"],
  ["68da9311@", "68da9311@"],
  [`@${THREAD}`, `@${THREAD}`],
  ["68da9311@thread", "68da9311@thread"],
  ["68da9311@1780000000", "68da9311@1780000000"],
  ["68da9311@1.2.3", "68da9311@1.2.3"],
] as const) {
  test(`a resume value of another shape is refused [${name}]`, () => {
    assert.equal(parseResumeValue(value), null);
  });
}

// Not in the Python file: the sizes and ages the picker writes, where Python's rounding and
// floor division could differ from JavaScript's.
test("a size is written as the terminal writes it, a tie rounding to the even tenth", () => {
  const size = (bytes: number): string => {
    const [row] = rows(
      blocksOf(
        DIRECTORY,
        [info("68da9311-0000-4000-8000-000000000001", "t", 1, { size: bytes })],
        0,
        NOW,
        THREAD,
      ),
    ) as [Json];
    return row.text.text.split(" · ")[2];
  };
  assert.equal(size(1280), "1.2KB"); // 1.25 exactly: Python's format rounds half to even
  assert.equal(size(1792), "1.8KB"); // 1.75 exactly
  assert.equal(size(0), "0.0KB");
  assert.equal(size(1024 * 1024 - 1), "1024.0KB");
  assert.equal(size(1024 * 1024 * 3), "3.0MB");
  assert.equal(size(Math.round(1024 * 1024 * 2.25)), "2.2MB");
});

test("an age is the largest whole unit, and a session from the future is just now", () => {
  const age = (seconds: number): string => {
    const session = info("68da9311-0000-4000-8000-000000000001", "t", 0, {
      lastModified: NOW.getTime() - seconds * 1000,
    });
    const [row] = rows(blocksOf(DIRECTORY, [session], 0, NOW, THREAD)) as [Json];
    return row.text.text.split(" · ")[1];
  };
  assert.equal(age(59), "just now");
  assert.equal(age(60), "1 minute ago");
  assert.equal(age(3599), "59 minutes ago");
  assert.equal(age(3600), "1 hour ago");
  assert.equal(age(86400 * 2 + 5), "2 days ago");
  assert.equal(age(-30), "just now");
});
