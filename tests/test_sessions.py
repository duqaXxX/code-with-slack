import asyncio
import dataclasses
import json
import logging
import re
import subprocess
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    RateLimitEvent,
    ResultError,
    ResultMessage,
    StreamEvent,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
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
from code_with_slack.render.renderer import TurnRenderer
from code_with_slack.render.sinks import UpdateLimiter
from code_with_slack.render.status import Status, ThreadStatus
from code_with_slack.sessions import SessionDeps, SessionManager, resolve_directory
from code_with_slack.state import StateStore
from tests.fakes import (
    BOT,
    CHANNEL,
    FIXTURES,
    OTHER_THREAD,
    OWNER,
    TEAM,
    THREAD,
    CanUseToolCall,
    EndOfStream,
    FakeClaudeClient,
    FakeSlack,
    HookRun,
    sdk_json,
    sdk_messages,
    split_turns,
)

NESTED = texts.NESTED


WRITES = (
    "chat.postMessage",
    "chat.startStream",
    "chat.appendStream",
    "chat.stopStream",
    "chat.update",
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
            # A generous burst: these tests are about session orchestration, not the shared
            # limiter's own pacing (that lives in test_sinks.py), and several use a real 2s
            # `wait_for` budget the production rate (one write every ~1.33s past 5) would blow.
            update_limiter=UpdateLimiter(burst=1_000),
        )
        self.manager = SessionManager(self.deps)

    def factory(self, options: ClaudeAgentOptions) -> FakeClaudeClient:
        script = self._scripts.pop(0) if self._scripts else {}
        client = FakeClaudeClient(options, **script)
        self.clients.append(client)
        return client

    def session(self, thread: str = THREAD) -> Any:
        """The live session of `thread`, opened as a top-level owner message would."""
        session = self.manager.open(CHANNEL, thread)
        assert session is not None
        return session

    def replies(self) -> list[str]:
        """The text every posted message ends up showing, in the order they were posted."""
        return self.slack.message_texts()

    def bodies(self) -> list[str]:
        """Each reply's own text: `replies()` with the empty entries a silent closing message
        leaves (it carries no markdown or tool line) filtered out."""
        return [r for r in self.replies() if r]

    def reactions(self, thread: str = THREAD) -> list[str]:
        """Every reaction shown on `thread`'s root message, in order (D10): `reactions.add`
        alone, since `StatusReaction` always adds the new one before removing the previous."""
        return [
            args["name"]
            for method, args in self.slack.calls
            if method == "reactions.add" and args["timestamp"] == thread
        ]

    def written_text(self) -> str:
        """Every markdown a write carried: blocks of a post or an update, chunks of a stream."""
        blocks = "\n".join(
            b["text"]
            for m in ("chat.postMessage", "chat.update")
            for a in self.slack.calls_to(m)
            for b in a.get("blocks") or []
            if b.get("type") == "markdown"
        )
        chunks = "\n".join(
            c["text"]
            for m in ("chat.startStream", "chat.appendStream", "chat.stopStream")
            for a in self.slack.calls_to(m)
            for c in a.get("chunks") or []
            if c.get("type") == "markdown_text"
        )
        return f"{blocks}\n{chunks}"

    def cards(self) -> list[dict[str, Any]]:
        """The task cards of the first reply's message, as it shows them now."""
        return self.slack.message_cards()[0] if self.slack.created_ts else []


@pytest.fixture
async def harness_for(slack: FakeSlack, tmp_path: Path) -> AsyncIterator[Callable[..., Harness]]:
    made: list[Harness] = []

    def make(*scripts: dict[str, Any]) -> Harness:
        made.append(Harness(slack, tmp_path, list(scripts)))
        return made[-1]

    yield make
    for harness in made:
        await harness.manager.close_all()


async def test_a_turn_replies_in_the_thread_and_records_the_session(
    harness_for: Callable[..., Harness],
) -> None:
    turn_messages = sdk_messages("tools")
    h = harness_for({"turns": [turn_messages]})
    turn = await h.session().submit("list the files")
    await asyncio.wait_for(turn.done.wait(), 2)
    assert h.clients[0].queries == ["list the files"]
    assert all(a.get("thread_ts") == THREAD for a in h.slack.calls_to("chat.postMessage"))
    result = turn_messages[-1]
    assert isinstance(result, ResultMessage)
    assert h.state.thread(CHANNEL, THREAD).session_id == result.session_id
    [stop] = h.slack.calls_to("chat.stopStream")
    assert stop["blocks"][-1]["type"] == "context"  # the footer closes the reply


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
    h.session()  # opens the thread
    h.state.set_session(CHANNEL, THREAD, "stored-session")
    await h.session().ensure_connected()
    assert h.clients[0].options.resume == "stored-session"


async def test_clear_records_the_new_session_id(harness_for: Callable[..., Harness]) -> None:
    first, second = split_turns(sdk_messages("clear"))
    h = harness_for({"turns": [first, second]})
    session = h.session()
    await asyncio.wait_for((await session.submit("hi")).done.wait(), 2)
    await asyncio.wait_for((await session.submit("/clear")).done.wait(), 2)
    assert isinstance(second[-1], ResultMessage)
    assert h.state.thread(CHANNEL, THREAD).session_id == second[-1].session_id


async def test_a_gone_session_is_removed_and_says_so(harness_for: Callable[..., Harness]) -> None:
    # D7: a stored session id that fails to resume no longer falls back to a fresh session.
    gone = ResultError(
        "Claude Code returned an error result: No conversation found",
        data={
            "subtype": "error_during_execution",
            "is_error": True,
            "errors": ["No conversation found with session ID: gone"],
        },
    )
    h = harness_for({"connect_error": gone})
    h.session()  # opens the thread
    h.state.set_session(CHANNEL, THREAD, "gone")
    turn = await h.session().submit("hello")
    await asyncio.wait_for(turn.done.wait(), 2)
    assert [c.options.resume for c in h.clients] == ["gone"]
    assert h.bodies() == [texts.SESSION_GONE]
    assert h.state.thread(CHANNEL, THREAD) is None
    assert h.manager.get(CHANNEL, THREAD) is None
    assert h.slack.pushes() == 1  # the reply's stream stops once


async def test_a_gone_session_fails_a_queued_turn_and_leaks_no_process(
    harness_for: Callable[..., Harness],
) -> None:
    # Regression: the worker used to take the next queued turn after SessionGone and start a
    # second, unrecorded Claude Code session for it instead of refusing.
    gone = ResultError(
        "Claude Code returned an error result: No conversation found",
        data={
            "subtype": "error_during_execution",
            "is_error": True,
            "errors": ["No conversation found with session ID: gone"],
        },
    )
    h = harness_for({"connect_error": gone})
    session = h.session()  # opens the thread
    h.state.set_session(CHANNEL, THREAD, "gone")
    first = await session.submit("hello")
    second = await session.submit("again")
    await asyncio.wait_for(asyncio.gather(first.done.wait(), second.done.wait()), 2)
    # Only the one failed attempt: no second client started for the queued turn.
    assert [c.options.resume for c in h.clients] == ["gone"]
    assert h.bodies() == [texts.SESSION_GONE, texts.SESSION_GONE]
    # The worker must not be left spinning on an empty queue nobody will ever fill again.
    await asyncio.sleep(0)
    names = {t.get_name() for t in asyncio.all_tasks()}
    assert f"worker-{CHANNEL}-{THREAD}" not in names
    assert f"idle-close-{CHANNEL}-{THREAD}" not in names
    await h.manager.close_all()
    assert all(not c.connected for c in h.clients)


