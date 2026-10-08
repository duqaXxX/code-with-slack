"""A compaction as the session shows it (issue #169), replayed from recordings made on 2026-10-08
with claude-agent-sdk 0.2.164 and its bundled CLI 2.1.292, with the daemon's own CLI arguments
(`--replay-user-messages`): `compact` is a whole session, `auto-compact` is the one turn of a
longer session in which Claude Code compacted on its own.

Measured there: `compact_boundary` is the first frame of its turn that could show anything. On
`/compact` only `status` and `init` frames come before it, and the prompt is never replayed; on an
automatic compaction at the start of a turn the replay of the prompt comes before it as well."""

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from claude_agent_sdk import SystemMessage, UserMessage

from code_with_slack import sessions
from tests.fakes import sdk_messages, split_turns
from tests.test_sessions import Harness, split_background, until
from tests.test_sessions_prompt_replay import play, send


def boundary(frames: list[Any]) -> SystemMessage:
    return next(
        m for m in frames if isinstance(m, SystemMessage) and m.subtype == "compact_boundary"
    )


def compacting(frames: list[Any]) -> int:
    """Where the `status` frame that says `compacting` sits in `frames`."""
    return next(
        i
        for i, m in enumerate(frames)
        if isinstance(m, SystemMessage)
        and m.subtype == "status"
        and m.data.get("status") == "compacting"
    )


def status_lines(h: Harness) -> list[str]:
    """What the thread's status line was set to, in order; empty for a cleared line."""
    calls = h.slack.calls_to("assistant.threads.setStatus")
    return ["".join(call.get("loading_messages") or []) for call in calls]


async def test_a_compact_command_says_how_many_tokens_it_saved_and_the_session_goes_on(
    harness_for: Callable[..., Harness],
) -> None:
    turns = split_turns(sdk_messages("compact"))
    h = harness_for({"turns": turns})
    for prompt in ("remember a word", "read the ledger", "/compact", "which word"):
        await asyncio.wait_for((await h.session().submit(prompt)).done.wait(), 2)
    assert [text.strip() for text in h.slack.stream_texts()] == [
        "ok",
        "read",
        "Compacted the conversation: 20.7k → 5.0k tokens.",
        "MARK-ALPHA",
    ]


async def test_a_compaction_before_a_turns_first_words_shows_above_them(
    harness_for: Callable[..., Harness],
) -> None:
    h = harness_for({"turns": []})
    running = await send(h, h.session(), "read the ledgers again")
    play(h, sdk_messages("auto-compact"))
    await asyncio.wait_for(running.done.wait(), 2)
    [text] = h.slack.stream_texts()
    assert text.split("\n\n") == ["Compacted the conversation: 67.9k → 10.6k tokens.", "read"]


async def test_a_boundary_with_no_turn_due_starts_no_reply(
    harness_for: Callable[..., Harness],
) -> None:
    turns = split_turns(sdk_messages("compact"))
    h = harness_for({"turns": turns[:1]})
    session = h.session()
    await asyncio.wait_for((await session.submit("remember a word")).done.wait(), 2)
    h.clients[0].inject([boundary(turns[2])])
    await asyncio.sleep(0.05)
    assert len(h.slack.stream_ts) == 1 and session.idle


async def test_the_thread_says_it_is_compacting_until_the_compaction_ends(
    harness_for: Callable[..., Harness],
) -> None:
    turn = split_turns(sdk_messages("compact"))[2]
    cut = compacting(turn) + 1
    h = harness_for({"turns": []})
    running = await send(h, h.session(), "/compact")
    assert status_lines(h)[-1] == "Working…"
    h.clients[0].inject(turn[:cut])
    await until(lambda: status_lines(h)[-1] == "Compacting conversation…")
    [call] = [
        c for c in h.slack.calls_to("assistant.threads.setStatus") if c.get("loading_messages")
    ][-1:]
    assert call["status"] == "is compacting the conversation…"
    h.clients[0].inject(turn[cut:])
    await asyncio.wait_for(running.done.wait(), 2)
    await until(lambda: status_lines(h)[-1] == "")


async def test_a_turn_that_goes_on_after_a_compaction_says_it_is_working_again(
    harness_for: Callable[..., Harness],
) -> None:
    turn = sdk_messages("auto-compact")
    began, ended = compacting(turn) + 1, turn.index(boundary(turn)) + 1
    h = harness_for({"turns": []})
    running = await send(h, h.session(), "read the ledgers again")
    play(h, turn[:began])
    await until(lambda: status_lines(h)[-1] == "Compacting conversation…")
    play(h, turn[began:ended])
    await until(lambda: status_lines(h)[-1] == "Working…")
    play(h, turn[ended:])
    await asyncio.wait_for(running.done.wait(), 2)


async def test_a_report_turn_that_compacts_first_is_waited_for(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorded background run, with the recorded frames of an automatic compaction placed
    where a compaction as a turn begins puts them, after the report turn's `init` and `status`.
    Only the order is arranged by hand; every frame is recorded."""
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.05)
    first, notice, report = split_background()
    auto = sdk_messages("auto-compact")
    start, end = compacting(auto), auto.index(boundary(auto))
    began, *ended = [m for m in auto[start : end + 1] if not isinstance(m, UserMessage)]
    lead = next(i for i, m in enumerate(report) if not isinstance(m, SystemMessage))
    h = harness_for({"turns": [first]})
    await asyncio.wait_for((await h.session().submit("start it")).done.wait(), 2)
    h.clients[0].inject([*notice, *report[:lead], began])
    await asyncio.sleep(0.3)  # several times the wait for a report turn
    assert h.slack.calls_to("chat.stopStream") == []
    assert status_lines(h)[-1] == "Compacting conversation…"
    h.clients[0].inject([*ended, *report[lead:]])
    await until(lambda: bool(h.slack.calls_to("chat.stopStream")))
    [text] = h.slack.stream_texts()
    assert "Compacted the conversation: 67.9k → 10.6k tokens." in text
