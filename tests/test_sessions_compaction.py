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

from claude_agent_sdk import SystemMessage

from tests.fakes import sdk_messages, split_turns
from tests.test_sessions import Harness
from tests.test_sessions_prompt_replay import play, send


def boundary(frames: list[Any]) -> SystemMessage:
    return next(
        m for m in frames if isinstance(m, SystemMessage) and m.subtype == "compact_boundary"
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
