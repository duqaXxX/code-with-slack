import asyncio
import dataclasses
import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, Message, RateLimitEvent, ResultError, ResultMessage
from claude_agent_sdk._internal.message_parser import parse_message
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
    StopHook,
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

        async def trusted(directory: Path) -> bool:
            return True

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
            workspace_trusted=trusted,
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


async def test_bypass_survives_a_restart(
    harness_for: Callable[..., Harness], tmp_path: Path
) -> None:
    h = harness_for({}, {})
    session = h.session()
    await session.set_bypass(True)
    assert h.clients[0].modes == ["bypassPermissions"] and session.bypass
    assert json.loads((tmp_path / "state.json").read_text())["channels"][CHANNEL]["bypass"]
    # A new daemon: state.json read again, a new manager, a new Claude Code process.
    reloaded = dataclasses.replace(h.deps, state=StateStore(tmp_path / "state.json"))
    restarted = SessionManager(reloaded).get(CHANNEL)
    assert restarted is not None and restarted.bypass
    await restarted.ensure_connected()
    assert h.clients[1].modes == ["bypassPermissions"]
    await restarted.set_bypass(False)
    assert h.clients[1].modes[-1] == "default"
    assert not StateStore(tmp_path / "state.json").get(CHANNEL).bypass
    await restarted.close()


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
    await until(lambda: any(is_report(r) for r in h.replies()))
    assert not is_report(h.replies()[0])


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


# A report opens with Claude Code's notification summary (recorded: `Background command "..."
# completed (exit code 0)`, `Agent "..." finished`), never with a tool line.
REPORT = re.compile(r'^[✅❌] (Background command|Agent) "', re.MULTILINE)


def is_report(reply: str) -> bool:
    """A reply of Claude Code's own turn about a background task (not the owner's)."""
    return bool(REPORT.search(reply)) or texts.BACKGROUND_NOTICE in reply


async def test_a_report_opens_with_claude_code_s_summary_and_takes_the_footer(
    harness_for: Callable[..., Harness],
) -> None:
    first, notice, injected = split_background()
    h = harness_for({"turns": [first]})
    await asyncio.wait_for((await h.session().submit("start it")).done.wait(), 2)
    h.clients[0].inject(notice + injected)
    await until(lambda: len(h.replies()) == 2 and bool(h.replies()[1]))
    await asyncio.sleep(0.05)
    report = h.replies()[1]
    summary = next(m.summary for m in notice if isinstance(m, TaskNotificationMessage))
    assert report.startswith(f"✅ {summary}")  # Claude Code's own words, as in the terminal
    assert texts.BACKGROUND_NOTICE not in report
    shown = h.slack.message_blocks()
    assert {"type": "divider"} in shown[1] and {"type": "divider"} not in shown[0]  # latest only


async def test_an_owner_answer_behind_a_wrong_guess_keeps_its_footer(
    harness_for: Callable[..., Harness],
) -> None:
    first, notice, _ = split_background()
    h = harness_for({"turns": [first]})
    await asyncio.wait_for((await h.session().submit("start it")).done.wait(), 2)
    h.clients[0].inject(notice)  # the session now expects Claude Code's own turn
    await asyncio.sleep(0.05)
    h.clients[0].inject(sdk_messages("tools"))  # but the result says a person asked for it
    await until(lambda: len(h.slack.message_blocks()) == 2)
    await asyncio.sleep(0.1)
    assert {"type": "divider"} in h.slack.message_blocks()[1]


async def test_a_task_type_is_forgotten_when_the_task_ends(
    harness_for: Callable[..., Harness],
) -> None:
    first, notice, injected = split_background()
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    assert session._tasks
    h.clients[0].inject(notice + injected)
    await until(lambda: len(h.replies()) == 2)
    assert session._tasks == {}


# A background agent's end, as the SDK delivered it (recorded 2026-09-24 by
# actions/scripts/2026-09-24-task-notification-summary-probe.py): its summary is the agent's
# result, not a status line.
def agent_end(task_id: str, tool_use_id: str) -> Message:
    return parse_message(
        {
            "type": "system",
            "subtype": "task_notification",
            "task_id": task_id,
            "tool_use_id": tool_use_id,
            "status": "completed",
            "output_file": "/home/dev/tasks/out",
            "summary": "| File | Lines |\n|---|---|\n| README.md | 3 |",
            "usage": {"total_tokens": 11181, "tool_uses": 0, "duration_ms": 10400},
            "session_id": "00000000-0000-0000-0000-000000000001",
            "uuid": "00000000-0000-0000-0000-000000000002",
        }
    )


