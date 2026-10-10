/**
 * `!resume`: Claude Code's session picker, as a message with a Resume button per session.
 *
 * The terminal's `/resume` is interactive and the SDK does not offer it (not among the session's
 * commands, Claude Code 2.1.280), so the daemon answers the word itself, the way the terminal
 * does: `/resume` alone opens the picker, `/resume <session>` resumes by id or name (commands
 * reference, read 2026-09-25). Each row shows what a picker row shows: the session's name or
 * title, the time since its last activity, its git branch and its size (sessions reference, read
 * 2026-09-25). The rows come from the agent's session listing, newest first. Ordering that list
 * by the sessions' last message belongs to the Claude back end, which reads the transcripts.
 */
import type { ListedSession } from "../../agent/seam.ts";
import { oneLine } from "../../core/reply/words.ts";
import * as texts from "../../core/texts.ts";
import { type ContextBlock, contextBlock } from "./reply/blocks.ts";
import { take } from "./reply/chars.ts";
import { shownAsWritten } from "./reply/escape.ts";

export const RESUME_ROWS = 20;
export const RESUME_ACTION = "session_resume";
export const TITLE_LIMIT = 80;
export const ID_SHOWN = 8; // the first characters of a session id the list shows, and `!resume` takes
const SLACK_TS = /^\d+\.\d+$/;

/** One session of the picker: its columns as text, and the button that resumes it. */
export interface ResumeRow {
  type: "section";
  block_id: string;
  text: { type: "mrkdwn"; text: string };
  accessory: {
    type: "button";
    action_id: string;
    value: string;
    text: { type: "plain_text"; text: string };
  };
}

export type ResumeBlock = ContextBlock | ResumeRow;

/** The Resume button's value: the session, and the thread of the owner's `!resume` message it is
 * resumed into (the list is a top-level post, not that thread). */
export function resumeValue(sessionId: string, threadTs: string): string {
  return `${sessionId}@${threadTs}`;
}

/** The session id and thread ts a Resume button carries; null for any other shape. The value is
 * untrusted like every click's: only its form is checked here. */
export function parseResumeValue(value: string): [sessionId: string, threadTs: string] | null {
  const at = value.indexOf("@");
  if (at === -1) return null;
  const sessionId = value.slice(0, at);
  const threadTs = value.slice(at + 1);
  if (sessionId === "" || !SLACK_TS.test(threadTs)) return null;
  return [sessionId, threadTs];
}

/**
 * The sessions `!resume <target>` names: those whose id is `target` or starts with it, when it is
 * at least as long as the ID_SHOWN characters the list shows (so a short title such as "add" is
 * not read as an id), else those whose title (set with /rename or generated, the SDK's
 * `custom_title`) is `target`.
 */
export function matching(sessions: readonly ListedSession[], target: string): ListedSession[] {
  const byId = sessions.filter(
    (s) => Array.from(target).length >= ID_SHOWN && s.id.startsWith(target),
  );
  return byId.length > 0 ? byId : sessions.filter((s) => s.customTitle === target);
}

/** As the terminal's picker writes it ("2 days ago", seen 2026-09-25). */
function age(modifiedMs: number, now: Date): string {
  const seconds = Math.max(0, now.getTime() / 1000 - modifiedMs / 1000);
  for (const [unit, length] of [
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
  ] as const) {
    if (seconds >= length) {
      // Python's `seconds // length`: the quotient rounded up by the division is taken back.
      let count = Math.floor(seconds / length);
      if (count * length > seconds) count -= 1;
      return `${count} ${unit}${count > 1 ? "s" : ""} ago`;
    }
  }
  return "just now";
}

/** `bytes` over `unit`, with one decimal as Python's `f"{x:.1f}"` writes it: an exact tie goes to
 * the even tenth, where `toFixed` goes up. A size over a power of two is exact in a double. */
function tenths(bytes: number, unit: number): string {
  const scaled = (bytes / unit) * 10;
  const floor = Math.floor(scaled);
  const rest = scaled - floor;
  const rounded = rest > 0.5 || (rest === 0.5 && floor % 2 === 1) ? floor + 1 : floor;
  return `${Math.floor(rounded / 10)}.${rounded % 10}`;
}

/** As the terminal's picker writes it ("953.1KB" for 976,000 bytes: 1,024 per KB). */
function size(bytes: number | null): string | null {
  if (bytes === null) return null;
  if (bytes < 1024 * 1024) return `${tenths(bytes, 1024)}KB`;
  return `${tenths(bytes, 1024 * 1024)}MB`;
}

function row(session: ListedSession, now: Date, threadTs: string): ResumeRow {
  // Shown as the terminal's picker shows it, HEAD outside a repository included.
  const branch = session.branch ? shownAsWritten(session.branch) : null;
  const title = shownAsWritten(oneLine(session.title, TITLE_LIMIT));
  // The terminal's picker shows no id, since picking a row resumes it; here the id's start is
  // what `!resume <id>` takes. Plain text, as the rest of the row (the maintainer, 2026-09-26).
  const parts = [
    title,
    age(session.lastModified, now),
    branch,
    size(session.size),
    take(session.id, ID_SHOWN),
  ];
  return {
    type: "section",
    block_id: `session-${session.id}`,
    text: { type: "mrkdwn", text: parts.filter((part) => part).join(" · ") },
    accessory: {
      type: "button",
      action_id: RESUME_ACTION,
      value: resumeValue(session.id, threadTs),
      text: { type: "plain_text", text: texts.RESUME_BUTTON },
    },
  };
}

/**
 * The picker: the newest RESUME_ROWS of `sessions`, the ones of `directory` that can be resumed.
 * `openElsewhere` counts the sessions a thread already holds (D6: one session lives in one
 * thread): they are left out of the rows, since resuming one is refused anyway, and named by one
 * line under the list, so the limit applies to what the owner can act on. Each Resume button
 * carries `threadTs`, the thread of the owner's `!resume` message, where the session is resumed.
 */
export function resumeBlocks(
  directory: string,
  sessions: readonly ListedSession[],
  openElsewhere: number,
  now: Date,
  threadTs: string,
): ResumeBlock[] {
  // The list's own lines are the daemon's notices, small and grey; the rows keep their button.
  const shown = shownAsWritten(directory);
  let held: ContextBlock[] = [];
  if (openElsewhere === 1) held = [contextBlock(texts.RESUME_OPEN_ONE)];
  else if (openElsewhere)
    held = [contextBlock(texts.fill(texts.RESUME_OPEN_MANY, { count: openElsewhere }))];
  if (sessions.length === 0) {
    const empty = held.length > 0 ? texts.RESUME_NONE_LEFT : texts.RESUME_EMPTY;
    return [contextBlock(texts.fill(empty, { directory: shown })), ...held];
  }
  const blocks: ResumeBlock[] = [
    contextBlock(texts.fill(texts.RESUME_LIST, { directory: shown })),
    ...sessions.slice(0, RESUME_ROWS).map((s) => row(s, now, threadTs)),
  ];
  if (sessions.length > RESUME_ROWS) {
    blocks.push(contextBlock(texts.fill(texts.RESUME_MORE, { rows: RESUME_ROWS })));
  }
  return [...blocks, ...held];
}
