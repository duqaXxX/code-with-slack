/** One prompt and its reply, the turn that is running, and the notes about prompts that never ran. */
import { randomUUID } from "node:crypto";
import type { PromptContent } from "../../agent/seam.ts";
import type { Reply } from "../../chat/seam.ts";
import type { TurnRenderer } from "../reply/renderer.ts";
import { blank, oneLine } from "../reply/words.ts";
import * as texts from "../texts.ts";
import { ASKED_LIMIT, NOT_SENT_START } from "./constants.ts";
import { Event } from "./tasks.ts";

export class Turn {
  readonly prompt: PromptContent;
  sink: Reply;
  readonly done = new Event();
  /** The daemon's own id for the prompt, which the agent names when it takes it. */
  readonly uuid: string = randomUUID();

  constructor(prompt: PromptContent, sink: Reply) {
    this.prompt = prompt;
    this.sink = sink;
  }
}

export interface ActiveTurn {
  /** Null for a turn the agent started itself, to report a task. */
  readonly turn: Turn | null;
  readonly renderer: TurnRenderer;
  /** Prompts whose replay arrived while this turn ran: the agent took them into it. */
  readonly taken: Turn[];
}

/** The owner's question on one line, as a note about a message that was not sent quotes it. */
export function asked(prompt: PromptContent): string {
  const text =
    typeof prompt === "string"
      ? prompt
      : prompt
          .filter((part) => part.type === "text")
          .map((part) => part.text)
          .join(" ");
  return blank(text) ? texts.PROMPT_IMAGE : oneLine(text, ASKED_LIMIT);
}

/**
 * The note that names turns that will never be sent: how many, why, and the start of each, so
 * the owner knows which to send again.
 */
export function notSent(turns: readonly Turn[], because: string): string {
  const header = texts.fill(turns.length === 1 ? texts.NOT_SENT_ONE : texts.NOT_SENT_MANY, {
    count: turns.length,
    because,
  });
  const starts = turns.map((turn) => `- "${oneLine(asked(turn.prompt), NOT_SENT_START)}"`);
  return [header, ...starts].join("\n");
}

/** The note for the reply a turn that took `count` prompts into itself ends with. */
export function takenNote(count: number): string {
  return texts.fill(count === 1 ? texts.TAKEN_INTO_REPLY_ONE : texts.TAKEN_INTO_REPLY_MANY, {
    count,
  });
}
