"""Test doubles at the two boundaries the testing rules allow: Slack's AsyncWebClient and the
SDK's ClaudeSDKClient. Everything below those boundaries is the real code.

Fixtures are recorded and scrubbed (see CONTRIBUTING.md). SDK fixtures are the CLI's wire lines,
parsed by the SDK's own parser: an internal import, in tests only, so a fixture can never be a
shape the SDK would not produce. If a SDK release moves the parser, this import fails loudly.
"""

import asyncio
import json
import re
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
from claude_agent_sdk import ClaudeAgentOptions, Message, ResultMessage
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk.types import PermissionResult, ToolPermissionContext
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from code_with_slack.trust import Repository, Unkeyed, locate

FIXTURES = Path(__file__).parent / "fixtures"
OWNER = "U000ALICE"
STRANGER = "U000BOB"
TEAM = "T000TEAM"
OTHER_TEAM = "T000OTHER"
CHANNEL = "C000CHAN"
OTHER_CHANNEL = "C000CHN2"
BOT = "U000BOT"
# The root message's ts of a synthetic thread: a Slack thread_ts is that message's own epoch time.
THREAD = "1780000000.000001"
OTHER_THREAD = "1780000000.000002"


async def any_repository(directory: Path) -> Repository | None:
    """`trust.trusted_repository` for a test about something else: every repository counts as
    trusted, and the owner's own `~/.claude.json` is never read."""
    try:
        return await asyncio.to_thread(locate, directory)
    except Unkeyed:
        return None


def sdk_messages(name: str) -> list[Message]:
    out: list[Message] = []
    for line in (FIXTURES / "sdk" / f"{name}.jsonl").read_text().splitlines():
        message = parse_message(json.loads(line))
        if message is not None:
            out.append(message)
    return out


def split_turns(messages: list[Message]) -> list[list[Message]]:
    turns: list[list[Message]] = [[]]
    for message in messages:
        turns[-1].append(message)
        if isinstance(message, ResultMessage):
            turns.append([])
    return [t for t in turns if t]


def sdk_json(name: str) -> Any:
    return json.loads((FIXTURES / "sdk" / f"{name}.json").read_text())


def slack_payload(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / "slack" / f"{name}.json").read_text())
    return data


class EndOfStream:
    """A point in a script where the CLI process exits and its message stream ends."""


@dataclass(frozen=True)
class CanUseToolCall:
    """A point in a scripted turn where the CLI would ask the host for a permission decision."""

    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str = "toolu_fake_1"


@dataclass(frozen=True)
class HookRun:
    """A point in a scripted turn where the CLI runs the host's hooks for `event`: Stop before
    the result, PostToolUse after a tool."""

    input: dict[str, Any]
    event: str = "Stop"


