/**
 * The commands Claude Code offers a session started through the SDK, on the release this run is
 * on, against what the repository recorded and what `docs/limits.md` says.
 *
 * Two questions a new release raises about commands:
 *
 * - which commands are new, or gone: a new one is typed in Slack as `!name` from the day the SDK is
 *   pinned, so somebody has to see what it does there; `changes` compares the list a session is
 *   offered with `tests/fixtures/sdk/server-info.json`, the list recorded when the fixtures were;
 * - whether a command the limits page calls not offered still is: `notOffered` reads the page's
 *   table, and the scene `commands not offered` sends each one (claim P23).
 *
 * The list is asked of a session with no settings loaded, as the fixture was recorded: the owner's
 * own skills and plugins add commands of theirs, which are no business of a release. Asking starts
 * Claude Code and sends no prompt, so it spends no tokens.
 */

import { readFileSync } from "node:fs";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ClaudeBackend } from "../src/agent/claude/backend.ts";
import type { PermissionAnswer, QuestionAnswer, RequestHandler } from "../src/agent/seam.ts";

const ROOT = join(import.meta.dirname, "..");
const LIMITS = join(ROOT, "docs", "limits.md");
const RECORDED = join(ROOT, "tests", "fixtures", "sdk", "server-info.json");
const RECORDED_SHOWN = "tests/fixtures/sdk/server-info.json";
const SDK_SECTION = "Limits of the Claude Agent SDK";
// Claude Code's whole answer to a command it does not offer a session (measured 2026-10-09,
// Claude Code 2.1.292, on 22 commands).
export const UNAVAILABLE = "isn't available in this environment";

export interface Changes {
  /** Offered now, absent from the recording. */
  readonly new: readonly string[];
  /** Recorded, no longer offered. */
  readonly gone: readonly string[];
}

function basename(path: string): string {
  return path.split("/").at(-1) ?? path;
}

/**
 * The commands `docs/limits.md` names in the table of the SDK's limits, without the slash.
 * Throws when the section or its table is missing: a page that lists nothing would make the
 * claim hold on nothing.
 */
export function notOffered(path: string = LIMITS): string[] {
  const text = readFileSync(path, "utf8");
  const heading = `## ${SDK_SECTION}\n`;
  if (!text.includes(heading))
    throw new RangeError(`${basename(path)}: no section '${SDK_SECTION}'`);
  const section = (text.split(heading)[1] ?? "").split("\n## ")[0] ?? "";
  const rows = section
    .split(/\r?\n/)
    .filter((line) => line.startsWith("|"))
    .slice(2);
  const names = rows.flatMap((row) => {
    const cell = row.replace(/^\|+|\|+$/g, "").split("|")[1] ?? "";
    return [...cell.matchAll(/`\/([a-z-]+)`/g)].map((m) => m[1] ?? "");
  });
  if (names.length === 0) {
    throw new RangeError(`${basename(path)}: the table under '${SDK_SECTION}' names no command`);
  }
  return names;
}

/** The names of the commands the fixture recorded. */
export function recorded(path: string = RECORDED): Set<string> {
  const record: unknown = JSON.parse(readFileSync(path, "utf8"));
  const commands = (record as { commands?: { name?: unknown }[] }).commands ?? [];
  return new Set(commands.map((command) => String(command.name)));
}

export function changes(offered: ReadonlySet<string>, known: ReadonlySet<string>): Changes {
  return {
    new: [...offered].filter((name) => !known.has(name)).sort(),
    gone: [...known].filter((name) => !offered.has(name)).sort(),
  };
}

// Asking a command list starts a session and sends no prompt: nothing it could ask is answered.
const NO_ANSWERS: RequestHandler = {
  permission: async (): Promise<PermissionAnswer> => ({ allow: false, message: "not asked" }),
  question: async (): Promise<QuestionAnswer> => ({ answered: false, message: "not asked" }),
};

/**
 * The commands a session with no settings is offered on the installed release, from the
 * back end's own `info()`: the daemon's `!help` and its passthrough read them there.
 */
export async function offeredNow(): Promise<Set<string>> {
  const root = await mkdtemp(join(tmpdir(), "awaydesk-probe-commands-"));
  try {
    // The owner's own `~/.claude.json` is not read: Chrome stays off for this session.
    const home = join(root, "home");
    const folder = join(root, "folder");
    await mkdir(home);
    await mkdir(folder);
    await writeFile(join(home, ".claude.json"), "{}");
    const session = await new ClaudeBackend({ home }).start(
      {
        folder,
        resume: null,
        settingsSources: [],
        model: null,
        effort: null,
        permissionMode: null,
      },
      NO_ANSWERS,
    );
    try {
      return new Set((await session.info()).commands.map((command) => command.name));
    } finally {
      await session.close();
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

/** What changed in the list of commands, and what each change asks for. */
export function report(found: Changes, cli: string): string {
  if (found.new.length === 0 && found.gone.length === 0) {
    return `Commands: the same as recorded (Claude Code ${cli}).`;
  }
  const lines = [`Commands: the list differs from the recording (Claude Code ${cli}).`];
  if (found.new.length > 0) {
    lines.push(
      "",
      "  New, typed in Slack as `!name` once this release is pinned. Try each in a thread",
      "  and decide what Slack shows for it:",
      ...found.new.map((name) => `  [ ] /${name}`),
    );
  }
  if (found.gone.length > 0) {
    lines.push(
      "",
      "  Gone. Look for each in the docs and in `docs/limits.md`:",
      ...found.gone.map((name) => `  [ ] /${name}`),
    );
  }
  lines.push("", `  Then record ${RECORDED_SHOWN} again.`);
  return lines.join("\n");
}
