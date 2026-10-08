"""A compaction as the session shows it (issue #169), replayed from recordings made on 2026-10-08
with claude-agent-sdk 0.2.164 and its bundled CLI 2.1.292, with the daemon's own CLI arguments
(`--replay-user-messages`): `compact` is a whole session; `auto-compact` is the one turn of a
longer session in which Claude Code compacted on its own; `auto-compact-report-turn` is the end
of a session just past its threshold, from the turn that starts a background command to the turn
Claude Code starts to report it, which compacts first.

Measured there: a compaction opens with a `status` frame that says `compacting` and ends with one
that carries `compact_result`, and `compact_boundary` follows. They come before every frame of
their turn that could show anything. On `/compact` only `status` and `init` frames come before,
and the prompt is never replayed; on an automatic compaction at the start of a turn the replay of
the prompt comes before the boundary as well."""

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from claude_agent_sdk import SystemMessage

from code_with_slack import sessions
from tests.fakes import EndOfStream, sdk_messages, split_turns
from tests.test_sessions import Harness, status_lines, until
from tests.test_sessions_prompt_replay import play, send

COMPACTING = "Compacting conversation…"


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


async def test_a_compaction_with_no_turn_due_starts_no_reply_and_shows_no_line(
    harness_for: Callable[..., Harness],
) -> None:
    turns = split_turns(sdk_messages("compact"))
    h = harness_for({"turns": turns[:1]})
    session = h.session()
    await asyncio.wait_for((await session.submit("remember a word")).done.wait(), 2)
    await until(lambda: status_lines(h)[-1] == "")
    h.clients[0].inject([turns[2][compacting(turns[2])]])
    await asyncio.sleep(0.05)
    assert len(h.slack.stream_ts) == 1 and session.idle
    assert COMPACTING not in status_lines(h)


async def test_the_thread_says_it_is_compacting_until_the_compaction_ends(
    harness_for: Callable[..., Harness],
) -> None:
    turn = split_turns(sdk_messages("compact"))[2]
    cut = compacting(turn) + 1
    h = harness_for({"turns": []})
    running = await send(h, h.session(), "/compact")
    assert status_lines(h)[-1] == "Working…"
    h.clients[0].inject(turn[:cut])
    await until(lambda: status_lines(h)[-1] == COMPACTING)
    shown = h.slack.calls_to("assistant.threads.setStatus")[-1]
    assert shown["status"] == "is compacting the conversation…"
    h.clients[0].inject(turn[cut:])
    await asyncio.wait_for(running.done.wait(), 2)
    await until(lambda: status_lines(h)[-1] == "")


async def test_a_turn_that_goes_on_after_a_compaction_says_it_is_working_again(
    harness_for: Callable[..., Harness],
) -> None:
    turn = sdk_messages("auto-compact")
    began = compacting(turn) + 1
    h = harness_for({"turns": []})
    running = await send(h, h.session(), "read the ledgers again")
    play(h, turn[:began])
    await until(lambda: status_lines(h)[-1] == COMPACTING)
    # The frame that carries `compact_result` ends it, before the boundary arrives.
    assert "compact_result" in turn[began].data
    play(h, turn[began : began + 1])
    await until(lambda: status_lines(h)[-1] == "Working…")
    play(h, turn[began + 1 :])
    await asyncio.wait_for(running.done.wait(), 2)


async def test_a_permission_mode_report_does_not_end_the_compacting_line(
    harness_for: Callable[..., Harness],
) -> None:
    turn = split_turns(sdk_messages("compact"))[2]
    cut = compacting(turn) + 1
    h = harness_for({"turns": []})
    running = await send(h, h.session(), "/compact")
    h.clients[0].inject(turn[:cut])
    await until(lambda: status_lines(h)[-1] == COMPACTING)
    h.clients[0].inject(sdk_messages("permission-mode-status"))
    await asyncio.sleep(0.05)
    assert status_lines(h)[-1] == COMPACTING
    h.clients[0].inject(turn[cut:])
    await asyncio.wait_for(running.done.wait(), 2)


async def test_a_process_that_exits_while_compacting_leaves_no_compacting_line_behind(
    harness_for: Callable[..., Harness],
) -> None:
    turns = split_turns(sdk_messages("compact"))
    cut = compacting(turns[2]) + 1
    h = harness_for({"turns": [[*turns[2][:cut], EndOfStream()]]}, {"turns": [turns[3]]})
    session = h.session()
    await asyncio.wait_for((await session.submit("/compact")).done.wait(), 2)
    before = len(status_lines(h))
    await asyncio.wait_for((await session.submit("which word")).done.wait(), 2)
    after = status_lines(h)[before:]
    assert "Working…" in after and COMPACTING not in after


async def test_a_report_turn_that_compacts_first_is_waited_for(
    harness_for: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "INJECTED_TURN_WAIT", 0.05)
    first, report = split_turns(sdk_messages("auto-compact-report-turn"))
    began = compacting(report) + 1
    h = harness_for({"turns": [first]})
    await asyncio.wait_for((await h.session().submit("start it")).done.wait(), 2)
    h.clients[0].inject(report[:began])
    await asyncio.sleep(0.3)  # several times the wait for a report turn
    assert h.slack.calls_to("chat.stopStream") == []
    assert status_lines(h)[-1] == COMPACTING
    h.clients[0].inject(report[began:])
    await until(lambda: bool(h.slack.calls_to("chat.stopStream")))
    [text] = h.slack.stream_texts()
    assert "Compacted the conversation: 68.2k → 10.7k tokens." in text