class FakeClaudeClient:
    """Stands in for ClaudeSDKClient at its public methods and plays scripted turns."""

    def __init__(
        self,
        options: ClaudeAgentOptions,
        *,
        turns: list[list[Message | CanUseToolCall | HookRun | EndOfStream]] | None = None,
        server_info: dict[str, Any] | None = None,
        context_usage: dict[str, Any] | None = None,
        connect_error: Exception | None = None,
        connect_gate: asyncio.Event | None = None,
        server_info_error: Exception | None = None,
        context_usage_error: Exception | None = None,
        disconnect_error: Exception | None = None,
        disconnect_gate: asyncio.Event | None = None,
    ) -> None:
        self.options = options
        self._turns = list(turns or [])
        self._feed: asyncio.Queue[list[Message | CanUseToolCall | HookRun | EndOfStream]] = (
            asyncio.Queue()
        )
        self._server_info = server_info or {
            "commands": sdk_json("server-info")["commands"],
            "models": sdk_json("server-info")["models"],
            "current_permission_mode": "default",
        }
        self._context_usage = context_usage or sdk_json("context-usage")
        self._connect_error = connect_error
        # A connect that waits for the test, as a CLI does while it starts.
        self._connect_gate = connect_gate
        self._server_info_error = server_info_error
        self._context_usage_error = context_usage_error
        self._disconnect_error = disconnect_error
        # A disconnect that waits for the test, as a CLI takes real time to flush and exit after
        # EOF (SubprocessCLITransport.close()).
        self._disconnect_gate = disconnect_gate
        self.connected = False
        # A prompt as sent: text, or the user messages of an image prompt (streaming input).
        self.queries: list[Any] = []
        self.modes: list[str] = []
        self.models_set: list[str | None] = []
        self.interrupts = 0
        self.stopped_tasks: list[str] = []
        self.permission_results: list[PermissionResult] = []

    async def connect(self) -> None:
        if self._connect_gate is not None:
            await self._connect_gate.wait()
        if self._connect_error is not None:
            raise self._connect_error
        self.connected = True

    async def disconnect(self) -> None:
        if self._disconnect_gate is not None:
            await self._disconnect_gate.wait()
        self.connected = False
        if self._disconnect_error is not None:
            raise self._disconnect_error

    async def set_model(self, model: str | None = None) -> None:
        self.models_set.append(model)

    async def query(self, prompt: str | AsyncIterable[dict[str, Any]]) -> None:
        self.queries.append(prompt if isinstance(prompt, str) else [m async for m in prompt])
        if self._turns:
            self._feed.put_nowait(self._turns.pop(0))

    def inject(self, batch: list[Message | CanUseToolCall | HookRun | EndOfStream]) -> None:
        """Deliver a turn nobody asked for, as the CLI does for a background-task notification."""
        self._feed.put_nowait(batch)

    async def receive_messages(self) -> AsyncIterator[Message]:
        while True:
            batch = await self._feed.get()
            for item in batch:
                if isinstance(item, EndOfStream):
                    return
                if isinstance(item, CanUseToolCall):
                    assert self.options.can_use_tool is not None
                    result = await self.options.can_use_tool(
                        item.tool_name,
                        item.tool_input,
                        ToolPermissionContext(tool_use_id=item.tool_use_id),
                    )
                    self.permission_results.append(result)
                elif isinstance(item, HookRun):
                    for matcher in (self.options.hooks or {}).get(item.event, []):
                        for callback in matcher.hooks:
                            await callback(item.input, None, {"signal": None})  # type: ignore[arg-type]
                else:
                    yield item

    async def set_permission_mode(self, mode: str) -> None:
        self.modes.append(mode)

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def stop_task(self, task_id: str) -> None:
        self.stopped_tasks.append(task_id)

    async def get_server_info(self) -> dict[str, Any]:
        if self._server_info_error is not None:
            raise self._server_info_error
        return self._server_info

    async def get_context_usage(self) -> dict[str, Any]:
        if self._context_usage_error is not None:
            raise self._context_usage_error
        return self._context_usage


@dataclass
class FakeMessage:
    """One message of the fake workspace, as Slack keeps it: a post, or a stream. A stream takes
    `chunks` until it is stopped (by `chat.stopStream`, or by Slack after its lifetime:
    `FakeSlack.expire`); once stopped it takes `chat.update`, which replaces what it shows."""

    ts: str
    text: str = ""
    blocks: list[dict[str, Any]] = field(default_factory=list)
    streaming: bool = False
    chunks: list[dict[str, Any]] = field(default_factory=list)
    updated: bool = False  # a chat.update replaced the stream's own content
    deleted: bool = False


def card_of(block_or_chunk: dict[str, Any]) -> dict[str, Any]:
    """A task card as one plain dict, whether it came as a `task_update` chunk (strings) or a
    `task_card` block (rich text, as Slack reads it back)."""

    def plain(value: Any) -> str | None:
        if isinstance(value, dict):
            return "".join(str(e.get("text", "")) for s in value["elements"] for e in s["elements"])
        return None if value is None else str(value)

    card = {
        "id": block_or_chunk.get("id", block_or_chunk.get("task_id")),
        "title": block_or_chunk["title"],
        "status": block_or_chunk["status"],
    }
    for key in ("details", "output"):
        if (value := plain(block_or_chunk.get(key))) is not None:
            card[key] = value
    return card


