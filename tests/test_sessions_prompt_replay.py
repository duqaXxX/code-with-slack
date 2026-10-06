"""A prompt Claude Code takes into a running turn, known from the replay of the prompt (issue
#150), replayed from recordings made on 2026-10-06 with claude-agent-sdk 0.2.163 and its bundled
CLI 2.1.286, with `--replay-user-messages` (`prompt-replay-<scene>`).

Measured there: Claude Code re-emits each prompt as a `user` frame carrying the uuid the caller
sent. A prompt that gets a turn of its own is replayed after that turn's `init` and before its
first stream event; a prompt taken into a running turn is replayed inside that turn, before its
one result, which has an injected origin and none follows for the prompt."""

import asyncio
import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TaskNotificationMessage,
    ToolUseBlock,
    UserMessage,
)

from code_with_slack import texts
from code_with_slack.sessions import taken_note
from tests.fakes import EndOfStream, sdk_messages
from tests.test_sessions import Harness, until
from tests.test_sessions_report_turn import (
    assert_nothing_runs,
    assert_stop_has_nothing_to_stop,
    command_runs,
    worker_holds,
)

PROMPT = "Reply with the single word PINEAPPLE."


@dataclass
class Scene:
    """A recording cut at the owner's first result, the report turn's start, its first call and
    its result (`reported`), and the result of the prompt's own turn."""

    owner: list[Any]
    notice: list[Any]
    head: list[Any]  # the report turn up to and including its first tool call
    rest: list[Any]  # from there to the report turn's one result
    own: list[Any]  # the turn Claude Code starts for the prompt, when it is not taken in
    call_id: str | None  # None: the report turn makes no call (`at-init`)


def scene(name: str) -> Scene:
    m = sdk_messages(f"prompt-replay-{name}")
    results = [i for i, x in enumerate(m) if isinstance(x, ResultMessage)]
    notified = next(i for i, x in enumerate(m) if isinstance(x, TaskNotificationMessage))
    report = next(
        i
        for i in range(notified, len(m))
        if isinstance(m[i], SystemMessage) and m[i].subtype == "init"
    )
    call = next(
        (
            i
            for i in range(report, results[1])
            if isinstance(m[i], AssistantMessage)
            and m[i].parent_tool_use_id is None
            and any(isinstance(b, ToolUseBlock) for b in m[i].content)
        ),
        results[1] - 1,
    )
    block = (
        next((b for b in m[call].content if isinstance(b, ToolUseBlock)), None)
        if isinstance(m[call], AssistantMessage)
        else None
    )
    return Scene(
        owner=m[: results[0] + 1],
        notice=m[results[0] + 1 : report],
        head=m[report : call + 1],
        rest=m[call + 1 : results[1] + 1],
        own=m[results[1] + 1 : results[2] + 1],
        call_id=block.id if block else None,
    )


def acknowledged(frames: list[Any], uuid: str) -> list[Any]:
    """`frames` with the replay of the prompt (the user frame of plain text) carrying `uuid`.
    The daemon makes its own uuid for each prompt it sends, so the recorded one cannot match."""
    return [
        dataclasses.replace(f, uuid=uuid)
        if isinstance(f, UserMessage) and isinstance(f.content, str)
        else f
        for f in frames
    ]


async def send(h: Harness, session: Any, text: str) -> Any:
    """Submit a prompt and wait until the session has sent it to Claude Code."""
    sent = len(h.clients[0].sent) if h.clients else 0
    turn = await session.submit(text)
    await until(lambda: bool(h.clients) and len(h.clients[0].sent) == sent + 1)
    return turn


def play(h: Harness, frames: list[Any]) -> None:
    """Deliver `frames` as the stream does, the replay of the last prompt sent in them."""
    h.clients[0].inject(acknowledged(frames, h.clients[0].sent[-1]["uuid"]))


async def first_turn(h: Harness, session: Any, r: Scene) -> None:
    """The owner's first prompt, which starts a background command and ends."""
    turn = await send(h, session, "start a background command")
    play(h, r.owner)
    await asyncio.wait_for(turn.done.wait(), 2)


