/**
 * The agent's permission requests and questions as Slack draws them: the approval message, the
 * one-line question with Answer and Skip, the form that asks it one question at a time, and the
 * answered question that replaces a request. Every limit here is Slack's. What is pending and
 * what the answer is belong to `core/requests.ts`.
 *
 * Everything the model wrote (the title, the description, the input, a question's header) goes
 * to Slack with `&`, `<` and `>` escaped and a zero-width space after each backtick, so no
 * `<url|label>` can hide what it links and no text can close its code block.
 */
import type { PermissionRequest, Question, QuestionOption } from "../../agent/seam.ts";
import { oneLine, PY_SPACE } from "../../core/reply/words.ts";
import { answerOf } from "../../core/requests.ts";
import * as texts from "../../core/texts.ts";
import { shownAsWritten } from "./reply/escape.ts";

export const SECTION_LIMIT = 3000;
export const MESSAGE_BLOCKS = 50; // Slack's limit on blocks in one message
export const OPTION_TEXT_LIMIT = 75;
export const LABEL_LIMIT = 2000; // an input block's label

export const APPROVAL_ALLOW = "approval_allow";
export const APPROVAL_DENY = "approval_deny";
export const QUESTION_OPEN = "question_open";
export const QUESTION_SKIP = "question_skip";
export const QUESTION_FORM = "question_form";
// Text typed under Other, per question: four at the worst (every character escaped, `"` becoming
// `\"`) still keep the draft under private_metadata's 3,000 characters.
export const TYPED_LIMIT = 300;

type Block = Record<string, unknown>;
type Answers = Record<string, string | string[]>;

const EDGES = new RegExp(`^[${PY_SPACE}]+|[${PY_SPACE}]+$`, "g");
const RUNS = new RegExp(`[${PY_SPACE}]+`);

function strip(text: string): string {
  return text.replace(EDGES, "");
}

/** The first `limit` characters, counted in code points as Slack and Python count them. */
function head(text: string, limit: number): string {
  return Array.from(text).slice(0, limit).join("");
}

function length(text: string): number {
  return Array.from(text).length;
}

function cut(text: string, limit: number): string {
  return length(text) <= limit ? text : `${head(text, limit - 1)}…`;
}

/** `text` with every run of white space as one space. */
function squeezed(text: string): string {
  return strip(text).split(RUNS).join(" ");
}

function button(actionId: string, label: string, value: string, style?: string): Block {
  const entry: Block = {
    type: "button",
    action_id: actionId,
    value,
    text: { type: "plain_text", text: label },
  };
  if (style) entry.style = style;
  return entry;
}

function context(text: string): Block {
  return { type: "context", elements: [{ type: "mrkdwn", text }] };
}

/** `text` in pieces that each fit `limit` once shown as written, never splitting an escape. */
function codeChunks(text: string, limit: number): string[] {
  const chunks: string[] = [];
  let chunk = "";
  let size = 0;
  for (const char of text) {
    // Only these four change when shown as written; any other code point stays one character.
    const width = "&<>`".includes(char) ? length(shownAsWritten(char)) : 1;
    if (size + width > limit) {
      chunks.push(chunk);
      chunk = "";
      size = 0;
    }
    chunk += char;
    size += width;
  }
  return chunk !== "" || chunks.length === 0 ? [...chunks, chunk] : chunks;
}

function code(chunk: string): Block {
  return {
    type: "section",
    text: { type: "mrkdwn", text: `\`\`\`\n${shownAsWritten(chunk)}\n\`\`\`` },
  };
}

/**
 * The request the owner approves: everything the tool will run with, never cut silently, since
 * Approve hands the agent the whole input whatever was shown.
 */
