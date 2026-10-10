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

from awaydesk import sessions, texts
from awaydesk.sessions import taken_note
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


async def own_turn_follows(h: Harness, session: Any, r: Scene, prompt: Any) -> None:
    """The report turn has ended with no replay of the prompt in it: the prompt is still owed a
    turn, which Claude Code starts and replays it at (`at-init`, `after-tools`)."""
    await until(lambda: session._active is None)
    assert not prompt.done.is_set()
    assert list(session._sent) == [prompt]  # held across the report's result
    play(h, r.own)
    await asyncio.wait_for(prompt.done.wait(), 2)
    await until(lambda: session._active is None)
    assert_nothing_runs(h, session)
    assert texts.TAKEN_INTO_REPLY_ONE not in said(h)
    assert "PINEAPPLE" in said(h)
    # The first reply (which the report also renders into), and the prompt's own.
    assert len(h.slack.calls_to("chat.startStream")) == 3
    await assert_stop_has_nothing_to_stop(h, session)


@pytest.mark.parametrize("name", ["at-init", "after-tools"])
async def test_a_prompt_sent_into_a_report_turn_but_not_taken_in_keeps_its_place(
    harness_for: Callable[..., Harness], name: str
) -> None:
    r = scene(name)
    if r.call_id is None:  # `at-init` makes no call: the prompt goes in once the turn runs
        h = harness_for({})
        session = h.session()
        await first_turn(h, session, r)
        h.clients[0].inject(r.head)
        await until(lambda: session._active is not None)
        h.clients[0].inject(r.notice)
        await until(lambda: not session._tasks)
        prompt = await send(h, session, PROMPT)
    else:
        h = harness_for({})
        session = h.session()
        prompt = await prompt_during_the_report_turn(h, session, r)
    play(h, r.rest)  # the report's one result, no replay in it
    await own_turn_follows(h, session, r, prompt)


@pytest.mark.parametrize("name", ["at-init", "after-tools"])
async def test_a_prompt_given_to_a_report_turn_that_did_not_take_it_is_put_back(
    harness_for: Callable[..., Harness], name: str
) -> None:
    # `_start_turn` makes the prompt the report turn's owner; with no replay in the turn the
    # result's origin sends it back to wait for its own turn.
    r = scene(name)
    h = harness_for({})
    session = h.session()
    await first_turn(h, session, r)
    prompt = await send(h, session, PROMPT)
    play(h, [*r.notice, *r.head, *r.rest])
    await until(lambda: session._active is None and prompt in session._sent)
    await own_turn_follows(h, session, r, prompt)


async def test_stop_after_a_prompt_was_taken_in_releases_it_once(
    harness_for: Callable[..., Harness],
) -> None:
    # Assembled from two recordings, not recorded as one: `during-tool` up to the replay, then
    # the interrupted ending of `stop-queued`. What Claude Code does with a prompt it took in
    # when an interrupt arrives is not measured; this pins what the daemon does if the result is
    # the interrupted one with an injected origin.
    r = scene("during-tool")
    ending = scene("stop-queued").rest[-3:]  # the result of the cut command, the note, the result
    assert isinstance(ending[-1], ResultMessage) and ending[-1].terminal_reason == "aborted_tools"
    h = harness_for({})
    session = h.session()
    prompt = await prompt_during_the_report_turn(h, session, r)
    replayed = next(
        i for i, f in enumerate(r.rest) if isinstance(f, UserMessage) and isinstance(f.content, str)
    )
    play(h, r.rest[: replayed + 1])
    await until(lambda: bool(session._active.taken))
    assert await session.stop() is True
    h.clients[0].inject(ending)
    await asyncio.wait_for(prompt.done.wait(), 2)
    await until(lambda: session._active is None)
    assert_nothing_runs(h, session)
    assert said(h).count(texts.TAKEN_INTO_REPLY_ONE) == 1
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


# --- Whose turn starts, known from the replay (issue #205) ---------------------------------------
# `prompt-replay-before-notification`: recorded on 2026-10-09 with claude-agent-sdk 0.2.164 and
# its bundled CLI 2.1.292. The prompt is sent just before a background command's notification:
# its turn opens with `init`, the notification, the replay and then its words; the turn that
# reports the command follows on its own, with no replay before its words.


@dataclass
class Crossing:
    owner: list[Any]  # the first prompt's turn, which starts the background command
    notice: list[Any]  # the task frames that end the command, as they come with no turn running
    own: list[Any]  # the crossing prompt's turn, replay included, without the task frames
    report: list[Any]  # the turn Claude Code starts to report the command


