/**
 * The inputs of the two hooks the daemon registers, into session events.
 *
 * The Stop hook's input carries the effort level Claude Code runs at: the footer's only source
 * for it, since no message reports it. Every hook input carries the `cwd` the session works in;
 * PostToolUse reports it after each tool, so a turn stopped or failed before its Stop still
 * moves the footer's branch.
 */
import type { SessionEvent } from "../seam.ts";
import { isRecord, string, words } from "./wire.ts";

function folderChanged(input: unknown): SessionEvent[] {
  const folder = isRecord(input) ? words(input.cwd) : null;
  return folder === null ? [] : [{ type: "folder_changed", folder }];
}

/**
 * A Stop hook's input: the effort the session runs at, then its folder. The effort is always
 * said: `effort` is in the CLI's Stop input since Claude Code 2.1.280 and absent when the model
 * takes no effort parameter, which reads as null.
 */
export function stopHookEvents(input: unknown): SessionEvent[] {
  const effort = isRecord(input) && isRecord(input.effort) ? input.effort : {};
  return [{ type: "effort_observed", level: string(effort.level) }, ...folderChanged(input)];
}

/** A PostToolUse hook's input: the folder the session works in after the tool. */
export function postToolUseHookEvents(input: unknown): SessionEvent[] {
  return folderChanged(input);
}
