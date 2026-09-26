"""Turn SDK messages into what the owner sees: text as it is written, and one task per tool
call or background task. Generic over message types: no branch names a tool, so a tool Claude
Code adds tomorrow renders with no change here."""

from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    Message,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    UserMessage,
)
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
from code_with_slack.footer import format_tokens

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
    name: str = ""  # the tool's name, for a line folded into a summary
    task: bool = False  # a task's line (a subagent, a background command): never folded


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


def format_duration(seconds: float) -> str:
    whole = int(seconds)
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m {whole % 60}s"
    return f"{whole // 3600}h {whole % 3600 // 60}m"


def ended_line(summary: str, status: str, duration_ms: int | None) -> str:
    """How a task's end opens Claude Code's report of it: the notification's own summary, as
    the terminal prints it (`Agent "..." finished · 10s`), with the duration when there is one."""
    line = f"{'❌' if status == 'failed' else '✅'} {summary}"
    return line if duration_ms is None else f"{line} · {format_duration(duration_ms / 1000)}"


def terminal_status(status: str) -> tuple[TaskStatus, str | None]:
    if status == "failed":
        return "error", None
    if status in ("stopped", "killed"):
        return "complete", STOPPED
    return "complete", None


class TurnRenderer:
    def __init__(self, sink: Sink) -> None:
        self._sink = sink
        self._lines: dict[str, TaskUpdate] = {}
        self._root_of: dict[str, str] = {}
        self._children: dict[str, list[str]] = {}
        self._line_of_task: dict[str, str] = {}
        # Tasks started and not yet ended (task_id -> entry id). Only the lifecycle frames say
        # this reliably: a subagent can move to the background without a second task_started.
        self._running: dict[str, str] = {}
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
                entry = self._lines.get(self._line_of_task.get(message.task_id, ""))
                if entry is not None:
                    await self._set(
                        replace(entry, details=one_line(message.description, OUTPUT_LIMIT))
                    )
            case TaskNotificationMessage():
                await self._task_ended(message.task_id, message.status, message.summary)
            case TaskUpdatedMessage(status=str() as status) if status in TERMINAL_TASK_STATUSES:
                await self._task_ended(message.task_id, status, None)
            case SystemMessage(subtype="compact_boundary", data=data):
                await self._compacted(data.get("compact_metadata") or {})
            case ResultMessage():
                self.result = message
                if not self._wrote_text and message.result:
                    await self._text(message.result)
            case _:
                pass

    @property
    def running_tasks(self) -> list[str]:
        """Ids of the tasks that outlive the turn; a later task frame fed here updates them."""
        return list(self._running)

    def task_title(self, task_id: str) -> str | None:
        """The line title of a task this reply shows, running or ended."""
        entry = self._lines.get(self._line_of_task.get(task_id, ""))
        return entry.title if entry else None

    def owns(self, tool_use_id: str) -> bool:
        """Whether this reply holds the line of that tool call, or of the subagent it runs in."""
        return tool_use_id in self._lines or tool_use_id in self._root_of

    async def close(self, footer: str | None) -> None:
        """End the reply: every open tool line is closed, except a task still running, whose line
        stays open until its own end arrives through `feed` or `stop_running`."""
        interrupted = self.result is not None and self.result.terminal_reason in INTERRUPTED
        if not self._wrote_text and not self._lines:
            # A command that prints nothing (a local one, say) still gets a visible answer.
            await self._text(texts.STOPPED if interrupted else texts.NO_OUTPUT)
        running = set(self._running.values())
        closing = [
            replace(entry, status="in_progress", details=BACKGROUND, task=True)
            if entry.id in running
            else replace(entry, status="complete", output=STOPPED if interrupted else entry.output)
            for entry in self._lines.values()
            if entry.id in running or entry.status in ("pending", "in_progress")
        ]
        self._lines.update((entry.id, entry) for entry in closing)
        await self._sink.finish(closing, footer)

    async def stop_running(self) -> None:
        """The Claude Code process is gone and its tasks with it: close their lines as stopped."""
        for task_id in list(self._running):
            await self._task_ended(task_id, "stopped", None)

    async def feed_notice(self, text: str) -> None:
        """A note from the daemon itself, written before Claude Code's reply. It is not Claude's
        text, so a local command's result still shows after it."""
        await self._sink.text(text + "\n\n")

    async def feed_error(self, text: str) -> None:
        """A failure outside the SDK stream (the client died): say so in the reply."""
        await self._text("\n\n" + text)

    async def _compacted(self, metadata: dict[str, Any]) -> None:
        """Claude Code compacted the conversation, on request or on its own: say by how much."""
        before, after = metadata.get("pre_tokens"), metadata.get("post_tokens")
        if isinstance(before, int) and isinstance(after, int):
            line = texts.COMPACTED.format(before=format_tokens(before), after=format_tokens(after))
        else:
            line = texts.COMPACTED_PLAIN
        await self._text(("\n\n" if self._wrote_text else "") + line + "\n\n")

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
            if root is None or root not in self._lines:
                await self._set(TaskUpdate(block.id, title, "in_progress", name=block.name))
            else:
                self._root_of[block.id] = root
                await self._child(root, title)
        elif isinstance(block, ToolResultBlock | ServerToolResultBlock):
            entry = self._lines.get(block.tool_use_id)
            if entry is not None and entry.id in self._running.values():
                # The call only launched a task, which is still running: it outlives the call, so
                # its line becomes a task's line and stays open. A task that ends before its
                # call's result (a long command in the foreground) never gets here.
                await self._set(
                    replace(
                        entry, details=BACKGROUND, task=True, output=result_summary(block.content)
                    )
                )
            elif entry is not None:
                failed = isinstance(block, ToolResultBlock) and bool(block.is_error)
                await self._set(
                    replace(
                        entry,
                        status="error" if failed else "complete",
                        output=result_summary(block.content),
                    )
                )

    async def _task_started(self, message: TaskStartedMessage) -> None:
        if message.tool_use_id and message.tool_use_id in self._lines:
            # A call's task: Claude Code starts one for a long command in the foreground too
            # (recorded: `interrupt.jsonl`, CLI 2.1.283). The line stays the call's until the
            # call's result arrives with the task still running (`_block`).
            self._line_of_task[message.task_id] = message.tool_use_id
            self._running[message.task_id] = message.tool_use_id
            return
        line_id = f"task-{message.task_id}"
        self._line_of_task[message.task_id] = line_id
        self._running[message.task_id] = line_id
        title = one_line(message.description, TITLE_LIMIT)
        name = message.task_type or "task"
        await self._set(TaskUpdate(line_id, title, "in_progress", name=name, task=True))

    async def _task_ended(self, task_id: str, status: str, summary: str | None) -> None:
        self._running.pop(task_id, None)
        line_id = self._line_of_task.get(task_id, f"task-{task_id}")
        entry = self._lines.get(line_id) or TaskUpdate(
            line_id,
            one_line(summary or task_id, TITLE_LIMIT),
            "in_progress",
            name="task",
            task=True,
        )
        final, stopped = terminal_status(status)
        await self._set(
            replace(
                entry,
                status=final,
                details=None,
                output=stopped or (one_line(summary, OUTPUT_LIMIT) if summary else None),
            )
        )

    async def _child(self, root: str, line: str) -> None:
        lines = self._children.setdefault(root, [])
        lines.append(line)
        del lines[:-CHILD_LINES]
        await self._set(replace(self._lines[root], details="\n".join(lines)))

    async def _set(self, update: TaskUpdate) -> None:
        self._lines[update.id] = update
        await self._sink.task(update)

    async def _text(self, markdown: str) -> None:
        if markdown:
            self._wrote_text = True
            await self._sink.text(markdown)