export function approvalBlocks(approvalId: string, request: PermissionRequest): Block[] {
  const heading = request.title
    ? shownAsWritten(request.title)
    : texts.fill(texts.APPROVAL_PROMPT, { tool: shownAsWritten(request.toolName) });
  const blocks: Block[] = [
    { type: "section", text: { type: "mrkdwn", text: head(heading, SECTION_LIMIT) } },
  ];
  if (request.description) {
    blocks.push(context(head(shownAsWritten(request.description), SECTION_LIMIT)));
  }
  const detail = JSON.stringify(request.input, null, 2);
  const chunks = codeChunks(detail, SECTION_LIMIT - length("```\n\n```"));
  const room = MESSAGE_BLOCKS - blocks.length - 1; // the actions block closes it
  let codeBlocks: Block[];
  if (chunks.length > room) {
    // Past one message, the start and the end stay (a payload hides at the end of padding)
    // and a line says how much is not shown, so the owner can Deny rather than guess.
    const shown = chunks.slice(0, room - 2);
    const tail = chunks.at(-1) as string;
    const hidden = chunks.slice(room - 2, -1).reduce((total, chunk) => total + length(chunk), 0);
    const notice = texts.fill(texts.APPROVAL_CUT, { count: hidden.toLocaleString("en-US") });
    codeBlocks = [...shown.map(code), context(notice), code(tail)];
  } else {
    codeBlocks = chunks.map(code);
  }
  blocks.push(...codeBlocks, {
    type: "actions",
    elements: [
      button(APPROVAL_ALLOW, "Approve", approvalId, "primary"),
      button(APPROVAL_DENY, "Deny", approvalId, "danger"),
    ],
  });
  return blocks;
}

/**
 * An answered request as the terminal keeps it, for a question whose answers the reply cannot
 * show: `User answered Claude's questions:`, then `· question → answer` per question, with no
 * buttons left to press.
 */
export function answeredBlocks(
  questions: readonly Question[],
  answers: Readonly<Record<string, string | readonly string[]>>,
): Block[] {
  const lines = [texts.ANSWERED];
  for (const q of questions) {
    const answer = Object.hasOwn(answers, q.text) ? (answers[q.text] as string | string[]) : "";
    const shown = typeof answer === "string" ? answer : answer.join(", ");
    lines.push(`${texts.NESTED}· ${shownAsWritten(q.text)} → ${shownAsWritten(shown)}`);
  }
  // A context element holds at most 3,000 characters; long questions and answers are cut.
  return [context(cut(lines.join("\n"), SECTION_LIMIT))];
}

/**
 * The request in the channel: one line naming the questions, with Answer (which opens the form)
 * and Skip. It stays one line however many questions the agent asks.
 */
export function questionBlocks(approvalId: string, questions: readonly Question[]): Block[] {
  const headers = questions.map((q) => `*${shownAsWritten(q.header || q.text)}*`).join(" · ");
  const count =
    questions.length === 1
      ? texts.QUESTIONS_ONE
      : texts.fill(texts.QUESTIONS_MANY, { count: questions.length });
  return [
    {
      type: "section",
      text: { type: "mrkdwn", text: head(`${count}: ${headers}`, SECTION_LIMIT) },
    },
    {
      type: "actions",
      elements: [
        button(QUESTION_OPEN, texts.QUESTION_ANSWER, approvalId, "primary"),
        button(QUESTION_SKIP, "Skip", approvalId),
      ],
    },
  ];
}

/**
 * What the owner filled in the form so far, carried by the form itself (private_metadata) from
 * one question to the next: option indexes picked and text typed, per question.
 */
export interface Draft {
  readonly approvalId: string;
  readonly channelId: string;
  readonly threadTs: string;
  /** The question on screen. */
  readonly active: number;
  readonly picks: ReadonlyMap<number, readonly number[]>;
  readonly typed: ReadonlyMap<number, string>;
}

/** A draft of the request `approvalId` posted in this thread, with nothing filled in unless given. */
export function newDraft(
  approvalId: string,
  channelId: string,
  threadTs: string,
  filled: Partial<Pick<Draft, "active" | "picks" | "typed">> = {},
): Draft {
  return {
    approvalId,
    channelId,
    threadTs,
    active: filled.active ?? 0,
    picks: filled.picks ?? new Map(),
    typed: filled.typed ?? new Map(),
  };
}

