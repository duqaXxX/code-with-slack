"""Turn SDK messages into what the owner sees: text as it is written, and one task per tool
call or background task. Generic over message types: no branch names a tool, so a tool Claude
Code adds tomorrow renders with no change here. The few tools the terminal shows in words of its
own live in `previews`, which falls back to this generic view for any other."""

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
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from code_with_slack import texts
from code_with_slack.footer import format_tokens
from code_with_slack.render.previews import PREVIEWED, Preview, answered, preview

TaskStatus = Literal["pending", "in_progress", "complete", "error"]
TaskFrame = TaskStartedMessage | TaskProgressMessage | TaskNotificationMessage | TaskUpdatedMessage
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
    name: str = ""  # the tool's name, which chooses its preview
    task: bool = False  # a subagent's or a background command's card
    calls: int = 0  # calls made inside it (a subagent's), counted in its title
    preview: Preview | None = None  # the terminal's own view of a finished call
    # On a card of a run of calls (`render.fold`): the line that replaces the card once the
    # reply's body has ended; empty removes the card. None on any other card, which stays.
    folded: str | None = None

    @property
    def shown_preview(self) -> Preview | None:
        """The preview, on a call that finished well and only there: a failed, running or task
        card keeps its own view whatever it carries, so no path can show a preview over an
        error."""
        return self.preview if self.status == "complete" and not self.task else None


class Sink(Protocol):
    async def text(self, markdown: str, *, notice: bool = False) -> None: ...
    async def task(self, update: TaskUpdate) -> None: ...
    async def finish(self, closing: list[TaskUpdate]) -> None: ...
    async def close_out(self, footer: str | None) -> bool: ...
    async def wait_landed(self) -> bool: ...
    async def settle(self) -> bool: ...


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
    line = f"{'✗' if status == 'failed' else '✓'} {summary}"
    return line if duration_ms is None else f"{line} · {format_duration(duration_ms / 1000)}"


def terminal_status(status: str) -> tuple[TaskStatus, str | None]:
    if status == "failed":
        return "error", None
    if status in ("stopped", "killed"):
        return "complete", STOPPED
    return "complete", None


