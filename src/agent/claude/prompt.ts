/**
 * What a turn sends to Claude Code: one user message, of the owner's text or of content blocks.
 *
 * The shape is the Agent SDK's streaming input, the only mode that takes images. A turn is one
 * user message, which carries the uuid Claude Code replays it under (`--replay-user-messages`).
 */
import type { UUID } from "node:crypto";
import type { SDKUserMessage } from "@anthropic-ai/claude-agent-sdk";
import type { Prompt } from "../seam.ts";

type Content = SDKUserMessage["message"]["content"];
type Block = Exclude<Content, string>[number];
type ImageBlock = Extract<Block, { type: "image" }>;
type MediaType = Extract<ImageBlock["source"], { type: "base64" }>["media_type"];

function contentOf(prompt: Prompt): Content {
  if (typeof prompt.content === "string") return prompt.content;
  return prompt.content.map((part): Block => {
    if (part.type === "text") return { type: "text", text: part.text };
    // The core admits the image types Claude takes and no other (`attachments.IMAGE_TYPES`),
    // so the seam's plain string is one of the SDK's four here.
    const media_type = part.mediaType as MediaType;
    return { type: "image", source: { type: "base64", media_type, data: part.data } };
  });
}

/**
 * `prompt` as the one user message of a turn, under its id: the message the SDK itself builds
 * for a string, plus the uuid. The id is a UUID the core made.
 */
export function userMessage(prompt: Prompt): SDKUserMessage {
  return {
    type: "user",
    message: { role: "user", content: contentOf(prompt) },
    parent_tool_use_id: null,
    uuid: prompt.id as UUID,
  };
}
