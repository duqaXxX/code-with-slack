import asyncio
import dataclasses
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
    TaskStartedMessage,
    TaskUpdatedMessage,
)

from code_with_slack import sessions, texts
from code_with_slack.approvals import Answer, Approvals, Approve
from code_with_slack.footer import UsageCache
from code_with_slack.guards import Identity
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

    def replies(self) -> list[str]:
        """The text every posted message ends up showing, in the order they were posted."""
        return self.slack.message_texts()

    def written_text(self) -> str:
        return "\n".join(
            b["text"]
            for m in ("chat.postMessage", "chat.update")
            for a in self.slack.calls_to(m)
            for b in a.get("blocks") or []
            if b.get("type") == "markdown"
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


async def test_a_turn_replies_in_the_main_window_and_records_the_session(
    harness_for: Callable[..., Harness],
) -> None:
    turn_messages = sdk_messages("tools")
    h = harness_for({"turns": [turn_messages]})
    turn = await h.session().submit("list the files")
    await asyncio.wait_for(turn.done.wait(), 2)
    assert h.clients[0].queries == ["list the files"]
    assert all(a.get("thread_ts") is None for a in h.slack.calls_to("chat.postMessage"))
    result = turn_messages[-1]
    assert isinstance(result, ResultMessage)
    assert h.state.get(CHANNEL).session_id == result.session_id
    last = [a for m, a in h.slack.calls if m in ("chat.postMessage", "chat.update")][-1]
    assert last["blocks"][-1]["type"] == "context"  # the footer closes the reply


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
    await asyncio.wait_for((await session.submit("hi")).done.wait(), 2)
    await asyncio.wait_for((await session.submit("/clear")).done.wait(), 2)
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
    turn = await h.session().submit("hello")
    await asyncio.wait_for(turn.done.wait(), 2)
    assert [c.options.resume for c in h.clients] == ["gone", None]
    assert "could not be resumed" in h.written_text()
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
    first = await session.submit("first")
    second = await session.submit("second")
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
    turn = await session.submit("clean")
    await until(lambda: bool(h.approvals._pending))
    assert await session.stop() is True
    await asyncio.wait_for(turn.done.wait(), 2)
    assert h.clients[0].interrupts == 1
    assert isinstance(h.clients[0].permission_results[0], PermissionResultDeny)
    assert len(h.slack.calls_to("chat.delete")) == 1
    assert await session.stop() is False


async def test_ask_user_question_returns_answers(harness_for: Callable[..., Harness]) -> None:
    recorded = sdk_json("ask-can-use-tool")
    call = CanUseToolCall(recorded["tool_name"], recorded["input"])
    h = harness_for({"turns": [[call, *sdk_messages("tools")]]})
    turn = await h.session().submit("ask me")
    await until(lambda: bool(h.approvals._pending))
    approval_id = next(iter(h.approvals._pending))
    answers = {q["question"]: q["options"][0]["label"] for q in recorded["input"]["questions"]}
    h.approvals.resolve(approval_id, CHANNEL, Answer(answers))
    await asyncio.wait_for(turn.done.wait(), 2)
    result = h.clients[0].permission_results[0]
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == {"questions": recorded["input"]["questions"], "answers": answers}


async def test_injected_turn_gets_its_own_reply(harness_for: Callable[..., Harness]) -> None:
    turns = split_turns(sdk_messages("background"))
    h = harness_for({"turns": [turns[0]]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    for later in turns[1:]:
        h.clients[0].inject(later)
    await until(lambda: any(texts.BACKGROUND_NOTICE in r for r in h.replies()))
    assert texts.BACKGROUND_NOTICE not in h.replies()[0]


async def test_rate_limit_event_invalidates_usage(harness_for: Callable[..., Harness]) -> None:
    events = [m for m in sdk_messages("tools") if isinstance(m, RateLimitEvent)]
    assert events, "the spec measured one RateLimitEvent on a client's first turn"
    h = harness_for({"turns": [sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("a")).done.wait(), 2)
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
        turn = await h.session().submit("SECRET-PROMPT-CONTENT")
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


async def test_rebinding_ends_the_waiting_replies_and_says_why(
    harness_for: Callable[..., Harness], tmp_path: Path
) -> None:
    h = harness_for({})  # no scripted turn: the first query never answers
    session = h.session()
    first = await session.submit("one")
    second = await session.submit("two")
    await until(lambda: h.clients and h.clients[0].queries == ["one"])
    other = tmp_path / "other"
    other.mkdir()
    await h.manager.bind(CHANNEL, other)
    await asyncio.wait_for(asyncio.gather(first.done.wait(), second.done.wait()), 2)
    ended = texts.ENDED.format(reason=texts.ENDED_REBOUND)
    assert all(ended in r for r in h.replies())
    shown = [blocks[-1] for blocks in h.slack.message_blocks()]
    assert all(texts.WRITING not in str(b) and texts.WAITING not in str(b) for b in shown)


async def test_a_message_during_a_bind_gets_the_new_directory(
    harness_for: Callable[..., Harness], tmp_path: Path
) -> None:
    h = harness_for({}, {})
    await h.session().ensure_connected()
    other = tmp_path / "other"
    other.mkdir()
    binding = asyncio.create_task(h.manager.bind(CHANNEL, other))
    await asyncio.sleep(0)  # the bind is closing the old session
    during = h.manager.get(CHANNEL)
    await binding
    assert during is not None and during.directory == other
    assert h.manager.get(CHANNEL) is during


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
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    h.clients[0].inject(notice)
    await asyncio.sleep(0.05)
    second = await session.submit("next")
    await asyncio.sleep(0.05)
    assert h.clients[0].queries == ["start it"]
    h.clients[0].inject(injected)
    await asyncio.wait_for(second.done.wait(), 2)
    assert h.clients[0].queries == ["start it", "next"]
    background = [texts.BACKGROUND_NOTICE in r for r in h.replies()]
    # The second reply appeared (waiting) when it was sent, before the background report began.
    assert background == [False, False, True]


def running_block(blocks: list[dict[str, Any]]) -> str | None:
    """The running list a message shows, if any."""
    running = texts.RUNNING.split("{")[0]
    for block in blocks:
        if block.get("type") == "context":
            text = str(block["elements"][0]["text"])
            if text.startswith(running):
                return text
    return None


async def test_the_running_list_follows_the_latest_reply(
    harness_for: Callable[..., Harness],
) -> None:
    first, notice, injected = split_background()
    h = harness_for({"turns": [first, sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    shown = h.slack.message_blocks()
    assert running_block(shown[0]) is not None
    await asyncio.wait_for((await session.submit("next")).done.wait(), 2)
    shown = h.slack.message_blocks()
    assert running_block(shown[0]) is None  # moved to the latest reply
    assert running_block(shown[1]) is not None
    h.clients[0].inject(notice + injected)
    await until(lambda: any(texts.BACKGROUND_NOTICE in r for r in h.replies()))
    await asyncio.sleep(0.05)
    assert all(running_block(blocks) is None for blocks in h.slack.message_blocks())
    assert h.replies()[0].startswith("✓")  # the line where the task started


async def test_a_background_task_frame_stays_out_of_other_replies(
    harness_for: Callable[..., Harness],
) -> None:
    first, notice, injected = split_background()
    h = harness_for({"turns": [first]})
    await asyncio.wait_for((await h.session().submit("start it")).done.wait(), 2)
    h.clients[0].inject(notice + injected)
    await until(lambda: any(texts.BACKGROUND_NOTICE in r for r in h.replies()))
    await asyncio.sleep(0.05)
    report = next(r for r in h.replies() if texts.BACKGROUND_NOTICE in r)
    task_id = next(m.task_id for m in notice if isinstance(m, TaskNotificationMessage))
    assert task_id not in report


async def test_a_background_agent_s_calls_update_its_line_and_open_no_reply(
    harness_for: Callable[..., Harness],
) -> None:
    recorded = sdk_messages("subagent")
    turn = [m for m in recorded if getattr(m, "parent_tool_use_id", None) is None]
    children = [m for m in recorded if getattr(m, "parent_tool_use_id", None) is not None]
    h = harness_for({"turns": [turn]})
    await asyncio.wait_for((await h.session().submit("start it")).done.wait(), 2)
    posted = len(h.slack.posted_ts)
    h.clients[0].inject(children)
    await asyncio.sleep(0.1)
    assert len(h.slack.posted_ts) == posted
    assert "Bash" in h.replies()[0].splitlines()[0]


async def test_ended_tasks_are_forgotten_past_the_limit(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "TASK_REPLIES_KEPT", 1)
    first, notice, injected = split_background()
    renamed = [
        dataclasses.replace(m, task_id="other")
        if isinstance(m, TaskStartedMessage | TaskNotificationMessage | TaskUpdatedMessage)
        else m
        for m in first
    ]
    h = harness_for({"turns": [first, renamed]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    h.clients[0].inject(notice + injected)
    await until(lambda: any(texts.BACKGROUND_NOTICE in r for r in h.replies()))
    await asyncio.wait_for((await session.submit("again")).done.wait(), 2)
    assert list(session._task_replies) == ["other"]


async def test_a_failed_background_task_shows_why_in_the_reply_that_started_it(
    harness_for: Callable[..., Harness],
) -> None:
    first, notice, injected = split_background()
    # The recorded order: a terminal task_updated (no summary), then task_notification (summary).
    failed = [
        dataclasses.replace(m, status="failed")
        if isinstance(m, TaskNotificationMessage | TaskUpdatedMessage)
        else m
        for m in notice
    ]
    summary = next(m.summary for m in notice if isinstance(m, TaskNotificationMessage))
    h = harness_for({"turns": [first]})
    await asyncio.wait_for((await h.session().submit("start it")).done.wait(), 2)
    h.clients[0].inject(failed + injected)
    await until(lambda: any(texts.BACKGROUND_NOTICE in r for r in h.replies()))
    started = h.replies()[0]
    assert "✗" in started and " ".join(summary.split())[:40] in started


async def test_closing_the_session_stops_the_lines_of_running_tasks(
    harness_for: Callable[..., Harness],
) -> None:
    first, _, _ = split_background()
    h = harness_for({"turns": [first]})
    await asyncio.wait_for((await h.session().submit("start it")).done.wait(), 2)
    await h.manager.close_all()
    started = h.slack.message_blocks()[0]
    assert running_block(started) is None
    assert "Stopped" in started[0]["text"]


async def test_a_notification_with_no_turn_updates_its_line_and_releases_the_queue(
    harness_for: Callable[..., Harness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.1)
    first, notice, _ = split_background()
    h = harness_for({"turns": [first, sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    h.clients[0].inject(notice)
    await asyncio.sleep(0.05)
    second = await session.submit("next")
    await asyncio.wait_for(second.done.wait(), 2)
    # The task is known: its end shows on the line where it started, not in a post of its own.
    assert h.replies()[0].startswith("✓")
    assert not any(texts.BACKGROUND_NOTICE in r for r in h.replies())
    assert h.clients[0].queries == ["start it", "next"]


async def test_a_query_crossing_a_notification_never_hangs(
    harness_for: Callable[..., Harness],
) -> None:
    first, notice, injected = split_background()
    h = harness_for({"turns": [first, sdk_messages("tools"), sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    h.clients[0].inject(notice)
    second = await session.submit("next")
    await asyncio.wait_for(second.done.wait(), 2)
    h.clients[0].inject(injected)
    third = await session.submit("after")
    await asyncio.wait_for(third.done.wait(), 2)
    assert h.clients[0].queries == ["start it", "next", "after"]


async def test_a_notification_after_the_owner_query_was_sent_leaves_the_turn_to_the_owner(
    harness_for: Callable[..., Harness],
) -> None:
    # Measured 2026-09-24: the owner's prompt reached the CLI queue first, then the task ended;
    # the CLI reported the task inside the owner's turn and started no turn of its own.
    first, notice, _ = split_background()
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    second = await session.submit("next")
    await until(lambda: h.clients[0].queries == ["start it", "next"])
    h.clients[0].inject(notice)
    await asyncio.sleep(0.05)
    h.clients[0].inject(sdk_messages("tools"))
    await asyncio.wait_for(second.done.wait(), 2)
    replies = h.replies()
    assert texts.REPLY_ABOVE not in replies[1]
    assert not any(texts.BACKGROUND_NOTICE in r for r in replies)


async def test_a_slack_network_error_does_not_stop_the_session(
    harness_for: Callable[..., Harness],
) -> None:
    import aiohttp

    h = harness_for({"turns": [sdk_messages("tools"), sdk_messages("tools")]})
    down = aiohttp.ClientConnectionError("network down")
    for method in ("chat.postMessage", "chat.update"):
        h.slack.responses[method] = down
    session = h.session()
    await asyncio.wait_for((await session.submit("while offline")).done.wait(), 2)
    assert h.clients[0].connected
    h.slack.responses = FakeSlack().responses
    await asyncio.wait_for((await session.submit("back online")).done.wait(), 2)
    assert h.clients[0].queries == ["while offline", "back online"] and len(h.clients) == 1


async def test_a_cli_that_exits_ends_the_turn_and_the_next_message_reconnects(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for(
        {"turns": [[*sdk_messages("tools")[:3], EndOfStream()]]}, {"turns": [sdk_messages("tools")]}
    )
    session = h.session()
    await asyncio.wait_for((await session.submit("first")).done.wait(), 2)
    await asyncio.wait_for((await session.submit("second")).done.wait(), 2)
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
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    h.slack.responses["chat.postMessage"] = aiohttp.ClientConnectionError("network down")
    h.clients[0].inject(notice)
    await asyncio.sleep(0.02)
    second = await session.submit("next")
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
    turn = await h.session().submit("hello")
    await asyncio.wait_for(turn.done.wait(), 2)
    assert h.clients == []
    assert h.replies() == [texts.DIRECTORY_MISSING.format(directory=gone)]


async def test_an_unreadable_directory_says_how_to_grant_access(
    harness_for: Callable[..., Harness], tmp_path: Path
) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    h = harness_for({})
    h.state.bind(CHANNEL, locked)
    locked.chmod(0)
    try:
        turn = await h.session().submit("hello")
        await asyncio.wait_for(turn.done.wait(), 2)
    finally:
        locked.chmod(0o755)
    assert h.clients == []
    assert h.replies() == [texts.DIRECTORY_UNREADABLE.format(directory=locked)]


def statuses(h: Harness) -> list[str]:
    """The status line (last context block) of every write, in order."""
    return [
        a["blocks"][-1]["elements"][0]["text"]
        for m, a in h.slack.calls
        if m in ("chat.postMessage", "chat.update")
        and a.get("blocks")
        and a["blocks"][-1]["type"] == "context"
    ]


async def test_a_reply_shows_that_claude_is_writing_right_away(
    harness_for: Callable[..., Harness],
) -> None:
    ask = CanUseToolCall("Bash", {"command": "ls"})
    h = harness_for({"turns": [[ask, *sdk_messages("tools")], sdk_messages("tools")]})
    session = h.session()
    first = await session.submit("first")
    second = await session.submit("second")
    await until(lambda: bool(h.approvals._pending))
    assert statuses(h)[:2] == [texts.WRITING, texts.WAITING]
    h.approvals.resolve(next(iter(h.approvals._pending)), CHANNEL, Approve())
    await asyncio.wait_for(second.done.wait(), 2)
    assert first.done.is_set()
    replies = [
        a
        for a in h.slack.calls_to("chat.postMessage")
        if a.get("blocks", [{}])[0].get("type") != "section"
    ]
    assert len(replies) == 2  # one message per reply, the placeholder becomes the reply


async def test_the_footer_follows_an_effort_set_from_slack(
    harness_for: Callable[..., Harness],
) -> None:
    import dataclasses

    turn = sdk_messages("usage")
    result = turn[-1]
    assert isinstance(result, ResultMessage)
    effort_turn = [
        *turn[:-1],
        dataclasses.replace(
            result, result="Set effort level to high (this session only): Comprehensive"
        ),
    ]
    h = harness_for({"turns": [effort_turn, sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("/effort high")).done.wait(), 2)
    await asyncio.wait_for((await session.submit("next")).done.wait(), 2)
    assert "effort high" in statuses(h)[-1]