async def test_a_background_agent_s_end_reads_as_in_the_terminal(
    harness_for: Callable[..., Harness],
) -> None:
    first = split_turns(sdk_messages("subagent"))[0]
    started = next(m for m in first if isinstance(m, TaskStartedMessage))
    h = harness_for({"turns": [first]})
    await asyncio.wait_for((await h.session().submit("start it")).done.wait(), 2)
    assert started.tool_use_id is not None
    h.clients[0].inject([agent_end(started.task_id, started.tool_use_id), *sdk_messages("tools")])
    await until(lambda: len(h.replies()) == 2 and bool(h.replies()[1]))
    await asyncio.sleep(0.05)
    assert h.replies()[1].startswith(f'✅ Agent "{started.description}" finished · 10s')
    assert "README.md" not in h.replies()[1].splitlines()[0]


async def test_the_task_record_is_bounded(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "TASKS_KEPT", 1)
    first, _, _ = split_background()
    renamed = [
        dataclasses.replace(m, task_id="other") if isinstance(m, TaskStartedMessage) else m
        for m in first
    ]
    h = harness_for({"turns": [first, renamed]})
    session = h.session()
    await asyncio.wait_for((await session.submit("one")).done.wait(), 2)
    await asyncio.wait_for((await session.submit("two")).done.wait(), 2)
    assert list(session._tasks) == ["other"]


async def test_a_report_line_never_outlives_its_chance(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.05)
    first, notice, _ = split_background()
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    h.clients[0].inject(notice)  # and Claude Code starts no turn of its own
    await asyncio.sleep(0.2)
    assert session._ended == []


async def test_the_task_bookkeeping_goes_with_the_process(
    harness_for: Callable[..., Harness],
) -> None:
    first, _, _ = split_background()
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    assert session._tasks
    h.clients[0].inject([EndOfStream()])
    await until(lambda: session._client is None)
    assert session._tasks == {} and session._ended == []


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
    background = [is_report(r) for r in h.replies()]
    # The second reply appeared (waiting) when it was sent, before the background report began.
    assert background == [False, False, True]


def running_block(blocks: list[dict[str, Any]]) -> str | None:
    """The running counts a message's footer shows, if any."""
    blocks = [b for b in blocks if not str(b.get("block_id", "")).startswith("spacer")]
    footer = blocks[-1] if blocks and blocks[-1].get("type") == "context" else None
    text = str(footer["elements"][0]["text"]) if footer else ""
    return text[text.index("⏳") :] if "⏳" in text else None