def crossing() -> Crossing:
    m = sdk_messages("prompt-replay-before-notification")
    ends = [i for i, x in enumerate(m) if isinstance(x, ResultMessage)]
    second = m[ends[0] + 1 : ends[1] + 1]
    return Crossing(
        owner=m[: ends[0] + 1],
        notice=[x for x in second if isinstance(x, sessions.TASK_MESSAGES)],
        own=[x for x in second if not isinstance(x, sessions.TASK_MESSAGES)],
        report=m[ends[1] + 1 : ends[2] + 1],
    )


def misrouted(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if " went to a" in r.getMessage()]


def reply_with(h: Harness, word: str) -> int:
    """Which reply holds `word`: its place among the replies that say something."""
    [place] = [i for i, body in enumerate(h.bodies()) if word in body]
    return place


async def test_the_recorded_crossing_gives_the_prompt_and_the_report_a_reply_each(
    harness_for: Callable[..., Harness], caplog: pytest.LogCaptureFixture
) -> None:
    c = crossing()
    h = harness_for({})
    session = h.session()
    await first_turn(h, session, c)
    prompt = await send(h, session, PROMPT)
    play(h, [*c.own[:1], *c.notice, *c.own[1:]])  # as recorded: init, the notification, the replay
    await asyncio.wait_for(prompt.done.wait(), 2)
    h.clients[0].inject(c.report)
    await until(lambda: "REPORTED" in said(h) and session.idle and session._settled.is_set())
    assert misrouted(caplog) == []
    assert reply_with(h, "PINEAPPLE") != reply_with(h, "REPORTED")


async def test_a_prompt_s_turn_that_comes_while_a_report_is_awaited_goes_to_its_own_reply(
    harness_for: Callable[..., Harness], caplog: pytest.LogCaptureFixture
) -> None:
    # A report turn is awaited and a prompt is sent all the same (the session holds a prompt
    # while it awaits one, so only a notification read between that check and the send gets
    # here; the state is set by hand, as the tests of `_settle` do). Claude Code runs the prompt
    # first, and its replay says so before its first word.
    c = crossing()
    h = harness_for({})
    session = h.session()
    await first_turn(h, session, c)
    prompt = await send(h, session, PROMPT)
    h.clients[0].inject(c.notice)
    await asyncio.sleep(0.05)
    session._expect_injected_turn()
    play(h, c.own)
    await asyncio.wait_for(prompt.done.wait(), 2)
    assert misrouted(caplog) == []
    assert "PINEAPPLE" in h.bodies()[-1] and "STARTED" not in h.bodies()[-1]
    # The report is awaited again, and lands apart from the prompt's answer.
    assert session._injected_expected and not session._settled.is_set()
    h.clients[0].inject(c.report)
    await until(lambda: "REPORTED" in said(h) and session.idle and session._settled.is_set())
    assert misrouted(caplog) == []
    assert reply_with(h, "PINEAPPLE") != reply_with(h, "REPORTED")


async def test_a_report_turn_that_starts_while_a_prompt_waits_does_not_take_its_reply(
    harness_for: Callable[..., Harness], caplog: pytest.LogCaptureFixture
) -> None:
    # The prompt is already sent when the command ends, so no report turn is awaited, and the
    # report turn starts first all the same, with words and no replay. The frames are the
    # recording's; this order of the two turns is composed here, not recorded: it is the one
    # the daemon logged as `a background reply ... went to an owner reply`.
    c = crossing()
    h = harness_for({})
    session = h.session()
    await first_turn(h, session, c)
    prompt = await send(h, session, PROMPT)
    h.clients[0].inject(c.notice)
    await asyncio.sleep(0.05)
    assert not session._injected_expected
    h.clients[0].inject(c.report)
    await until(lambda: session._active is None and "REPORTED" in said(h))
    assert not prompt.done.is_set()
    play(h, c.own)
    await asyncio.wait_for(prompt.done.wait(), 2)
    await until(lambda: session.idle and session._settled.is_set())
    assert misrouted(caplog) == []
    assert reply_with(h, "PINEAPPLE") != reply_with(h, "REPORTED")


async def test_a_turn_with_no_replay_and_no_notification_waiting_is_the_prompt_s(
    harness_for: Callable[..., Harness], caplog: pytest.LogCaptureFixture
) -> None:
    # No recording shows a prompt's turn without its replay; if one comes (a turn that fails
    # before its first word was never recorded), it keeps the prompt's reply as long as Claude
    # Code has nothing to report.
    c = crossing()
    h = harness_for({})
    session = h.session()
    await first_turn(h, session, c)
    prompt = await send(h, session, PROMPT)
    h.clients[0].inject(
        [x for x in c.own if not (isinstance(x, UserMessage) and isinstance(x.content, str))]
    )
    await asyncio.wait_for(prompt.done.wait(), 2)
    assert misrouted(caplog) == []
    assert h.bodies() == ["STARTED", "PINEAPPLE"]
