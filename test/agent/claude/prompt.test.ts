/**
 * A prompt as the one user message of a turn. The shape is the Agent SDK's streaming input
 * (`SDKUserMessage`, TypeScript SDK 0.3.296), and the one Python's `prompt.user_message` built:
 * the message the SDK itself builds for a string, plus the uuid Claude Code replays it under.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import { userMessage } from "../../../src/agent/claude/prompt.ts";

const ID = "00000000-0000-4000-8000-000000000001";

test("a text prompt is one user message under the prompt's id", () => {
  assert.deepEqual(userMessage({ id: ID, content: "Reply with the single word ok." }), {
    type: "user",
    message: { role: "user", content: "Reply with the single word ok." },
    parent_tool_use_id: null,
    uuid: ID,
  });
});

test("a prompt with an image is one user message of content blocks, in order", () => {
  const message = userMessage({
    id: ID,
    content: [
      { type: "text", text: "What is in this picture?" },
      { type: "image", mediaType: "image/png", data: "aGVsbG8=" },
      { type: "text", text: "And in this one?" },
      { type: "image", mediaType: "image/jpeg", data: "d29ybGQ=" },
    ],
  });
  assert.deepEqual(message, {
    type: "user",
    message: {
      role: "user",
      content: [
        { type: "text", text: "What is in this picture?" },
        { type: "image", source: { type: "base64", media_type: "image/png", data: "aGVsbG8=" } },
        { type: "text", text: "And in this one?" },
        { type: "image", source: { type: "base64", media_type: "image/jpeg", data: "d29ybGQ=" } },
      ],
    },
    parent_tool_use_id: null,
    uuid: ID,
  });
});

test("the id of the message is the id the replay names", () => {
  const first = userMessage({ id: ID, content: "one" });
  const second = userMessage({ id: "00000000-0000-4000-8000-000000000002", content: "one" });
  assert.equal(first.uuid, ID);
  assert.notEqual(first.uuid, second.uuid);
});