class TurnRenderer:
    def __init__(self, sink: Sink, cwd: str | None = None) -> None:
        self._sink = sink
        self._cwd = cwd  # the session's folder, which previews name paths from
        self._lines: dict[str, TaskUpdate] = {}
        self._root_of: dict[str, str] = {}
        self._children: dict[str, list[str]] = {}
        self._line_of_task: dict[str, str] = {}
        # Tasks started and not yet ended (task_id -> entry id). Only the lifecycle frames say
        # this reliably: a subagent can move to the background without a second task_started.
        self._running: dict[str, str] = {}
        self._commands: set[str] = set()  # lines of tasks no call started (a command's)
        self._nested: set[str] = set()  # tasks shown on a command's line, not on their own
        # Tasks a call inside another call started (a long command a subagent runs), held aside
        # while that call is open: the root's work, shown on its card through `_child`. A task
        # that ends before its call's result stays here, dropped; one still running at the result
        # outlives its call and leaves (`_outlived`). Kept after the end, so a later frame of a
        # dropped one (a `task_updated` after its notification) still finds it.
        self._inside: set[str] = set()
        self._aside: dict[str, TaskStartedMessage] = {}  # the held-aside tasks not yet ended
        self._closed_calls: set[str] = set()  # nested calls whose result arrived
        self._promoted: list[TaskStartedMessage] = []  # `take_promoted`'s, not yet taken
        self._answers: dict[str, Preview] = {}  # a question's answers, until its call ends
        self._wrote_text = False
        self._after_text = False  # Claude's text is the last thing in the reply, no card since
        self._break_due = False  # the text block that just started follows text directly
        self.result: ResultMessage | None = None
        self.auth_failed = False
        # The category of the last reply Claude Code wrote itself about a failure, if any.
        self.error: str | None = None
        # The reply's footer, as decided by the turn(s) that have closed it so far (a report
        # turn can close it again): `close_out` ends the reply with it once nothing is owed.
        self._footer: str | None = None
        self._closed_out = False

    async def feed(self, message: Message) -> None:
        match message:
            case StreamEvent(parent_tool_use_id=None, event=event):
                delta = event.get("delta") or {}
                block = event.get("content_block") or {}
                if event.get("type") == "content_block_start" and block.get("type") == "text":
                    # Two text blocks with no card between them (a goal's inner turns, a Stop hook
                    # that continues the turn) would run together: the break waits for the first
                    # text, so a block that stays empty adds none.
                    self._break_due = self._after_text
                elif (
                    event.get("type") == "content_block_delta" and delta.get("type") == "text_delta"
                ):
                    text = str(delta.get("text", ""))
                    if text and self._break_due:
                        text, self._break_due = "\n\n" + text, False
                    await self._text(text)
            case AssistantMessage():
                await self._assistant(message)
            case UserMessage(content=list() as blocks):
                # One `tool_use_result` per message: it belongs to a result only when it is alone.
                results = [b for b in blocks if isinstance(b, ToolResultBlock)]
                result = message.tool_use_result if len(results) == 1 else None
                for block in blocks:
                    await self._block(block, message.parent_tool_use_id, result)
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
                elif not self._wrote_text and self.error is not None:
                    await self._text(texts.ERROR_REPLY.format(error=self.error), notice=True)
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

    def nests(self, message: TaskFrame) -> bool:
        """Whether this task frame is of a task held aside or dropped as the work of a call inside
        another call, a call whose root this reply holds: Claude Code reports such a task to the
        subagent, not to the conversation. `False` for a call this reply never saw, for a task
        that started after its call's result, and for one that outlived its call."""
        if isinstance(message, TaskStartedMessage):
            call = message.tool_use_id
            return call in self._root_of and call not in self._closed_calls
        return message.task_id in self._inside

    def take_promoted(self) -> list[TaskStartedMessage]:
        """The tasks that outlived the nested call that started them since the last call: each is
        now an ordinary task of this reply (a line, a running task), which the session records as
        it does any task it saw start."""
        promoted, self._promoted = self._promoted, []
        return promoted

    def answered(
        self,
        tool_use_id: str,
        questions: list[dict[str, Any]],
        answers: dict[str, str | list[str]],
    ) -> bool:
        """Keep a question's answers for the line of its call, which shows them once the call
        ends. False when this reply has no line of its own for that call (one asked inside a
        subagent shows on the subagent's line) or there is no answer to show: the caller keeps
        the answers elsewhere."""
        shown = answered(questions, answers)
        if shown is None or tool_use_id not in self._lines:
            return False
        self._answers[tool_use_id] = shown
        return True

    def owns(self, tool_use_id: str) -> bool:
        """Whether this reply holds the line of that tool call, or of the subagent it runs in."""
        return tool_use_id in self._lines or tool_use_id in self._root_of

    async def close(self, footer: str | None) -> None:
        """End the turn's own part of the reply: every open tool card is closed, except a task
        still running, whose card stays open until its own end arrives through `feed` or
        `stop_running`. Does not end the reply (`ReplySink.finish`); the caller follows with
        `close_out` once it knows nothing more is coming, which can be after more than one call
        here (a background task's own report turn closes the same reply again)."""
        interrupted = self.result is not None and self.result.terminal_reason in INTERRUPTED
        if not self._wrote_text and not self._lines:
            # A command that prints nothing (a local one, say) still gets a visible answer.
            await self._text(texts.STOPPED if interrupted else texts.NO_OUTPUT, notice=True)
        running = set(self._running.values())
        closing = [
            replace(entry, status="in_progress", details=BACKGROUND, task=True)
            if entry.id in running
            else replace(entry, status="complete", output=STOPPED if interrupted else entry.output)
            for entry in self._lines.values()
            if entry.id in running or entry.status in ("pending", "in_progress")
        ]
        self._lines.update((entry.id, entry) for entry in closing)
        # Only ever moves forward: a report turn passes none, and must not erase what an earlier
        # close already decided for a still-deferred end.
        self._footer = footer or self._footer
        await self._sink.finish(closing)

    @property
    def closed_out(self) -> bool:
        """Whether this reply has already ended."""
        return self._closed_out

    async def close_out(self) -> bool:
        """End the reply, with the footer `close` last decided; call after `close`. True when it
        ended on Slack (see `ReplySink.close_out`). A second call is a no-op."""
        self._closed_out = True
        return await self._sink.close_out(self._footer)

    @property
    def sink(self) -> Sink:
        return self._sink

    async def stop_running(self) -> None:
        """The Claude Code process is gone and its tasks with it: close their lines as stopped."""
        for task_id in list(self._running):
            await self._task_ended(task_id, "stopped", None)

    async def feed_notice(self, text: str) -> None:
        """A note before what follows it: usually the daemon's own line, before Claude Code's
        reply even starts. It is not Claude's text, so a local command's result still shows
        after it. D1's report turn feeds one into an already-written reply instead (the one
        that started the task it reports): a blank line still separates it from what is there,
        as a paragraph break would."""
        prefix = "\n\n" if self._wrote_text or self._lines else ""
        self._after_text = False
        await self._sink.text(prefix + text + "\n\n", notice=True)

    async def feed_error(self, text: str) -> None:
        """A failure outside the SDK stream (the client died): say so in the reply."""
        await self._text("\n\n" + text, notice=True)

    async def _compacted(self, metadata: dict[str, Any]) -> None:
        """Claude Code compacted the conversation, on request or on its own: say by how much."""
        before, after = metadata.get("pre_tokens"), metadata.get("post_tokens")
        if isinstance(before, int) and isinstance(after, int):
            line = texts.COMPACTED.format(before=format_tokens(before), after=format_tokens(after))
        else:
            line = texts.COMPACTED_PLAIN
        await self._text(("\n\n" if self._wrote_text else "") + line + "\n\n", notice=True)

    async def _assistant(self, message: AssistantMessage) -> None:
        if message.error == "authentication_failed":
            self.auth_failed = True
            await self._text(texts.AUTH_FAILED, notice=True)
            return
        if message.error is not None:
            # Claude Code wrote this reply itself: its words name the failure and what to do
            # next, as the terminal shows them. In the recorded 529 they come in this message
            # with no stream event of their own, so they are read from its blocks. With no words
            # here the result speaks: it repeats them, and `feed` falls back to the category.
            self.error = message.error
            words = "".join(b.text for b in message.content if isinstance(b, TextBlock)).strip()
            if words:
                await self._text(("\n\n" if self._wrote_text else "") + words, notice=True)
            return
        for block in message.content:
            await self._block(block, message.parent_tool_use_id)

    async def _block(self, block: object, parent: str | None, result: object = None) -> None:
        if isinstance(block, ToolUseBlock | ServerToolUseBlock):
            title = task_title(block.name, block.input)
            root = self._root_of.get(parent, parent) if parent else None
            if root is not None and root in self._lines:
                self._root_of[block.id] = root
                await self._child(root, title)
            elif block.name in PREVIEWED:
                # Kept from the sink until it ends: what shows it then is its preview with no
                # card, or a card that says why it failed. An approval has a message of its own.
                self._lines[block.id] = TaskUpdate(block.id, title, "in_progress", name=block.name)
                self._after_text = False
            else:
                await self._set(TaskUpdate(block.id, title, "in_progress", name=block.name))
        elif isinstance(block, ToolResultBlock | ServerToolResultBlock):
            await self._outlived(block.tool_use_id)
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
            elif entry is not None and entry.output == STOPPED:
                # Its task already ended as stopped; the result that follows only reports the
                # rejection (recorded: `interrupt.jsonl`, CLI 2.1.283), and must not turn a
                # stopped call into a failed one.
                return
            elif entry is not None:
                failed = isinstance(block, ToolResultBlock) and bool(block.is_error)
                kept = self._answers.pop(block.tool_use_id, None)
                shown = None if failed else kept or preview(entry.name, result, self._cwd)
                await self._set(
                    replace(
                        entry,
                        status="error" if failed else "complete",
                        output=result_summary(block.content),
                        preview=shown,
                    )
                )

    async def _task_started(self, message: TaskStartedMessage) -> None:
        if self.nests(message):
            # Claude Code starts a task for a long command a subagent runs in the foreground of
            # its own context (recorded: `subagent-nested-command.jsonl`, CLI 2.1.286). The call
            # already shows on its root's card (`_child`): one work, one line, unless the task
            # outlives the call (`_outlived`).
            self._inside.add(message.task_id)
            self._aside[message.task_id] = message
            return
        # An id held aside before, starting again after its call closed, is an ordinary task
        # now: its later frames must reach it, or its end would never close its line.
        self._inside.discard(message.task_id)
        if message.tool_use_id and message.tool_use_id in self._lines:
            # A call's task: Claude Code starts one for a long command in the foreground too
            # (recorded: `interrupt.jsonl`, CLI 2.1.283). The line stays the call's until the
            # call's result arrives with the task still running (`_block`).
            self._line_of_task[message.task_id] = message.tool_use_id
            self._running[message.task_id] = message.tool_use_id
            return
        command = next((i for i in reversed(self._running.values()) if i in self._commands), None)
        if message.tool_use_id and command is not None and message.tool_use_id not in self._root_of:
            # Started by a call this reply never saw while a command's task runs: an agent inside
            # that command (a forked skill typed as a command streams none of its own calls,
            # recorded: `skill-fork-command.jsonl`, CLI 2.1.283). It shows on the command's
            # line, as a subagent's calls show on the subagent's: one work, one line.
            self._nested.add(message.task_id)
            await self._child(command, one_line(message.description, TITLE_LIMIT))
            return
        await self._open_line(message)

    async def _open_line(self, message: TaskStartedMessage) -> None:
        line_id = f"task-{message.task_id}"
        self._line_of_task[message.task_id] = line_id
        self._running[message.task_id] = line_id
        if message.tool_use_id is None:
            self._commands.add(line_id)
        title = one_line(message.description, TITLE_LIMIT)
        name = message.task_type or "task"
        await self._set(TaskUpdate(line_id, title, "in_progress", name=name, task=True))

    async def _outlived(self, call: str) -> None:
        """A nested call's result: a task it started that is still running outlives it (recorded:
        `subagent-nested-background.jsonl`, CLI 2.1.286, where the result comes first), and from
        here is an ordinary background task, as for a call at the top level (`_block`)."""
        if call not in self._root_of:
            return
        self._closed_calls.add(call)
        for task_id, started in list(self._aside.items()):
            if started.tool_use_id == call:
                del self._aside[task_id]
                self._inside.discard(task_id)
                # Queued before the line is written: a write that fails must not leave a
                # running task the session never adopts.
                self._promoted.append(started)
                await self._open_line(started)

    async def _task_ended(self, task_id: str, status: str, summary: str | None) -> None:
        if task_id in self._inside:
            self._aside.pop(task_id, None)  # dropped: it ended before its call's result
            return
        if task_id in self._nested:
            self._nested.discard(task_id)  # the command's own end closes its line
            return
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
        calls = self._lines[root].calls + 1
        # A call that runs calls of its own (a subagent) keeps its line once it ends, whether it
        # ran in the foreground or not: the nested calls are its work, not one more call.
        await self._set(
            replace(self._lines[root], details="\n".join(lines), task=True, calls=calls)
        )

    async def _set(self, update: TaskUpdate) -> None:
        if update.id not in self._lines:
            # Only a new line puts a card after the text. A line that changes does so where its
            # card sits, and the text stays the last thing in the reply.
            self._after_text = False
        self._lines[update.id] = update
        await self._sink.task(update)

    async def _text(self, markdown: str, *, notice: bool = False) -> None:
        if markdown:
            self._wrote_text = True
            self._after_text = not notice  # a notice ends with its own break
            await self._sink.text(markdown, notice=notice)
