/** The waits, the limits and the words of a session: Python's module constants, by their names. */
import type { SessionEventType } from "../../agent/seam.ts";
import type { SessionStatus } from "../../chat/seam.ts";

// The events that belong to a turn: the first one that arrives with no turn running starts one
// (Python's `TURN_MESSAGES`: a stream event, an assistant or a user message, a result).
export const TURN_EVENTS: ReadonlySet<SessionEventType> = new Set([
  "message_started",
  "message_ended",
  "text_started",
  "text_delta",
  "text",
  "call_started",
  "call_ended",
  "agent_error",
  "turn_ended",
]);
export const TASK_EVENTS: ReadonlySet<SessionEventType> = new Set([
  "task_started",
  "task_progress",
  "task_ended",
  "task_updated",
]);
// After a background task's notification, the CLI starts a turn of its own to report it
// (measured on Claude Code 2.1.280). If that turn never comes, the owner's queue moves on.
export const INJECTED_TURN_WAIT = 30.0;
// How long the answer to `!stop` waits for the reply it cut short to end. An interrupted turn
// ends within a second or two; a final write that is retried takes ten.
export const STOP_TAIL_WAIT = 15.0;
// Each task type (measured on Claude Code 2.1.280: `local_bash` for a background command,
// `local_agent` for a subagent) as the footer counts it and as its end line names it, the way
// the terminal prints `Agent "..." finished`. A type not listed counts and reads as a task, so a
// new kind shows with no change.
export const TASK_KINDS: Readonly<Record<string, readonly [counted: string, named: string]>> = {
  local_bash: ["shell", "Background command"],
  local_agent: ["agent", "Agent"],
};
export const UNKNOWN_KIND: readonly [counted: string, named: string] = ["task", "Task"];
// Tasks whose type and description the session keeps. A task can end with a terminal
// `task_updated` and no notification (SDK docstring), so past this many the oldest are dropped.
export const TASKS_KEPT = 200;
// The task types whose notification summary is already the terminal's end line (measured: a
// command's reads `Background command "..." completed (exit code 0)`; an agent's is its result).
export const SUMMARY_IS_END_LINE: ReadonlySet<string> = new Set(["local_bash"]);
// Tasks whose reply the session remembers. An ended task stays a while, since an agent can
// report again after its notification; past this many, the oldest ended ones are forgotten.
export const TASK_REPLIES_KEPT = 100;
// How often a stopping daemon checks whether every channel's turns have ended.
export const DRAIN_POLL_SECONDS = 0.5;
// A session's agent process closes after this long with nothing to do (idle, and no approval,
// question or background report pending); the next message rebuilds it and resumes.
export const IDLE_CLOSE_SECONDS = 3600.0;
// The one hold a thread can have open at once, in the session's waiting set alongside real
// approval ids (`waitingForOwner` and the idle-close timer treat every entry the same).
export const HOLD_MARKER = "d8-hold";
// How much of the owner's question a note about a message that was not sent quotes.
export const ASKED_LIMIT = 100;
// How much of each message a note about messages that were not sent quotes.
export const NOT_SENT_START = 60;
// What the reader says ended the stream when the back end gave no reason of its own.
export const PROCESS_EXITED = "the Claude Code process exited";
// The root's status as `state.json` has held it since the Python daemon: the name of the
// reaction that showed it. The session index and the crash repair read these words.
export const STORED_STATUS: Readonly<Record<SessionStatus, string>> = {
  working: "hourglass_flowing_sand",
  waiting: "raised_hand",
  done: "white_check_mark",
  error: "x",
};