async def prompt_during_the_report_turn(h: Harness, session: Any, r: Scene) -> Any:
    """The state the live hang had: a report turn runs a command while the session holds no
    expectation of one (`_settled` is set), so the owner's prompt is sent into it at once. The
    notification frames reach the session once the turn runs (a notification during a turn
    reaches no `_notified`), as the continued agent's did; they are the recording's own, only
    their place differs."""
    await first_turn(h, session, r)
    h.clients[0].inject(r.head)
    assert r.call_id is not None
    await command_runs(session, r.call_id)
    h.clients[0].inject(r.notice)
    await until(lambda: not session._tasks)
    prompt = await send(h, session, PROMPT)
    assert session._active is not None and session._active.turn is None
    return prompt


def said(h: Harness) -> str:
    return "\n".join(h.replies())


async def test_a_prompt_taken_into_a_report_turn_is_released_when_that_turn_ends(
    harness_for: Callable[..., Harness],
) -> None:
    r = scene("during-tool")
    h = harness_for({})
    session = h.session()
    prompt = await prompt_during_the_report_turn(h, session, r)
    play(h, r.rest)  # the replay is in it, with the command's result; one result ends both
    await asyncio.wait_for(prompt.done.wait(), 2)
    await until(lambda: session._active is None)
    assert_nothing_runs(h, session)
    assert texts.TAKEN_INTO_REPLY_ONE in said(h)
    assert "PINEAPPLE" not in said(h)  # the replay is an acknowledgement, never text
    assert len(h.slack.calls_to("chat.startStream")) == 2  # the owner's first reply, the report's
    await assert_stop_has_nothing_to_stop(h, session)


async def test_a_prompt_sent_just_before_a_report_turn_starts_is_released_with_it(
    harness_for: Callable[..., Harness],
) -> None:
    # The prompt is in `_sent` when the report turn's first frame arrives, so `_start_turn` makes
    # it the turn's owner; the recording's own frames then show Claude Code took it in.
    r = scene("during-tool")
    h = harness_for({})
    session = h.session()
    await first_turn(h, session, r)
    prompt = await send(h, session, PROMPT)
    play(h, [*r.notice, *r.head, *r.rest])
    await asyncio.wait_for(prompt.done.wait(), 2)
    await until(lambda: session._active is None)
    assert_nothing_runs(h, session)
    assert texts.TAKEN_INTO_REPLY_ONE in said(h)
    assert len(h.slack.calls_to("chat.startStream")) == 2  # the prompt's reply holds the report
    await assert_stop_has_nothing_to_stop(h, session)


@pytest.mark.parametrize("name", ["at-init", "after-tools"])
async def test_a_prompt_not_taken_in_gets_its_own_turn_and_reply(
    harness_for: Callable[..., Harness], name: str
) -> None:
    # `at-init`: the prompt reached Claude Code at the report turn's `init`. `after-tools`: the
    # report turn had no tool result left and was writing its text. Both report turns end with
    # their own result and Claude Code then starts a turn for the prompt. The daemon holds the
    # prompt until the report turn ends, so it sends it after that result and the replay comes
    # with no turn running: nothing is released early and no note says the prompt was taken.
    r = scene(name)
    h = harness_for({})
    session = h.session()
    await first_turn(h, session, r)
    play(h, r.notice)
    await until(lambda: not session._settled.is_set())
    h.clients[0].inject([*r.head, *r.rest[:-1]])
    await until(lambda: session._active is not None)
    prompt = await session.submit(PROMPT)
    await worker_holds(session, prompt)
    assert len(h.clients[0].sent) == 1
    h.clients[0].inject(r.rest[-1:])
    await until(lambda: len(h.clients[0].sent) == 2)
    assert not prompt.done.is_set()
    play(h, r.own)
    await asyncio.wait_for(prompt.done.wait(), 2)
    await until(lambda: session._active is None)
    assert_nothing_runs(h, session)
    assert texts.TAKEN_INTO_REPLY_ONE not in said(h)
    assert "PINEAPPLE" in said(h)  # the answer, not the replay of the question
    assert len(h.slack.calls_to("chat.startStream")) == 2
    await assert_stop_has_nothing_to_stop(h, session)


