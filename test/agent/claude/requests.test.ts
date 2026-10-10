/**
 * The permission callback's arguments into the seam's requests, and the answers back.
 *
 * The question inputs are recorded: `ask-can-use-tool.json` and `ask-preview-can-use-tool.json`
 * (Claude Code 2.1.286). The options of a plain permission call are those the TypeScript SDK
 * 0.3.296 passed on 2026-10-10: `toolUseID`, `description`, `displayName` and `suggestions`
 * present, `title` absent. The answer of a multi-select as a list is what the Python daemon sent
 * and Claude Code took (`ask-answered.jsonl`).
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  permissionResult,
  QUESTION_TOOL,
  questionResult,
  toRequest,
} from "../../../src/agent/claude/requests.ts";
import { type JsonObject, sdkJson, sdkRecords } from "../../support/fixtures.ts";

const ASK = sdkJson("ask-can-use-tool") as JsonObject;
const ASK_PREVIEW = sdkJson("ask-preview-can-use-tool") as JsonObject;
const BASH = { command: "echo ok", description: "Print ok" };

// Requests.

test("a tool's call that asks is a permission request for that call", () => {
  const context = {
    toolUseID: "toolu_000",
    description: "Print ok",
    displayName: "Bash",
    suggestions: [],
  };
  assert.deepEqual(toRequest("request-1", "Bash", BASH, context), {
    type: "permission",
    requestId: "request-1",
    callId: "toolu_000",
    toolName: "Bash",
    input: BASH,
    title: null,
    description: "Print ok",
  });
});

test("the agent's own sentence for a request crosses when it writes one", () => {
  const context = { toolUseID: "toolu_000", title: "Claude wants to print ok" };
  const request = toRequest("request-1", "Bash", BASH, context);
  assert.equal(request.type, "permission");
  assert.deepEqual(request.type === "permission" ? [request.title, request.description] : null, [
    "Claude wants to print ok",
    null,
  ]);
});

test("a question is a question request, each question with its options", () => {
  const input = ASK.input as JsonObject;
  assert.equal(ASK.tool_name, QUESTION_TOOL);
  assert.deepEqual(toRequest("request-2", QUESTION_TOOL, input, { toolUseID: "toolu_000" }), {
    type: "question",
    requestId: "request-2",
    callId: "toolu_000",
    questions: [
      {
        header: "Colour preference",
        text: "Which colour do you prefer?",
        multiSelect: false,
        options: [
          { label: "Red", description: "The colour red", preview: null },
          { label: "Blue", description: "The colour blue", preview: null },
        ],
      },
      {
        header: "Green preference",
        text: "Do you also like green?",
        multiSelect: true,
        options: [
          { label: "Yes", description: "I like green", preview: null },
          { label: "No", description: "I don't like green", preview: null },
        ],
      },
    ],
  });
});

test("an option's preview crosses with it", () => {
  const input = ASK_PREVIEW.input as JsonObject;
  const context = { toolUseID: ASK_PREVIEW.tool_use_id as string };
  const request = toRequest("request-3", QUESTION_TOOL, input, context);
  assert.equal(request.type, "question");
  if (request.type !== "question") return;
  assert.equal(request.callId, "toolu_01DRb6mwGd2AsnBb1YjRS282");
  const [question] = request.questions;
  assert.equal(question?.header, "Status Line");
  assert.equal(question?.text, "Which status line layout would you prefer?");
  assert.deepEqual(
    question?.options.map((option) => [option.label, option.preview]),
    [
      ["Minimal", "~/project/main.js:42:15 | main\n"],
      ["Detailed", "~/project/main.js | Ln 42, Col 15 | main | Modified | UTF-8\n"],
    ],
  );
  assert.ok(question?.options[0]?.description?.startsWith("Shows only essential information"));
});

test("a question with no header and an option with no description still cross", () => {
  const input = { questions: [{ question: "Go on?", options: [{ label: "Yes" }] }] };
  const request = toRequest("request-4", QUESTION_TOOL, input, { toolUseID: "toolu_000" });
  assert.deepEqual(request.type === "question" ? request.questions : null, [
    {
      header: "",
      text: "Go on?",
      multiSelect: false,
      options: [{ label: "Yes", description: null, preview: null }],
    },
  ]);
});

test("a question tool's call with no question to show is a permission request", () => {
  const shapes: unknown[] = [
    undefined,
    null,
    [],
    "Go on?",
    [{ header: "No question", options: [] }],
    [{ question: "Go on?", options: "Yes" }],
    [{ question: "Go on?", options: [{ description: "No label" }] }],
    ["Go on?"],
  ];
  for (const questions of shapes) {
    const input = { questions };
    const request = toRequest("request-5", QUESTION_TOOL, input, { toolUseID: "toolu_000" });
    assert.equal(request.type, "permission", JSON.stringify(questions));
    assert.deepEqual(request.type === "permission" ? request.input : null, input);
  }
});

test("another tool's questions are its input and nothing more", () => {
  const request = toRequest("request-6", "Survey", ASK.input as JsonObject, {
    toolUseID: "toolu_000",
  });
  assert.equal(request.type, "permission");
});

test("a call that names itself with nothing has no call id", () => {
  const request = toRequest("request-7", "Bash", BASH, { toolUseID: "" });
  assert.equal(request.callId, null);
});

// Answers.

test("an approval allows the call with the input it had", () => {
  assert.deepEqual(permissionResult({ allow: true }, BASH), {
    behavior: "allow",
    updatedInput: BASH,
  });
});

test("an approval with a changed input allows the call with that input", () => {
  const changedInput = { command: "echo changed" };
  assert.deepEqual(permissionResult({ allow: true, changedInput }, BASH), {
    behavior: "allow",
    updatedInput: changedInput,
  });
});

test("a denial tells the agent why", () => {
  const message = "The owner denied this from Slack.";
  assert.deepEqual(permissionResult({ allow: false, message }, BASH), {
    behavior: "deny",
    message,
  });
});

test("an answered question goes back as the questions asked and their answers", () => {
  // What the Python daemon sent, as Claude Code echoed it in the result of the call.
  const echoed = sdkRecords("ask-answered").find(
    (record) => record.type === "user" && record.tool_use_result !== undefined,
  )?.tool_use_result as JsonObject;
  const input = { questions: echoed.questions };
  const answers = {
    "Which color do you prefer?": "Blue",
    "Do you also like green?": ["Yes", "Only in spring"],
  };
  assert.deepEqual(echoed.answers, answers);
  assert.deepEqual(questionResult({ answered: true, answers }, input), {
    behavior: "allow",
    updatedInput: { questions: echoed.questions, answers },
  });
});

test("the questions go back as the agent wrote them, with what the seam does not carry", () => {
  const questions = [{ question: "Go on?", options: [{ label: "Yes", extra: 1 }], extra: 2 }];
  const answers = { "Go on?": "Yes" };
  const result = questionResult({ answered: true, answers }, { questions, other: true });
  assert.deepEqual(result, { behavior: "allow", updatedInput: { questions, answers } });
});

test("an answer to an input that held no question goes back with none", () => {
  const answers = { "Go on?": "Yes" };
  assert.deepEqual(questionResult({ answered: true, answers }, {}), {
    behavior: "allow",
    updatedInput: { questions: [], answers },
  });
});

test("a skipped question denies the call with what the agent is told", () => {
  const message = "The owner dismissed the question without answering.";
  assert.deepEqual(questionResult({ answered: false, message }, ASK.input as JsonObject), {
    behavior: "deny",
    message,
  });
});
