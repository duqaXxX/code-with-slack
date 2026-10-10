/**
 * The permission callback's arguments into the seam's requests, and the owner's answers into
 * what the SDK takes back.
 *
 * Claude Code asks through one callback for a tool it may not run and for a clarifying question.
 * The question is a call of the tool `AskUserQuestion`, and its answers travel back in the
 * updated input (code.claude.com/docs/en/agent-sdk/user-input): this is the permission protocol,
 * not rendering.
 */
import type { CanUseTool, PermissionResult } from "@anthropic-ai/claude-agent-sdk";
import type {
  PermissionAnswer,
  PermissionRequest,
  Question,
  QuestionAnswer,
  QuestionOption,
  QuestionRequest,
} from "../seam.ts";
import { isRecord, string, words } from "./wire.ts";

export const QUESTION_TOOL = "AskUserQuestion";

/**
 * What the daemon reads of the callback's options. `title` was absent in both calls measured on
 * the TypeScript SDK 0.3.296 (2026-10-10); the request names its call (`toolUseID`).
 */
export type CallContext = Partial<
  Pick<Parameters<CanUseTool>[2], "toolUseID" | "title" | "description">
>;

type Input = Readonly<Record<string, unknown>>;

function optionOf(value: unknown): QuestionOption | null {
  if (!isRecord(value)) return null;
  const label = string(value.label);
  if (label === null) return null;
  return { label, description: words(value.description), preview: words(value.preview) };
}

function questionOf(value: unknown): Question | null {
  if (!isRecord(value) || !Array.isArray(value.options)) return null;
  const text = string(value.question);
  if (text === null) return null;
  const options: QuestionOption[] = [];
  for (const entry of value.options) {
    const option = optionOf(entry);
    if (option === null) return null;
    options.push(option);
  }
  return {
    header: string(value.header) ?? "",
    text,
    multiSelect: value.multiSelect === true,
    options,
  };
}

/**
 * The questions of an `AskUserQuestion` input, or null when it holds none or one the form
 * cannot show (a shape other than the measured one): the call then asks as any other tool.
 */
function questionsOf(input: Input): Question[] | null {
  if (!Array.isArray(input.questions) || input.questions.length === 0) return null;
  const questions: Question[] = [];
  for (const entry of input.questions) {
    const question = questionOf(entry);
    if (question === null) return null;
    questions.push(question);
  }
  return questions;
}

/** One call of the permission callback as the request the core shows, under `requestId`. */
export function toRequest(
  requestId: string,
  toolName: string,
  input: Input,
  context: CallContext,
): PermissionRequest | QuestionRequest {
  const callId = words(context.toolUseID);
  const questions = toolName === QUESTION_TOOL ? questionsOf(input) : null;
  if (questions !== null) {
    return {
      type: "question",
      requestId,
      callId,
      toolName,
      title: words(context.title),
      questions,
    };
  }
  return {
    type: "permission",
    requestId,
    callId,
    toolName,
    input,
    title: words(context.title),
    description: words(context.description),
  };
}

/** The owner's answer to a permission request for a call that had `input`. */
export function permissionResult(answer: PermissionAnswer, input: Input): PermissionResult {
  if (!answer.allow) return { behavior: "deny", message: answer.message };
  return { behavior: "allow", updatedInput: { ...(answer.changedInput ?? input) } };
}

/**
 * The owner's answer to a question whose call had `input`: the questions as Claude Code wrote
 * them, with the answer of each under its text.
 */
export function questionResult(answer: QuestionAnswer, input: Input): PermissionResult {
  if (!answer.answered) return { behavior: "deny", message: answer.message };
  const questions = Array.isArray(input.questions) ? input.questions : [];
  return { behavior: "allow", updatedInput: { questions, answers: answer.answers } };
}