async def test_stop_during_a_report_turn_with_a_prompt_queued_leaves_nothing_behind(
    harness_for: Callable[..., Harness],
) -> None:
    # `stop-queued`: `interrupt()` ends the report turn with `error_during_execution`, and the
    # prompt queued behind it runs as a turn of its own with a result of its own.
    r = scene("stop-queued")
    h = harness_for({})
    session = h.session()
    prompt = await prompt_during_the_report_turn(h, session, r)
    assert await session.stop() is True
    assert h.clients[0].interrupts == 1
    play(h, r.rest)  # no replay in it: the prompt was queued, not taken in
    await until(lambda: session._active is None)
    assert not prompt.done.is_set()
    play(h, r.own)
    await asyncio.wait_for(prompt.done.wait(), 2)
    await until(lambda: session._active is None)
    assert_nothing_runs(h, session)
    assert texts.TAKEN_INTO_REPLY_ONE not in said(h)
    await assert_stop_has_nothing_to_stop(h, session)


async def test_a_replay_naming_no_sent_prompt_acknowledges_nothing(
    harness_for: Callable[..., Harness],
) -> None:
    # A resumed session, or anything else the daemon did not send: its uuid is nobody's.
    r = scene("during-tool")
    h = harness_for({})
    session = h.session()
    prompt = await prompt_during_the_report_turn(h, session, r)
    h.clients[0].inject(acknowledged(r.rest, "00000000-0000-4000-8000-000000000000"))
    await until(lambda: session._active is None)
    assert not prompt.done.is_set() and list(session._sent) == [prompt]
    assert texts.TAKEN_INTO_REPLY_ONE not in said(h)
    play(h, r.own)
    await asyncio.wait_for(prompt.done.wait(), 2)
    assert_nothing_runs(h, session)


async def test_a_prompt_taken_in_when_the_client_ends_is_noted_as_not_sent(
    harness_for: Callable[..., Harness],
) -> None:
    r = scene("during-tool")
    h = harness_for({})
    session = h.session()
    prompt = await prompt_during_the_report_turn(h, session, r)
    play(h, r.rest[:-1])  # up to the replay, before the result
    await until(lambda: bool(session._active.taken))
    h.clients[0].inject([EndOfStream()])
    await asyncio.wait_for(prompt.done.wait(), 2)
    await until(lambda: not session.busy)
    assert not session._sent
    assert texts.NOT_SENT_ONE.format(count=1, because=texts.BECAUSE_STOPPED) in said(h)


def test_the_note_counts_the_prompts_taken_in() -> None:
    # The worker sends one prompt at a time, so a turn takes in one today; the note reads `taken`.
    assert taken_note(1) == texts.TAKEN_INTO_REPLY_ONE
    assert taken_note(2) == (
        "Claude Code took 2 messages into this reply: "
        "send them again if they are not answered here."
    )


async def test_every_prompt_goes_out_with_a_uuid_of_the_daemons_own(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({"turns": [[], [], []]})
    session = h.session()
    blocks: list[Any] = [{"type": "text", "text": "look at this"}]
    for prompt in ("first", "/usage", blocks):
        turn = await send(h, session, prompt)  # type: ignore[arg-type]
        turn.done.set()
    client = h.clients[0]
    assert client.options.extra_args["replay-user-messages"] is None
    assert [m["message"]["content"] for m in client.sent] == ["first", "/usage", blocks]
    uuids = [m["uuid"] for m in client.sent]
    assert all(isinstance(u, str) and u for u in uuids) and len(set(uuids)) == 3
    assert client.queries[:2] == ["first", "/usage"]  # a string prompt keeps its string content
    assert all(m["type"] == "user" and m["parent_tool_use_id"] is None for m in client.sent)
