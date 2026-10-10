import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { getLogger, setLevel, setWriter, timestamp } from "../src/log.ts";

let restore: ((line: string) => void) | null = null;

function captured(): string[] {
  const lines: string[] = [];
  restore = setWriter((line) => lines.push(line));
  return lines;
}

afterEach(() => {
  if (restore !== null) setWriter(restore);
  restore = null;
  setLevel("INFO");
});

test("a line reads time level name and message", () => {
  const lines = captured();
  getLogger("awaydesk.core.state").warning("could not write");
  assert.equal(lines.length, 1);
  assert.match(
    lines[0] as string,
    /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} WARNING awaydesk\.core\.state: could not write$/,
  );
});

test("a debug line is left out until the level asks for it", () => {
  const lines = captured();
  const logger = getLogger("awaydesk.agent");
  logger.debug("claude stderr: 12 chars");
  assert.deepEqual(lines, []);
  setLevel("DEBUG");
  logger.debug("claude stderr: 12 chars");
  assert.equal(lines.length, 1);
});

test("the time is written as the python daemon wrote it", () => {
  assert.equal(timestamp(new Date(2026, 9, 10, 9, 5, 3, 42)), "2026-10-10 09:05:03,042");
});