async def test_a_turn_that_races_sessiongone_while_its_sink_is_made_gets_session_gone(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A second `submit` can be suspended while its reply's sink is made, while the first turn's
    # `ensure_connected` finds the stored session gone, closes the session and drains the queue
    # (empty at that point). The second turn is queued only after that: nothing will ever take
    # it from an ended worker, so it must be resolved right there, as gone too.
    gone = ResultError(
        "Claude Code returned an error result: No conversation found",
        data={
            "subtype": "error_during_execution",
            "is_error": True,
            "errors": ["No conversation found with session ID: gone"],
        },
    )
    h = harness_for({"connect_error": gone})
    session = h.session()
    h.state.set_session(CHANNEL, THREAD, "gone")
    real = sessions.ThreadSession._sink
    gates = [asyncio.Event(), asyncio.Event()]
    entered: list[int] = []

    async def gated(self: Any) -> Any:
        sink = await real(self)
        gate = gates[len(entered)]
        entered.append(1)
        await gate.wait()
        return sink

    monkeypatch.setattr(sessions.ThreadSession, "_sink", gated)
    first_task = asyncio.create_task(session.submit("hello"))
    await until(lambda: len(entered) == 1)  # "hello" is paused while its sink is made
    second_task = asyncio.create_task(session.submit("again"))
    await until(lambda: len(entered) == 2)  # "again" is paused there too
    gates[0].set()  # let "hello" queue itself and start its worker
    first_turn = await first_task
    await asyncio.wait_for(first_turn.done.wait(), 2)
    assert session.closed  # the SessionGone branch has already closed and drained the queue
    gates[1].set()  # only now does "again" reach the point where it would enqueue itself
    second_turn = await asyncio.wait_for(second_task, 2)
    await asyncio.wait_for(second_turn.done.wait(), 2)
    assert h.bodies() == [texts.SESSION_GONE, texts.SESSION_GONE]
    await asyncio.sleep(0)
    names = {t.get_name() for t in asyncio.all_tasks()}
    assert f"worker-{CHANNEL}-{THREAD}" not in names
    await h.manager.close_all()


async def test_a_turn_that_races_a_plain_close_while_its_sink_is_made_gets_session_closed(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Whatever closed the session while the sink was made, it must not always be answered as
    # SessionGone: a plain close (an idle close, a restart) leaves the thread's own entry in
    # state.json, unlike SessionGone's close (D7), and that is the only thing telling the two
    # apart once `self._closed` is true either way.
    h = harness_for({})
    session = h.session()
    real = sessions.ThreadSession._sink
    gate = asyncio.Event()
    entered: list[int] = []

    async def gated(self: Any) -> Any:
        sink = await real(self)
        entered.append(1)
        await gate.wait()
        return sink

    monkeypatch.setattr(sessions.ThreadSession, "_sink", gated)
    submit_task = asyncio.create_task(session.submit("hello"))
    await until(lambda: len(entered) == 1)  # paused while its sink is made
    await session.close()  # a plain close: the thread's entry stays in state.json
    assert h.state.thread(CHANNEL, THREAD) is not None
    gate.set()
    turn = await asyncio.wait_for(submit_task, 2)
    await asyncio.wait_for(turn.done.wait(), 2)
    assert h.bodies() == [texts.SESSION_CLOSED]


async def test_a_direct_sessiongone_call_leaves_no_idle_timer_task(
    harness_for: Callable[..., Harness],
) -> None:
    # `!status` (or `!bypass`) can hit SessionGone with no worker ever created: the idle-close
    # timer armed when the session was handed out (D9) must not be left running past the close.
    gone = ResultError(
        "Claude Code returned an error result: No conversation found",
        data={
            "subtype": "error_during_execution",
            "is_error": True,
            "errors": ["No conversation found with session ID: gone"],
        },
    )
    h = harness_for({"connect_error": gone})
    session = h.session()  # touched on hand-out: its idle-close timer is armed already
    h.state.set_session(CHANNEL, THREAD, "gone")
    await session.status()
    assert session.closed
    await asyncio.sleep(0)
    names = {t.get_name() for t in asyncio.all_tasks()}
    assert f"idle-close-{CHANNEL}-{THREAD}" not in names
    assert f"worker-{CHANNEL}-{THREAD}" not in names


async def test_a_direct_gone_call_rescues_the_worker_s_taken_turn(
    harness_for: Callable[..., Harness],
) -> None:
    # Regression (proven at 5e435de): a direct call (`!status`) holds `_connect_lock` while it
    # hits SessionGone; if the worker had already taken a turn and was itself blocked acquiring
    # that same lock, cancelling the worker (to end it) lost that turn: cancelled mid-`await`,
    # it never reached its own except clause, so `_taken` was never failed and the reply stayed
    # "writing" forever.
    gone = ResultError(
        "Claude Code returned an error result: No conversation found",
        data={
            "subtype": "error_during_execution",
            "is_error": True,
            "errors": ["No conversation found with session ID: gone"],
        },
    )
    gate = asyncio.Event()
    h = harness_for({"connect_error": gone, "connect_gate": gate})
    session = h.session()
    h.state.set_session(CHANNEL, THREAD, "gone")
    status_task = asyncio.create_task(session.status())  # holds the connect lock, gated
    await until(lambda: len(h.clients) == 1)
    turn = await session.submit("hello")
    await until(lambda: session._taken is not None)  # the worker took it, now waits on the lock
    gate.set()
    await asyncio.wait_for(status_task, 2)
    assert session.closed
    await asyncio.wait_for(turn.done.wait(), 2)  # must not hang
    assert h.bodies() == [texts.SESSION_GONE]


async def test_a_restart_rebuilds_a_stored_thread_with_its_bypass(
    harness_for: Callable[..., Harness], tmp_path: Path
) -> None:
    h = harness_for({}, {})
    session = h.session()
    await session.set_bypass(True)
    assert h.clients[0].modes == ["bypassPermissions"] and session.bypass
    stored = json.loads((tmp_path / "state.json").read_text())
    assert stored["channels"][CHANNEL]["threads"][THREAD]["bypass"]
    # A new daemon: state.json read again, a new manager, a new Claude Code process.
    reloaded = dataclasses.replace(h.deps, state=StateStore(tmp_path / "state.json"))
    restarted = SessionManager(reloaded).get(CHANNEL, THREAD)
    assert restarted is not None and restarted.bypass
    await restarted.ensure_connected()
    assert h.clients[1].modes == ["bypassPermissions"]
    await restarted.set_bypass(False)
    assert h.clients[1].modes[-1] == "default"
    assert not StateStore(tmp_path / "state.json").thread(CHANNEL, THREAD).bypass
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
    assert h.approvals.resolve(approval_id, CHANNEL, THREAD, Approve()) is not None
    await asyncio.wait_for(second.done.wait(), 2)
    assert first.done.is_set()
    assert h.clients[0].queries == ["first", "second"]
    assert isinstance(h.clients[0].permission_results[0], PermissionResultAllow)


async def test_stop_landed_gives_up_when_the_stopped_turn_never_ends(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "STOP_TAIL_WAIT", 0.05)
    h = harness_for({"turns": [[CanUseToolCall("Bash", {"command": "rm -rf build"})]]})
    session = h.session()
    turn = await session.submit("clean")
    await until(lambda: bool(h.approvals._pending))
    assert await session.stop() is True
    await asyncio.wait_for(session.stop_landed(), 2)
    assert not turn.done.is_set()  # it did not end: the wait is bounded


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
    # The answer to the stop waits for the reply it cut short: it sits under its footer.
    await asyncio.wait_for(session.stop_landed(), 2)
    assert turn.done.is_set()
    assert h.clients[0].interrupts == 1
    assert isinstance(h.clients[0].permission_results[0], PermissionResultDeny)
    assert len(h.slack.calls_to("chat.delete")) == 1
    # Crash repair (issue #19): `_delete_request` clears the state entry too.
    assert h.state.thread(CHANNEL, THREAD).requests == ()
    assert await session.stop() is False


async def test_ask_user_question_returns_answers(harness_for: Callable[..., Harness]) -> None:
    recorded = sdk_json("ask-can-use-tool")
    call = CanUseToolCall(recorded["tool_name"], recorded["input"])
    h = harness_for({"turns": [[call, *sdk_messages("tools")]]})
    turn = await h.session().submit("ask me")
    await until(lambda: bool(h.approvals._pending))
    approval_id = next(iter(h.approvals._pending))
    answers = {q["question"]: q["options"][0]["label"] for q in recorded["input"]["questions"]}
    h.approvals.resolve(approval_id, CHANNEL, THREAD, Answer(answers))
    await asyncio.wait_for(turn.done.wait(), 2)
    result = h.clients[0].permission_results[0]
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == {"questions": recorded["input"]["questions"], "answers": answers}


def answered_turn() -> tuple[list[Any], str, dict[str, Any]]:
    """The recorded turn of an answered question (ask-answered.jsonl, CLI 2.1.286), with the
    permission request where the CLI makes it: after the call, before its result. With it, the
    call's id, which the request carries (measured), and the call's input."""
    messages = sdk_messages("ask-answered")
    call = next(
        b
        for m in messages
        if isinstance(m, AssistantMessage)
        for b in m.content
        if isinstance(b, ToolUseBlock)
    )
    at = next(i for i, m in enumerate(messages) if isinstance(m, UserMessage))
    ask = CanUseToolCall(call.name, call.input, tool_use_id=call.id)
    return [*messages[:at], ask, *messages[at:]], call.id, call.input


async def test_an_answered_question_stays_in_the_reply_and_its_request_goes(
    harness_for: Callable[..., Harness],
) -> None:
    # Issue #82: the answers show under the call's line, where the question was asked, so what
    # Claude does next shows below them; the request message has done its job.
    batch, _, asked_input = answered_turn()
    h = harness_for({"turns": [batch]})
    turn = await h.session().submit("ask me")
    await until(lambda: bool(h.approvals._pending))
    request_ts = h.slack.posted_ts[-1]
    assert h.state.thread(CHANNEL, THREAD).requests == (request_ts,)
    approval_id = next(iter(h.approvals._pending))
    answers = {q["question"]: q["options"][0]["label"] for q in asked_input["questions"]}
    h.approvals.resolve(approval_id, CHANNEL, THREAD, Answer(answers))
    await asyncio.wait_for(turn.done.wait(), 2)
    assert [a["ts"] for a in h.slack.calls_to("chat.delete")] == [request_ts]
    assert all(a["ts"] != request_ts for a in h.slack.calls_to("chat.update"))
    assert h.state.thread(CHANNEL, THREAD).requests == ()
    cards = [c for message in h.slack.message_cards() for c in message]
    [card] = [c for c in cards if c["title"] == texts.ANSWERED]
    assert card["status"] == "complete"
    shown = [
        b["elements"][0]["text"]
        for _, a in h.slack.calls
        for c in a.get("chunks") or []
        if c["type"] == "blocks"
        for b in c["blocks"]
        if b["type"] == "context"
    ]
    first = asked_input["questions"][0]
    assert shown and f"· {first['question']} → {first['options'][0]['label']}" in shown[0]


async def test_answers_the_reply_cannot_show_stay_in_the_request(
    harness_for: Callable[..., Harness],
) -> None:
    # A question whose call the reply holds no line for (asked inside a subagent, say): the
    # request is rewritten into the record, with no buttons, as the terminal keeps it.
    recorded = sdk_json("ask-can-use-tool")
    call = CanUseToolCall(recorded["tool_name"], recorded["input"], tool_use_id="toolu_unseen")
    h = harness_for({"turns": [[call, *sdk_messages("tools")]]})
    turn = await h.session().submit("ask me")
    await until(lambda: bool(h.approvals._pending))
    request_ts = h.slack.posted_ts[-1]
    approval_id = next(iter(h.approvals._pending))
    answers = {q["question"]: q["options"][0]["label"] for q in recorded["input"]["questions"]}
    h.approvals.resolve(approval_id, CHANNEL, THREAD, Answer(answers))
    await asyncio.wait_for(turn.done.wait(), 2)
    assert h.slack.calls_to("chat.delete") == []
    [update] = [a for a in h.slack.calls_to("chat.update") if a["ts"] == request_ts]
    text = update["blocks"][0]["elements"][0]["text"]
    first = recorded["input"]["questions"][0]
    assert text.startswith(f"{texts.ANSWERED}\n{texts.NESTED}· {first['question']} → ")
    assert [b["type"] for b in update["blocks"]] == ["context"]
    assert h.state.thread(CHANNEL, THREAD).requests == ()


async def test_a_record_slack_refuses_removes_the_request(
    harness_for: Callable[..., Harness],
) -> None:
    recorded = sdk_json("ask-can-use-tool")
    call = CanUseToolCall(recorded["tool_name"], recorded["input"], tool_use_id="toolu_unseen")
    h = harness_for({"turns": [[call, *sdk_messages("tools")]]})
    turn = await h.session().submit("ask me")
    await until(lambda: bool(h.approvals._pending))
    request_ts = h.slack.posted_ts[-1]
    h.slack.responses["chat.update"] = {"ok": False, "error": "msg_too_long"}
    approval_id = next(iter(h.approvals._pending))
    answers = {q["question"]: q["options"][0]["label"] for q in recorded["input"]["questions"]}
    h.approvals.resolve(approval_id, CHANNEL, THREAD, Answer(answers))
    await until(lambda: bool(h.slack.calls_to("chat.delete")))
    # Its buttons would no longer work: the request goes.
    assert [a["ts"] for a in h.slack.calls_to("chat.delete")] == [request_ts]
    h.slack.responses.pop("chat.update")
    await asyncio.wait_for(turn.done.wait(), 2)


async def test_an_approval_request_is_tracked_in_state_while_it_is_open(
    harness_for: Callable[..., Harness],
) -> None:
    # Deleting the answered request's own message, which clears it from state too
    # (`ThreadSession._delete_request`), is slack_app.py's `on_decision`'s job: out of scope for
    # a raw `Approvals.resolve` call, as `test_queue_waits_while_approval_pending` already makes
    # for the approval itself.
    ask = CanUseToolCall("Bash", {"command": "ls"})
    h = harness_for({"turns": [[ask, *sdk_messages("tools")]]})
    session = h.session()
    await session.submit("list the files")
    await until(lambda: bool(h.approvals._pending))
    assert h.state.thread(CHANNEL, THREAD).requests == (h.slack.posted_ts[-1],)


async def test_the_status_field_tracks_working_then_clears_once_done(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({"turns": [sdk_messages("tools")]})
    session = h.session()
    turn = await session.submit("list the files")
    assert h.state.thread(CHANNEL, THREAD).status == Status.WORKING.value
    await asyncio.wait_for(turn.done.wait(), 2)
    await until(lambda: h.state.thread(CHANNEL, THREAD).status is None)


async def test_the_ended_field_keeps_the_roots_final_reaction_until_work_starts_again(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({"turns": [sdk_messages("tools"), sdk_messages("tools")]})
    session = h.session()
    turn = await session.submit("list the files")
    assert h.state.thread(CHANNEL, THREAD).ended is None
    await asyncio.wait_for(turn.done.wait(), 2)
    await until(lambda: h.state.thread(CHANNEL, THREAD).ended == Status.DONE.value)
    await session.submit("and again")
    stored = h.state.thread(CHANNEL, THREAD)
    assert (stored.status, stored.ended) == (Status.WORKING.value, None)


async def test_a_close_that_cuts_a_turn_short_keeps_x_as_the_ended_reaction(
    harness_for: Callable[..., Harness],
) -> None:
    ask = CanUseToolCall("Bash", {"command": "rm -rf build"})
    h = harness_for({"turns": [[ask, *sdk_messages("tools")]]})
    session = h.session()
    await session.submit("clean")
    await until(lambda: bool(h.approvals._pending))
    await session.close()
    stored = h.state.thread(CHANNEL, THREAD)
    assert (stored.status, stored.ended) == (None, Status.ERROR.value)


async def test_close_leaves_every_repair_field_cleared(
    harness_for: Callable[..., Harness],
) -> None:
    ask = CanUseToolCall("Bash", {"command": "rm -rf build"})
    h = harness_for({"turns": [[ask, *sdk_messages("tools")]]})
    session = h.session()
    await session.submit("clean")
    await until(lambda: bool(h.approvals._pending))
    approval_id = next(iter(h.approvals._pending))
    request_ts = h.approvals._pending[approval_id].message_ts
    await session.close()
    stored = h.state.thread(CHANNEL, THREAD)
    assert stored.open_replies == ()
    assert stored.requests == ()
    assert stored.status is None
    # Issue #19 fix round item 8: the pending approval's own message is actually deleted too.
    deleted = [a["ts"] for a in h.slack.calls_to("chat.delete")]
    assert deleted == [request_ts]


async def test_waiting_for_owner_reflects_an_open_approval(
    harness_for: Callable[..., Harness],
) -> None:
    ask = CanUseToolCall("Bash", {"command": "ls"})
    h = harness_for({"turns": [[ask, *sdk_messages("tools")]]})
    session = h.session()
    await session.submit("list the files")
    await until(lambda: bool(h.approvals._pending))
    assert session.waiting_for_owner is True
    h.approvals.resolve(next(iter(h.approvals._pending)), CHANNEL, THREAD, Approve())
    await until(lambda: session.waiting_for_owner is False)


async def test_running_kinds_reflects_a_task_that_outlives_its_turn(
    harness_for: Callable[..., Harness],
) -> None:
    first = split_turns(sdk_messages("background"))[0]
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    assert session.busy is False
    assert session.running_kinds  # a task still runs, though the session itself is not busy


async def test_no_reply_ever_carries_a_channel_mention(harness_for: Callable[..., Harness]) -> None:
    # `<!channel>` was removed with the thread model: a full turn that ends with a closing
    # message, posts an approval request and a question request must never bring it back.
    recorded = sdk_json("ask-can-use-tool")
    ask = CanUseToolCall("Bash", {"command": "ls"})
    question = CanUseToolCall(recorded["tool_name"], recorded["input"])
    h = harness_for({"turns": [[ask, question, *sdk_messages("tools")]]})
    turn = await h.session().submit("do it")
    await until(lambda: bool(h.approvals._pending))
    approval_id = next(iter(h.approvals._pending))
    assert h.approvals.resolve(approval_id, CHANNEL, THREAD, Approve()) is not None
    await until(lambda: bool(h.approvals._pending))
    question_id = next(iter(h.approvals._pending))
    answers = {q["question"]: q["options"][0]["label"] for q in recorded["input"]["questions"]}
    assert h.approvals.resolve(question_id, CHANNEL, THREAD, Answer(answers)) is not None
    await asyncio.wait_for(turn.done.wait(), 2)
    for _, args in h.slack.calls:
        assert "<!channel>" not in json.dumps(args)


async def test_a_turn_with_a_task_an_approval_and_a_question_is_one_stream_and_two_posts(
    harness_for: Callable[..., Harness],
) -> None:
    # D1: a turn that starts a background task, asks for an approval and a question, then the
    # report turn for that task, writes exactly the approval request, the question request, and
    # one stream for the reply, which stops once, when the task's report is in. Nothing else.
    first, notice, injected = split_background()
    recorded = sdk_json("ask-can-use-tool")
    ask = CanUseToolCall("Bash", {"command": "ls"})
    question = CanUseToolCall(recorded["tool_name"], recorded["input"])
    h = harness_for({"turns": [[ask, question, *first]]})
    session = h.session()
    turn = await session.submit("do it and start something")
    await until(lambda: bool(h.approvals._pending))
    approval_id = next(iter(h.approvals._pending))
    assert h.approvals.resolve(approval_id, CHANNEL, THREAD, Approve()) is not None
    await until(lambda: bool(h.approvals._pending))
    question_id = next(iter(h.approvals._pending))
    answers = {q["question"]: q["options"][0]["label"] for q in recorded["input"]["questions"]}
    assert h.approvals.resolve(question_id, CHANNEL, THREAD, Answer(answers)) is not None
    await asyncio.wait_for(turn.done.wait(), 2)
    h.clients[0].inject(notice + injected)
    await until(lambda: bool(h.slack.calls_to("chat.stopStream")))
    await asyncio.sleep(0.05)
    posts = h.slack.calls_to("chat.postMessage")
    # D1: exactly these, in this order, nothing else.
    assert [p["text"] for p in posts] == ["Bash: ls", "AskUserQuestion"]
    assert len(h.slack.stream_ts) == 1 and len(h.slack.calls_to("chat.stopStream")) == 1
    assert h.slack.pushes() == 3


def test_asked_cuts_a_long_prompt_and_names_an_image_only_prompt() -> None:
    long_prompt = "word " * 30  # more characters than ASKED_LIMIT
    cut = sessions.asked(long_prompt)
    assert len(cut) == sessions.ASKED_LIMIT and cut.endswith("…")
    image_only = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}}
    ]
    assert sessions.asked(image_only) == texts.PROMPT_IMAGE  # type: ignore[arg-type]
    mixed = [{"type": "text", "text": "look at this"}, image_only[0]]
    assert sessions.asked(mixed) == "look at this"  # type: ignore[arg-type]


async def test_an_injected_turn_edits_the_reply_that_started_the_task(
    harness_for: Callable[..., Harness],
) -> None:
    turns = split_turns(sdk_messages("background"))
    h = harness_for({"turns": [turns[0]]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    for later in turns[1:]:
        h.clients[0].inject(later)
    await until(lambda: is_report(h.bodies()[0]))
    await asyncio.sleep(0.05)
    # D1: the report is appended to the one reply there is: no separate message carries it.
    assert len(h.slack.stream_ts) == 1 and h.slack.posted_ts == []


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


async def test_bind_is_refused_while_a_thread_of_the_channel_is_busy(
    harness_for: Callable[..., Harness], tmp_path: Path
) -> None:
    ask = CanUseToolCall("Bash", {"command": "ls"})
    h = harness_for({"turns": [[ask, *sdk_messages("tools")]]})
    session = h.session()
    await session.submit("list the files")
    await until(lambda: bool(h.approvals._pending))
    other = tmp_path / "other"
    other.mkdir()
    assert await h.manager.bind(CHANNEL, other) is False
    assert h.state.channel(CHANNEL).directory == h.tmp_path
    h.approvals.resolve(next(iter(h.approvals._pending)), CHANNEL, THREAD, Approve())
    await until(lambda: session.idle)


async def test_bind_accepted_when_every_thread_is_idle_keeps_the_old_thread_s_folder(
    harness_for: Callable[..., Harness], tmp_path: Path
) -> None:
    h = harness_for({"turns": [sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("list the files")).done.wait(), 2)
    other = tmp_path / "other"
    other.mkdir()
    assert await h.manager.bind(CHANNEL, other) is True
    assert h.state.channel(CHANNEL).directory == other
    # The thread already open is untouched: same folder, same live session, no closing.
    assert h.state.thread(CHANNEL, THREAD).directory == h.tmp_path
    assert session.directory == h.tmp_path and h.session() is session
    assert h.clients[0].connected is True
    # A thread opened after the bind gets the new folder.
    fresh = h.manager.open(CHANNEL, OTHER_THREAD)
    assert fresh is not None and fresh.directory == other


async def test_two_threads_of_one_channel_are_two_sessions_with_separate_clients(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({"turns": [sdk_messages("tools")]}, {"turns": [sdk_messages("tools")]})
    first = h.manager.open(CHANNEL, THREAD)
    second = h.manager.open(CHANNEL, OTHER_THREAD)
    assert first is not None and second is not None and first is not second
    assert h.manager.get(CHANNEL, THREAD) is first  # the same live session, not rebuilt
    await asyncio.wait_for((await first.submit("a")).done.wait(), 2)
    await asyncio.wait_for((await second.submit("b")).done.wait(), 2)
    assert len(h.clients) == 2
    assert h.clients[0].queries == ["a"] and h.clients[1].queries == ["b"]
    assert set(h.manager.sessions_of(CHANNEL)) == {first, second}
    starts = h.slack.calls_to("chat.startStream")
    assert {p.get("thread_ts") for p in starts} == {THREAD, OTHER_THREAD}


async def test_stop_channel_stops_every_busy_session_of_the_channel(
    harness_for: Callable[..., Harness],
) -> None:
    turn = [CanUseToolCall("Bash", {"command": "rm -rf build"}), *sdk_messages("interrupt")]
    h = harness_for({"turns": [turn]}, {"turns": [turn]})
    first = h.manager.open(CHANNEL, THREAD)
    second = h.manager.open(CHANNEL, OTHER_THREAD)
    a = await first.submit("a")
    b = await second.submit("b")
    await until(lambda: len(h.approvals._pending) == 2)
    assert await h.manager.stop_channel(CHANNEL) is True
    await asyncio.wait_for(asyncio.gather(a.done.wait(), b.done.wait()), 2)
    assert h.clients[0].interrupts == 1 and h.clients[1].interrupts == 1
    assert h.reactions(THREAD)[-1] == Status.DONE.value  # a stop is not an error
    assert h.reactions(OTHER_THREAD)[-1] == Status.DONE.value
    assert await h.manager.stop_channel(CHANNEL) is False


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
REPORT = re.compile(r'^[✓✗] (Background command|Agent) "', re.MULTILINE)


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
    await until(lambda: is_report(h.bodies()[0]))
    await asyncio.sleep(0.05)
    report = h.bodies()[0]  # D1: appended to the one reply there is
    summary = next(m.summary for m in notice if isinstance(m, TaskNotificationMessage))
    assert f"✓ {summary}" in report  # Claude Code's own words, as in the terminal
    assert texts.BACKGROUND_NOTICE not in report
    # The task has now fully ended (report in, nothing else owed): the closing message that
    # waited for it posts at last, with its footer.
    closing = h.slack.message_blocks()[-1]
    assert {"type": "divider"} in closing


async def test_a_report_turn_s_writes_debounce_like_any_other_reply(
    harness_for: Callable[..., Harness],
) -> None:
    # D1: the reply it renders into is already `_finished`, which used
    # to make every streamed delta flush its own `chat.update` instead of debouncing.
    first, notice, injected = split_background()
    deltas = sum(
        1
        for m in injected
        if isinstance(m, StreamEvent)
        and m.event.get("type") == "content_block_delta"
        and (m.event.get("delta") or {}).get("type") == "text_delta"
    )
    assert deltas > 3, "the recording should have enough deltas to prove debouncing"
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    before = len(h.slack.calls_to("chat.update"))
    h.clients[0].inject(notice + injected)
    await until(lambda: is_report(h.bodies()[0]))
    await asyncio.sleep(0.2)
    updates = len(h.slack.calls_to("chat.update")) - before
    assert updates <= 3, f"{updates} chat.update calls for one report turn (no debounce)"


async def test_an_owner_answer_behind_a_wrong_guess_keeps_its_footer(
    harness_for: Callable[..., Harness],
) -> None:
    first, notice, _ = split_background()
    h = harness_for({"turns": [first]})
    await asyncio.wait_for((await h.session().submit("start it")).done.wait(), 2)
    h.clients[0].inject(notice)  # the session now expects Claude Code's own turn
    await asyncio.sleep(0.05)
    h.clients[0].inject(sdk_messages("tools"))  # but the result says a person asked for it
    await until(lambda: bool(h.slack.calls_to("chat.stopStream")))
    # the answer joins the reply that waited for the task, which ends with the footer
    assert {"type": "divider"} in h.slack.calls_to("chat.stopStream")[-1]["blocks"]


async def test_a_task_type_is_forgotten_when_the_task_ends(
    harness_for: Callable[..., Harness],
) -> None:
    first, notice, injected = split_background()
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    assert session._tasks
    h.clients[0].inject(notice + injected)
    await until(lambda: is_report(h.bodies()[0]))
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
    await until(lambda: is_report(h.bodies()[0]))
    await asyncio.sleep(0.05)
    line = next(  # D1: appended to the one reply there is, not a body of its own
        line for line in h.bodies()[0].splitlines() if line.startswith('✓ Agent "')
    )
    assert line.startswith(f'✓ Agent "{started.description}" finished · 10s')
    assert "README.md" not in line


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


async def test_a_suppressed_notification_s_closing_still_posts_eventually(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    # D1: the CLI can suppress the notification altogether (SDK
    # TaskUpdatedMessage docstring). Only the terminal task_updated arrives; the closing message
    # must still post once INJECTED_TURN_WAIT passes, not wait on it forever.
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.2)
    first, notice, _ = split_background()
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    a = session._task_replies["bny2rux7d"]
    h.clients[0].inject([m for m in notice if not isinstance(m, TaskNotificationMessage)])
    await until(lambda: a.closed_out, limit=1.0)


async def test_a_report_whose_target_already_closed_out_gets_its_own_reply(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    # D1: a notification arriving later than INJECTED_TURN_WAIT lets `_expire_unreported` close
    # the reply out first; the report turn the CLI starts once that late notification finally
    # comes must not render into that already-closed reply (an edit, which never notifies, with
    # any overflow posting below the closing it can no longer touch): it gets a fresh reply.
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.1)
    first, notice, injected = split_background()
    ended = [m for m in notice if not isinstance(m, TaskNotificationMessage)]
    notification = [m for m in notice if isinstance(m, TaskNotificationMessage)]
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    a = session._task_replies["bny2rux7d"]
    h.clients[0].inject(ended)
    # closed out already, on the late-notification path
    await until(lambda: a.closed_out, limit=1.0)
    h.clients[0].inject(notification + injected)
    await until(lambda: any(is_report(r) for r in h.replies()))
    await asyncio.sleep(0.05)
    assert not is_report(h.bodies()[0])  # a's own body is untouched
    assert any(is_report(r) for r in h.bodies()[1:])  # the report got a reply of its own


async def test_the_unreported_expiry_timer_does_not_outlive_close(
    harness_for: Callable[..., Harness],
) -> None:
    # D1: `_expire_unreported`'s own task lives in `_expiring` (its
    # own set, kept apart from `_background`'s shared-client tasks), which `close` must cancel
    # along with everything else, or it would try to post through a session that is already
    # gone once its (real, 30s) wait finally elapses.
    first, notice, _ = split_background()
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    h.clients[0].inject([m for m in notice if not isinstance(m, TaskNotificationMessage)])
    await until(lambda: bool(session._unreported))
    await session.close(reason=texts.ENDED_IDLE)
    assert not any(not t.done() for t in session._expiring)


async def test_closing_a_session_does_not_cancel_a_pending_usage_refresh(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    # D1: a usage refresh shares one UsageProbe, and its one client,
    # across every session; `UsageProbe.__call__` does not handle being cancelled mid-query,
    # which would leave that shared client answering the next lookup, of any session, late.
    # `close` cancels `_expiring`'s own timers, never `_background`'s.
    h = harness_for({})
    session = h.session()
    started = asyncio.Event()

    async def slow_refresh() -> None:
        started.set()
        await asyncio.sleep(10)

    monkeypatch.setattr(h.deps.usage, "refresh_if_stale", slow_refresh)
    session._refresh_usage()
    await asyncio.wait_for(started.wait(), 2)
    task = next(iter(session._background))
    await session.close(reason=texts.ENDED_IDLE)
    assert not task.done()
    task.cancel()  # nothing else will, once the test is done with it


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


def split_nested_command() -> tuple[list[Any], list[Any], list[Any], list[int]]:
    """The recorded subagent that runs two long commands: the owner's turn, what the main stream
    carries while the agent works (each command's own task, then the agent's), the report turn,
    and the index in the middle part where each notification ends."""
    first, later = split_turns(sdk_messages("subagent-nested-command"))[:2]
    start = next(
        i for i, m in enumerate(later) if isinstance(m, SystemMessage) and m.subtype == "init"
    )
    work = later[:start]
    ends = [i for i, m in enumerate(work) if isinstance(m, TaskNotificationMessage)]
    return first, work, later[start:], ends


def split_nested_background() -> tuple[list[Any], list[Any], list[Any], list[Any], list[Any]]:
    """The recorded subagent whose command outlives it: the owner's turn; what the main stream
    carries until the agent's first end; the report turn; the command's end and the agent's
    second start and end; the second report turn."""
    first, middle, third = split_turns(sdk_messages("subagent-nested-background"))
    start = next(
        i for i, m in enumerate(middle) if isinstance(m, SystemMessage) and m.subtype == "init"
    )
    again = next(
        i for i, m in enumerate(third) if isinstance(m, SystemMessage) and m.subtype == "init"
    )
    return first, middle[:start], middle[start:], third[:again], third[again:]


def renamed_background() -> tuple[list[Any], list[Any], list[Any]]:
    """The same recorded background run as `split_background`, with its task and tool ids
    changed so a second one can run alongside the first with no id collision (D1)."""
    raw = (FIXTURES / "sdk" / "background.jsonl").read_text()
    raw = raw.replace("bny2rux7d", "bc41other").replace(
        "toolu_01LiZwhcy5g12fYSVLgAq5TS", "toolu_01OTHERxxxxxxxxxxxxxxxxx"
    )
    messages = [m for m in (parse_message(json.loads(line)) for line in raw.splitlines()) if m]
    first, later = split_turns(messages)[:2]
    start = next(
        i for i, m in enumerate(later) if isinstance(m, SystemMessage) and m.subtype == "init"
    )
    return first, later[:start], later[start:]


async def test_a_report_turn_starting_over_the_expiry_s_write_leaves_the_root_on_done(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The report turn starts after INJECTED_TURN_WAIT, while `_expire_injected_turn` already
    # writes the end of the reply that started the task: `_start_turn` cancels that write, which
    # must not leave the reply's end unresolved (and ⏳ on the root for good).
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.05)
    first, notice, injected = split_background()
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    a = session._task_replies["bny2rux7d"]
    gate = h.slack.gate = asyncio.Event()
    h.slack.gated.clear()
    h.clients[0].inject(notice)
    await asyncio.wait_for(h.slack.gated.wait(), 1.0)  # the expiry's end is inside its write
    h.clients[0].inject(injected)  # the report turn starts over that write
    await until(lambda: session._active is not None, limit=1.0)
    h.slack.gate = None
    gate.set()
    await until(lambda: h.reactions()[-1:] == [Status.DONE.value] and session.idle, limit=3.0)
    assert session._unlanded == set()
    assert await asyncio.wait_for(a.sink.wait_landed(), 1.0) is True


async def test_two_notifications_a_moment_apart_end_both_replies_and_show_done(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.05)
    first, notice, injected = split_background()
    first_b, notice_b, _ = renamed_background()
    h = harness_for({"turns": [first, first_b]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start A")).done.wait(), 2)
    await asyncio.wait_for((await session.submit("start B")).done.wait(), 2)
    a = session._task_replies["bny2rux7d"]
    b = session._task_replies["bc41other"]
    gate = h.slack.gate = asyncio.Event()
    h.slack.gated.clear()
    h.clients[0].inject(notice)
    await asyncio.wait_for(h.slack.gated.wait(), 1.0)  # the expiry's end of A is in its write
    h.clients[0].inject(notice_b + injected)  # the report turn starts over it
    await until(lambda: session._active is not None, limit=1.0)
    h.slack.gate = None
    gate.set()
    await until(lambda: a.closed_out and b.closed_out, limit=2.0)
    await until(lambda: h.reactions()[-1:] == [Status.DONE.value] and session.idle, limit=3.0)
    assert session._unlanded == set() and not session._injected_expected
    assert await asyncio.wait_for(a.sink.wait_landed(), 1.0) is True
    assert await asyncio.wait_for(b.sink.wait_landed(), 1.0) is True
    assert not any(m.streaming for m in h.slack.messages.values())


async def test_a_report_turn_keeps_the_reply_it_renders_into_open_while_the_expiry_sweeps(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Guard against letting the expiry's work run beside a turn (`_start_turn`'s cancel is the
    # exclusion). Replies A and B each hold a task. The first notification's wait passes and the
    # expiry's sweep stops on the end of that reply; the second notification arrives and the
    # report turn starts, rendering into the second reply. A sweep that resumed under the turn
    # would close that reply with its snapshot of `active` (None). `_sweep_closed_out` walks a
    # set, whose order follows the renderers' hashes; the order it needs (A first) is fixed below.
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.05)
    first, notice, injected = split_background()
    first_b, notice_b, _ = renamed_background()
    h = harness_for({"turns": [first, first_b]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start A")).done.wait(), 2)
    await asyncio.wait_for((await session.submit("start B")).done.wait(), 2)
    first_reply = session._task_replies["bny2rux7d"]
    target = session._task_replies["bc41other"]  # the second notified: the turn renders there
    rank = {id(first_reply): 1, id(target): 2}
    monkeypatch.setattr(
        TurnRenderer, "__hash__", lambda self: rank.get(id(self), object.__hash__(self))
    )
    gate = h.slack.gate = asyncio.Event()
    h.slack.gated.clear()
    h.clients[0].inject(notice)
    await asyncio.wait_for(h.slack.gated.wait(), 1.0)
    h.clients[0].inject(notice_b + injected[:3])  # the turn's first message starts it
    await until(lambda: session._active is not None, limit=1.0)
    h.slack.gate = None
    gate.set()
    await asyncio.sleep(0.05)  # whatever the sweep still does, it has done by now
    assert not target.closed_out
    h.clients[0].inject(injected[3:])
    await until(lambda: target.closed_out, limit=3.0)
    await until(lambda: not any(m.streaming for m in h.slack.messages.values()), limit=2.0)
    shown = h.slack.message_blocks()[1]
    assert "49.2k" in shown[-1]["elements"][0]["text"]  # the report turn's footer, not the first's


async def test_an_owner_prompt_sent_during_the_expiry_s_work_runs_after_it(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.05)
    first, notice, _ = split_background()
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    a = session._task_replies["bny2rux7d"]
    gate = h.slack.gate = asyncio.Event()
    h.slack.gated.clear()
    h.clients[0].inject(notice)
    await asyncio.wait_for(h.slack.gated.wait(), 1.0)  # the expiry's end is inside its write
    turn = await asyncio.wait_for(session.submit("and now?"), 2)
    h.clients[0].inject(sdk_messages("tools"))  # its turn starts over that write
    await until(lambda: session._active is not None, limit=1.0)
    h.slack.gate = None
    gate.set()
    await asyncio.wait_for(turn.done.wait(), 2)
    await until(lambda: h.reactions()[-1:] == [Status.DONE.value] and session.idle, limit=2.0)
    assert session._unlanded == set()
    assert await asyncio.wait_for(a.sink.wait_landed(), 1.0) is True


async def test_a_close_during_the_expiry_s_work_ends_promptly_and_leaks_no_task(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.05)
    first, notice, _ = split_background()
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    a = session._task_replies["bny2rux7d"]
    # A delay, not the gate: `close` settles every reply, which waits for the write in flight,
    # so a held write would hold the close too.
    h.slack.delay = 0.1
    h.clients[0].inject(notice)
    await until(lambda: a.closed_out, limit=1.0)  # the expiry is writing
    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(session.close(), 2)
    assert asyncio.get_running_loop().time() - started < 1.0
    h.slack.delay = 0
    await asyncio.sleep(0.2)  # the write Slack still held answers, then nothing is left
    leaked = [
        t.get_name()
        for t in asyncio.all_tasks()
        if any(name in repr(t) for name in ("_expire_injected_turn", "_end_out"))
    ]
    assert leaked == []
    assert await asyncio.wait_for(a.sink.wait_landed(), 1.0) is True


async def two_replies_with_a_gate(
    h: Harness, session: Any
) -> tuple[list[Any], list[Any], list[Any], Any, asyncio.Event]:
    """Two replies, each holding a background task, and the Slack gate armed on the first
    notification's expiry (the end of a reply is where it holds). Returns the frames of the
    first notification, those of the second, the report turn, the second reply and the gate."""
    _, notice, injected = split_background()
    _, notice_b, _ = renamed_background()
    await asyncio.wait_for((await session.submit("start A")).done.wait(), 2)
    await asyncio.wait_for((await session.submit("start B")).done.wait(), 2)
    gate = h.slack.gate = asyncio.Event()
    h.slack.gated.clear()
    return notice, notice_b, injected, session._task_replies["bc41other"], gate


async def test_a_notification_during_the_expiry_s_standalone_write_waits_for_its_own_turn(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The expiry posts a background update (a task of no tracked reply) and a second notification
    # arrives while that write is out. Its wait starts when the write ends, not never: the session
    # keeps reading as waiting for the report turn, and the owner's prompt stays behind it.
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.05)
    first, _, _ = split_background()
    first_b, _, _ = renamed_background()
    h = harness_for({"turns": [first, first_b]})
    session = h.session()
    notice, notice_b, injected, b, gate = await two_replies_with_a_gate(h, session)
    del session._task_replies["bny2rux7d"]  # A's reply is gone: its frames are held
    h.clients[0].inject(notice)
    await asyncio.wait_for(h.slack.gated.wait(), 1.0)  # the expiry's standalone is in its write
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 30)  # the next wait outlives the test
    h.clients[0].inject(notice_b)
    await until(lambda: session._injected_expected)
    working = session._expiry
    h.slack.gate = None
    gate.set()
    await until(lambda: session._expiry is not working)  # the wait of the second notification
    assert len(h.slack.created_ts) == 3  # the expiry posted the held update, a reply of its own
    assert not session._expiry.done()
    assert session._injected_expected and not session._settled.is_set() and not session.idle
    await session.submit("and now?")
    await asyncio.sleep(0.1)
    assert h.clients[0].queries == ["start A", "start B"]  # held behind the report turn
    assert Status.DONE.value not in h.reactions()[2:]
    h.clients[0].inject(injected)
    await until(lambda: b.closed_out and session._settled.is_set(), limit=3.0)
    assert not session._injected_expected


async def test_a_notification_during_the_expiry_s_sweep_gets_its_wait_started_after_it(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The sweep ends A's reply while the second notification arrives: with nothing armed for it
    # the session would read as waiting for ever, holding every prompt behind a turn that may
    # never come. Its own wait runs out, and the session reads idle again.
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.05)
    first, _, _ = split_background()
    first_b, _, _ = renamed_background()
    h = harness_for({"turns": [first, first_b]})
    session = h.session()
    notice, notice_b, _, b, gate = await two_replies_with_a_gate(h, session)
    a = session._task_replies["bny2rux7d"]
    h.clients[0].inject(notice)
    await asyncio.wait_for(h.slack.gated.wait(), 1.0)  # the sweep is in the end of A
    h.clients[0].inject(notice_b)
    await until(lambda: session._injected_expected)
    h.slack.gate = None
    gate.set()
    await until(lambda: a.closed_out and b.closed_out, limit=3.0)
    await until(lambda: h.reactions()[-1:] == [Status.DONE.value] and session.idle, limit=3.0)
    assert not session._injected_expected and session._settled.is_set()
    assert session._unlanded == set()


async def test_a_close_while_a_notification_waits_after_the_expiry_s_work_leaks_no_task(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.05)
    first, _, _ = split_background()
    first_b, _, _ = renamed_background()
    h = harness_for({"turns": [first, first_b]})
    session = h.session()
    notice, notice_b, _, _, gate = await two_replies_with_a_gate(h, session)
    h.clients[0].inject(notice)
    await asyncio.wait_for(h.slack.gated.wait(), 1.0)
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 30)
    h.clients[0].inject(notice_b)
    await until(lambda: session._injected_expected)
    working = session._expiry
    h.slack.gate = None
    gate.set()
    await until(lambda: session._expiry is not working)
    waiting = session._expiry
    assert waiting is not None and not waiting.done()
    await asyncio.wait_for(session.close(), 2)
    assert waiting.done()
    assert not [t for t in asyncio.all_tasks() if "_expire_injected_turn" in repr(t)]


async def test_a_close_during_the_expiry_s_write_with_a_notification_pending_arms_no_timer(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The expiry task `close` is cancelling must not arm the wait of the notification that came
    # during its write: `close` cancels twice, which is all that would catch such a timer.
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.05)
    created = 0
    original = sessions.ThreadSession._expire_injected_turn

    def counting(self: Any) -> Any:  # counts at creation: a timer cancelled unstarted counts too
        nonlocal created
        created += 1
        return original(self)

    monkeypatch.setattr(sessions.ThreadSession, "_expire_injected_turn", counting)
    first, _, _ = split_background()
    first_b, _, _ = renamed_background()
    h = harness_for({"turns": [first, first_b]})
    session = h.session()
    notice, notice_b, _, _, gate = await two_replies_with_a_gate(h, session)
    del session._task_replies["bny2rux7d"]  # A's frames are held: the expiry posts them
    h.clients[0].inject(notice)
    await asyncio.wait_for(h.slack.gated.wait(), 1.0)  # the standalone is in its write
    h.clients[0].inject(notice_b)
    await until(lambda: session._injected_expected)
    closing = asyncio.create_task(session.close())
    await until(lambda: session.closed)
    h.slack.gate = None
    gate.set()
    await asyncio.wait_for(closing, 2)
    calls = len(h.slack.calls)
    await asyncio.sleep(0.2)
    assert created == 1  # the one that is writing, and no other
    assert not [t for t in asyncio.all_tasks() if "_expire_injected_turn" in repr(t)]
    assert len(h.slack.calls) == calls  # nothing writes to Slack after the close


async def test_a_sweep_that_fails_still_arms_the_wait_of_a_notification_that_came_during_it(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.05)
    first, _, _ = split_background()
    first_b, _, _ = renamed_background()
    h = harness_for({"turns": [first, first_b]})
    session = h.session()
    notice, notice_b, *_ = await two_replies_with_a_gate(h, session)
    h.slack.gate = None
    sweeps = 0

    async def failing(self: Any) -> None:
        nonlocal sweeps
        if asyncio.current_task() is not session._expiry:
            return  # `_expire_unreported` sweeps too: only the expiry's own sweep fails
        sweeps += 1
        if sweeps == 1:
            h.clients[0].inject(notice_b)  # a notification arrives during the sweep's writes
            await until(lambda: session._injected_expected)
            monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 30)
            raise RuntimeError("the sweep failed")

    monkeypatch.setattr(sessions.ThreadSession, "_sweep_closed_out", failing)
    h.clients[0].inject(notice)
    await until(lambda: sweeps == 1)
    working = session._expiry
    await until(lambda: session._expiry is not working)
    assert not session._expiry.done()
    assert session._injected_expected and not session._settled.is_set()


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
    background = [is_report(r) for r in h.bodies()]
    # D1: the report is appended to the reply that started the task, not a new one of its own.
    assert background == [True, False]


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
    # D1: the running count lives in the footer, and that one waits for the task; the body never
    # carries it either, so nothing shows it yet.
    assert all(running_block(blocks) is None for blocks in h.slack.message_blocks())
    await asyncio.wait_for((await session.submit("next")).done.wait(), 2)
    # the first reply's own stop is still deferred; the new, unrelated reply stops at once and
    # carries the count in its own footer.
    assert h.slack.calls_to("chat.delete") == []
    assert running_block(h.slack.message_blocks()[-1]) == "⏳ 1 shell"
    h.clients[0].inject(notice + injected)
    await until(lambda: is_report(h.bodies()[0]))
    # the task has ended: no reply's footer shows a running count any more. The second reply
    # already stopped, so this update of it debounces like any other change to a finished reply
    # (D1): `until` gives it the room to land.
    await until(lambda: all(running_block(blocks) is None for blocks in h.slack.message_blocks()))


async def test_two_prompts_in_a_row_each_reply_ends_with_its_own_stop(
    harness_for: Callable[..., Harness],
) -> None:
    first, notice, injected = split_background()
    h = harness_for({"turns": [first, sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    # D1: the first reply's stream stays open for its background task: nothing has stopped yet.
    assert len(h.slack.stream_ts) == 1 and h.slack.calls_to("chat.stopStream") == []
    await asyncio.wait_for((await session.submit("next")).done.wait(), 2)
    # the second reply has nothing of its own pending: its stream stops right away, the footer
    # on the stop.
    assert len(h.slack.stream_ts) == 2 and len(h.slack.calls_to("chat.stopStream")) == 1
    assert {"type": "divider"} in h.slack.message_blocks()[-1]
    h.clients[0].inject(notice + injected)
    await until(lambda: len(h.slack.calls_to("chat.stopStream")) == 2)
    # the first reply stops now, once its task has fully ended, with a footer of its own: no
    # longer the latest, it still says how its last turn ended. The second's stop is untouched.
    assert {"type": "divider"} in h.slack.calls_to("chat.stopStream")[-1]["blocks"]
    assert h.slack.pushes() == 2


async def test_two_prompts_tasks_ending_together_each_reply_ends(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    # D1: the CLI's one report turn opens with the end of every task
    # that finished together but renders into only the first one's reply; the second must still
    # end, not wait forever for a report turn that was never coming for it specifically.
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.2)
    first, notice, injected = split_background()
    first_b, notice_b, _ = renamed_background()
    h = harness_for({"turns": [first, first_b]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start A")).done.wait(), 2)
    await asyncio.wait_for((await session.submit("start B")).done.wait(), 2)
    a = session._task_replies["bny2rux7d"]
    b = session._task_replies["bc41other"]
    assert a is not b
    # Both tasks end while idle, then Claude Code's one report turn, which renders into A's
    # reply (the first of the two ended tasks).
    h.clients[0].inject(notice + notice_b + injected)
    await until(lambda: a.closed_out)
    await until(lambda: b.closed_out)
    await until(lambda: not any(m.streaming for m in h.slack.messages.values()))
    assert len(h.slack.calls_to("chat.stopStream")) == 2


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


async def test_a_background_agent_s_calls_update_its_card_and_open_no_reply(
    harness_for: Callable[..., Harness],
) -> None:
    recorded = sdk_messages("subagent")
    turn = [m for m in split_turns(recorded)[0] if getattr(m, "parent_tool_use_id", None) is None]
    children = [m for m in recorded if getattr(m, "parent_tool_use_id", None) is not None]
    h = harness_for({"turns": [turn]})
    await asyncio.wait_for((await h.session().submit("start it")).done.wait(), 2)
    posted = len(h.slack.posted_ts)
    h.clients[0].inject(children)
    # A task that outlived its own turn updating its card afterward debounces like any other
    # change (D1): `until` gives it the room to land.
    await until(lambda: any("Bash" in c.get("details", "") for c in h.cards()))
    assert len(h.slack.posted_ts) == posted and len(h.slack.stream_ts) == 1
    [agent] = [c for c in h.cards() if c["title"].startswith("Agent")]
    assert "Bash" in agent["details"] and " call" in agent["title"]


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
    await until(lambda: is_report(h.bodies()[0]))
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
    assert "✗" in started and " ".join(summary.split())[:40] in started


async def test_closing_the_session_stops_the_cards_of_running_tasks(
    harness_for: Callable[..., Harness],
) -> None:
    first, _, _ = split_background()
    h = harness_for({"turns": [first]})
    await asyncio.wait_for((await h.session().submit("start it")).done.wait(), 2)
    await h.manager.close_all()
    assert running_block(h.slack.message_blocks()[0]) is None
    assert [(c["status"], c.get("output")) for c in h.cards()] == [("complete", "Stopped")]
    assert not any(m.streaming for m in h.slack.messages.values())


async def test_a_shutdown_during_an_active_turn_with_a_background_task_ends_both_replies(
    harness_for: Callable[..., Harness],
) -> None:
    # D1: a restart or shutdown while a turn is active, with an earlier background task still
    # running: `_close_reply(force=True)` runs before `_stop_task_replies` has actually stopped
    # the task, and both replies still end with their streams stopped, nothing posted.
    first, _, _ = split_background()
    partial = [m for m in sdk_messages("tools") if type(m).__name__ != "ResultMessage"][:22]
    h = harness_for({"turns": [first, partial]})  # the second query gets no scripted result
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    await session.submit("second")
    await until(lambda: session._active is not None)
    await session.close()  # a restart or a shutdown: ends everything
    assert h.slack.posted_ts == []
    assert not any(m.streaming for m in h.slack.messages.values())
    assert len(h.slack.stream_ts) == 2


async def test_an_idle_close_ends_a_reply_that_still_waited_for_its_task(
    harness_for: Callable[..., Harness],
) -> None:
    # D1: an idle close (like a restart or SessionGone) stops the still-open stream at once, with
    # its footer.
    first, _, _ = split_background()
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    assert h.slack.calls_to("chat.stopStream") == []
    await session.close(reason=texts.ENDED_IDLE)
    [stop] = h.slack.calls_to("chat.stopStream")
    assert {"type": "divider"} in stop["blocks"]
    assert h.slack.posted_ts == []


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
    # That line updates a reply whose own turn has already finished, which debounces like any
    # other change to a finished reply (D1): `until` gives it the room to land.
    await until(lambda: [c["status"] for c in h.cards()] == ["complete"])
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
    assert not any(is_report(r) for r in h.replies())


async def test_a_slack_network_error_does_not_stop_the_session(
    harness_for: Callable[..., Harness],
) -> None:
    import aiohttp

    h = harness_for({"turns": [sdk_messages("tools"), sdk_messages("tools")]})
    down = aiohttp.ClientConnectionError("network down")
    for method in WRITES:
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


async def test_a_cli_that_exits_mid_turn_ends_its_reply_once(
    harness_for: Callable[..., Harness],
) -> None:
    # Cut right after the reply's first message_start: where that falls among the init, status
    # and rate-limit messages changes between CLI releases.
    tools = sdk_messages("tools")
    started = next(
        i
        for i, m in enumerate(tools)
        if isinstance(m, StreamEvent) and m.event["type"] == "message_start"
    )
    h = harness_for({"turns": [[*tools[: started + 1], EndOfStream()]]})
    session = h.session()
    await asyncio.wait_for((await session.submit("first")).done.wait(), 2)
    assert len(h.slack.stream_ts) == 1 and h.slack.pushes() == 1
    assert not any(m.streaming for m in h.slack.messages.values())


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
    for method in WRITES:
        h.slack.responses[method] = aiohttp.ClientConnectionError("network down")
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
    assert h.bodies() == [texts.DIRECTORY_MISSING.format(directory=gone)]
    assert h.slack.pushes() == 1  # the reply's one stop: an error the owner has to act on


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
    assert h.bodies() == [texts.DIRECTORY_UNREADABLE.format(directory=locked)]
    assert h.slack.pushes() == 1


def statuses(h: Harness) -> list[str]:
    """The footer (the last context block) of every write that carried one, in order."""
    lasts = []
    for m, a in h.slack.calls:
        blocks = a.get("blocks") or []
        if (
            m in ("chat.postMessage", "chat.update", "chat.stopStream")
            and blocks
            and blocks[-1]["type"] == "context"
        ):
            lasts.append(blocks[-1]["elements"][0]["text"])
    return lasts


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
    assert "*effort* high" in statuses(h)[-1]


def with_stop_hook(turn: list[Message], hook_input: dict[str, Any]) -> list[Any]:
    """A recorded turn with the CLI's Stop hook call where the CLI makes it: before the result."""
    return [*turn[:-1], HookRun(hook_input), turn[-1]]


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
    assert "*effort* medium" in statuses(h)[-1]


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
    assert "*effort* default" in statuses(h)[-1]


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
    assert "*effort* low" in statuses(h)[-1]
    await h.manager.close_all()
    await asyncio.wait_for((await h.session().submit("next")).done.wait(), 2)
    assert "*effort* medium" in statuses(h)[-1]


async def test_an_approval_slack_refuses_to_show_is_denied_and_logged(
    harness_for: Callable[..., Harness], caplog: pytest.LogCaptureFixture
) -> None:
    from slack_sdk.errors import SlackApiError

    refused = SlackApiError("ratelimited", {"ok": False, "error": "ratelimited"})
    # The approval request is the only post: Slack refuses it; the reply's stream is unaffected.
    h = harness_for(
        {"turns": [[CanUseToolCall("Bash", {"command": "ls"}), *sdk_messages("tools")]]}
    )
    h.slack.responses["chat.postMessage"] = refused
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
    [stop] = h.slack.calls_to("chat.stopStream")
    assert "blocks" not in stop  # no footer to show, and the stream still stopped
    assert not any(m.streaming for m in h.slack.messages.values())


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
    assert "1.5k *tok*" in statuses(h)[-1]


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
    # no reply had started: the taken message is named in one message of its own
    [note] = h.slack.calls_to("chat.postMessage")
    assert texts.NOT_SENT_ONE.format(because=texts.BECAUSE_SHUTDOWN) in note["text"]


async def test_a_setup_failure_after_connect_closes_the_client(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({"server_info_error": RuntimeError("x")}, {})
    with pytest.raises(RuntimeError):
        await h.session().ensure_connected()
    assert h.clients[0].connected is False
    await h.session().ensure_connected()  # the next attempt starts one process, not two
    assert len(h.clients) == 2 and h.clients[1].connected


async def test_a_bind_after_a_turn_never_touches_that_thread_s_own_record(
    harness_for: Callable[..., Harness], tmp_path: Path
) -> None:
    messages = sdk_messages("tools")
    h = harness_for({"turns": [messages]})
    result = messages[-1]
    assert isinstance(result, ResultMessage)
    await asyncio.wait_for((await h.session().submit("list the files")).done.wait(), 2)
    assert h.state.thread(CHANNEL, THREAD).session_id == result.session_id
    other = tmp_path / "other"
    other.mkdir()
    assert await h.manager.bind(CHANNEL, other) is True  # affects only the channel's next thread
    assert h.state.channel(CHANNEL).directory == other
    assert h.state.thread(CHANNEL, THREAD).directory == h.tmp_path
    assert h.state.thread(CHANNEL, THREAD).session_id == result.session_id


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


async def test_a_listed_model_without_a_value_is_left_out(
    harness_for: Callable[..., Harness],
) -> None:
    # The setup builds one option per entry from `value`: an entry a future CLI lists without
    # one must not break every first prompt, so it is dropped where the list is read.
    info = {
        "commands": [],
        "current_permission_mode": "default",
        "models": [
            {"value": "default", "displayName": "Default (recommended)"},
            {"displayName": "No value"},
            {"value": ""},
            "not an entry",
            {"value": "haiku", "displayName": "Haiku 4.5"},
        ],
    }
    h = harness_for({"server_info": info})
    session = h.session()
    await session.ensure_connected()
    assert [m["value"] for m in session.models] == ["default", "haiku"]


async def test_every_message_is_posted_without_link_previews(
    harness_for: Callable[..., Harness],
) -> None:
    ask = CanUseToolCall("Bash", {"command": "ls"})
    h = harness_for({"turns": [[ask, *sdk_messages("tools")]]})
    turn = await h.session().submit("list the files")
    await until(lambda: len(h.approvals._pending) == 1)
    h.approvals.resolve(next(iter(h.approvals._pending)), CHANNEL, THREAD, Approve())
    await asyncio.wait_for(turn.done.wait(), 2)
    posts = h.slack.calls_to("chat.postMessage")
    assert posts  # the approval request
    assert all(p.get("unfurl_links") is False and p.get("unfurl_media") is False for p in posts)


async def test_resume_creates_a_new_thread_already_on_the_given_session(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({"turns": [sdk_messages("tools")]}, {})
    await asyncio.wait_for((await h.session().submit("list the files")).done.wait(), 2)
    other = "68da9311-0000-4000-8000-00000000abcd"
    resumed = await h.manager.resume(CHANNEL, OTHER_THREAD, other)
    assert resumed is not None
    assert h.state.thread(CHANNEL, OTHER_THREAD).session_id == other
    assert h.state.thread(CHANNEL, THREAD).session_id != other  # the original thread is untouched
    await resumed.ensure_connected()  # the next message on the new thread starts on that session
    assert h.clients[-1].options.resume == other


async def test_resume_creates_a_thread_with_bypass_off_regardless_of_others(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({}, {})
    await h.session().set_bypass(True)  # bypass is per thread (D3): only the original thread's
    resumed = await h.manager.resume(CHANNEL, OTHER_THREAD, "68da9311-0000-4000-8000-00000000abcd")
    assert resumed is not None and not resumed.bypass
    await resumed.ensure_connected()
    assert h.clients[-1].modes == []


async def test_resume_creates_an_independent_thread_even_while_another_is_busy(
    harness_for: Callable[..., Harness],
) -> None:
    ask = CanUseToolCall("Bash", {"command": "ls"})
    h = harness_for({"turns": [[ask, *sdk_messages("tools")]]})
    await h.session().submit("list the files")
    await until(lambda: len(h.approvals._pending) == 1)
    resumed = await h.manager.resume(CHANNEL, OTHER_THREAD, "68da9311-0000-4000-8000-00000000abcd")
    assert resumed is not None
    assert h.state.thread(CHANNEL, OTHER_THREAD) is not None
    assert h.session().busy  # the original thread's turn is unaffected


async def test_resume_returns_none_when_the_channel_is_unbound(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for()
    assert await h.manager.resume("C000OTHER", THREAD, "some-session") is None


def test_open_returns_none_when_the_channel_is_unbound(harness_for: Callable[..., Harness]) -> None:
    h = harness_for()
    assert h.manager.open("C000OTHER", THREAD) is None


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
    assert statuses(h)[-1].startswith("claude-haiku-4-5-20251001 · ")
    assert f" · {folder.name} · " in statuses(h)[-1]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo one level down the channel's folder, which is no repo itself."""
    repo = tmp_path / "app"
    subprocess.run(["git", "init", "-q", "-b", "feature-x", str(repo)], check=True)
    return repo


async def test_the_footer_follows_the_folder_the_session_works_in(
    harness_for: Callable[..., Harness], tmp_path: Path, repo: Path
) -> None:
    # The layout where the bound folder's branch was always missing (#37). The folder shown
    # stays the channel's, where the owner bound it; the branch is the session's.
    moved = {**sdk_json("stop-hook"), "cwd": str(repo)}
    h = harness_for({"turns": [with_stop_hook(sdk_messages("tools"), moved)]})
    session = h.session()
    await asyncio.wait_for((await session.submit("list the files")).done.wait(), 2)
    assert re.search(
        rf" · {re.escape(tmp_path.name)} · feature-x · \(\+0,-0\) · [\d.]+[kM]? \*tok\* · \*ctx\* ",
        statuses(h)[-1],
    )
    lines = (await session.status()).splitlines()
    assert lines[0] == f"Directory: `{tmp_path}`"
    values = lines[lines.index("Now: idle") + 1 :]
    assert values[0] == f"Working in: `{repo}`"
    assert "Branch: `feature-x`" in values and "Uncommitted: `(+0,-0)`" in values


async def test_a_tool_s_hook_moves_the_branch_when_no_stop_hook_runs(
    harness_for: Callable[..., Harness], tmp_path: Path, repo: Path
) -> None:
    # A turn stopped or failed runs no Stop hook; PostToolUse still reports where the session
    # went after the tool (measured 2026-09-27 on 2.1.283), so the footer is not left behind.
    messages = sdk_messages("tools")
    first_result = next(
        i
        for i, m in enumerate(messages)
        if isinstance(m, UserMessage)
        and isinstance(m.content, list)
        and any(isinstance(b, ToolResultBlock) for b in m.content)
    )
    moved = {**sdk_json("post-tool-use-hook"), "cwd": str(repo)}
    hook = HookRun(moved, "PostToolUse")
    turn = [*messages[: first_result + 1], hook, *messages[first_result + 1 :]]
    h = harness_for({"turns": [turn]})
    session = h.session()
    await asyncio.wait_for((await session.submit("list the files")).done.wait(), 2)
    assert f" · {tmp_path.name} · feature-x · (+0,-0) · " in statuses(h)[-1]
    assert session.working_directory == repo


async def test_a_restarted_client_starts_again_in_the_bound_folder(
    harness_for: Callable[..., Harness], repo: Path
) -> None:
    moved = {**sdk_json("stop-hook"), "cwd": str(repo)}
    h = harness_for({"turns": [with_stop_hook(sdk_messages("tools"), moved)]}, {"turns": []})
    session = h.session()
    await asyncio.wait_for((await session.submit("list the files")).done.wait(), 2)
    assert "feature-x" in statuses(h)[-1]
    await h.manager.close_all()
    text = await h.session().status()
    assert "Working in" not in text and "feature-x" not in text


async def test_status_lists_the_footer_s_values_of_the_latest_reply(
    harness_for: Callable[..., Harness], tmp_path: Path
) -> None:
    # The session stayed in the channel's folder, which is no repo: no branch, no changes.
    stayed = {**sdk_json("stop-hook"), "cwd": str(tmp_path)}
    h = harness_for({"turns": [with_stop_hook(sdk_messages("tools"), stayed)]})
    session = h.session()
    await asyncio.wait_for((await session.submit("list the files")).done.wait(), 2)
    await until(lambda: h.usage_fetches == 1)  # the turn's own refresh of the limits
    text = await session.status()
    assert text.startswith("Directory:") and "Claude Code: `2.1.286`" in text
    tokens = re.search(r"([\d.]+[kM]?) \*tok\*", statuses(h)[-1])
    assert tokens is not None
    lines = text.splitlines()
    assert lines[lines.index("Now: idle") + 1 :] == [
        "Model: `claude-haiku-4-5-20251001`",
        "Effort: `medium`",
        f"Session tokens: `{tokens.group(1)}`",
        "Context: `7%`",
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


async def test_status_shows_a_running_task_while_the_reply_s_own_closing_still_waits(
    harness_for: Callable[..., Harness],
) -> None:
    # D1: the closing message (where the running count would show) waits for the task; `!status`
    # is the one place that still shows it meanwhile.
    first, _, _ = split_background()
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    assert running_block(h.slack.message_blocks()[-1]) is None
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


async def test_a_skill_typed_as_a_command_shows_its_task_while_it_runs(
    harness_for: Callable[..., Harness],
) -> None:
    # skill-fork-command.jsonl (CLI 2.1.286): the forked skill's task starts and ends before the
    # turn's first message, and none of its calls is streamed.
    messages = sdk_messages("skill-fork-command")
    started = next(i for i, m in enumerate(messages) if isinstance(m, TaskStartedMessage))
    h = harness_for({"turns": [messages[: started + 1]]})
    turn = await h.session().submit("/list-files")
    await until(lambda: [c["status"] for c in h.cards()] == ["in_progress"])
    assert h.cards()[0]["title"] == "/list-files"
    h.clients[0].inject(messages[started + 1 :])
    await asyncio.wait_for(turn.done.wait(), 2)
    assert [(c["title"], c["status"]) for c in h.cards()] == [("/list-files", "complete")]
    assert len(h.slack.stream_ts) == 1  # one reply: the task's card is the owner's turn's


def started_of(name: str) -> TaskStartedMessage:
    return next(m for m in sdk_messages(name) if isinstance(m, TaskStartedMessage))


async def test_a_task_started_by_a_call_does_not_start_the_owner_s_turn(
    harness_for: Callable[..., Harness],
) -> None:
    # foreground.jsonl: a long Bash call's task, whose tool_use_id names that call.
    h = harness_for({"turns": [[started_of("foreground")]]})
    session = h.session()
    await session.submit("run it")
    await until(lambda: bool(h.clients) and h.clients[0].queries == ["run it"])
    await asyncio.sleep(0.05)
    assert session._active is None and len(session._held) == 1
    # nothing is written: no task card in a reply, and no reply
    assert [m for m, _ in h.slack.calls if m.startswith("chat.")] == []


async def test_a_command_s_task_waits_while_a_report_turn_is_expected(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({})
    session = h.session()
    await session.submit("/list-files")
    await until(lambda: bool(h.clients) and h.clients[0].queries == ["/list-files"])
    # The prompt is sent, and a report turn is expected meanwhile (a turn that crossed it).
    session._expect_injected_turn()
    h.clients[0].inject([started_of("skill-fork-command")])
    await until(lambda: len(session._held) == 1)
    assert session._active is None


async def test_a_task_started_after_its_call_ended_is_an_ordinary_task_of_the_agent_s_reply(
    harness_for: Callable[..., Harness],
) -> None:
    # The `nested` frame is hand-written, with no recording behind it: a subagent asked to start
    # a subagent ran the command itself (2026-10-03, CLI 2.1.286), so no real task of type agent
    # under an agent is known. A task that starts once its call has ended is not that call's
    # foreground work: it is any background task, on the reply holding the agent.
    recorded = sdk_messages("subagent")
    turn = [m for m in split_turns(recorded)[0] if getattr(m, "parent_tool_use_id", None) is None]
    child_call = next(
        b.id
        for m in recorded
        if isinstance(m, AssistantMessage) and m.parent_tool_use_id is not None
        for b in m.content
        if isinstance(b, ToolUseBlock)
    )
    nested = dataclasses.replace(
        started_of("subagent"), task_id="nested", tool_use_id=child_call, description="nested"
    )
    h = harness_for({"turns": [turn]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    posted = len(h.slack.posted_ts)
    children = [m for m in recorded if getattr(m, "parent_tool_use_id", None) is not None]
    h.clients[0].inject([*children, nested])
    await until(lambda: any(c["title"] == "nested" for c in h.cards()))
    assert len(h.slack.posted_ts) == posted and len(h.slack.stream_ts) == 1  # no reply of its own
    assert "nested" in session._task_replies


async def test_a_nested_task_that_ends_before_its_call_s_result_is_the_agent_s_foreground_work(
    harness_for: Callable[..., Harness],
) -> None:
    # Hand-written like the one above (an agent under an agent is unrecorded), in the order the
    # recorded nested command has: the task's end comes before its call's result.
    recorded = sdk_messages("subagent")
    turn = [m for m in split_turns(recorded)[0] if getattr(m, "parent_tool_use_id", None) is None]
    children = [m for m in recorded if getattr(m, "parent_tool_use_id", None) is not None]
    call = next(
        m
        for m in children
        if isinstance(m, AssistantMessage) and any(isinstance(b, ToolUseBlock) for b in m.content)
    )
    child_call = next(b.id for b in call.content if isinstance(b, ToolUseBlock))
    nested = dataclasses.replace(
        started_of("subagent"), task_id="nested", tool_use_id=child_call, description="nested"
    )
    ended = dataclasses.replace(
        next(m for m in sdk_messages("subagent") if isinstance(m, TaskNotificationMessage)),
        task_id="nested",
        tool_use_id=child_call,
    )
    h = harness_for({"turns": [turn]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    at = children.index(call) + 1
    h.clients[0].inject([*children[:at], nested, ended, *children[at:]])
    await asyncio.sleep(0.1)
    assert not any(c["title"] == "nested" for c in h.cards())
    assert "nested" not in session._task_replies and "nested" not in session._tasks
    assert session._ended == [] and session._expiry is None


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
    # the queued message gets no reply of its own: the running one's end names it
    assert len(h.slack.stream_ts) == 1
    assert texts.NOT_SENT_ONE.format(because=texts.BECAUSE_RESTARTED) in h.slack.stream_texts()[0]
    assert h.state.thread(CHANNEL, THREAD).session_id == result.session_id


async def test_a_stop_waits_for_the_reply_s_final_write(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "DRAIN_POLL_SECONDS", 0.01)
    *running, result = sdk_messages("tools")
    h = harness_for({"turns": [running]})
    turn = await h.session().submit("first")
    await until(lambda: bool(h.clients) and h.clients[0].queries == ["first"])
    footer_read = asyncio.Event()
    original = type(h.clients[0]).get_context_usage

    async def slow(self: Any) -> dict[str, Any]:
        await footer_read.wait()
        return await original(self)

    monkeypatch.setattr(type(h.clients[0]), "get_context_usage", slow)
    drained = asyncio.create_task(h.manager.drain(asyncio.Event()))
    h.clients[0].inject([result])
    await asyncio.sleep(0.1)
    assert not drained.done()  # the footer is still being read: the reply is not final yet
    footer_read.set()
    await asyncio.wait_for(drained, 2)
    assert turn.done.is_set()
    assert len(h.slack.calls_to("chat.stopStream")) == 1  # the reply has ended, footer and all


@pytest.mark.parametrize("bypass", [True, False])
async def test_a_stop_posts_nothing_in_a_thread_that_has_nothing_running(
    harness_for: Callable[..., Harness], bypass: bool
) -> None:
    # Bypass outlives a restart (D3), so nothing is said about it: a message would only notify.
    h = harness_for({})
    await h.session().set_bypass(bypass)
    h.state.set_session(CHANNEL, THREAD, "sess-ran")
    await asyncio.wait_for(h.manager.drain(asyncio.Event()), 2)
    assert h.slack.calls_to("chat.postMessage") == []
    assert h.slack.calls_to("assistant.threads.setStatus") == []


def status_lines(h: Harness) -> list[str]:
    """What the thread's status line said, in order; an empty string where it was cleared."""
    calls = h.slack.calls_to("assistant.threads.setStatus")
    return [(a.get("loading_messages") or [""])[0] for a in calls]


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
    approval_id = next(iter(h.approvals._pending))
    assert h.approvals.resolve(approval_id, CHANNEL, THREAD, Approve()) is not None
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
    [note] = h.slack.calls_to("chat.postMessage")  # nothing ran: the note is a message of its own
    assert texts.NOT_SENT_ONE.format(because=texts.BECAUSE_RESTARTED) in note["text"]


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


def split_at_task_start(turn: list[Any]) -> tuple[list[Any], list[Any]]:
    """A recorded turn cut where its background task starts: what came before, and the rest."""
    at = next(i for i, m in enumerate(turn) if isinstance(m, TaskStartedMessage))
    return turn[:at], turn[at:]


async def test_a_stop_does_not_wait_for_a_task_its_running_turn_starts_afterwards(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A session that ran `launchctl kill TERM` then waited in the background for the new process
    # held the restart until `!stop` or the limit (issue #87, three times on 2026-09-30). The
    # signal names no sender; the one that ordered it has a turn running when it arrives.
    monkeypatch.setattr(sessions, "DRAIN_POLL_SECONDS", 0.01)
    first, _, _ = split_background()
    before, after = split_at_task_start(first)
    h = harness_for({"turns": [before]})
    session = h.session()
    turn = await session.submit("restart the daemon")
    await until(lambda: bool(h.clients) and h.clients[0].queries == ["restart the daemon"])
    drained = asyncio.create_task(h.manager.drain(asyncio.Event()))
    await asyncio.sleep(0.05)
    assert not drained.done()  # its turn still runs
    h.clients[0].inject(after)
    await asyncio.wait_for(drained, 1)
    assert turn.done.is_set()
    assert session._running_task_ids()  # still running: the shutdown ends it
    await asyncio.sleep(0.05)
    assert not any(line.startswith("Restart waits") for line in status_lines(h))
    await asyncio.wait_for(h.manager.close_all(), 2)  # the shutdown that follows
    stored = h.state.thread(CHANNEL, THREAD)
    assert stored is not None and not stored.open_replies and stored.status is None
    # Its turn ended well: the task the shutdown ends is its own wait, not work cut short (decided
    # 2026-10-01: ✅, as `!stop` showed before this change).
    assert h.reactions()[-1] == Status.DONE.value
    assert Status.ERROR.value not in h.reactions()


async def test_a_stop_still_waits_for_a_task_the_running_turn_started_before_it(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "DRAIN_POLL_SECONDS", 0.01)
    first, _, _ = split_background()
    *running, result = first
    h = harness_for({"turns": [running]})
    turn = await h.session().submit("start it")
    await until(lambda: bool(h.clients) and h.clients[0].queries == ["start it"])
    await until(lambda: bool(h.session()._running_task_ids()))
    drained = asyncio.create_task(h.manager.drain(asyncio.Event()))
    h.clients[0].inject([result])
    await asyncio.wait_for(turn.done.wait(), 2)
    await asyncio.sleep(0.1)
    assert not drained.done()  # the task began before the signal: it is waited for
    # Said by the thread's status line, never by a message: no push, and nothing left behind.
    waits = texts.RESTART_WAITS.format(counts="1 shell", them="it")
    await until(lambda: status_lines(h)[-1:] == [waits])
    assert waits == "Restart waits for 1 shell · !stop ends it now"
    assert h.slack.calls_to("chat.postMessage") == []
    drained.cancel()


def stopped_end(notice: list[Any]) -> list[Any]:
    """The recorded end of a background task, as `stop_task` makes it: a `killed` task_updated
    and a `stopped` notification (SDK 0.2.160 `stop_task` docstring)."""
    return [
        dataclasses.replace(m, status="killed", patch={**m.patch, "status": "killed"})
        if isinstance(m, TaskUpdatedMessage)
        else dataclasses.replace(m, status="stopped")
        if isinstance(m, TaskNotificationMessage)
        else m
        for m in notice
    ]


async def test_a_stop_held_by_a_background_task_says_so_and_bang_stop_ends_it(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A task that never ends (a watcher) held a restart for the whole limit (2026-09-27).
    monkeypatch.setattr(sessions, "DRAIN_POLL_SECONDS", 0.01)
    first, notice, _ = split_background()
    task_id = next(m.task_id for m in first if isinstance(m, TaskStartedMessage))
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    drained = asyncio.create_task(h.manager.drain(asyncio.Event()))
    await asyncio.sleep(0.1)
    assert not drained.done()
    # Said by the thread's status line, never by a message: no push, and nothing left behind.
    waits = texts.RESTART_WAITS.format(counts="1 shell", them="it")
    await until(lambda: status_lines(h)[-1:] == [waits])
    assert waits == "Restart waits for 1 shell · !stop ends it now"
    assert h.slack.calls_to("chat.postMessage") == []
    assert await session.stop()
    assert h.clients[0].stopped_tasks == [task_id]
    assert h.clients[0].interrupts == 0  # no turn was running
    # No report turn follows a stopped task, so the stop does not wait INJECTED_TURN_WAIT for one.
    h.clients[0].inject(stopped_end(notice))
    await asyncio.wait_for(drained, 1)


async def test_a_stop_of_a_background_task_ends_the_reply_like_any_other_end(
    harness_for: Callable[..., Harness],
) -> None:
    # `!stop` (S2): the task's own end lets the reply's stream stop, with the footer, as a
    # normal end does. Nothing is posted, and the ✅ is the stop's own.
    first, notice, _ = split_background()
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    assert await session.stop()
    await asyncio.sleep(0.05)
    assert h.slack.calls_to("chat.stopStream") == []  # the task has not ended yet
    h.clients[0].inject(stopped_end(notice))
    await until(lambda: bool(h.slack.calls_to("chat.stopStream")))
    assert {"type": "divider"} in h.slack.calls_to("chat.stopStream")[-1]["blocks"]
    assert h.slack.posted_ts == [] and h.slack.pushes() == 1
    assert h.reactions()[-1] == Status.DONE.value


async def test_a_stopped_report_turn_ends_like_any_other_end(
    harness_for: Callable[..., Harness],
) -> None:
    # A report turn `!stop` cuts short ends like a stopped owner turn does: its reply's stream
    # stops with the footer, once.
    first, notice, injected = split_background()
    partial = [m for m in injected if not isinstance(m, ResultMessage)]
    result = next(m for m in injected if isinstance(m, ResultMessage))
    interrupted = dataclasses.replace(result, terminal_reason="aborted_streaming")
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    h.clients[0].inject(notice)
    await until(lambda: session._injected_expected)
    h.clients[0].inject(partial)
    await until(lambda: session._active is not None)
    assert await session.stop() is True
    h.clients[0].inject([interrupted])
    await until(lambda: bool(h.slack.calls_to("chat.stopStream")))
    assert h.slack.posted_ts == [] and h.slack.pushes() == 1
    assert not any(m.streaming for m in h.slack.messages.values())


async def test_the_next_prompt_after_bang_stop_does_not_wait_for_a_report(
    harness_for: Callable[..., Harness],
) -> None:
    # Measured 2026-09-27 on Claude Code 2.1.283: the stopped task's notification stays queued
    # and no turn reports it; the owner's next prompt waited INJECTED_TURN_WAIT (30 s).
    first, notice, _ = split_background()
    h = harness_for({"turns": [first, sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    assert await session.stop()
    h.clients[0].inject(stopped_end(notice))
    await until(lambda: not session._running_counts())
    following = await session.submit("next")
    await asyncio.wait_for(following.done.wait(), 1)
    assert h.clients[0].queries == ["start it", "next"]


async def test_bang_stop_ends_background_tasks_outside_a_stop_too(
    harness_for: Callable[..., Harness],
) -> None:
    first, _, _ = split_background()
    task_id = next(m.task_id for m in first if isinstance(m, TaskStartedMessage))
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    assert await session.stop()
    assert h.clients[0].stopped_tasks == [task_id]
    assert h.clients[0].interrupts == 0  # no turn was running


async def test_a_close_while_a_word_starts_the_client_leaves_no_process(
    harness_for: Callable[..., Harness],
) -> None:
    # `!help` or `!status` starts the client in its own task, outside the prompt queue; the
    # trigger for a concurrent close is now close_all at shutdown (and, later, an idle close).
    gate = asyncio.Event()
    h = harness_for({"connect_gate": gate})
    session = h.session()
    word = asyncio.create_task(session.ensure_connected())
    await until(lambda: len(h.clients) == 1)
    closing = asyncio.create_task(session.close())
    await asyncio.sleep(0.05)
    assert not closing.done()  # the close waits for the connect in progress
    gate.set()
    await asyncio.wait_for(closing, 2)
    await asyncio.wait_for(word, 2)
    assert len(h.clients) == 1 and h.clients[0].connected is False


async def test_bypass_asked_while_the_session_closes_is_not_stored(
    harness_for: Callable[..., Harness],
) -> None:
    gate = asyncio.Event()
    h = harness_for({"connect_gate": gate})
    session = h.session()
    word = asyncio.create_task(session.set_bypass(True))
    await until(lambda: len(h.clients) == 1)
    closing = asyncio.create_task(session.close())
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.wait_for(closing, 2)
    with pytest.raises(sessions.SessionClosed):
        await asyncio.wait_for(word, 2)
    stored = h.state.thread(CHANNEL, THREAD)
    assert stored is not None and stored.bypass is None  # never stored


async def test_the_status_of_a_session_closed_meanwhile_is_not_given(
    harness_for: Callable[..., Harness],
) -> None:
    gate = asyncio.Event()
    h = harness_for({"connect_gate": gate})
    session = h.session()
    word = asyncio.create_task(session.status())
    await until(lambda: len(h.clients) == 1)
    closing = asyncio.create_task(session.close())
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.wait_for(closing, 2)
    with pytest.raises(sessions.SessionClosed):  # connected before the close, read after it
        await asyncio.wait_for(word, 2)
    with pytest.raises(sessions.SessionClosed):
        await session.status()


async def test_a_closed_session_starts_no_client(harness_for: Callable[..., Harness]) -> None:
    h = harness_for()
    session = h.session()
    await session.close()
    with pytest.raises(sessions.SessionClosed):
        await session.ensure_connected()
    assert h.clients == []


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
    stored = h.state.thread(CHANNEL, THREAD)
    assert stored is not None and stored.bypass is None  # never stored


# D9: the effort level `/effort` sets is stored per thread and passed back on the next connect;
# a Claude Code process with nothing to do for an hour closes itself, and the next message
# rebuilds it.


async def test_a_result_reporting_an_effort_change_stores_it(
    harness_for: Callable[..., Harness],
) -> None:
    turn = sdk_messages("usage")
    result = turn[-1]
    assert isinstance(result, ResultMessage)
    effort_turn = [
        *turn[:-1],
        dataclasses.replace(
            result, result="Set effort level to high (this session only): Comprehensive"
        ),
    ]
    h = harness_for({"turns": [effort_turn]})
    await asyncio.wait_for((await h.session().submit("/effort high")).done.wait(), 2)
    assert h.state.thread(CHANNEL, THREAD).effort == "high"


async def test_setting_effort_back_to_auto_stores_the_default(
    harness_for: Callable[..., Harness],
) -> None:
    turn = sdk_messages("usage")
    result = turn[-1]
    assert isinstance(result, ResultMessage)
    effort_turn = [
        *turn[:-1],
        dataclasses.replace(result, result="Effort level set to auto (this session only)"),
    ]
    h = harness_for({"turns": [effort_turn]})
    session = h.session()  # opens the thread
    h.state.set_effort(CHANNEL, THREAD, "high")  # a level was stored from an earlier turn
    await asyncio.wait_for((await session.submit("/effort auto")).done.wait(), 2)
    assert h.state.thread(CHANNEL, THREAD).effort is None


async def test_the_client_is_launched_with_the_stored_effort(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({})
    h.session()  # opens the thread
    h.state.set_effort(CHANNEL, THREAD, "low")
    await h.session().ensure_connected()
    assert h.clients[0].options.effort == "low"


async def test_the_footer_shows_a_stored_effort_at_once_after_a_reconnect(
    harness_for: Callable[..., Harness],
) -> None:
    # A stored `!effort` is sent to Claude Code on every reconnect; the footer must not show
    # "unknown" for the level the daemon itself just asked for, before any turn reports another.
    h = harness_for({})
    h.session()
    h.state.set_effort(CHANNEL, THREAD, "low")
    assert "Effort: `low`" in await h.session().status()


async def test_an_idle_session_closes_itself_after_the_delay_and_posts_nothing(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "IDLE_CLOSE_SECONDS", 0.05)
    h = harness_for({"turns": [sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("hi")).done.wait(), 2)
    # `done` is set before the thread status's last write (the end now runs in its own task):
    # let it land, well inside the 0.05 s delay, so only the close itself is counted.
    await asyncio.sleep(0.02)
    calls_before = len(h.slack.calls)  # every kind: postMessage, update, delete
    await until(lambda: not h.clients[0].connected, limit=1)
    assert session.closed
    assert len(h.slack.calls) == calls_before  # a silent close


async def test_the_idle_close_does_not_fire_while_a_turn_runs(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "IDLE_CLOSE_SECONDS", 0.05)
    ask = CanUseToolCall("Bash", {"command": "ls"})
    h = harness_for({"turns": [[ask, *sdk_messages("tools")]]})
    session = h.session()
    turn = await session.submit("list the files")
    await until(lambda: bool(h.approvals._pending))
    await asyncio.sleep(0.1)  # past the idle delay, still waiting on the owner's decision
    assert not session.closed and h.clients[0].connected is True
    h.approvals.resolve(next(iter(h.approvals._pending)), CHANNEL, THREAD, Approve())
    await asyncio.wait_for(turn.done.wait(), 2)
    await until(lambda: session.closed, limit=1)  # idle again: the timer restarted


async def test_the_idle_close_waits_for_a_background_task(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "IDLE_CLOSE_SECONDS", 0.05)
    first, notice, injected = split_background()
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    assert session._running_kinds()  # the task outlived its turn
    await asyncio.sleep(0.1)  # past the idle delay, the task is still running
    assert not session.closed and h.clients[0].connected is True
    h.clients[0].inject(notice + injected)
    await until(lambda: not session._running_kinds())
    await until(lambda: session.closed, limit=1)  # idle again once the task ends


async def test_the_idle_close_waits_for_an_expected_report_turn(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "IDLE_CLOSE_SECONDS", 0.05)
    first, notice, injected = split_background()
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    h.clients[0].inject(notice)  # a report turn is expected; not idle until it arrives
    await asyncio.sleep(0.1)  # past the idle delay
    assert not session.closed and h.clients[0].connected is True
    h.clients[0].inject(injected)
    await until(lambda: is_report(h.bodies()[-1]))
    await until(lambda: session.closed, limit=1)


async def test_a_message_after_a_close_gets_a_rebuilt_session_never_the_closed_one(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({"turns": [sdk_messages("tools")]})
    session = h.session()
    h.state.set_session(CHANNEL, THREAD, "prior")
    h.state.set_effort(CHANNEL, THREAD, "high")
    await session.close()
    assert session.closed
    rebuilt = h.manager.get(CHANNEL, THREAD)
    assert rebuilt is not None and rebuilt is not session
    turn = await rebuilt.submit("next")
    await asyncio.wait_for(turn.done.wait(), 2)
    assert h.clients[-1].options.resume == "prior"
    assert h.clients[-1].options.effort == "high"


async def test_the_next_message_after_an_idle_close_resumes_with_effort(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "IDLE_CLOSE_SECONDS", 0.05)
    turn = sdk_messages("usage")
    result = turn[-1]
    assert isinstance(result, ResultMessage)
    effort_turn = [
        *turn[:-1],
        dataclasses.replace(
            result, result="Set effort level to high (this session only): Comprehensive"
        ),
    ]
    h = harness_for({"turns": [effort_turn]}, {"turns": [sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("/effort high")).done.wait(), 2)
    await until(lambda: session.closed, limit=1)
    stored = h.state.thread(CHANNEL, THREAD)
    rebuilt = h.manager.get(CHANNEL, THREAD)
    assert rebuilt is not None and rebuilt is not session
    await asyncio.wait_for((await rebuilt.submit("next")).done.wait(), 2)
    assert h.clients[1].options.resume == stored.session_id
    assert h.clients[1].options.effort == "high"


async def test_sessions_of_leaves_out_a_closed_session(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({})
    session = h.session()
    await session.close()
    assert h.manager.sessions_of(CHANNEL) == []


async def test_a_gone_session_is_left_out_of_sessions_of(
    harness_for: Callable[..., Harness],
) -> None:
    # The case Task 2 left open: a resume that finds its session gone stays a closed entry
    # in the manager until something looks it up again; sessions_of must not show it meanwhile.
    gone = ResultError(
        "Claude Code returned an error result: No conversation found",
        data={
            "subtype": "error_during_execution",
            "is_error": True,
            "errors": ["No conversation found with session ID: gone"],
        },
    )
    h = harness_for({"connect_error": gone})
    session = h.session()
    h.state.set_session(CHANNEL, THREAD, "gone")
    turn = await session.submit("hello")
    await asyncio.wait_for(turn.done.wait(), 2)
    assert session.closed
    assert h.manager.sessions_of(CHANNEL) == []


# D9 races: the resume race, the routing race, the re-arm points after a word's connect and an
# expired report wait, the effort edge cases and the eviction of a closed session.


async def test_ensure_connected_waits_for_the_predecessor_s_disconnect_before_resuming(
    harness_for: Callable[..., Harness],
) -> None:
    # SubprocessCLITransport.close() notes the CLI needs real time to flush and exit after EOF:
    # a rebuild must never resume the same session id while that is still in flight.
    gate = asyncio.Event()
    h = harness_for({"disconnect_gate": gate}, {"turns": [sdk_messages("tools")]})
    session = h.session()
    await session.ensure_connected()
    h.state.set_session(CHANNEL, THREAD, "prior")
    closing = asyncio.create_task(session.close())
    await asyncio.sleep(0.05)  # close() is now blocked inside the gated disconnect
    assert not closing.done()
    assert session.closed  # closing has started...
    assert h.clients[0].connected is True  # ...but has not finished
    rebuilt = h.manager.get(CHANNEL, THREAD)
    assert rebuilt is not None and rebuilt is not session
    connecting = asyncio.create_task(rebuilt.ensure_connected())
    await asyncio.sleep(0.05)
    assert not connecting.done()  # waiting on the predecessor: no second client started yet
    assert len(h.clients) == 1
    gate.set()
    await asyncio.wait_for(closing, 2)
    await asyncio.wait_for(connecting, 2)
    assert len(h.clients) == 2
    assert h.clients[1].options.resume == "prior"


async def test_close_all_waits_for_an_idle_close_already_in_flight(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "IDLE_CLOSE_SECONDS", 0.05)
    gate = asyncio.Event()
    h = harness_for({"disconnect_gate": gate})
    session = h.session()
    await session.ensure_connected()
    await until(lambda: session.closed, limit=1)  # the idle close fired and is now in flight
    assert h.clients[0].connected is True  # blocked inside the gated disconnect
    closing_all = asyncio.create_task(h.manager.close_all())
    await asyncio.sleep(0.05)
    assert not closing_all.done()  # waits for the in-flight close rather than returning early
    gate.set()
    await asyncio.wait_for(closing_all, 2)
    assert h.clients[0].connected is False


async def test_close_all_continues_past_a_session_whose_close_raises(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({}, {})
    first = h.session(THREAD)
    second = h.session(OTHER_THREAD)
    await first.ensure_connected()
    await second.ensure_connected()

    async def raising_close(reason: str = texts.ENDED_SHUTDOWN) -> None:
        # A real `close()` always sets `done_closing` from its own `finally`, whatever fails.
        first.done_closing.set()
        raise RuntimeError("boom")

    first.close = raising_close  # type: ignore[method-assign]
    await asyncio.wait_for(h.manager.close_all(), 2)
    assert h.clients[1].connected is False  # the second session still closed
    assert h.manager.sessions_of(CHANNEL) == []


async def test_a_lookup_touches_the_idle_timer_before_any_await(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The timer armed when the turn ended is close to firing; a lookup (a message arriving) must
    # reset it to a fresh IDLE_CLOSE_SECONDS, synchronously, before any slow step (a download, a
    # Slack call) a caller might do on the way to its own submit.
    monkeypatch.setattr(sessions, "IDLE_CLOSE_SECONDS", 0.1)
    h = harness_for({"turns": [sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("hi")).done.wait(), 2)
    await asyncio.sleep(0.07)  # close to the stale timer's own delay, not yet closed
    looked_up = h.manager.get(CHANNEL, THREAD)
    assert looked_up is session
    await asyncio.sleep(0.07)  # past where the STALE timer would have fired (0.1s from the end)
    assert not session.closed  # the touch gave it a fresh window instead


async def test_a_lookup_of_an_idle_session_still_closes_it_eventually(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The touch above resets the clock; it does not disarm it. A lookup with no submit ever
    # following stays a session with nothing to do, and closes on its own new schedule.
    monkeypatch.setattr(sessions, "IDLE_CLOSE_SECONDS", 0.05)
    h = harness_for({"turns": [sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("hi")).done.wait(), 2)
    assert h.manager.get(CHANNEL, THREAD) is session
    await until(lambda: session.closed, limit=1)


async def test_submit_on_a_closed_session_raises_session_closed(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({})
    session = h.session()
    await session.close()
    with pytest.raises(sessions.SessionClosed):
        await session.submit("hi")


async def test_ensure_connected_arms_the_idle_close_too(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A daemon word (`!status`, `!help`, `!bypass`) connects with no turn ever submitted.
    monkeypatch.setattr(sessions, "IDLE_CLOSE_SECONDS", 0.05)
    h = harness_for({})
    session = h.session()
    await session.ensure_connected()
    await until(lambda: session.closed, limit=1)


async def test_the_idle_close_arms_again_when_no_report_turn_ever_comes(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.05)
    monkeypatch.setattr(sessions, "IDLE_CLOSE_SECONDS", 0.05)
    first, notice, _ = split_background()
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    h.clients[0].inject(notice)  # a report turn is expected, but never sent in this test
    await until(lambda: session.closed, limit=2)  # INJECTED_TURN_WAIT elapses, then the timer


async def test_a_model_change_with_no_effort_does_not_clear_the_stored_level(
    harness_for: Callable[..., Harness],
) -> None:
    turn = sdk_messages("usage")
    result = turn[-1]
    assert isinstance(result, ResultMessage)
    model_turn = [
        *turn[:-1],
        dataclasses.replace(result, result="Set model to `Sonnet 5` for this session only"),
    ]
    h = harness_for({"turns": [model_turn]})
    session = h.session()  # opens the thread
    h.state.set_effort(CHANNEL, THREAD, "high")  # stored from an earlier turn
    await asyncio.wait_for((await session.submit("/model sonnet")).done.wait(), 2)
    assert h.state.thread(CHANNEL, THREAD).effort == "high"  # unknown, not cleared


async def test_an_unrecognized_stored_effort_is_dropped_not_sent(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({})
    h.session()  # opens the thread
    h.state.set_effort(CHANNEL, THREAD, "ultra")  # not one of the SDK's EffortLevel values
    await h.session().ensure_connected()
    assert h.clients[0].options.effort is None


async def test_an_idle_closed_session_is_evicted_even_with_no_further_lookup(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "IDLE_CLOSE_SECONDS", 0.05)
    h = harness_for({"turns": [sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("hi")).done.wait(), 2)
    await until(lambda: not h.clients[0].connected, limit=1)
    key = (CHANNEL, THREAD)
    await until(lambda: key not in h.manager._sessions, limit=1)


async def test_the_idle_close_does_not_fire_while_a_plain_turn_runs(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    # No approval at all this time: just a turn that has not finished yet.
    monkeypatch.setattr(sessions, "IDLE_CLOSE_SECONDS", 0.05)
    h = harness_for({"turns": [[]]})  # a turn with no messages: stays "sent" forever
    session = h.session()
    turn = await session.submit("still working")
    await asyncio.sleep(0.1)
    assert not session.closed
    assert not turn.done.is_set()


async def test_drain_suppresses_the_idle_close(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "IDLE_CLOSE_SECONDS", 0.05)
    h = harness_for({"turns": [sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("hi")).done.wait(), 2)
    session.draining = True
    await asyncio.sleep(0.1)
    assert not session.closed


# D9 close ordering: close() must signal done_closing even when a step inside it raises, the
# predecessor chain must hold past an unconnected middle generation, submit's own awaits must
# not be closeable under it, and a few more missed re-arm points.


async def test_close_sets_done_closing_even_if_a_step_inside_it_raises(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness_for({})
    session = h.session()
    await session.ensure_connected()

    async def boom() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(session, "_stop_task_replies", boom)
    with pytest.raises(RuntimeError):
        await session.close()
    assert session.done_closing.is_set()
    assert h.manager.sessions_of(CHANNEL) == []  # on_closed still ran too


async def test_close_all_waits_for_a_predecessor_evicted_before_it_but_still_disconnecting(
    harness_for: Callable[..., Harness],
) -> None:
    # A (idle-closing, gated) is evicted by a lookup that builds B; B never connects. close_all
    # only ever sees B in `_sessions`, yet must still wait for A's own disconnect to finish.
    gate = asyncio.Event()
    h = harness_for({"disconnect_gate": gate})
    session_a = h.session()
    await session_a.ensure_connected()
    closing_a = asyncio.create_task(session_a.close())
    await asyncio.sleep(0.05)  # A is now blocked inside the gated disconnect
    assert session_a.closed and h.clients[0].connected is True
    session_b = h.manager.get(CHANNEL, THREAD)
    assert session_b is not None and session_b is not session_a
    closing_all = asyncio.create_task(h.manager.close_all())
    await asyncio.sleep(0.05)
    assert not closing_all.done()  # chained through B, waiting on A's still-gated disconnect
    gate.set()
    await asyncio.wait_for(closing_a, 2)
    await asyncio.wait_for(closing_all, 2)
    assert h.clients[0].connected is False


async def test_a_third_generation_session_waits_through_an_unconnected_middle_one(
    harness_for: Callable[..., Harness],
) -> None:
    # A disconnecting (gated); B is built as its replacement but closes before ever connecting
    # (so it never itself waited on A); C must still wait for A, through B's own close().
    gate = asyncio.Event()
    h = harness_for({"disconnect_gate": gate}, {"turns": [sdk_messages("tools")]})
    session_a = h.session()
    await session_a.ensure_connected()
    h.state.set_session(CHANNEL, THREAD, "prior")
    closing_a = asyncio.create_task(session_a.close())
    await asyncio.sleep(0.05)  # A is now blocked inside the gated disconnect
    assert session_a.closed and h.clients[0].connected is True
    session_b = h.manager.get(CHANNEL, THREAD)
    assert session_b is not None and session_b is not session_a
    closing_b = asyncio.create_task(session_b.close())
    await asyncio.sleep(0.05)
    assert not closing_b.done()  # B's own close is chained behind A's still-open disconnect
    session_c = h.manager.get(CHANNEL, THREAD)
    assert session_c is not None and session_c not in (session_a, session_b)
    connecting_c = asyncio.create_task(session_c.ensure_connected())
    await asyncio.sleep(0.05)
    assert not connecting_c.done()  # must not resume the same id while A is still exiting
    assert len(h.clients) == 1
    gate.set()
    await asyncio.wait_for(closing_a, 2)
    await asyncio.wait_for(closing_b, 2)
    await asyncio.wait_for(connecting_c, 2)
    assert len(h.clients) == 2
    assert h.clients[1].options.resume == "prior"


async def test_the_idle_close_cannot_fire_during_submit_s_own_awaits(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "IDLE_CLOSE_SECONDS", 0.05)
    h = harness_for({"turns": [sdk_messages("tools"), sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("hi")).done.wait(), 2)
    # `submit`'s own chat.postMessage calls (through `_sink`/`sink.open`) now take longer than
    # the idle delay: without the fix, the timer armed at submit's own entry would reset instead
    # of staying cancelled, and fire while `submit` is still awaiting one of them.
    h.slack.delay = 0.2
    turn = await session.submit("again")
    await asyncio.wait_for(turn.done.wait(), 2)
    assert h.clients[0].queries == ["hi", "again"]


async def test_the_crash_tail_re_arms_the_idle_close(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "IDLE_CLOSE_SECONDS", 0.05)
    h = harness_for({"turns": [[*sdk_messages("tools")[:3], EndOfStream()]]})
    session = h.session()
    await asyncio.wait_for((await session.submit("first")).done.wait(), 2)
    await until(lambda: session._client is None, limit=1)  # the reader's crash tail ran
    await until(lambda: session.closed, limit=1)  # ...and re-armed the timer there


async def test_a_failed_query_re_arms_the_idle_close(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "IDLE_CLOSE_SECONDS", 0.05)
    h = harness_for({})
    session = h.session()
    await session.ensure_connected()

    async def boom(prompt: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(h.clients[0], "query", boom)
    turn = await session.submit("hi")
    await asyncio.wait_for(turn.done.wait(), 2)
    await until(lambda: session.closed, limit=1)


# The status reaction (D10): one on each session's root message, driven by `ThreadSession`
# itself. `Harness.reactions` reads it from `FakeSlack.calls`, never a session's own internals.


async def test_a_plain_turn_shows_working_then_done(harness_for: Callable[..., Harness]) -> None:
    h = harness_for({"turns": [sdk_messages("tools")]})
    turn = await h.session().submit("list the files")
    await asyncio.wait_for(turn.done.wait(), 2)
    # `turn.done` is set (`_settle`) before the sweep that checks whether the session is now
    # idle enough for ✅ (both run in `_finish`'s own `finally`, in that order).
    await until(lambda: h.reactions() == [Status.WORKING.value, Status.DONE.value])


async def test_a_turn_with_an_approval_shows_waiting_then_working_again(
    harness_for: Callable[..., Harness],
) -> None:
    ask = CanUseToolCall("Bash", {"command": "ls"})
    h = harness_for({"turns": [[ask, *sdk_messages("tools")]]})
    turn = await h.session().submit("list the files")
    await until(lambda: bool(h.approvals._pending))
    assert h.reactions() == [Status.WORKING.value, Status.WAITING.value]
    h.approvals.resolve(next(iter(h.approvals._pending)), CHANNEL, THREAD, Approve())
    await asyncio.wait_for(turn.done.wait(), 2)
    expected = [
        Status.WORKING.value,
        Status.WAITING.value,
        Status.WORKING.value,
        Status.DONE.value,
    ]
    await until(lambda: h.reactions() == expected)


async def test_a_background_task_outliving_the_turn_stays_working_until_its_report_closes(
    harness_for: Callable[..., Harness],
) -> None:
    first, notice, injected = split_background()
    h = harness_for({"turns": [first]})
    await asyncio.wait_for((await h.session().submit("start it")).done.wait(), 2)
    # The turn itself ended, but the task it started still runs: no ✅ yet.
    assert h.reactions() == [Status.WORKING.value]
    h.clients[0].inject(notice + injected)
    await until(lambda: is_report(h.bodies()[0]))
    await asyncio.sleep(0.05)
    assert h.reactions() == [Status.WORKING.value, Status.DONE.value]


async def test_a_failed_turn_shows_error(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness_for({})
    session = h.session()
    await session.ensure_connected()

    async def boom(prompt: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(h.clients[0], "query", boom)
    turn = await session.submit("hi")
    await asyncio.wait_for(turn.done.wait(), 2)
    assert h.reactions() == [Status.WORKING.value, Status.ERROR.value]


async def test_stop_shows_done(harness_for: Callable[..., Harness]) -> None:
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
    # A stop the owner gave is not an error: ✅, and it stands through the turn's own tail.
    assert h.reactions()[-1] == Status.DONE.value
    assert session._error_standing is False
    assert h.state.thread(CHANNEL, THREAD).status is None


async def test_quick_turn_after_stop_ends_on_working(
    harness_for: Callable[..., Harness],
) -> None:
    # D10 item 1: `StatusReaction.current` only updates once its own `reactions.add` returns.
    # A quick turn right after a stop can end (and try to react working, then done) before
    # that slow round trip lands, so gating on `current` alone missed the standing ❌ and let
    # an already-in-flight ⏳ add stick with nothing left to ever remove it.
    h = harness_for(
        {
            "turns": [
                [CanUseToolCall("Bash", {"command": "rm -rf build"}), *sdk_messages("interrupt")],
                sdk_messages("tools"),
            ]
        }
    )
    session = h.session()
    turn = await session.submit("clean")
    await until(lambda: bool(h.approvals._pending))
    assert await session.stop() is True
    await asyncio.wait_for(turn.done.wait(), 2)
    await until(lambda: session._status._current is Status.DONE)
    real_add = h.slack.reactions_add

    async def slow_add(**kw: Any) -> Any:
        await asyncio.sleep(0.3)  # a Slack round trip slower than the turn itself
        return await real_add(**kw)

    h.slack.reactions_add = slow_add  # type: ignore[method-assign]
    quick = await session.submit("quick")
    await asyncio.wait_for(quick.done.wait(), 2)
    await asyncio.sleep(0.8)
    on_root: set[str] = set()
    for method, args in h.slack.calls:
        if method == "reactions.add":
            on_root.add(args["name"])
        elif method == "reactions.remove":
            on_root.discard(args["name"])
    assert on_root == {Status.DONE.value}


async def test_a_gone_session_shows_error(harness_for: Callable[..., Harness]) -> None:
    gone = ResultError(
        "Claude Code returned an error result: No conversation found",
        data={
            "subtype": "error_during_execution",
            "is_error": True,
            "errors": ["No conversation found with session ID: gone"],
        },
    )
    h = harness_for({"connect_error": gone})
    h.session()  # opens the thread
    h.state.set_session(CHANNEL, THREAD, "gone")
    turn = await h.session().submit("hello")
    await asyncio.wait_for(turn.done.wait(), 2)
    assert h.reactions()[-1] == Status.ERROR.value


async def test_a_drain_that_cuts_a_busy_session_shows_error(
    harness_for: Callable[..., Harness],
) -> None:
    *running, _ = sdk_messages("tools")
    h = harness_for({"turns": [running]})
    turn = await h.session().submit("first")
    await until(lambda: bool(h.clients) and h.clients[0].queries == ["first"])
    cut_short = asyncio.Event()
    drained = asyncio.create_task(h.manager.drain(cut_short))
    await asyncio.sleep(0.05)
    cut_short.set()
    await asyncio.wait_for(drained, 2)
    assert not turn.done.is_set()  # not settled by the drain itself
    await h.manager.close_all()  # the shutdown that follows a cut-short drain
    assert h.reactions()[-1] == Status.ERROR.value


async def test_work_drain_path_leaves_hourglass(harness_for: Callable[..., Harness]) -> None:
    # D10 item 2: `_work`'s own draining branch dropped a taken turn's prompt with `_fail`'s
    # `notify=False`, which never reacted at all: the ⏳ (or ✅, once the session read idle) a
    # submit had already shown stood as if that turn had gone through, though it never did.
    h = harness_for({"turns": [sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("first")).done.wait(), 2)
    session._settled.clear()  # a report turn still in flight: the worker waits on it
    second = await session.submit("second")
    await until(lambda: session._taken is not None)
    session.draining = True
    session._settled.set()
    await asyncio.wait_for(second.done.wait(), 2)
    await session.close()
    await session.done_closing.wait()
    on_root: set[str] = set()
    for method, args in h.slack.calls:
        if method == "reactions.add":
            on_root.add(args["name"])
        elif method == "reactions.remove":
            on_root.discard(args["name"])
    assert on_root == {Status.ERROR.value}


async def test_close_leaves_latest_closing_showing_a_running_shell(
    harness_for: Callable[..., Harness],
) -> None:
    # item 3/7: `set_running("")` on the latest, already-closed-out reply only debounces; a
    # shutdown's own event loop iteration ends right after `close()` returns, so a `_later`
    # still waiting on its own timer never gets to run, and the closing keeps a stale count.
    first, _, _ = split_background()
    h = harness_for({"turns": [first, sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    await asyncio.wait_for((await session.submit("next")).done.wait(), 2)
    assert running_block(h.slack.message_blocks()[-1]) == "⏳ 1 shell"
    await session.close()
    await session.done_closing.wait()
    shown = [running_block(b) for b in h.slack.message_blocks()]
    assert all(r is None for r in shown)


async def test_a_top_level_status_word_gets_no_reaction(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({})
    await h.session().status()
    assert h.reactions() == []


async def test_owner_query_crossing_a_task_notification_is_answered_once_with_no_error(
    harness_for: Callable[..., Harness],
) -> None:
    # `_settle`: the turn's start guessed Claude Code's own report (`_injected_expected`), but
    # the result says a person asked it after all, with the owner's own turn still in `_sent`
    # to redirect it to. The turn itself succeeded: no ❌, and its answer, already in the
    # misrouted reply, is that reply's own: the owner turn's own reply is never written.
    h = harness_for({"turns": []})
    session = h.session()
    owner = await session.submit("what happened")
    await until(lambda: bool(h.clients) and h.clients[0].queries == ["what happened"])
    session._injected_expected = True  # Claude Code's own report was also expected right now
    h.clients[0].inject(sdk_messages("tools"))  # the result: a genuine human turn after all
    await asyncio.wait_for(owner.done.wait(), 2)
    await asyncio.sleep(0.1)
    assert h.reactions() == [Status.WORKING.value]  # no ❌: this turn actually succeeded
    assert len(h.slack.stream_ts) == 1 and h.slack.posted_ts == []
    assert h.slack.pushes() == 1


async def test_an_abandon_s_error_stands_through_a_later_unreported_expiry(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    # D10: `_expire_unreported`'s own tail used to call `_react_done_if_idle` unconditionally,
    # flipping a standing ❌ (here, `_abandon(error=True)`, a crashed CLI) back to ✅ once the
    # session reads idle again, even though no new work ever started.
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.1)
    first, notice, _ = split_background()
    ended = [m for m in notice if not isinstance(m, TaskNotificationMessage)]
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    h.clients[0].inject(ended)  # the task's terminal update, no notification yet: unreported
    await asyncio.sleep(0.02)
    assert session._unreported  # `_expire_unreported`'s own timer is now scheduled
    h.clients[0].inject([EndOfStream()])  # the CLI process is gone
    await until(lambda: session._client is None)
    assert h.reactions()[-1] == Status.ERROR.value
    await asyncio.sleep(0.15)  # past INJECTED_TURN_WAIT: the expiry timer has now fired
    assert h.reactions()[-1] == Status.ERROR.value


async def test_a_dropped_queued_turn_with_a_running_task_gets_a_note_and_no_reply(
    harness_for: Callable[..., Harness],
) -> None:
    # A restart drain drops the queued turn while a background task still runs (drain lets it
    # keep running): the message gets no reply of its own; no turn runs, so one note names it.
    first, _, _ = split_background()
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    second = await session.submit("next")  # queued: the worker has not taken it yet
    await session.drop_queued(error=True)
    await asyncio.wait_for(second.done.wait(), 2)
    assert len(h.slack.stream_ts) == 1  # the running reply's stream is the only one
    [note] = h.slack.calls_to("chat.postMessage")
    assert texts.NOT_SENT_ONE.format(because=texts.BECAUSE_RESTARTED) in note["text"]


async def test_closing_a_session_with_only_a_running_task_left_shows_error(
    harness_for: Callable[..., Harness],
) -> None:
    # `busy` (active or sent turns) misses a task that outlived its own turn: `close()` must
    # still react ❌ for cutting it short, or the ⏳ from the turn that started it never clears.
    first, _, _ = split_background()
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    assert h.reactions() == [Status.WORKING.value]  # the task still runs: no ✅ yet
    assert session.busy is False  # the bug: `busy` alone would miss the running task
    await session.close()
    assert h.reactions() == [Status.WORKING.value, Status.ERROR.value]


async def test_a_turn_that_finishes_after_a_restart_drain_dropped_a_queued_one_ends_on_done(
    harness_for: Callable[[dict[str, Any]], Harness],
) -> None:
    # A restart drain drops a queued turn (❌) while the running turn waits on an approval the
    # owner can still answer; the answer shows ⏳ again, and when that turn ends the root reads
    # ✅: the ❌ stood only until the next state was asked for.
    ask = CanUseToolCall("Bash", {"command": "ls"})
    msgs = sdk_messages("tools")
    h = harness_for({"turns": [[*msgs[:13], ask, *msgs[13:]]]})
    session = h.session()
    first = await session.submit("first")
    second = await session.submit("second")
    await until(lambda: bool(h.approvals._pending))
    drained = asyncio.create_task(h.manager.drain(asyncio.Event()))
    await asyncio.wait_for(second.done.wait(), 2)
    approval_id = next(iter(h.approvals._pending))
    assert h.approvals.resolve(approval_id, CHANNEL, THREAD, Approve()) is not None
    await asyncio.wait_for(first.done.wait(), 2)
    await asyncio.wait_for(drained, 3)
    await h.manager.close_all()
    await asyncio.sleep(0.2)
    assert Status.ERROR.value in h.reactions()
    assert h.reactions()[-1] == Status.DONE.value


async def test_a_close_right_after_a_turn_ends_leaves_done_alone_on_the_root(
    harness_for: Callable[..., Harness],
) -> None:
    # Issue #104: the session reads idle while its reader is still between the two calls that
    # move ⏳ to ✅. A restart's drain closes it there, and the close cancels the reader.
    h = harness_for({"turns": [sdk_messages("tools")]})
    session = h.session()
    remove = h.slack.reactions_remove

    async def slow_for_working(**kwargs: Any) -> Any:
        if kwargs["name"] == Status.WORKING.value:
            await asyncio.sleep(0.3)
        return await remove(**kwargs)

    h.slack.reactions_remove = slow_for_working  # type: ignore[method-assign]
    await session.submit("list the files")
    await until(lambda: Status.DONE.value in h.reactions() and session.restart_ready)
    await session.close()
    removed = [args["name"] for args in h.slack.calls_to("reactions.remove")]
    assert Status.WORKING.value in removed
    assert Status.DONE.value not in removed[removed.index(Status.WORKING.value) :]


# --- D8: hold reactions (Phase 3 final fix wave) ---


async def test_hold_end_reacts_from_the_thread_s_history_after_a_client_reset(
    harness_for: Callable[..., Harness],
) -> None:
    # `_client is None` alone is not "brand-new thread": a D9 idle close or a restart evicts the
    # object and hands the thread a fresh one on its next lookup, `_client` reset but its
    # history (a stored session id) intact.
    h = harness_for({"turns": [sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("list the files")).done.wait(), 2)
    assert h.state.thread(CHANNEL, THREAD).session_id is not None
    session._client = None  # what a D9 idle close or a restart hands the thread's next lookup
    session.hold_start()
    await asyncio.sleep(0)  # let the WAITING reaction's fire-and-forget task run
    await session.hold_end(continued=False)
    await asyncio.sleep(0)
    assert h.reactions()[-1] == Status.DONE.value


async def test_hold_end_clears_the_reaction_for_a_thread_that_never_ran(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({})
    session = h.session()
    assert h.state.thread(CHANNEL, THREAD).session_id is None
    session.hold_start()
    await asyncio.sleep(0)
    await session.hold_end(continued=False)
    await asyncio.sleep(0)
    assert session._status.current is None


async def test_hold_keeps_a_standing_error_and_cancel_restores_it(
    harness_for: Callable[..., Harness],
) -> None:
    # `hold_start`'s own WAITING reaction must not reset `_error_standing` (it did, through
    # `_react`), or Cancel right after turns a standing ❌ back into ✅: a hold is not new work.
    h = harness_for({"turns": [sdk_messages("tools")]})
    session = h.session()
    await asyncio.wait_for((await session.submit("go")).done.wait(), 2)
    h.clients[0].inject([EndOfStream()])  # the CLI process is gone: `_abandon(error=True)`
    await until(lambda: session._client is None)
    assert h.reactions()[-1] == Status.ERROR.value
    session.hold_start()
    assert session._error_standing is True  # not reset by the WAITING reaction
    await asyncio.sleep(0)
    await session.hold_end(continued=False)
    await asyncio.sleep(0)
    assert h.reactions()[-1] == Status.ERROR.value  # Cancel restores it, not ✅


async def test_cancel_while_another_approval_is_open_shows_waiting_not_working(
    harness_for: Callable[..., Harness],
) -> None:
    # hold_end's not-idle branch reacted WORKING unconditionally; a parallel approval this
    # thread still holds must keep showing ✋, not ⏳ (`_react_waiting_or_working`).
    h = harness_for(
        {"turns": [[CanUseToolCall("Bash", {"command": "ls"}), *sdk_messages("tools")]]}
    )
    session = h.session()
    turn = await session.submit("list the files")
    await until(lambda: bool(h.approvals._pending))
    assert session.waiting_for_owner  # the approval is open
    session.hold_start()
    await asyncio.sleep(0)
    await session.hold_end(continued=False)
    await asyncio.sleep(0)
    assert h.reactions()[-1] == Status.WAITING.value  # not WORKING: the approval is still open
    approval_id = next(iter(h.approvals._pending))
    assert h.approvals.resolve(approval_id, CHANNEL, THREAD, Approve()) is not None
    await asyncio.wait_for(turn.done.wait(), 2)


async def test_working_in_does_not_resolve_a_path_on_every_lookup(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    # `working_in` used to call `Path.resolve()` on the event loop for every live session on
    # every message; each session's folder is now cached once, at construction.
    h = harness_for(
        {"turns": [[CanUseToolCall("Bash", {"command": "ls"}), *sdk_messages("tools")]]}
    )
    first = h.session(THREAD)
    second = h.session(OTHER_THREAD)
    await first.submit("clean")
    await until(lambda: bool(h.approvals._pending))  # first is genuinely busy now

    def boom(self: Path, *args: Any, **kwargs: Any) -> Path:
        raise AssertionError("working_in must not resolve a path on every lookup")

    monkeypatch.setattr(Path, "resolve", boom)
    try:
        assert h.manager.working_in(besides=second) is first
    finally:
        monkeypatch.undo()
    approval_id = next(iter(h.approvals._pending))
    assert h.approvals.resolve(approval_id, CHANNEL, THREAD, Approve()) is not None


async def test_the_manager_names_the_threads_with_a_live_session(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({"turns": [sdk_messages("tools")]})
    assert h.manager.live_threads() == set()
    session = h.session()
    assert h.manager.live_threads() == {(CHANNEL, THREAD)}
    await session.close()
    assert h.manager.live_threads() == set()  # a closed one is no longer in the way


async def test_a_stop_says_what_it_waits_for_in_a_message_where_slack_refuses_a_status(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "DRAIN_POLL_SECONDS", 0.01)
    monkeypatch.setattr(ThreadStatus, "_refused", True)  # the token cannot set a thread status
    first, _, _ = split_background()
    h = harness_for({"turns": [first]})
    await asyncio.wait_for((await h.session().submit("start it")).done.wait(), 2)
    drained = asyncio.create_task(h.manager.drain(asyncio.Event()))
    await asyncio.sleep(0.1)
    assert not drained.done()
    posted = [p["text"] for p in h.slack.calls_to("chat.postMessage")]
    assert posted == [texts.RESTART_WAITS_MESSAGE.format(counts="1 shell")]  # once, however long
    assert h.slack.calls_to("assistant.threads.setStatus") == []
    drained.cancel()


async def test_a_post_outside_the_session_sets_its_thread_status_again(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({})  # a turn that never answers: `Working…` shows
    session = h.session()
    await session.submit("hello")
    told: list[bool] = []
    session._thread_status.wrote = lambda: told.append(True)  # type: ignore[method-assign]
    h.manager.wrote(CHANNEL, THREAD)
    h.manager.wrote(CHANNEL, "1790000000.999999")  # no live session there: nothing to set
    assert told == [True]


class ExpectedTurns:
    """Counts how often a session starts waiting for a report turn."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.entered = 0
        original = sessions.ThreadSession._expect_injected_turn

        def counting(session: Any) -> None:
            self.entered += 1
            original(session)

        monkeypatch.setattr(sessions.ThreadSession, "_expect_injected_turn", counting)


async def test_the_commands_a_subagent_runs_arm_no_wait_for_a_report_turn(
    harness_for: Callable[..., Harness],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # subagent-nested-command.jsonl: each long command the agent runs gets a task of its own on
    # the main stream, started by a call that has a parent. Claude Code reports it to the agent,
    # so no turn follows it.
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.05)
    waits = ExpectedTurns(monkeypatch)
    first, work, _, ends = split_nested_command()
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    (agent_task,) = session._task_replies
    h.clients[0].inject(work[: ends[1] + 1])
    await asyncio.sleep(0.2)  # longer than the wait: nothing was armed to fire in it
    assert waits.entered == 0 and session._expiry is None
    assert session._settled.is_set() and not session._injected_expected
    assert "no turn followed a task notification" not in caplog.text
    assert list(session._tasks) == [agent_task] and list(session._task_replies) == [agent_task]
    assert session._ended == [] and session._unreported == {} and session._held == []


async def test_an_owner_prompt_is_not_held_while_a_subagent_s_command_ends(
    harness_for: Callable[..., Harness],
) -> None:
    first, work, _, ends = split_nested_command()
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    h.clients[0].inject(work[: ends[0] + 1])
    await asyncio.sleep(0.05)
    await session.submit("and now?")
    # INJECTED_TURN_WAIT is the production 30 s: a held prompt would not reach the client here
    await until(lambda: h.clients[0].queries == ["start it", "and now?"], limit=1.0)


async def test_a_subagent_s_commands_have_no_card_and_no_end_line_in_the_report(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.2)
    waits = ExpectedTurns(monkeypatch)
    first, work, report, _ = split_nested_command()
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    h.clients[0].inject(work)
    h.clients[0].inject(report)
    await until(lambda: session.idle and h.reactions()[-1:] == [Status.DONE.value], limit=3.0)
    assert waits.entered == 1  # the agent's own notification
    (body,) = h.bodies()  # D1: the report renders into the reply that started the agent
    assert body.count("✓ ") == 1 and '✓ Agent "' in body
    ((card,),) = h.slack.message_cards()  # the two commands show on the agent's card
    assert card["title"].endswith("· 2 calls") and card["status"] == "complete"
    assert not any(m.streaming for m in h.slack.messages.values())


async def test_a_subagent_s_command_whose_reply_is_not_tracked_is_held_as_before(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A restart or an idle close dropped the reply that holds the agent's call: the session cannot
    # tell the command's task is nested, and treats it as any task of no known reply (held, and a
    # report turn expected). Ignoring it instead would drop a top-level task of an unknown call.
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.05)
    waits = ExpectedTurns(monkeypatch)
    first, work, _, ends = split_nested_command()
    h = harness_for({"turns": [first]})
    session = h.session()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    session._task_replies.clear()
    nested = work[ends[0]].task_id  # the frames of the first command's task alone
    h.clients[0].inject([m for m in work[: ends[0] + 1] if getattr(m, "task_id", None) == nested])
    await until(lambda: waits.entered == 1)
    assert any(isinstance(m, TaskNotificationMessage) for m in session._held)


async def nested_background_running(h: Harness, session: Any) -> tuple[str, list[Any], list[Any]]:
    """Plays the recorded subagent up to its first report turn's end, with its command still
    running. Returns that command's task id and the frames still to come."""
    _, work, report, tail, report_two = split_nested_background()
    await asyncio.wait_for((await session.submit("start it")).done.wait(), 2)
    h.clients[0].inject(work)
    h.clients[0].inject(report)
    command = next(m.task_id for m in work if isinstance(m, TaskStartedMessage))
    return command, tail, report_two


async def test_a_subagent_s_command_that_outlives_its_call_is_a_running_shell(
    harness_for: Callable[..., Harness],
) -> None:
    # subagent-nested-background.jsonl: the command runs in the background of a subagent that
    # ends (and reports) before it does; its own end follows the report turn.
    first = split_nested_background()[0]
    h = harness_for({"turns": [first]})
    session = h.session()
    command, _, _ = await nested_background_running(h, session)
    await until(lambda: command in session._task_replies and session._active is None)
    await asyncio.sleep(0.1)
    assert session._running_kinds() == "1 shell" and command in session._running_task_ids()
    assert not session.idle and not session.restart_ready
    assert h.reactions()[-1] == Status.WORKING.value  # the first report turn did not end it
    drained = asyncio.create_task(h.manager.drain(asyncio.Event()))
    await asyncio.sleep(0.1)
    assert not drained.done()  # a drain waits for the command
    drained.cancel()
    assert await session.stop() is True
    assert h.clients[0].stopped_tasks == [command]


async def test_a_subagent_s_command_and_the_agent_s_second_end_close_the_reply_as_before(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.2)
    first = split_nested_background()[0]
    h = harness_for({"turns": [first]})
    session = h.session()
    command, tail, report_two = await nested_background_running(h, session)
    await until(lambda: command in session._task_replies and session._active is None)
    h.clients[0].inject(tail)
    h.clients[0].inject(report_two)
    await until(lambda: session.idle and h.reactions()[-1:] == [Status.DONE.value], limit=3.0)
    assert session._running_kinds() == "" and session._running_task_ids() == []
    assert session._unlanded == set()
    assert not any(m.streaming for m in h.slack.messages.values())
    assert [c["status"] for c in h.cards()] == ["complete", "complete"]
    assert len(h.slack.stream_ts) == 1  # every report rendered into the reply that began it