/** The draft as `private_metadata` text, compact because Slack caps it at 3,000 characters. */
export function dumpDraft(draft: Draft): string {
  // `JSON.stringify` keeps non-Latin text as it is: Slack counts characters, not bytes.
  return JSON.stringify({
    a: draft.approvalId,
    c: draft.channelId,
    h: draft.threadTs,
    n: draft.active,
    p: Object.fromEntries([...draft.picks].map(([k, v]) => [String(k), v])),
    t: Object.fromEntries([...draft.typed].map(([k, v]) => [String(k), v])),
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value: unknown): string {
  if (typeof value !== "string") throw new Error("malformed draft");
  return value;
}

function index(value: unknown): number {
  const found = typeof value === "string" ? value : typeof value === "number" ? String(value) : "";
  if (!/^\d+$/.test(found)) throw new Error("malformed draft");
  return Number(found);
}

/** The draft `dumpDraft` wrote; throws on anything else. */
export function loadDraft(dumped: string): Draft {
  const data: unknown = JSON.parse(dumped);
  if (!isRecord(data) || !isRecord(data.p) || !isRecord(data.t)) throw new Error("malformed draft");
  const picks = new Map<number, number[]>();
  for (const [key, value] of Object.entries(data.p)) {
    if (!Array.isArray(value)) throw new Error("malformed draft");
    picks.set(index(key), value.map(index));
  }
  const typed = new Map<number, string>();
  for (const [key, value] of Object.entries(data.t)) typed.set(index(key), text(value));
  return newDraft(text(data.a), text(data.c), text(data.h), {
    active: index(data.n),
    picks,
    typed,
  });
}

function fieldsOf(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

/**
 * The draft with what the question on screen shows now: Slack sends the form's state with Next
 * and Submit, but only for the blocks on screen.
 */
export function absorb(draft: Draft, stateValues: unknown): Draft {
  const at = draft.active;
  const values = fieldsOf(stateValues);
  const picked = fieldsOf(fieldsOf(values[`q${at}`]).answer);
  const single = fieldsOf(picked.selected_option);
  const several = picked.selected_options;
  const chosen: unknown[] =
    Array.isArray(several) && several.length > 0
      ? several
      : Object.keys(single).length > 0
        ? [single]
        : [];
  const typedText = fieldsOf(fieldsOf(values[`o${at}`]).other).value;
  const typed = typeof typedText === "string" ? strip(typedText) : "";
  const picks = new Map(draft.picks);
  picks.set(
    at,
    chosen.map((o) => index(fieldsOf(o).value)),
  );
  const texts_ = new Map(draft.typed);
  texts_.set(at, typed);
  return {
    ...draft,
    picks: new Map([...picks].filter(([, v]) => v.length > 0)),
    typed: new Map([...texts_].filter(([, v]) => v !== "")),
  };
}

function answerAt(
  draft: Draft,
  questions: readonly Question[],
  at: number,
): string | string[] | null {
  const question = questions[at];
  if (question === undefined) throw new RangeError(`no question ${at}`);
  return answerOf(question, draft.picks.get(at) ?? [], draft.typed.get(at));
}

export function isAnswered(draft: Draft, questions: readonly Question[], at: number): boolean {
  return answerAt(draft, questions, at) !== null;
}

/** The index of the first question with no answer, or null when every one has one. */
export function firstUnanswered(draft: Draft, questions: readonly Question[]): number | null {
  const found = questions.findIndex((_, at) => answerAt(draft, questions, at) === null);
  return found === -1 ? null : found;
}

/** Every question's answer by its text, or null while one is unanswered. */
export function draftAnswers(draft: Draft, questions: readonly Question[]): Answers | null {
  const entries: [string, string | string[]][] = [];
  for (const [at, question] of questions.entries()) {
    const answer = answerAt(draft, questions, at);
    if (answer === null) return null;
    entries.push([question.text, answer]);
  }
  return Object.fromEntries(entries);
}

/**
 * An option's preview as it is shown: its own lines, with no blank line around them; empty when
 * it holds nothing to read.
 */
function previewOf(option: QuestionOption): string {
  const preview = option.preview ?? "";
  return strip(preview) !== "" ? preview.replace(/^\n+|\n+$/g, "") : "";
}

/**
 * Whether an option says more than a choice holds: Slack caps an option object's `text` and
 * `description` at 75 characters each (option object reference, read 2026-10-03), and it has no
 * place for a `preview`.
 */
function saysMore(question: Question): boolean {
  return question.options.some(
    (option) =>
      length(squeezed(option.label)) > OPTION_TEXT_LIMIT ||
      length(squeezed(option.description ?? "")) > OPTION_TEXT_LIMIT ||
      previewOf(option) !== "",
  );
}

function optionEntry(at: number, option: QuestionOption, described: boolean): Block {
  const entry: Block = {
    text: { type: "plain_text", text: oneLine(option.label, OPTION_TEXT_LIMIT) },
    value: String(at),
  };
  // Slack refuses an empty text: a description of blanks alone is none.
  const description = oneLine(option.description ?? "", OPTION_TEXT_LIMIT);
  if (described && description !== "") {
    entry.description = { type: "plain_text", text: description };
  }
  return entry;
}

function bold(text: string): Block {
  return { type: "text", text: cut(text, SECTION_LIMIT), style: { bold: true } };
}

/**
 * An option with every word the agent wrote for it, as one rich text block: its label in bold,
 * its description, and its preview in a preformatted element, which keeps its line breaks. Rich
 * text shows its text as written, so nothing in it is read as markup (a modal takes no
 * `markdown` block; block references, read 2026-10-03). The reference names no limit for a rich
 * text element's text: a section's is kept for each.
 */
function optionWhole(option: QuestionOption): Block {
  const parts: Block[] = [bold(oneLine(option.label, SECTION_LIMIT))];
  const description = strip(option.description ?? "");
  if (description !== "")
    parts.push({ type: "text", text: `\n${cut(description, SECTION_LIMIT)}` });
  const elements: Block[] = [{ type: "rich_text_section", elements: parts }];
  const preview = previewOf(option);
  if (preview !== "") {
    const shown = { type: "text", text: cut(preview, SECTION_LIMIT) };
    elements.push({ type: "rich_text_preformatted", elements: [shown] });
  }
  return { type: "rich_text", elements };
}

/**
 * The form, one question at a time: Slack has no tabs, so the modal's own button reads
 * `Next (1/3)` and leads to the next question, and `Submit` on the last; each needs an answer
 * before the next. The question shows as radio buttons, or checkboxes when several may be
 * picked, each option with its description, and an Other field (tools reference: "type your own
 * text through the Other row"). Both are optional in Slack's eyes: Next checks that the question
 * has one. When an option says more than a choice holds (`saysMore`), the question and every
 * option are shown whole above the choice, which keeps the labels alone under the question's
 * header (issue #47).
 */
export function questionView(draft: Draft, questions: readonly Question[]): Block {
  const blocks: Block[] = [];
  const count = questions.length;
  const at = draft.active;
  const question = questions[at];
  if (question === undefined) throw new RangeError(`no question ${at}`);
  if (count > 1) {
    const header = shownAsWritten(strip(question.header));
    const where = texts.fill(texts.QUESTION_WHERE, { number: at + 1, count });
    blocks.push(context(header !== "" ? `${header} · ${where}` : where));
  }
  const whole = saysMore(question);
  let label = oneLine(question.text, LABEL_LIMIT);
  if (whole) {
    const asked = { type: "rich_text_section", elements: [bold(label)] };
    blocks.push({ type: "rich_text", elements: [asked] });
    blocks.push(...question.options.map(optionWhole));
    // A header of blanks alone would leave the choice an empty label, which Slack refuses.
    const header = strip(question.header);
    label = oneLine(header !== "" ? header : texts.QUESTION_ANSWER, LABEL_LIMIT);
  }
  const options = question.options.map((o, i) => optionEntry(i, o, !whole));
  const element: Block = {
    type: question.multiSelect ? "checkboxes" : "radio_buttons",
    action_id: "answer",
    options,
  };
  const picked = (draft.picks.get(at) ?? []).flatMap((i) => options[i] ?? []);
  if (picked.length > 0 && question.multiSelect) {
    element.initial_options = picked;
  } else if (picked.length > 0) {
    element.initial_option = picked[0];
  }
  const other: Block = {
    type: "plain_text_input",
    action_id: "other",
    max_length: TYPED_LIMIT,
    placeholder: { type: "plain_text", text: texts.QUESTION_OTHER_HINT },
  };
  const typed = draft.typed.get(at);
  if (typed) other.initial_value = typed;
  blocks.push(
    {
      type: "input",
      block_id: `q${at}`,
      optional: true,
      label: { type: "plain_text", text: label },
      element,
    },
    {
      type: "input",
      block_id: `o${at}`,
      optional: true,
      label: { type: "plain_text", text: texts.QUESTION_OTHER },
      element: other,
    },
  );
  return {
    type: "modal",
    callback_id: QUESTION_FORM,
    private_metadata: dumpDraft(draft),
    title: { type: "plain_text", text: texts.QUESTION_TITLE },
    submit: {
      type: "plain_text",
      text: at < count - 1 ? texts.fill(texts.QUESTION_NEXT, { number: at + 1, count }) : "Submit",
    },
    close: { type: "plain_text", text: texts.QUESTION_CLOSE },
    blocks,
  };
}
