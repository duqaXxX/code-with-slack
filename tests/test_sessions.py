import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, RateLimitEvent, ResultError, ResultMessage
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    SystemMessage,
    TaskNotificationMessage,
)

from code_with_slack import sessions, texts
from code_with_slack.approvals import Answer, Approvals, Approve
from code_with_slack.footer import UsageCache
from code_with_slack.guards import Identity
from code_with_slack.render.sinks import StreamingSwitch
from code_with_slack.sessions import SessionDeps, SessionManager, resolve_directory
from code_with_slack.state import StateStore
from tests.fakes import (
    BOT,
    CHANNEL,
    OWNER,
    TEAM,
    CanUseToolCall,
    EndOfStream,
    FakeClaudeClient,
    FakeSlack,
    sdk_json,
    sdk_messages,
    split_turns,
)


async def until(condition: Callable[[], bool], limit: float = 2.0) -> None:
    """Poll a condition on the fakes, which expose no event to wait on."""
    async with asyncio.timeout(limit):
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


class Harness:
    def __init__(self, slack: FakeSlack, tmp_path: Path, scripts: list[dict[str, Any]]) -> None:
        self.slack = slack
        self.tmp_path = tmp_path
        self.state = StateStore(tmp_path / "state.json")
        self.state.bind(CHANNEL, tmp_path)
        self.clients: list[FakeClaudeClient] = []
        self.approvals = Approvals()
        self.usage_fetches = 0
        self._scripts = scripts

        async def fetch() -> str:
            self.usage_fetches += 1
            return "Current session: 5% used"

        self.deps = SessionDeps(
            slack=slack,
            identity=Identity(OWNER, TEAM, BOT),
            state=self.state,
            approvals=self.approvals,
            usage=UsageCache(fetch),
            streaming=StreamingSwitch(),
            client_factory=self.factory,
        )
        self.manager = SessionManager(self.deps)

    def factory(self, options: ClaudeAgentOptions) -> FakeClaudeClient:
        script = self._scripts.pop(0) if self._scripts else {}
        client = FakeClaudeClient(options, **script)
        self.clients.append(client)
        return client

    def session(self) -> Any:
        session = self.manager.get(CHANNEL)
        assert session is not None
        return session

    def streamed_text(self) -> str:
        return "".join(
            c.get("text", "")
            for m in ("chat.startStream", "chat.appendStream", "chat.stopStream")
            for a in self.slack.calls_to(m)
            for c in a.get("chunks") or []
            if c.get("type") == "markdown_text"
        )


@pytest.fixture
async def harness_for(slack: FakeSlack, tmp_path: Path) -> AsyncIterator[Callable[..., Harness]]:
    made: list[Harness] = []

    def make(*scripts: dict[str, Any]) -> Harness:
        made.append(Harness(slack, tmp_path, list(scripts)))
        return made[-1]

    yield make
    for harness in made:
        await harness.manager.close_all()


async def test_a_turn_streams_in_its_thread_and_records_the_session(
    harness_for: Callable[..., Harness],
) -> None:
    turn_messages = sdk_messages("tools")
    h = harness_for({"turns": [turn_messages]})
    turn = await h.session().submit("list the files", "111.222")
    await asyncio.wait_for(turn.done.wait(), 2)
    assert h.clients[0].queries == ["list the files"]
    assert h.slack.calls_to("chat.startStream")[0]["thread_ts"] == "111.222"
    result = turn_messages[-1]
    assert isinstance(result, ResultMessage)
    assert h.state.get(CHANNEL).session_id == result.session_id
    assert h.slack.calls_to("chat.stopStream")[0]["blocks"][0]["type"] == "context"


async def test_the_client_is_launched_as_the_design_says(
    harness_for: Callable[..., Harness], tmp_path: Path
) -> None:
    h = harness_for({})
    await h.session().ensure_connected()
    options = h.clients[0].options
    assert options.cwd == str(tmp_path)
    assert options.resume is None
    assert options.setting_sources == ["user", "project", "local"]
    assert options.include_partial_messages is True
    assert options.extra_args == {"allow-dangerously-skip-permissions": None}
    assert options.can_use_tool is not None
    assert options.cli_path is None


