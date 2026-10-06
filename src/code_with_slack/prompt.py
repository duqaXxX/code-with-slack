"""What a turn sends to Claude Code: one user message, of the owner's text or of content blocks.

The shapes are the Agent SDK's streaming input (docs "Streaming Input", read 2026-09-25): the only
mode that takes images. A turn is one user message, so `user_message` yields exactly one, which
carries the uuid Claude Code replays it under (`--replay-user-messages`).
"""

from collections.abc import AsyncIterator
from typing import Any, Literal, TypedDict, cast


class TextBlock(TypedDict):
    type: Literal["text"]
    text: str


class ImageSource(TypedDict):
    type: Literal["base64"]
    media_type: str
    data: str


class ImageBlock(TypedDict):
    type: Literal["image"]
    source: ImageSource


ContentBlock = TextBlock | ImageBlock


# The owner's text, or the content blocks of one user message when it carries images.
Prompt = str | list[ContentBlock]


class UserContent(TypedDict):
    role: Literal["user"]
    content: Prompt


class TurnMessage(TypedDict):
    type: Literal["user"]
    message: UserContent
    parent_tool_use_id: None
    uuid: str


async def user_message(prompt: Prompt, uuid: str) -> AsyncIterator[dict[str, Any]]:
    """`prompt` as the one user message of a turn, under `uuid`: the only producer of a message
    prompt. A text is the message the SDK itself builds for a string, plus the uuid."""
    message: TurnMessage = {
        "type": "user",
        "message": {"role": "user", "content": prompt},
        "parent_tool_use_id": None,
        "uuid": uuid,
    }
    # The SDK types its input as plain dicts, which a TypedDict is not to mypy.
    yield cast(dict[str, Any], message)
