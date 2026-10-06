"""An agent continued with `SendMessage` in a daemon that never saw the call that first started
it (issue #150), replayed from a recording made on 2026-10-05 with claude-agent-sdk 0.2.163 and
its bundled CLI 2.1.286 (`report-turn-agent-resume`).

Measured there: an agent continued with `SendMessage` keeps its `task_id`, and its own frames name
the call that first started it, not the `SendMessage` call. Claude Code takes a prompt that
reaches it while a turn runs into that turn, with no result of the prompt's own, so the session
must hold the owner's next prompt until the turn that reports the agent has ended."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, ToolUseBlock

from tests.fakes import sdk_messages
from tests.test_sessions import Harness, until


@dataclass
class Recording:
    """A recording cut where its last report turn starts and where that turn's first tool call
    is made: `head` runs up to the call, `rest` from there to the turn's one result."""

    messages: list[Any]
    results: list[int]
    report: int
    call: int

    @property
    def call_id(self) -> str:
        message = self.messages[self.call]
        return next(b.id for b in message.content if isinstance(b, ToolUseBlock))

    @property
    def head(self) -> list[Any]:
        return self.messages[self.report : self.call + 1]

    @property
    def rest(self) -> list[Any]:
        return self.messages[self.call + 1 : self.results[-2] + 1]


def is_init(message: Any) -> bool:
    return isinstance(message, SystemMessage) and message.subtype == "init"


def cut(messages: list[Any]) -> Recording:
    """The recording ends with a prompt of the driver's own that gets a result of its own, after
    one result with an origin: the last report turn."""
    results = [i for i, m in enumerate(messages) if isinstance(m, ResultMessage)]
    reported = next(i for i in reversed(results) if messages[i].origin)
    report = max(i for i in range(reported) if is_init(messages[i]))
    call = next(
        i
        for i in range(report, reported)
        if isinstance(messages[i], AssistantMessage)
        and messages[i].parent_tool_use_id is None
        and any(isinstance(b, ToolUseBlock) for b in messages[i].content)
    )
    return Recording(messages, results, report, call)


async def command_runs(session: Any, call_id: str) -> None:
    """Until a turn is running and has shown the call."""
    await until(lambda: session._active is not None and session._active.renderer.owns(call_id))


async def worker_holds(session: Any, turn: Any) -> None:
    """Until the session's worker has taken the turn and is waiting to send it, then a moment
    more: a worker that sends it does so in the next few steps of the loop."""
    await until(lambda: session._taken is turn or turn in session._sent)
    await asyncio.sleep(0.1)


def assert_nothing_runs(h: Harness, session: Any) -> None:
    assert session.busy is False
    assert not session._sent
    assert session.restart_ready is True
    assert session.restart_hold == ""
    assert h.manager.restart_holds() == []
    assert session._interrupting is False


async def assert_stop_has_nothing_to_stop(h: Harness, session: Any) -> None:
    interrupts = h.clients[0].interrupts
    assert await session.stop() is False
    assert h.clients[0].interrupts == interrupts
    assert_nothing_runs(h, session)


async def play_the_continued_agent(
    harness_for: Callable[..., Harness], *, restarted: bool
) -> tuple[Harness, Any]:
    """The `agent-resume` recording, the session itself deciding when the second prompt is sent:
    the owner has an agent continued with `SendMessage`, the agent ends, Claude Code starts the
    turn that reports it and that turn runs a command; the owner's prompt is queued then.
    `restarted`: the daemon starts at the `SendMessage` turn, so it never saw the call that first
    started the agent."""
    r = cut(sdk_messages("report-turn-agent-resume"))
    m = r.messages
    owner, first_report, sent, _, answered = r.results
    send_turn = m[first_report + 1 : sent + 1]
    agent_works = m[sent + 1 : r.report]
    if restarted:
        h = harness_for({"turns": [send_turn]})
        session = h.session()
    else:
        h = harness_for({"turns": [m[: owner + 1], send_turn]})
        session = h.session()
        await asyncio.wait_for((await session.submit("start an agent")).done.wait(), 2)
        h.clients[0].inject(m[owner + 1 : first_report + 1])
        await until(lambda: session.idle)
    await asyncio.wait_for((await session.submit("continue the agent")).done.wait(), 2)
    client = h.clients[0]
    sent_so_far = len(client.queries)
    client.inject([*agent_works, *r.head])
    await command_runs(session, r.call_id)
    prompt = await session.submit("what did it find?")
    await worker_holds(session, prompt)
    # The report turn would take the prompt into itself and end it with its own one result.
    assert len(client.queries) == sent_so_far
    client.inject(r.rest)
    await until(lambda: len(client.queries) == sent_so_far + 1)
    assert session._active is None
    client.inject(m[r.results[-2] + 1 : answered + 1])
    await asyncio.wait_for(prompt.done.wait(), 2)
    await until(lambda: session._active is None)
    return h, session


async def test_a_continued_agent_after_a_restart_opens_no_turn_and_leaves_nothing_behind(
    harness_for: Callable[..., Harness],
) -> None:
    # The daemon never saw the `Agent` call the continued agent's frames name.
    h, session = await play_the_continued_agent(harness_for, restarted=True)
    assert_nothing_runs(h, session)
    await assert_stop_has_nothing_to_stop(h, session)


async def test_a_continued_agent_in_the_daemon_that_saw_its_first_call_leaves_nothing_behind(
    harness_for: Callable[..., Harness],
) -> None:
    h, session = await play_the_continued_agent(harness_for, restarted=False)
    assert_nothing_runs(h, session)
    await assert_stop_has_nothing_to_stop(h, session)


async def test_a_continued_agent_frame_never_starts_a_turn(
    harness_for: Callable[..., Harness],
) -> None:
    # Frames of an agent whose call no reply holds, while no turn runs: nothing of Claude Code's
    # own turn is under way, and the reply of the owner's next prompt is not theirs to take.
    r = cut(sdk_messages("report-turn-agent-resume"))
    m = r.messages
    _, first_report, sent, _, _ = r.results
    h = harness_for({"turns": [m[first_report + 1 : sent + 1]]})
    session = h.session()
    await asyncio.wait_for((await session.submit("continue the agent")).done.wait(), 2)
    agent_works = m[sent + 1 : r.report]
    # Twice: the agent continued a second time works under the same unknown call.
    h.clients[0].inject(agent_works)
    h.clients[0].inject(agent_works)
    await asyncio.sleep(0.2)
    assert session._active is None
    assert len(h.slack.calls_to("chat.startStream")) == 1  # the owner's turn alone has a reply