class FakeSlack(AsyncWebClient):
    """The real AsyncWebClient, with the network replaced: every method builds its real arguments
    and ends in api_call, which records them and answers from `responses` or the recordings.

    Streams behave as the recordings measured (2026-09-28 and 2026-09-29): an open stream refuses
    `chat.update` (`streaming_state_conflict`) and `chat.delete` (`cant_delete_message`); a
    stream that is not open refuses `chat.appendStream` and `chat.stopStream`
    (`message_not_in_streaming_state`)."""

    def __init__(self) -> None:
        super().__init__(token="xox" + "b-fake")
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.posted_ts: list[str] = []  # the ts of every chat.postMessage, in order
        self.stream_ts: list[str] = []  # the ts of every chat.startStream, in order
        self.created_ts: list[str] = []  # both, in the order the messages were created
        self.messages: dict[str, FakeMessage] = {}
        self.stream_ends = 0  # streams that stopped, by the daemon's stop or by `expire`
        # Every call waits this long before answering, as a slow Slack API round trip would.
        self.delay = 0.0
        # While set, a call to `gate_method` waits here until the event is set, after `gated`
        # says one arrived: a test holds a write open and acts inside it, with no race against
        # a timer.
        self.gate: asyncio.Event | None = None
        self.gate_method = "chat.stopStream"
        self.gated = asyncio.Event()
        # A response may be an exception: the call raises it, as a network failure would. It may
        # be a function of the call's arguments, which returns the answer (or the exception).
        self.responses: dict[str, Any] = {
            "auth.test": slack_payload("api-auth-test"),
            "conversations.info": slack_payload("api-conversations-info"),
            "conversations.members": slack_payload("api-conversations-members"),
            "chat.postMessage": slack_payload("api-chat-postMessage"),
            "chat.startStream": slack_payload("api-chat-startStream"),
            "chat.getPermalink": slack_payload("api-chat-getPermalink"),
        }

    def _new_ts(self) -> str:
        return f"1790000000.{len(self.created_ts) + 1:06d}"

    def expire(self, ts: str) -> None:
        """Slack closes a stream that lived 5 minutes and never stopped (measured)."""
        self.messages[ts].streaming = False
        self.stream_ends += 1

    def _stream_state(self, method: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """Slack's answer when the stream's state refuses `method`, or None when it allows it."""
        message = self.messages.get(str(args.get("ts")))
        if message is None:
            return None
        if method in ("chat.appendStream", "chat.stopStream") and not message.streaming:
            return {"ok": False, "error": "message_not_in_streaming_state"}
        if method == "chat.update" and message.streaming:
            return {"ok": False, "error": "streaming_state_conflict"}
        if method == "chat.delete" and message.streaming:
            return {"ok": False, "error": "cant_delete_message"}
        return None

    def _record(self, method: str, args: dict[str, Any], answer: dict[str, Any]) -> None:
        """Keep what each message shows, as the recordings read back."""
        if method in ("chat.postMessage", "chat.startStream"):
            ts = str(answer["ts"])
            self.created_ts.append(ts)
            message = self.messages[ts] = FakeMessage(ts)
            if method == "chat.postMessage":
                self.posted_ts.append(ts)
                message.text = str(args.get("text", ""))
                message.blocks = list(args.get("blocks") or [])
            else:
                self.stream_ts.append(ts)
                message.streaming = True
                message.chunks = list(args.get("chunks") or [])
            return
        message = self.messages.get(str(args.get("ts")))
        if message is None:
            return
        if method == "chat.appendStream":
            message.chunks += list(args.get("chunks") or [])
        elif method == "chat.stopStream":
            message.chunks += list(args.get("chunks") or [])
            message.streaming = False
            message.blocks = list(args.get("blocks") or [])
            self.stream_ends += 1
        elif method == "chat.update":
            message.updated = True
            message.text = str(args.get("text", ""))
            message.blocks = list(args.get("blocks") or [])
        elif method == "chat.delete":
            message.deleted = True

    async def api_call(  # type: ignore[override]
        self,
        api_method: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        data: Any = None,
        **_: Any,
    ) -> AsyncSlackResponse:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.gate is not None and api_method == self.gate_method:
            self.gated.set()
            await self.gate.wait()
        args = {**(params or {}), **(json or {}), **(data if isinstance(data, dict) else {})}
        self.calls.append((api_method, args))
        answer = self.responses.get(api_method, {"ok": True})
        if callable(answer):  # an answer that depends on the call's arguments
            answer = answer(args)
        if (
            api_method in ("chat.appendStream", "chat.stopStream")
            and api_method not in self.responses
        ):
            # Measured 2026-09-29: both answer the channel and the message's ts.
            answer = {"ok": True, "channel": args.get("channel"), "ts": args.get("ts")}
        if api_method == "conversations.replies" and api_method not in self.responses:
            answer = self._thread(args)
        if isinstance(answer, list):  # a scripted sequence: one answer per call, the last repeats
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, BaseException):
            raise answer
        if api_method in ("chat.postMessage", "chat.startStream") and answer is self.responses.get(
            api_method
        ):  # the default: a new ts for each message
            answer = {**answer, "ts": self._new_ts()}
        refused = self._stream_state(api_method, args)
        if refused is not None:
            answer = refused
        if answer.get("ok", True) is not False:
            self._record(api_method, args, answer)
        return AsyncSlackResponse(
            client=self,
            http_verb="POST",
            api_url=api_method,
            req_args=args,
            data=answer,
            headers={},
            status_code=200,
        ).validate()

    def _stream_text(self, ts: str) -> str:
        """A stream's `text` as Slack reads it back (recorded 2026-09-28, `message-mixed-*` and
        `message-markdown-*`): the blank lines kept, `**b**` as `*b*`, a heading without its
        `## `, a list bullet as `•`, a link as `<url|label>`; then its cards' titles."""
        text, cards = self._shown(ts)
        text = re.sub(r"\[([^\]]*)\]\(([^)]*)\)", r"<\2|\1>", text)
        text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
        text = re.sub(r"^#+ ", "", text, flags=re.M)
        text = re.sub(r"^[-*] ", "• ", text, flags=re.M)
        return " ".join([text, *(c["title"] for c in cards)]).strip()

    def _thread(self, args: dict[str, Any]) -> dict[str, Any]:
        """What `conversations.replies` reads back of the messages this fake holds: the daemon's
        own, as its bot wrote them (a stream carries `streaming_state`), newer than `oldest`."""
        oldest = float(args.get("oldest") or 0)
        found = [
            {
                "ts": m.ts,
                "user": BOT,
                "type": "message",
                "text": m.text or self._stream_text(m.ts),
                "blocks": m.blocks,
                **(
                    {"streaming_state": "in_progress" if m.streaming else "completed"}
                    if m.ts in self.stream_ts
                    else {}
                ),
            }
            for m in self.messages.values()
            if float(m.ts) > oldest and not m.deleted
        ]
        return {"ok": True, "messages": found, "has_more": False}

    def _shown(self, ts: str) -> tuple[str, list[dict[str, Any]]]:
        """What a message shows now: its body text and its cards. A stream shows what its chunks
        say until a `chat.update` replaces it with the blocks that update carried."""
        message = self.messages[ts]
        if message.updated or not message.chunks:
            text = "\n\n".join(b["text"] for b in message.blocks if b.get("type") == "markdown")
            cards = [card_of(b) for b in message.blocks if b.get("type") == "task_card"]
            return text, cards
        text = "".join(c["text"] for c in message.chunks if c["type"] == "markdown_text")
        cards: dict[str, dict[str, Any]] = {}
        for chunk in message.chunks:
            if chunk["type"] == "task_update":
                cards[chunk["id"]] = card_of(chunk)
        return text.strip(), list(cards.values())

    def message_texts(self) -> list[str]:
        """The text each message shows last (Claude's words, as paragraphs), in the order the
        messages were created, posts and streams alike."""
        return [self._shown(ts)[0] for ts in self.created_ts]

    def stream_texts(self) -> list[str]:
        """The text of the messages that began as streams, in the order they started: a reply's
        own messages, with no approval request or notice among them."""
        return [self._shown(ts)[0] for ts in self.stream_ts]

    def message_cards(self) -> list[list[dict[str, Any]]]:
        """The task cards each message shows last, as plain dicts, in creation order."""
        return [self._shown(ts)[1] for ts in self.created_ts]

    def message_blocks(self) -> list[list[dict[str, Any]]]:
        """The blocks each message shows last (a stream's are the ones its stop wrote), in
        creation order."""
        return [self.messages[ts].blocks for ts in self.created_ts]

    def calls_to(self, method: str) -> list[dict[str, Any]]:
        return [args for name, args in self.calls if name == method]

    def pushes(self) -> int:
        """How many times Slack would notify the owner of a thread the owner started: a new
        message that is a post, and a stream when it stops (measured 2026-09-29: nothing at its
        start), whichever way it stops (the daemon's stop, or Slack's at the stream's end of
        life). An edit never does. Posts count only those of a reply: an approval is a post too."""
        return len(self.posted_ts) + self.stream_ends

    async def reactions_add(
        self, *, channel: str, name: str, timestamp: str, **kwargs: Any
    ) -> AsyncSlackResponse:
        """The arguments StatusReaction sends (slack_sdk 3.44.1's AsyncWebClient.reactions_add
        signature, docs.slack.dev/reference/methods/reactions.add, read 2026-09-28)."""
        kwargs.update({"channel": channel, "name": name, "timestamp": timestamp})
        return await self.api_call("reactions.add", params=kwargs)

    async def reactions_remove(
        self, *, channel: str, name: str, timestamp: str, **kwargs: Any
    ) -> AsyncSlackResponse:
        """Same, but without the `file`/`file_comment` padding the real
        AsyncWebClient.reactions_remove always adds: StatusReaction never sends them, and a test
        asserting on recorded calls would otherwise have to filter them out."""
        kwargs.update({"channel": channel, "name": name, "timestamp": timestamp})
        return await self.api_call("reactions.remove", params=kwargs)


