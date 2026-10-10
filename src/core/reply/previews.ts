/**
 * The few tools the terminal shows in words of its own, and nothing else.
 *
 * Rendering stays generic: a tool not named here shows as its name, counted when it ran more
 * than once, and its line, with no change anywhere. For the tools below, the terminal writes a
 * sentence or a preview instead, and this module reproduces it from what the agent's events
 * carry. What an `Edit` or a `Write` did to a file arrives as the seam's `FileChange`, which the
 * Claude back end reads from an undocumented shape and gives as null for any other: the generic
 * line is shown then. An answered question is the exception: its lines come from the questions
 * and answers the daemon itself sent back (`answered`), not from a result.
 */
import type { DiffHunk, FileChange, Question } from "../../agent/seam.ts";
import type { Preview } from "../../chat/seam.ts";
import { ANSWERED, fill } from "../texts.ts";
import { spaced, splitLines } from "./words.ts";

// How the terminal folds finished calls of these tools. Captured from the terminal on Claude Code
// 2.1.283 (2026-09-27), where Bash also does the searching (the CLI has no Grep or Glob tool):
// `echo hi` read `Ran 1 shell command`, `ls` `Listed 1 directory`, a grep `Searched for 1
// pattern`. That classification of the command is undocumented, so every Bash call reads here as
// a shell command.
export const WORDS: Readonly<Record<string, readonly [one: string, many: string]>> = {
  Bash: ["Ran {n} shell command", "Ran {n} shell commands"],
  Read: ["Read {n} file", "Read {n} files"],
};
// A diff line's colour where Slack draws none (mobile); an emoji is two columns wide, so a
// context line gets two spaces and the numbers stay aligned.
export const MARKS: Readonly<Record<string, string>> = {
  "-": "\u{1f7e5}",
  "+": "\u{1f7e9}",
  " ": "  ",
};
// The terminal shows a new file's first lines, then how many it leaves out.
export const NEW_FILE_LINES = 10;
// The tools `preview` reads. A reply shows a call of one only once it has ended: a stream cannot
// take back a card, and a call that ended well is its preview alone.
export const PREVIEWED: ReadonlySet<string> = new Set(["Edit", "Write"]);

/** How `n` finished calls of `name` read in a folded line. */
export function folded(name: string, n: number): string {
  const words = Object.hasOwn(WORDS, name) ? WORDS[name] : undefined;
  if (words === undefined) {
    return n === 1 ? name : `${name} ×${n}`;
  }
  return fill(n === 1 ? words[0] : words[1], { n });
}

function counted(n: number, noun = "line"): string {
  return `${n} ${noun}${n === 1 ? "" : "s"}`;
}

/** The parts of a POSIX path as Python's `PurePosixPath` keeps them: no empty part, no `.`. */
function parts(path: string): string[] {
  return path.split("/").filter((part) => part !== "" && part !== ".");
}

/** The path as the terminal names it: relative to the session's folder when inside it. */
function shown(path: string, cwd: string | null): string {
  if (!cwd) return path;
  const base = parts(cwd);
  const inner = parts(path);
  const inside =
    path.startsWith("/") === cwd.startsWith("/") &&
    base.length <= inner.length &&
    base.every((part, index) => inner[index] === part);
  if (!inside) return path;
  return inner.slice(base.length).join("/") || ".";
}

/**
 * Lines added and removed, and the hunks numbered as the terminal numbers them: a removed line
 * by its old number, any other by its new one, `...` between hunks. The sign leads the line,
 * where Slack desktop's diff highlighting looks for it; a coloured square follows, since Slack
 * mobile does not highlight (both measured 2026-09-27): `-🟥 7 sette`, `+🟩 7 7`.
 */
function diff(
  hunks: readonly DiffHunk[],
): { added: number; removed: number; body: string[] } | null {
  let added = 0;
  let removed = 0;
  const rows: [number: number, text: string][] = [];
  for (const [index, hunk] of hunks.entries()) {
    let { oldStart: old, newStart: fresh } = hunk;
    if (index > 0) rows.push([0, "..."]);
    for (const line of hunk.lines) {
      const sign = line.charAt(0);
      const text = line.slice(1);
      if (sign === "-") {
        rows.push([old, `-${text}`]);
        old += 1;
        removed += 1;
      } else if (sign === "+") {
        rows.push([fresh, `+${text}`]);
        fresh += 1;
        added += 1;
      } else if (sign === " ") {
        rows.push([fresh, ` ${text}`]);
        old += 1;
        fresh += 1;
      } else if (sign !== "\\") {
        // `\ No newline at end of file` is about the line above: skipped
        return null;
      }
    }
  }
  const width = String(Math.max(0, ...rows.map(([n]) => n))).length;
  const body = rows.map(([n, text]) => {
    const sign = text.charAt(0);
    return n === 0 ? text : `${sign}${MARKS[sign]} ${String(n).padStart(width)} ${text.slice(1)}`;
  });
  return { added, removed, body };
}

function changed(added: number, removed: number): string {
  const said = added > 0 ? [`Added ${counted(added)}`] : [];
  if (removed > 0) {
    said.push(said.length > 0 ? `removed ${counted(removed)}` : `Removed ${counted(removed)}`);
  }
  return said.join(", ") || "No change";
}

/**
 * The answers to a question as the terminal keeps them: a line per question that has one,
 * `· question → answer`, a multi-select's answers joined by commas. Null with no answer.
 */
export function answered(
  questions: readonly Pick<Question, "text">[],
  answers: Readonly<Record<string, string | readonly string[]>>,
): Preview | null {
  const lines: string[] = [];
  for (const question of questions) {
    const answer = Object.hasOwn(answers, question.text) ? (answers[question.text] ?? "") : "";
    const said = typeof answer === "string" ? answer : answer.join(", ");
    if (said !== "") {
      // One line each, however the question or a typed answer was written.
      lines.push(`· ${spaced(question.text)} → ${spaced(said)}`);
    }
  }
  if (lines.length === 0) return null;
  return { title: ANSWERED, summary: "", body: lines.join("\n"), language: "", plain: true };
}

/**
 * The terminal's view of a finished `Edit` or `Write` from what it did to a file, or null for
 * any other tool and for a call that reports no change. `cwd` is the session's folder.
 */
export function preview(
  name: string,
  change: FileChange | null,
  cwd: string | null,
): Preview | null {
  if (change === null) return null;
  const path = shown(change.path, cwd);
  if (name === "Write" && change.kind === "created") {
    if (change.content === null) return null;
    const lines = splitLines(change.content);
    const width = String(Math.min(lines.length, NEW_FILE_LINES)).length;
    const body = lines
      .slice(0, NEW_FILE_LINES)
      .map((line, index) => `${String(index + 1).padStart(width)} ${line}`);
    if (lines.length > NEW_FILE_LINES) body.push(`… +${counted(lines.length - NEW_FILE_LINES)}`);
    return {
      title: `Write(${path})`,
      summary: `Wrote ${counted(lines.length)} to ${path}`,
      body: body.join("\n"),
      language: "",
      plain: false,
    };
  }
  if (PREVIEWED.has(name) && change.hunks.length > 0) {
    const numbered = diff(change.hunks);
    if (numbered === null) return null;
    // The terminal names an edit `Update`, and a Write over an existing file keeps `Write`.
    return {
      title: `${name === "Edit" ? "Update" : "Write"}(${path})`,
      summary: changed(numbered.added, numbered.removed),
      body: numbered.body.join("\n"),
      language: "diff",
      plain: false,
    };
  }
  return null;
}
