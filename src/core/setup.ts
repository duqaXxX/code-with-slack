/**
 * Session setup: the choice asked before the first prompt of a new session, with the model, the
 * effort and bypass, and how a raw selection is read against the models the agent listed. The
 * controls themselves are a provider's.
 *
 * Every default means "pass nothing": the model and the effort the agent would pick on its own.
 */
import type { ModelInfo } from "../agent/seam.ts";

export const DEFAULT = "default";

/** What the owner chose for a session; `DEFAULT` is a model or effort left to the agent. */
export interface Choice {
  readonly model: string;
  readonly effort: string;
  readonly bypass: boolean;
}

export const DEFAULT_CHOICE: Choice = { model: DEFAULT, effort: DEFAULT, bypass: false };

/** The levels `model` accepts, in the agent's order; empty for a model without effort. */
export function effortLevels(models: readonly ModelInfo[], model: string): readonly string[] {
  const entry = models.find((m) => m.value === model && m.supportsEffort);
  return entry === undefined ? [] : entry.supportedEffortLevels;
}

/** What a provider's controls report, before it is checked against the models listed. */
export interface Selection {
  /** The model's value, or `DEFAULT` when nothing is selected. */
  readonly model: string;
  readonly effort: string;
  readonly bypass: boolean;
}

/**
 * The owner's choice from a selection. A model the agent did not list reads as its default; an
 * effort the chosen model does not support reads as `Default`, which is what the message shows
 * after a model change.
 */
export function choiceOf(models: readonly ModelInfo[], selection: Selection): Choice {
  const listed = selection.model === DEFAULT || models.some((m) => m.value === selection.model);
  const model = listed ? selection.model : DEFAULT;
  const effort = effortLevels(models, model).includes(selection.effort)
    ? selection.effort
    : DEFAULT;
  return { model, effort, bypass: selection.bypass };
}

/** The data of the line that replaces the controls once the owner pressed Start. */
export interface Summary {
  /** The model's display name; the value itself when the agent did not list it. */
  readonly model: string;
  /** The level chosen, or null for the default. */
  readonly effort: string | null;
  readonly bypass: boolean;
}

export function summaryOf(models: readonly ModelInfo[], choice: Choice): Summary {
  const listed = models.find((m) => m.value === choice.model);
  return {
    model: listed === undefined ? choice.model : listed.displayName,
    effort: choice.effort === DEFAULT ? null : choice.effort,
    bypass: choice.bypass,
  };
}
