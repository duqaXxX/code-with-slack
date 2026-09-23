"""Turn SDK messages into what the owner sees: text as it is written, and one task per tool
call or background task. Generic over message types: no branch names a tool, so a tool Claude
Code adds tomorrow renders with no change here."""

from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

from claude_agent_sdk import AssistantMessage, Message, ResultMessage, StreamEvent, UserMessage
from claude_agent_sdk.types import (
    TERMINAL_TASK_STATUSES,
    ServerToolResultBlock,
    ServerToolUseBlock,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    ToolResultBlock,
    ToolUseBlock,
)

from code_with_slack import texts

TaskStatus = Literal["pending", "in_progress", "complete", "error"]
TITLE_LIMIT = 80
OUTPUT_LIMIT = 200
CHILD_LINES = 10
BACKGROUND = "Running in background"
STOPPED = "Stopped"
INTERRUPTED = {"aborted_streaming", "aborted_tools"}


@dataclass(frozen=True)
class TaskUpdate:
    id: str
    title: str
    status: TaskStatus
    details: str | None = None
    output: str | None = None


class Sink(Protocol):
    async def text(self, markdown: str) -> None: ...
    async def task(self, update: TaskUpdate) -> None: ...
    async def finish(self, closing: list[TaskUpdate], footer: str | None) -> None: ...


def one_line(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def task_title(name: str, tool_input: dict[str, Any]) -> str:
    """`Name: first non-empty string argument`, whatever the tool."""
    for value in tool_input.values():
        if isinstance(value, str) and value.strip():
            return one_line(f"{name}: {value}", TITLE_LIMIT)
    return name


def result_summary(content: object) -> str | None:
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    elif isinstance(content, dict):
        text = str(content.get("type", ""))
    else:
        return None
    first = next((line for line in text.splitlines() if line.strip()), "")
    return one_line(first, OUTPUT_LIMIT) or None


def terminal_status(status: str) -> tuple[TaskStatus, str | None]:
    if status == "failed":
        return "error", None
    if status in ("stopped", "killed"):
        return "complete", STOPPED
    return "complete", None


class TurnRenderer:
    def __init__(self, sink: Sink) -> None:
        self._sink = sink
        self._cards: dict[str, TaskUpdate] = {}
        self._root_of: dict[str, str] = {}
        self._children: dict[str, list[str]] = {}
        self._card_of_task: dict[str, str] = {}
        self._wrote_text = False
        self.result: ResultMessage | None = None
        self.auth_failed = False

    async def feed(self, message: Message) -> None:
        match message:
            case StreamEvent(parent_tool_use_id=None, event=event):
                delta = event.get("delta") or {}
                if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                    await self._text(str(delta.get("text", "")))
            case AssistantMessage():
                await self._assistant(message)
            case UserMessage(content=list() as blocks):
                for block in blocks:
                    await self._block(block, message.parent_tool_use_id)
            case TaskStartedMessage():
                await self._task_started(message)
            case TaskProgressMessage():
                card = self._cards.get(self._card_of_task.get(message.task_id, ""))
                if card is not None:
                    await self._set(
                        replace(card, details=one_line(message.description, OUTPUT_LIMIT))
                    )
            case TaskNotificationMessage():
                await self._task_ended(message.task_id, message.status, message.summary)
            case TaskUpdatedMessage(status=str() as status) if status in TERMINAL_TASK_STATUSES:
                await self._task_ended(message.task_id, status, None)
            case ResultMessage():
                self.result = message
                if not self._wrote_text and message.result:
                    await self._text(message.result)
            case _:
                pass

    async def close(self, footer: str | None) -> None:
        """Close every open task, then end the reply: a finished reply shows nothing running."""
        interrupted = self.result is not None and self.result.terminal_reason in INTERRUPTED
        closing = [
            replace(card, status="complete", output=STOPPED if interrupted else card.output)
            for card in self._cards.values()
            if card.status in ("pending", "in_progress")
        ]
        await self._sink.finish(closing, footer)

    async def feed_notice(self, text: str) -> None:
        """A note from the daemon itself, written before Claude Code's reply."""
        await self._text(text + "\n\n")

    async def feed_error(self, text: str) -> None:
        """A failure outside the SDK stream (the client died): say so in the reply."""
        await self._text("\n\n" + text)

    async def _assistant(self, message: AssistantMessage) -> None:
        if message.error == "authentication_failed":
            self.auth_failed = True
            await self._text(texts.AUTH_FAILED)
            return
        if message.error is not None:
            await self._text(texts.ERROR_REPLY.format(error=message.error))
            return
        for block in message.content:
            await self._block(block, message.parent_tool_use_id)

    async def _block(self, block: object, parent: str | None) -> None:
        if isinstance(block, ToolUseBlock | ServerToolUseBlock):
            title = task_title(block.name, block.input)
            root = self._root_of.get(parent, parent) if parent else None
            if root is None or root not in self._cards:
                await self._set(TaskUpdate(block.id, title, "in_progress"))
            else:
                self._root_of[block.id] = root
                await self._child(root, title)
        elif isinstance(block, ToolResultBlock | ServerToolResultBlock):
            card = self._cards.get(block.tool_use_id)
            if card is not None:
                failed = isinstance(block, ToolResultBlock) and bool(block.is_error)
                await self._set(
                    replace(
                        card,
                        status="error" if failed else "complete",
                        output=result_summary(block.content),
                    )
                )

    async def _task_started(self, message: TaskStartedMessage) -> None:
        if message.tool_use_id and message.tool_use_id in self._cards:
            self._card_of_task[message.task_id] = message.tool_use_id
            await self._set(replace(self._cards[message.tool_use_id], details=BACKGROUND))
            return
        card_id = f"task-{message.task_id}"
        self._card_of_task[message.task_id] = card_id
        await self._set(
            TaskUpdate(card_id, one_line(message.description, TITLE_LIMIT), "in_progress")
        )

    async def _task_ended(self, task_id: str, status: str, summary: str | None) -> None:
        card_id = self._card_of_task.get(task_id, f"task-{task_id}")
        card = self._cards.get(card_id) or TaskUpdate(
            card_id, one_line(summary or task_id, TITLE_LIMIT), "in_progress"
        )
        final, stopped = terminal_status(status)
        await self._set(
            replace(
                card,
                status=final,
                details=None,
                output=stopped or (one_line(summary, OUTPUT_LIMIT) if summary else None),
            )
        )

    async def _child(self, root: str, line: str) -> None:
        lines = self._children.setdefault(root, [])
        lines.append(line)
        del lines[:-CHILD_LINES]
        await self._set(replace(self._cards[root], details="\n".join(lines)))

    async def _set(self, update: TaskUpdate) -> None:
        self._cards[update.id] = update
        await self._sink.task(update)

    async def _text(self, markdown: str) -> None:
        if markdown:
            self._wrote_text = True
            await self._sink.text(markdown)
