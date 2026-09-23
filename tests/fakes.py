"""Test doubles at the two boundaries the testing rules allow: Slack's AsyncWebClient and the
SDK's ClaudeSDKClient. Everything below those boundaries is the real code.

Fixtures are recorded and scrubbed (see CONTRIBUTING.md). SDK fixtures are the CLI's wire lines,
parsed by the SDK's own parser: an internal import, in tests only, so a fixture can never be a
shape the SDK would not produce. If a SDK release moves the parser, this import fails loudly.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, Message, ResultMessage
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk.types import PermissionResult, ToolPermissionContext
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

FIXTURES = Path(__file__).parent / "fixtures"
OWNER = "U000ALICE"
STRANGER = "U000BOB"
TEAM = "T000TEAM"
OTHER_TEAM = "T000OTHER"
CHANNEL = "C000CHAN"
BOT = "U000BOT"


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


class FakeClaudeClient:
    """Stands in for ClaudeSDKClient at its public methods and plays scripted turns."""

    def __init__(
        self,
        options: ClaudeAgentOptions,
        *,
        turns: list[list[Message | CanUseToolCall | EndOfStream]] | None = None,
        server_info: dict[str, Any] | None = None,
        context_usage: dict[str, Any] | None = None,
        connect_error: Exception | None = None,
    ) -> None:
        self.options = options
        self._turns = list(turns or [])
        self._feed: asyncio.Queue[list[Message | CanUseToolCall | EndOfStream]] = asyncio.Queue()
        self._server_info = server_info or {
            "commands": sdk_json("server-info")["commands"],
            "current_permission_mode": "default",
        }
        self._context_usage = context_usage or sdk_json("context-usage")
        self._connect_error = connect_error
        self.connected = False
        self.queries: list[str] = []
        self.modes: list[str] = []
        self.interrupts = 0
        self.permission_results: list[PermissionResult] = []

    async def connect(self) -> None:
        if self._connect_error is not None:
            raise self._connect_error
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)
        if self._turns:
            self._feed.put_nowait(self._turns.pop(0))

    def inject(self, batch: list[Message | CanUseToolCall | EndOfStream]) -> None:
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
                else:
                    yield item

    async def set_permission_mode(self, mode: str) -> None:
        self.modes.append(mode)

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def get_server_info(self) -> dict[str, Any]:
        return self._server_info

    async def get_context_usage(self) -> dict[str, Any]:
        return self._context_usage


class FakeSlack(AsyncWebClient):
    """The real AsyncWebClient, with the network replaced: every method builds its real arguments
    and ends in api_call, which records them and answers from `responses` or the recordings."""

    def __init__(self) -> None:
        super().__init__(token="xox" + "b-fake")
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.posted_ts: list[str] = []  # the ts of every chat.postMessage, in order
        # A response may be an exception: the call raises it, as a network failure would.
        self.responses: dict[str, Any] = {
            "auth.test": slack_payload("api-auth-test"),
            "conversations.info": slack_payload("api-conversations-info"),
            "conversations.members": slack_payload("api-conversations-members"),
            "chat.postMessage": slack_payload("api-chat-postMessage"),
            "chat.startStream": slack_payload("api-chat-startStream"),
        }

    async def api_call(  # type: ignore[override]
        self,
        api_method: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        data: Any = None,
        **_: Any,
    ) -> AsyncSlackResponse:
        args = {**(params or {}), **(json or {}), **(data if isinstance(data, dict) else {})}
        self.calls.append((api_method, args))
        answer = self.responses.get(api_method, {"ok": True})
        if isinstance(answer, list):  # a scripted sequence: one answer per call, the last repeats
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, BaseException):
            raise answer
        if api_method == "chat.postMessage":
            if answer is self.responses.get(api_method):  # the default: a new ts for each message
                answer = {**answer, "ts": f"1790000000.{len(self.posted_ts) + 1:06d}"}
            self.posted_ts.append(str(answer["ts"]))
        return AsyncSlackResponse(
            client=self,
            http_verb="POST",
            api_url=api_method,
            req_args=args,
            data=answer,
            headers={},
            status_code=200,
        ).validate()

    def message_texts(self) -> list[str]:
        """The markdown each posted message shows last, in the order the messages were posted."""
        posted = iter(self.posted_ts)
        order: list[str] = []
        shown: dict[str, str] = {}
        for method, args in self.calls:
            if method == "chat.postMessage":
                ts = next(posted)
                order.append(ts)
            elif method == "chat.update":
                ts = args["ts"]
            else:
                continue
            markdown = [b["text"] for b in args.get("blocks") or [] if b.get("type") == "markdown"]
            shown[ts] = markdown[0] if markdown else shown.get(ts, "")
        return [shown[ts] for ts in order]

    def calls_to(self, method: str) -> list[dict[str, Any]]:
        return [args for name, args in self.calls if name == method]