async def test_a_restart_resumes_the_stored_session(harness_for: Callable[..., Harness]) -> None:
    h = harness_for({})
    h.state.set_session(CHANNEL, "stored-session")
    await h.session().ensure_connected()
    assert h.clients[0].options.resume == "stored-session"


async def test_clear_records_the_new_session_id(harness_for: Callable[..., Harness]) -> None:
    first, second = split_turns(sdk_messages("clear"))
    h = harness_for({"turns": [first, second]})
    session = h.session()
    await asyncio.wait_for((await session.submit("hi", "1.1")).done.wait(), 2)
    await asyncio.wait_for((await session.submit("/clear", "2.2")).done.wait(), 2)
    assert isinstance(second[-1], ResultMessage)
    assert h.state.get(CHANNEL).session_id == second[-1].session_id


async def test_stale_session_starts_fresh_and_says_so(harness_for: Callable[..., Harness]) -> None:
    gone = ResultError(
        "Claude Code returned an error result: No conversation found",
        data={
            "subtype": "error_during_execution",
            "is_error": True,
            "errors": ["No conversation found with session ID: gone"],
        },
    )
    h = harness_for({"connect_error": gone}, {"turns": [sdk_messages("tools")]})
    h.state.set_session(CHANNEL, "gone")
    turn = await h.session().submit("hello", "1.1")
    await asyncio.wait_for(turn.done.wait(), 2)
    assert [c.options.resume for c in h.clients] == ["gone", None]
    assert "could not be resumed" in h.streamed_text()
    assert h.state.get(CHANNEL).session_id not in (None, "gone")


async def test_bypass_is_memory_only(harness_for: Callable[..., Harness], tmp_path: Path) -> None:
    h = harness_for({}, {})
    session = h.session()
    await session.set_bypass(True)
    assert h.clients[0].modes == ["bypassPermissions"] and session.bypass
    stored = json.loads((tmp_path / "state.json").read_text())["channels"][CHANNEL]
    assert set(stored) == {"directory", "session_id"}
    await session.set_bypass(False)
    assert h.clients[0].modes[-1] == "default"
    restarted = SessionManager(h.deps).get(CHANNEL)
    assert restarted is not None and restarted.bypass is False


async def test_queue_waits_while_approval_pending(harness_for: Callable[..., Harness]) -> None:
    ask = CanUseToolCall("Bash", {"command": "ls"})
    h = harness_for({"turns": [[ask, *sdk_messages("tools")], sdk_messages("tools")]})
    session = h.session()
    first = await session.submit("first", "1.1")
    second = await session.submit("second", "2.2")
    await until(lambda: any("blocks" in a for a in h.slack.calls_to("chat.postMessage")))
    await asyncio.sleep(0.05)
    assert h.clients[0].queries == ["first"] and session.busy
    approval_id = next(iter(h.approvals._pending))
    assert h.approvals.resolve(approval_id, CHANNEL, Approve()) is not None
    await asyncio.wait_for(second.done.wait(), 2)
    assert first.done.is_set()
    assert h.clients[0].queries == ["first", "second"]
    assert isinstance(h.clients[0].permission_results[0], PermissionResultAllow)


async def test_stop_denies_pending_and_interrupts(harness_for: Callable[..., Harness]) -> None:
    h = harness_for(
        {
            "turns": [
                [CanUseToolCall("Bash", {"command": "rm -rf build"}), *sdk_messages("interrupt")]
            ]
        }
    )
    session = h.session()
    turn = await session.submit("clean", "1.1")
    await until(lambda: bool(h.approvals._pending))
    assert await session.stop() is True
    await asyncio.wait_for(turn.done.wait(), 2)
    assert h.clients[0].interrupts == 1
    assert isinstance(h.clients[0].permission_results[0], PermissionResultDeny)
    assert await session.stop() is False


async def test_ask_user_question_returns_answers(harness_for: Callable[..., Harness]) -> None:
    recorded = sdk_json("ask-can-use-tool")
    call = CanUseToolCall(recorded["tool_name"], recorded["input"])
    h = harness_for({"turns": [[call, *sdk_messages("tools")]]})
    turn = await h.session().submit("ask me", "1.1")
    await until(lambda: bool(h.approvals._pending))
    approval_id = next(iter(h.approvals._pending))
    answers = {q["question"]: q["options"][0]["label"] for q in recorded["input"]["questions"]}
    h.approvals.resolve(approval_id, CHANNEL, Answer(answers))
    await asyncio.wait_for(turn.done.wait(), 2)
    result = h.clients[0].permission_results[0]
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == {"questions": recorded["input"]["questions"], "answers": answers}


