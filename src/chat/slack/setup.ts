/**
 * Session setup in Slack: the message asked before the first prompt of a new session, with the
 * model, the effort and bypass, and the owner's choice read back from `state.values`. Pure
 * functions: the handlers post and wait, the session applies the choice.
 */
import type { ModelInfo } from "../../agent/seam.ts";
import {
  type Choice,
  choiceOf,
  DEFAULT,
  effortLevels,
  type Summary,
  summaryOf,
} from "../../core/setup.ts";
import * as texts from "../../core/texts.ts";

export const SETUP_BLOCK = "setup"; // the one `actions` block that holds every control
export const SETUP_MODEL = "setup_model";
export const SETUP_EFFORT = "setup_effort";
export const SETUP_BYPASS = "setup_bypass";
export const SETUP_START = "setup_start";

const BYPASS_ON = "on";
const OPTION_TEXT_LIMIT = 75; // a plain_text option's own limit in Slack

type Block = Record<string, unknown>;

/** The first `limit` characters, counted in code points as Slack counts them. */
function firstChars(text: string, limit: number): string {
  return Array.from(text).slice(0, limit).join("");
}

function option(label: string, value: string, description: Block | null = null): Block {
  const entry: Block = {
    text: { type: "plain_text", text: firstChars(label, OPTION_TEXT_LIMIT) },
    value,
  };
  if (description !== null) entry.description = description;
  return entry;
}

function modelOptions(models: readonly ModelInfo[]): Block[] {
  return models.map((m) =>
    option(
      m.displayName,
      m.value,
      // The CLI's own words, shown under the name (plain_text, at most 75 characters).
      m.description === null
        ? null
        : { type: "plain_text", text: firstChars(m.description, OPTION_TEXT_LIMIT) },
    ),
  );
}

function effortOptions(models: readonly ModelInfo[], model: string): Block[] {
  return [DEFAULT, ...effortLevels(models, model)].map((level) =>
    option(texts.fill(texts.SETUP_EFFORT_OPTION, { level }), level),
  );
}

function initial(options: Block[], value: string): Block {
  return options.find((o) => o.value === value) ?? (options[0] as Block);
}

function select(actionId: string, options: Block[], value: string): Block {
  return {
    type: "static_select",
    action_id: actionId,
    options,
    initial_option: initial(options, value),
  };
}

/**
 * The setup message: a header and one row of controls (Slack wraps it on a narrow screen): a
 * Model select (left out when the agent listed no model), an Effort select with the chosen
 * model's levels, the Bypass checkbox and Start. `choice` is what each control shows; Start
 * carries `setupId`, the one thing that resolves the question.
 */
export function setupBlocks(
  setupId: string,
  models: readonly ModelInfo[],
  choice: Choice,
): Block[] {
  const elements: Block[] = [];
  if (models.length > 0) {
    elements.push(select(SETUP_MODEL, modelOptions(models), choice.model));
  }
  elements.push(select(SETUP_EFFORT, effortOptions(models, choice.model), choice.effort));
  const bypass = option(texts.SETUP_BYPASS_OPTION, BYPASS_ON, {
    type: "mrkdwn",
    text: texts.SETUP_BYPASS_DESCRIPTION,
  });
  const checkboxes: Block = { type: "checkboxes", action_id: SETUP_BYPASS, options: [bypass] };
  if (choice.bypass) checkboxes.initial_options = [bypass];
  elements.push(checkboxes);
  elements.push({
    type: "button",
    action_id: SETUP_START,
    value: setupId,
    style: "primary",
    text: { type: "plain_text", text: texts.SETUP_START_BUTTON },
  });
  return [
    { type: "section", text: { type: "mrkdwn", text: texts.SETUP_HEADER } },
    { type: "actions", block_id: SETUP_BLOCK, elements },
  ];
}

function record(value: unknown): Readonly<Record<string, unknown>> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

/**
 * The owner's choice from a payload's `state.values`. A control with no selection, or one naming
 * something the agent did not list, reads as its default (`choiceOf`).
 */
export function readChoice(values: unknown, models: readonly ModelInfo[]): Choice {
  const row = record(record(values)[SETUP_BLOCK]);
  const selected = (action: string): string => {
    const value = record(record(row[action]).selected_option).value;
    return typeof value === "string" && value !== "" ? value : DEFAULT;
  };
  const picked = record(row[SETUP_BYPASS]).selected_options;
  const bypass = Array.isArray(picked) && picked.some((o) => record(o).value === BYPASS_ON);
  return choiceOf(models, {
    model: selected(SETUP_MODEL),
    effort: selected(SETUP_EFFORT),
    bypass,
  });
}

/** The one line that replaces the controls once the owner pressed Start. */
export function summary(models: readonly ModelInfo[], choice: Choice): string {
  const data: Summary = summaryOf(models, choice);
  return texts.fill(texts.SETUP_SUMMARY, {
    model: data.model,
    effort: data.effort ?? texts.SETUP_EFFORT_DEFAULT,
    bypass: data.bypass ? "on" : "off",
  });
}