class FakeClock:
    """The stream deadline's clock: a sleep ends when `advance` reaches its time, so a test
    crosses the 280 seconds without waiting."""

    def __init__(self) -> None:
        self.now = 0.0
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    async def sleep(self, seconds: float) -> None:
        wake = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now + seconds, wake))
        await wake

    def time(self) -> float:
        return self.now

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        for due, wake in list(self._sleepers):
            if due <= self.now and not wake.done():
                wake.set_result(None)
        self._sleepers = [(d, w) for d, w in self._sleepers if not w.done()]
        for _ in range(20):  # let what woke run to its next wait
            await asyncio.sleep(0)


class ResetAfterApply(FakeSlack):
    """Slack applied the call, then the connection reset before the answer came back: the
    daemon cannot tell whether it took effect. `reset_next` names the method that does it once."""

    def __init__(self) -> None:
        super().__init__()
        self.reset_next: str | None = None

    async def api_call(  # type: ignore[override]
        self, api_method: str, **kwargs: Any
    ) -> AsyncSlackResponse:
        answer = await super().api_call(api_method, **kwargs)
        if api_method == self.reset_next:
            self.reset_next = None
            raise aiohttp.ClientOSError(104, "Connection reset by peer")
        return answer


class SlowAfterApply(FakeSlack):
    """Slack applies the call, and its answer takes `slow_for` seconds more to come back: a
    task cancelled in that wait loses what the call did."""

    def __init__(self) -> None:
        super().__init__()
        self.slow_method: str | None = None
        self.slow_for = 0.0

    async def api_call(  # type: ignore[override]
        self, api_method: str, **kwargs: Any
    ) -> AsyncSlackResponse:
        answer = await super().api_call(api_method, **kwargs)
        if api_method == self.slow_method:
            await asyncio.sleep(self.slow_for)
        return answer