async def test_injected_turn_opens_its_own_thread(harness_for: Callable[..., Harness]) -> None:
    turns = split_turns(sdk_messages("background"))
    h = harness_for({"turns": [turns[0]]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it", "1.1")).done.wait(), 2)
    for later in turns[1:]:
        h.clients[0].inject(later)
    await until(
        lambda: any(
            a.get("text") == texts.BACKGROUND_ROOT for a in h.slack.calls_to("chat.postMessage")
        )
    )
    threads = {a["thread_ts"] for a in h.slack.calls_to("chat.startStream")}
    assert "1.1" in threads and len(threads) >= 2


async def test_rate_limit_event_invalidates_usage(harness_for: Callable[..., Harness]) -> None:
    events = [m for m in sdk_messages("tools") if isinstance(m, RateLimitEvent)]
    assert events, "the spec measured one RateLimitEvent on a client's first turn"
    h = harness_for({"turns": [sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("a", "1.1")).done.wait(), 2)
    await until(lambda: h.usage_fetches == 1)
    await h.deps.usage.refresh_if_stale()
    assert h.usage_fetches == 1  # still fresh
    h.clients[0].inject([events[0]])
    await asyncio.sleep(0.05)
    await h.deps.usage.refresh_if_stale()
    assert h.usage_fetches == 2


async def test_logs_hold_no_message_content(
    harness_for: Callable[..., Harness], caplog: pytest.LogCaptureFixture
) -> None:
    h = harness_for({"turns": [sdk_messages("tools")]})
    with caplog.at_level(logging.DEBUG, logger="code_with_slack"):
        turn = await h.session().submit("SECRET-PROMPT-CONTENT", "1.1")
        await asyncio.wait_for(turn.done.wait(), 2)
    assert "SECRET-PROMPT-CONTENT" not in caplog.text


async def test_rebinding_closes_the_client_and_forgets_the_session(
    harness_for: Callable[..., Harness], tmp_path: Path
) -> None:
    h = harness_for({}, {})
    h.state.set_session(CHANNEL, "old")
    await h.session().ensure_connected()
    other = tmp_path / "other"
    other.mkdir()
    await h.manager.bind(CHANNEL, other)
    assert h.clients[0].connected is False
    assert h.state.get(CHANNEL).session_id is None
    assert h.session().directory == other


def test_resolve_directory(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "app").mkdir(parents=True)
    (root / "file.txt").write_text("x")
    (tmp_path / "outside").mkdir()
    (root / "escape").symlink_to(tmp_path / "outside")
    assert resolve_directory(str(root / "app"), root) == (root / "app").resolve()
    assert resolve_directory(str(root), root) == root.resolve()
    assert resolve_directory(str(root / "app" / ".." / ".." / "outside"), root) is None
    assert resolve_directory(str(root / "escape"), root) is None
    assert resolve_directory(str(root / "file.txt"), root) is None
    assert resolve_directory(str(root / "missing"), root) is None


def split_background() -> tuple[list[Any], list[Any], list[Any]]:
    """The recorded background run: the owner's turn, the notification that arrives while idle,
    and the turn the CLI injects to report it."""
    first, later = split_turns(sdk_messages("background"))[:2]
    start = next(
        i
        for i, m in enumerate(later)
        if isinstance(m, SystemMessage)
        and m.subtype == "init"
        and not isinstance(m, TaskNotificationMessage)
    )
    return first, later[:start], later[start:]


async def test_owner_query_waits_for_an_expected_background_turn(
    harness_for: Callable[..., Harness],
) -> None:
    first, notice, injected = split_background()
    assert any(isinstance(m, TaskNotificationMessage) for m in notice)
    h = harness_for({"turns": [first, sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it", "1.1")).done.wait(), 2)
    h.clients[0].inject(notice)
    await asyncio.sleep(0.05)
    second = await session.submit("next", "2.2")
    await asyncio.sleep(0.05)
    assert h.clients[0].queries == ["start it"]
    h.clients[0].inject(injected)
    await asyncio.wait_for(second.done.wait(), 2)
    assert h.clients[0].queries == ["start it", "next"]
    roots = [
        a for a in h.slack.calls_to("chat.postMessage") if a.get("text") == texts.BACKGROUND_ROOT
    ]
    assert len(roots) == 1
    threads = [a["thread_ts"] for a in h.slack.calls_to("chat.startStream")]
    assert threads[0] == "1.1" and threads[-1] == "2.2" and len(threads) == 3


async def test_a_notification_with_no_turn_is_shown_and_releases_the_queue(
    harness_for: Callable[..., Harness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.1)
    first, notice, _ = split_background()
    h = harness_for({"turns": [first, sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it", "1.1")).done.wait(), 2)
    h.clients[0].inject(notice)
    await asyncio.sleep(0.05)
    second = await session.submit("next", "2.2")
    await asyncio.wait_for(second.done.wait(), 2)
    assert any(a.get("text") == texts.BACKGROUND_ROOT for a in h.slack.calls_to("chat.postMessage"))
    assert h.clients[0].queries == ["start it", "next"]


async def test_a_query_crossing_a_notification_never_hangs(
    harness_for: Callable[..., Harness],
) -> None:
    first, notice, injected = split_background()
    h = harness_for({"turns": [first, sdk_messages("tools"), sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it", "1.1")).done.wait(), 2)
    h.clients[0].inject(notice)
    second = await session.submit("next", "2.2")
    await asyncio.wait_for(second.done.wait(), 2)
    h.clients[0].inject(injected)
    third = await session.submit("after", "3.3")
    await asyncio.wait_for(third.done.wait(), 2)
    assert h.clients[0].queries == ["start it", "next", "after"]


async def test_a_slack_network_error_does_not_stop_the_session(
    harness_for: Callable[..., Harness],
) -> None:
    import aiohttp

    h = harness_for({"turns": [sdk_messages("tools"), sdk_messages("tools")]})
    down = aiohttp.ClientConnectionError("network down")
    for method in ("chat.startStream", "chat.appendStream", "chat.stopStream", "chat.postMessage"):
        h.slack.responses[method] = down
    session = h.session()
    await asyncio.wait_for((await session.submit("while offline", "1.1")).done.wait(), 2)
    assert h.clients[0].connected
    h.slack.responses = FakeSlack().responses
    await asyncio.wait_for((await session.submit("back online", "2.2")).done.wait(), 2)
    assert h.clients[0].queries == ["while offline", "back online"] and len(h.clients) == 1


async def test_a_cli_that_exits_ends_the_turn_and_the_next_message_reconnects(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for(
        {"turns": [[*sdk_messages("tools")[:3], EndOfStream()]]}, {"turns": [sdk_messages("tools")]}
    )
    session = h.session()
    await asyncio.wait_for((await session.submit("first", "1.1")).done.wait(), 2)
    await asyncio.wait_for((await session.submit("second", "2.2")).done.wait(), 2)
    assert [c.queries for c in h.clients] == [["first"], ["second"]]


async def test_a_failed_background_post_still_releases_the_queue(
    harness_for: Callable[..., Harness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aiohttp

    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.05)
    first, notice, _ = split_background()
    h = harness_for({"turns": [first, sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it", "1.1")).done.wait(), 2)
    h.slack.responses["chat.postMessage"] = aiohttp.ClientConnectionError("network down")
    h.clients[0].inject(notice)
    await asyncio.sleep(0.02)
    second = await session.submit("next", "2.2")
    await asyncio.wait_for(second.done.wait(), 2)
    assert h.clients[0].queries == ["start it", "next"]


async def test_a_missing_directory_asks_to_bind_again(
    harness_for: Callable[..., Harness], tmp_path: Path
) -> None:
    gone = tmp_path / "gone"
    gone.mkdir()
    h = harness_for({})
    h.state.bind(CHANNEL, gone)
    gone.rmdir()
    turn = await h.session().submit("hello", "1.1")
    await asyncio.wait_for(turn.done.wait(), 2)
    assert h.clients == []
    posted = [a["text"] for a in h.slack.calls_to("chat.postMessage")]
    assert posted == [texts.DIRECTORY_MISSING.format(directory=gone)]