async def test_running_counts_follow_the_latest_reply(
    harness_for: Callable[..., Harness],
) -> None:
    first, notice, injected = split_background()
    h = harness_for({"turns": [first, sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    shown = h.slack.message_blocks()
    assert running_block(shown[0]) == "⏳ 1 shell"  # task_type local_bash in the recording
    await asyncio.wait_for((await session.submit("next")).done.wait(), 2)
    shown = h.slack.message_blocks()
    assert running_block(shown[0]) is None  # moved to the latest reply
    assert running_block(shown[1]) is not None
    h.clients[0].inject(notice + injected)
    await until(lambda: any(is_report(r) for r in h.replies()))
    await asyncio.sleep(0.05)
    assert all(running_block(blocks) is None for blocks in h.slack.message_blocks())
    assert h.replies()[0].startswith("✅")  # the line where the task started


async def test_a_background_task_frame_stays_out_of_other_replies(
    harness_for: Callable[..., Harness],
) -> None:
    first, notice, injected = split_background()
    h = harness_for({"turns": [first]})
    await asyncio.wait_for((await h.session().submit("start it")).done.wait(), 2)
    h.clients[0].inject(notice + injected)
    await until(lambda: any(is_report(r) for r in h.replies()))
    await asyncio.sleep(0.05)
    report = next(r for r in h.replies() if is_report(r))
    task_id = next(m.task_id for m in notice if isinstance(m, TaskNotificationMessage))
    assert task_id not in report


async def test_a_background_agent_s_calls_update_its_line_and_open_no_reply(
    harness_for: Callable[..., Harness],
) -> None:
    recorded = sdk_messages("subagent")
    turn = [m for m in split_turns(recorded)[0] if getattr(m, "parent_tool_use_id", None) is None]
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
    await until(lambda: any(is_report(r) for r in h.replies()))
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
    await until(lambda: any(is_report(r) for r in h.replies()))
    started = h.replies()[0]
    assert "❌" in started and " ".join(summary.split())[:40] in started


async def test_closing_the_session_stops_the_lines_of_running_tasks(
    harness_for: Callable[..., Harness],
) -> None:
    first, _, _ = split_background()
    h = harness_for({"turns": [first]})
    await asyncio.wait_for((await h.session().submit("start it")).done.wait(), 2)
    await h.manager.close_all()
    assert running_block(h.slack.message_blocks()[0]) is None
    assert "Stopped" in h.replies()[0]


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
    assert h.replies()[0].startswith("✅")
    assert not any(is_report(r) for r in h.replies())
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
    assert not any(is_report(r) for r in replies)


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
    """The status line or footer (last context block but the spacer) of every write, in order."""
    lasts = []
    for m, a in h.slack.calls:
        blocks = [
            b for b in a.get("blocks") or [] if not str(b.get("block_id", "")).startswith("spacer")
        ]
        if m in ("chat.postMessage", "chat.update") and blocks and blocks[-1]["type"] == "context":
            lasts.append(blocks[-1]["elements"][0]["text"])
    return lasts


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


def with_stop_hook(turn: list[Message], hook_input: dict[str, Any]) -> list[Any]:
    """A recorded turn with the CLI's Stop hook call where the CLI makes it: before the result."""
    return [*turn[:-1], StopHook(hook_input), turn[-1]]


async def test_the_footer_shows_the_effort_claude_code_reports(
    harness_for: Callable[..., Harness], tmp_path: Path
) -> None:
    # A top-level effortLevel in the settings does not decide the level (Opus 5.5 ignores the
    # user's): the level Claude Code runs at is the one its Stop hook reports.
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text('{"effortLevel": "high"}')
    hook_input = sdk_json("stop-hook")
    h = harness_for({"turns": [with_stop_hook(sdk_messages("tools"), hook_input)]})
    await asyncio.wait_for((await h.session().submit("list the files")).done.wait(), 2)
    assert "effort medium" in statuses(h)[-1]


async def test_the_footer_leaves_out_the_effort_until_claude_code_reports_it(
    harness_for: Callable[..., Harness],
) -> None:
    # A local command's turn (`/usage`) runs no Stop hook: the level is not known yet.
    h = harness_for({"turns": [sdk_messages("usage")]})
    await asyncio.wait_for((await h.session().submit("/usage")).done.wait(), 2)
    assert statuses(h) and "effort" not in statuses(h)[-1]


async def test_the_footer_says_default_when_claude_code_reports_no_effort(
    harness_for: Callable[..., Harness],
) -> None:
    # Claude Code leaves the field out when the model takes no effort parameter.
    hook_input = {k: v for k, v in sdk_json("stop-hook").items() if k != "effort"}
    h = harness_for({"turns": [with_stop_hook(sdk_messages("tools"), hook_input)]})
    await asyncio.wait_for((await h.session().submit("list the files")).done.wait(), 2)
    assert "effort default" in statuses(h)[-1]


async def test_an_effort_set_before_a_restart_is_not_carried_over(
    harness_for: Callable[..., Harness],
) -> None:
    # Measured 2026-09-25: a resumed session runs at the settings' level, not the `/effort` one.
    import dataclasses

    turn = sdk_messages("usage")
    result = turn[-1]
    assert isinstance(result, ResultMessage)
    effort_turn = [
        *turn[:-1],
        dataclasses.replace(result, result="Set effort level to low (this session only): Quick"),
    ]
    h = harness_for(
        {"turns": [effort_turn]},
        {"turns": [with_stop_hook(sdk_messages("tools"), sdk_json("stop-hook"))]},
    )
    await asyncio.wait_for((await h.session().submit("/effort low")).done.wait(), 2)
    assert "effort low" in statuses(h)[-1]
    await h.manager.close_all()
    await asyncio.wait_for((await h.session().submit("next")).done.wait(), 2)
    assert "effort medium" in statuses(h)[-1]


async def test_an_approval_slack_refuses_to_show_is_denied_and_logged(
    harness_for: Callable[..., Harness], caplog: pytest.LogCaptureFixture
) -> None:
    from slack_sdk.errors import SlackApiError

    refused = SlackApiError("ratelimited", {"ok": False, "error": "ratelimited"})
    # The reply's message posts, the approval request does not, later messages do.
    h = harness_for(
        {"turns": [[CanUseToolCall("Bash", {"command": "ls"}), *sdk_messages("tools")]]}
    )
    h.slack.responses["chat.postMessage"] = [
        {"ok": True, "ts": "1790000000.000001"},
        refused,
        {"ok": True, "ts": "1790000000.000003"},
    ]
    turn = await h.session().submit("list the files")
    await asyncio.wait_for(turn.done.wait(), 2)
    result = h.clients[0].permission_results[0]
    assert isinstance(result, PermissionResultDeny)
    assert result.message == texts.APPROVAL_UNPOSTED
    assert "could not post an approval request" in caplog.text and "ratelimited" in caplog.text


async def test_a_footer_that_fails_to_build_still_ends_the_reply(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(cwd: Path) -> str | None:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(sessions, "git_branch", broken)
    h = harness_for({"turns": [sdk_messages("tools")]})
    turn = await h.session().submit("list the files")
    await asyncio.wait_for(turn.done.wait(), 2)
    last = [a for m, a in h.slack.calls if m in ("chat.postMessage", "chat.update")][-1]
    assert texts.WRITING not in json.dumps(last, ensure_ascii=False)


async def test_a_usage_entry_without_a_token_count_still_gets_a_footer(
    harness_for: Callable[..., Harness],
) -> None:
    messages = sdk_messages("tools")
    result = messages[-1]
    assert isinstance(result, ResultMessage) and result.model_usage
    model = next(iter(result.model_usage))
    trimmed = dataclasses.replace(
        result, model_usage={model: {"inputTokens": 1000, "outputTokens": 500}}
    )
    h = harness_for({"turns": [[*messages[:-1], trimmed]]})
    turn = await h.session().submit("list the files")
    await asyncio.wait_for(turn.done.wait(), 2)
    assert "1.5k tok" in statuses(h)[-1]


async def test_a_context_usage_failure_is_logged(
    harness_for: Callable[..., Harness], caplog: pytest.LogCaptureFixture
) -> None:
    h = harness_for({"turns": [sdk_messages("tools")], "context_usage_error": RuntimeError("x")})
    turn = await h.session().submit("list the files")
    await asyncio.wait_for(turn.done.wait(), 2)
    assert "could not read the context usage" in caplog.text


async def test_a_failed_disconnect_is_logged(
    harness_for: Callable[..., Harness], caplog: pytest.LogCaptureFixture
) -> None:
    h = harness_for({"disconnect_error": BrokenPipeError()})
    await h.session().ensure_connected()
    await h.session().close()
    assert "could not close Claude Code" in caplog.text and "BrokenPipeError" in caplog.text


async def test_a_turn_taken_while_claude_code_starts_is_ended_on_close(
    harness_for: Callable[..., Harness],
) -> None:
    gate = asyncio.Event()
    h = harness_for({"connect_gate": gate})
    session = h.session()
    turn = await session.submit("hello")
    await asyncio.sleep(0.05)  # the worker has taken the turn and waits for the CLI
    await session.close()
    assert turn.done.is_set()
    assert texts.ENDED.format(reason=texts.ENDED_SHUTDOWN) in h.written_text()


async def test_a_setup_failure_after_connect_closes_the_client(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({"server_info_error": RuntimeError("x")}, {})
    with pytest.raises(RuntimeError):
        await h.session().ensure_connected()
    assert h.clients[0].connected is False
    await h.session().ensure_connected()  # the next attempt starts one process, not two
    assert len(h.clients) == 2 and h.clients[1].connected


async def test_a_rebind_never_records_the_old_directory_s_session(
    harness_for: Callable[..., Harness], tmp_path: Path
) -> None:
    messages = sdk_messages("tools")
    h = harness_for({"turns": [messages[:-1]]})
    await h.session().submit("list the files")
    await until(lambda: len(h.clients) == 1 and h.clients[0].queries == ["list the files"])
    await asyncio.sleep(0.05)
    other = tmp_path / "other"
    other.mkdir()
    h.clients[0].inject([messages[-1]])  # the old turn's result is waiting to be read
    await h.manager.bind(CHANNEL, other)
    stored = h.state.get(CHANNEL)
    assert stored is not None and stored.directory == other and stored.session_id is None


def test_a_relative_bind_path_is_read_under_the_allowed_root(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    assert resolve_directory("app", tmp_path) == (tmp_path / "app").resolve()
    assert resolve_directory("../", tmp_path / "app") is None


async def test_a_folder_claude_code_does_not_trust_is_not_started(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({"turns": [sdk_messages("tools")]})

    async def untrusted(directory: Path) -> bool:
        return False

    h.deps.workspace_trusted = untrusted
    turn = await h.session().submit("list the files")
    await asyncio.wait_for(turn.done.wait(), 2)
    assert h.clients == []  # no Claude Code process, so no hook of the folder's ran
    assert "trust" in h.written_text()


async def test_bypass_from_the_folder_s_own_settings_shows_and_turns_off(
    harness_for: Callable[..., Harness],
) -> None:
    info = {"commands": [], "current_permission_mode": "bypassPermissions"}
    h = harness_for({"turns": [sdk_messages("tools"), sdk_messages("tools")], "server_info": info})
    session = h.session()
    await asyncio.wait_for((await session.submit("list the files")).done.wait(), 2)
    assert statuses(h)[-1].startswith("⚡ bypass")
    await session.set_bypass(False)
    assert h.clients[0].modes[-1] == "default"
    assert "Mode: `default`" in await session.status()
    await asyncio.wait_for((await session.submit("again")).done.wait(), 2)
    assert not statuses(h)[-1].startswith("⚡ bypass")


async def test_every_message_is_posted_without_link_previews(
    harness_for: Callable[..., Harness],
) -> None:
    ask = CanUseToolCall("Bash", {"command": "ls"})
    h = harness_for({"turns": [[ask, *sdk_messages("tools")]]})
    turn = await h.session().submit("list the files")
    await until(lambda: len(h.approvals._pending) == 1)
    h.approvals.resolve(next(iter(h.approvals._pending)), CHANNEL, Approve())
    await asyncio.wait_for(turn.done.wait(), 2)
    posts = h.slack.calls_to("chat.postMessage")
    assert len(posts) >= 2
    assert all(p.get("unfurl_links") is False and p.get("unfurl_media") is False for p in posts)


async def test_resume_points_the_channel_at_another_session_of_its_directory(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({"turns": [sdk_messages("tools")]}, {})
    await asyncio.wait_for((await h.session().submit("list the files")).done.wait(), 2)
    other = "68da9311-0000-4000-8000-00000000abcd"
    assert await h.manager.resume(CHANNEL, other)
    assert h.state.get(CHANNEL).session_id == other
    await h.session().ensure_connected()  # the next message starts Claude Code on that session
    assert h.clients[-1].options.resume == other


async def test_resume_keeps_bypass_as_the_terminal_s_resume_does(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({}, {})
    await h.session().set_bypass(True)
    assert await h.manager.resume(CHANNEL, "68da9311-0000-4000-8000-00000000abcd")
    assert h.manager.bypass_on(CHANNEL)
    await h.session().ensure_connected()
    assert h.clients[-1].modes == ["bypassPermissions"]


async def test_resume_without_bypass_leaves_the_mode_alone(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({}, {})
    await h.session().ensure_connected()
    assert await h.manager.resume(CHANNEL, "68da9311-0000-4000-8000-00000000abcd")
    await h.session().ensure_connected()
    assert h.clients[-1].modes == [] and not h.manager.bypass_on(CHANNEL)


async def test_resume_waits_for_an_idle_channel(harness_for: Callable[..., Harness]) -> None:
    ask = CanUseToolCall("Bash", {"command": "ls"})
    h = harness_for({"turns": [[ask, *sdk_messages("tools")]]})
    await h.session().submit("list the files")
    await until(lambda: len(h.approvals._pending) == 1)
    before = h.state.get(CHANNEL).session_id
    assert not await h.manager.resume(CHANNEL, "68da9311-0000-4000-8000-00000000abcd")
    assert h.state.get(CHANNEL).session_id == before and h.session().busy


async def test_resume_waits_for_background_tasks_to_end(
    harness_for: Callable[..., Harness],
) -> None:
    first, _, _ = split_background()
    h = harness_for({"turns": [first]})
    await asyncio.wait_for((await h.session().submit("start it")).done.wait(), 2)
    assert h.session()._running_counts()  # the recorded background shell still runs
    assert not await h.manager.resume(CHANNEL, "68da9311-0000-4000-8000-00000000abcd")


def test_the_list_holds_only_this_directory_s_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The terminal's picker shows "sessions from the current worktree" (sessions reference,
    # read 2026-09-25); list_sessions includes every worktree unless told not to.
    calls: list[dict[str, Any]] = []

    def spy(**kwargs: Any) -> list[Any]:
        calls.append(kwargs)
        return []

    monkeypatch.setattr(sessions, "list_sessions", spy)
    sessions.directory_sessions(tmp_path)
    assert calls == [{"directory": str(tmp_path), "include_worktrees": False}]


async def test_the_footer_names_the_bound_folder(harness_for: Callable[..., Harness]) -> None:
    h = harness_for({"turns": [sdk_messages("tools")]})
    await asyncio.wait_for((await h.session().submit("list the files")).done.wait(), 2)
    folder = h.tmp_path.resolve()
    assert statuses(h)[-1].endswith(f" · {folder.parent.name}/{folder.name}")


async def test_status_lists_the_footer_s_values_of_the_latest_reply(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({"turns": [with_stop_hook(sdk_messages("tools"), sdk_json("stop-hook"))]})
    session = h.session()
    await asyncio.wait_for((await session.submit("list the files")).done.wait(), 2)
    await until(lambda: h.usage_fetches == 1)  # the turn's own refresh of the limits
    text = await session.status()
    assert text.startswith("Directory:") and "Claude Code: `2.1.283`" in text
    tokens = re.search(r"([\d.]+[kM]?) tok", statuses(h)[-1])
    assert tokens is not None
    lines = text.splitlines()
    assert lines[lines.index("Now: idle") + 1 :] == [
        "Model: `claude-haiku-4-5-20251001`",
        "Effort: `medium`",
        "Context: `7%`",
        f"Session tokens: `{tokens.group(1)}`",
        "5h limit: `5%`",
    ]


async def test_status_before_any_turn_starts_the_client_and_leaves_out_the_tokens(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({"turns": []})
    text = await h.session().status()
    assert len(h.clients) == 1 and h.clients[0].queries == []
    # The version comes with a turn's `init` message, never on connect (measured 2026-09-26).
    assert f"Claude Code: `{texts.VERSION_PENDING}`" in text
    assert "Model: `claude-haiku-4-5-20251001`" in text and "Context: `7%`" in text
    # Only a turn's Stop hook, or `/effort`, reports the level: the settings do not say it.
    assert "Session tokens" not in text and "Effort" not in text


async def test_status_says_why_claude_code_cannot_start_in_place_of_the_footer_s_values(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({"turns": []})

    async def untrusted(directory: Path) -> bool:
        return False

    h.deps.workspace_trusted = untrusted
    text = await h.session().status()
    assert h.clients == []
    assert "Claude Code: `not started`" in text
    assert text.endswith(f"Now: idle\n{texts.DIRECTORY_UNTRUSTED.format(directory=h.tmp_path)}")


async def test_status_during_a_turn_shows_the_model_and_the_context(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({"turns": [sdk_messages("tools")[:-1]]})
    session = h.session()
    await session.submit("list the files")
    await until(lambda: len(h.clients) == 1 and h.clients[0].queries == ["list the files"])
    text = await session.status()
    assert texts.ACTIVITY_BUSY.format(queued=0) in text
    assert "Model: `claude-haiku-4-5-20251001`" in text and "Context: `7%`" in text


async def test_status_shows_the_tasks_still_running_as_the_latest_reply_does(
    harness_for: Callable[..., Harness],
) -> None:
    first, _, _ = split_background()
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    assert statuses(h)[-1].endswith("⏳ 1 shell")
    assert (await session.status()).endswith("\nBackground: `1 shell`")


async def test_status_refreshes_the_usage_limits_and_the_next_one_shows_them(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({"turns": []})
    session = h.session()
    await session.status()
    await until(lambda: h.usage_fetches == 1)
    assert "5h limit: `5%`" in await session.status()


async def test_status_after_claude_code_restarts_leaves_out_the_old_process_s_tokens(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({"turns": [with_stop_hook(sdk_messages("tools"), sdk_json("stop-hook"))]}, {})
    session = h.session()
    await asyncio.wait_for((await session.submit("list the files")).done.wait(), 2)
    text = await session.status()
    assert "Session tokens" in text and "Effort: `medium`" in text
    h.clients[0].inject([EndOfStream()])
    await until(lambda: session._client is None)
    text = await session.status()
    assert len(h.clients) == 2 and "Context: `7%`" in text
    assert "Session tokens" not in text and "Effort" not in text


async def test_status_says_claude_code_s_error_when_it_fails_to_start(
    harness_for: Callable[..., Harness], caplog: pytest.LogCaptureFixture
) -> None:
    h = harness_for({"connect_error": RuntimeError("x")})
    # The reason a prompt would get in the same state, never less.
    text = await h.session().status()
    assert text.endswith(f"Now: idle\n{texts.ERROR_REPLY.format(error='RuntimeError')}")
    assert "could not read the footer's values for the status" in caplog.text


async def test_status_follows_a_reply_that_reports_no_tokens(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({"turns": [sdk_messages("tools"), sdk_messages("usage")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("list the files")).done.wait(), 2)
    await asyncio.wait_for((await session.submit("/usage")).done.wait(), 2)
    assert " tok" not in statuses(h)[-1]
    assert "Session tokens" not in await session.status()


async def test_a_stop_lets_the_running_turn_finish_and_ends_the_queued_one(
    harness_for: Callable[..., Harness],
) -> None:
    *running, result = sdk_messages("tools")
    h = harness_for({"turns": [running, sdk_messages("tools")]})
    session = h.session()
    first = await session.submit("first")
    second = await session.submit("second")
    await until(lambda: bool(h.clients) and h.clients[0].queries == ["first"])
    drained = asyncio.create_task(h.manager.drain(asyncio.Event()))
    await asyncio.wait_for(second.done.wait(), 2)
    assert not drained.done() and not first.done.is_set()
    h.clients[0].inject([result])
    await asyncio.wait_for(drained, 2)
    assert first.done.is_set()
    assert h.clients[0].queries == ["first"]
    assert texts.ENDED.format(reason=texts.ENDED_RESTARTING) in h.replies()[1]
    assert h.state.get(CHANNEL).session_id == result.session_id


@pytest.mark.parametrize("bypass", [True, False])
async def test_a_stop_says_bypass_ends_only_where_it_is_on(
    harness_for: Callable[..., Harness], bypass: bool
) -> None:
    h = harness_for({})
    await h.session().set_bypass(bypass)
    await asyncio.wait_for(h.manager.drain(asyncio.Event()), 2)
    posted = [p["text"] for p in h.slack.calls_to("chat.postMessage")]
    assert posted == ([texts.BYPASS_RESTARTING] if bypass else [])


async def test_an_approval_asked_during_a_stop_stays_open_and_the_turn_finishes(
    harness_for: Callable[..., Harness],
) -> None:
    ask = CanUseToolCall("Bash", {"command": "ls"})
    h = harness_for({"turns": [[ask, *sdk_messages("tools")]]})
    turn = await h.session().submit("first")
    await until(lambda: bool(h.approvals._pending))
    drained = asyncio.create_task(h.manager.drain(asyncio.Event()))
    await asyncio.sleep(0.05)
    assert not drained.done() and not turn.done.is_set()
    # The owner answers while the daemon stops, as a Slack session that restarted it would need.
    assert h.approvals.resolve(next(iter(h.approvals._pending)), CHANNEL, Approve()) is not None
    await asyncio.wait_for(drained, 2)
    assert turn.done.is_set()
    assert isinstance(h.clients[0].permission_results[0], PermissionResultAllow)
    assert h.clients[0].interrupts == 0


async def test_a_turn_taken_before_a_stop_is_never_sent(
    harness_for: Callable[..., Harness],
) -> None:
    gate = asyncio.Event()
    h = harness_for({"connect_gate": gate, "turns": [sdk_messages("tools")]})
    turn = await h.session().submit("first")
    await until(lambda: bool(h.clients))
    drained = asyncio.create_task(h.manager.drain(asyncio.Event()))
    gate.set()
    await asyncio.wait_for(drained, 2)
    assert turn.done.is_set() and h.clients[0].queries == []
    assert texts.ENDED_RESTARTING in h.written_text()


async def test_a_second_signal_cuts_the_stop_short(harness_for: Callable[..., Harness]) -> None:
    *running, _ = sdk_messages("tools")
    h = harness_for({"turns": [running]})
    turn = await h.session().submit("first")
    await until(lambda: bool(h.clients) and h.clients[0].queries == ["first"])
    cut_short = asyncio.Event()
    drained = asyncio.create_task(h.manager.drain(cut_short))
    await asyncio.sleep(0.05)
    assert not drained.done()
    cut_short.set()
    await asyncio.wait_for(drained, 2)
    assert not turn.done.is_set()  # close_all ends it, as before


async def test_a_stop_waits_for_a_background_task_and_the_turn_that_reports_it(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "DRAIN_POLL_SECONDS", 0.01)
    first, notice, injected = split_background()
    ended = [m for m in notice if not isinstance(m, TaskNotificationMessage)]
    notification = [m for m in notice if isinstance(m, TaskNotificationMessage)]
    h = harness_for({"turns": [first]})
    await asyncio.wait_for((await h.session().submit("start it")).done.wait(), 2)
    drained = asyncio.create_task(h.manager.drain(asyncio.Event()))
    await asyncio.sleep(0.05)
    assert not drained.done()  # the task still runs
    # Live, the terminal task_updated came a moment before the notification (2026-09-26): the
    # task's line is closed, yet the turn that reports it has not started.
    h.clients[0].inject(ended)
    await asyncio.sleep(0.1)
    assert not drained.done()
    h.clients[0].inject(notification)
    await asyncio.sleep(0.05)
    assert not drained.done()  # Claude Code is expected to report it
    h.clients[0].inject(injected)
    await asyncio.wait_for(drained, 2)
    assert any(is_report(r) for r in h.replies())


async def test_a_stop_waits_only_a_while_for_a_notification_that_never_comes(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A task stopped with TaskStop can end with no notification (SDK TaskUpdatedMessage docstring).
    monkeypatch.setattr(sessions, "DRAIN_POLL_SECONDS", 0.01)
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.2)
    first, notice, _ = split_background()
    ended = [m for m in notice if not isinstance(m, TaskNotificationMessage)]
    h = harness_for({"turns": [first]})
    await asyncio.wait_for((await h.session().submit("start it")).done.wait(), 2)
    h.clients[0].inject(ended)
    await asyncio.sleep(0.05)
    drained = asyncio.create_task(h.manager.drain(asyncio.Event()))
    await asyncio.sleep(0.05)
    assert not drained.done()
    await asyncio.wait_for(drained, 1)


async def swap(h: Harness, how: str, tmp_path: Path) -> None:
    if how == "bind":
        other = tmp_path / "other"
        other.mkdir()
        await h.manager.bind(CHANNEL, other)
    else:
        assert await h.manager.resume(CHANNEL, "another")


@pytest.mark.parametrize("how", ["bind", "resume"])
async def test_a_swap_while_a_word_starts_the_client_leaves_no_process(
    harness_for: Callable[..., Harness], tmp_path: Path, how: str
) -> None:
    # `!help` or `!status` starts the client in its own task, outside the prompt queue.
    gate = asyncio.Event()
    h = harness_for({"connect_gate": gate})
    word = asyncio.create_task(h.session().ensure_connected())
    await until(lambda: len(h.clients) == 1)
    swapping = asyncio.create_task(swap(h, how, tmp_path))
    await asyncio.sleep(0.05)
    assert not swapping.done()  # the close waits for the connect in progress
    gate.set()
    await asyncio.wait_for(swapping, 2)
    await asyncio.wait_for(word, 2)
    assert len(h.clients) == 1 and h.clients[0].connected is False


async def test_a_closed_session_starts_no_client(harness_for: Callable[..., Harness]) -> None:
    h = harness_for()
    session = h.session()
    await session.close()
    with pytest.raises(sessions.SessionClosed):
        await session.ensure_connected()
    assert h.clients == []


async def test_bypass_asked_on_a_session_rebound_meanwhile_is_not_stored(
    harness_for: Callable[..., Harness], tmp_path: Path
) -> None:
    gate = asyncio.Event()
    h = harness_for({"connect_gate": gate})
    word = asyncio.create_task(h.session().set_bypass(True))
    await until(lambda: len(h.clients) == 1)
    swapping = asyncio.create_task(swap(h, "bind", tmp_path))
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.wait_for(swapping, 2)
    with pytest.raises(sessions.SessionClosed):
        await asyncio.wait_for(word, 2)
    stored = h.state.get(CHANNEL)
    assert stored is not None and stored.bypass is False


async def test_the_status_of_a_session_closed_meanwhile_is_not_given(
    harness_for: Callable[..., Harness], tmp_path: Path
) -> None:
    gate = asyncio.Event()
    h = harness_for({"connect_gate": gate})
    session = h.session()
    word = asyncio.create_task(session.status())
    await until(lambda: len(h.clients) == 1)
    swapping = asyncio.create_task(swap(h, "bind", tmp_path))
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.wait_for(swapping, 2)
    with pytest.raises(sessions.SessionClosed):  # connected before the close, read after it
        await asyncio.wait_for(word, 2)
    with pytest.raises(sessions.SessionClosed):
        await session.status()


async def test_bypass_whose_client_closes_under_it_says_the_session_closed(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({})
    session = h.session()
    client = await session.ensure_connected()

    async def closing(mode: str) -> None:
        await session.close()
        raise ConnectionError("the CLI went away")

    client.set_permission_mode = closing
    with pytest.raises(sessions.SessionClosed):
        await session.set_bypass(True)
    stored = h.state.get(CHANNEL)
    assert stored is not None and stored.bypass is False
