"""What a turn sends to Claude Code: the owner's text, or one user message of content blocks.

The shapes are the Agent SDK's streaming input (docs "Streaming Input", read 2026-09-25): the only
mode that takes images. A turn is one user message, so `user_message` yields exactly one.
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


class UserContent(TypedDict):
    role: Literal["user"]
    content: list[ContentBlock]


class TurnMessage(TypedDict):
    type: Literal["user"]
    message: UserContent
    parent_tool_use_id: None


# The owner's text, or the content blocks of one user message when it carries images.
Prompt = str | list[ContentBlock]


async def user_message(content: list[ContentBlock]) -> AsyncIterator[dict[str, Any]]:
    """`content` as the one user message of a turn, the only producer of a message prompt."""
    message: TurnMessage = {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }
    # The SDK types its input as plain dicts, which a TypedDict is not to mypy.
    yield cast(dict[str, Any], message)
