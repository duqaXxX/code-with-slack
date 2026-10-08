"""A compaction as the session shows it (issue #169), replayed from recordings made on 2026-10-08
with claude-agent-sdk 0.2.164 and its bundled CLI 2.1.292, with the daemon's own CLI arguments
(`--replay-user-messages`): `compact` and `compact-interrupt` are whole sessions, `auto-compact` is
the one turn of a longer session in which Claude Code compacted on its own.

Measured there: `compact_boundary` is the first frame of its turn that could show anything. On
`/compact` only `status` and `init` frames come before it, and the prompt is never replayed; on an
automatic compaction at the start of a turn the replay of the prompt comes before it as well."""

import asyncio
from collections.abc import Callable
from typing import Any

from claude_agent_sdk import SystemMessage

from code_with_slack import texts
from code_with_slack.footer import format_tokens
from tests.fakes import sdk_messages, split_turns
from tests.test_sessions import Harness
from tests.test_sessions_prompt_replay import play, send


def compacting_turn(name: str) -> list[Any]:
    """The turn of the recording `name` that holds a `status` frame saying `compacting`."""
    return next(
        turn
        for turn in split_turns(sdk_messages(name))
        if any(
            isinstance(m, SystemMessage)
            and m.subtype == "status"
            and m.data.get("status") == "compacting"
            for m in turn
        )
    )


def compacted_line(turn: list[Any]) -> str:
    """The line the recorded `compact_boundary` of `turn` reads as."""
    boundary = next(
        m for m in turn if isinstance(m, SystemMessage) and m.subtype == "compact_boundary"
    )
    metadata = boundary.data["compact_metadata"]
    return texts.COMPACTED.format(
        before=format_tokens(metadata["pre_tokens"]), after=format_tokens(metadata["post_tokens"])
    )


async def test_a_compact_command_says_how_many_tokens_it_saved(
    harness_for: Callable[..., Harness],
) -> None:
    turn = compacting_turn("compact")
    h = harness_for({"turns": [turn]})
    await asyncio.wait_for((await h.session().submit("/compact")).done.wait(), 2)
    assert [text.strip() for text in h.slack.stream_texts()] == [compacted_line(turn)]


async def test_a_compaction_before_a_turns_first_words_shows_above_them(
    harness_for: Callable[..., Harness],
) -> None:
    turn = compacting_turn("auto-compact")
    h = harness_for({"turns": []})
    running = await send(h, h.session(), "read the ledgers again")
    play(h, turn)
    await asyncio.wait_for(running.done.wait(), 2)
    [text] = h.slack.stream_texts()
    assert text.split("\n\n") == [compacted_line(turn), "read"]


async def test_a_compaction_cut_short_says_so_and_claims_no_saving(
    harness_for: Callable[..., Harness],
) -> None:
    turn = compacting_turn("compact-interrupt")
    h = harness_for({"turns": [turn]})
    await asyncio.wait_for((await h.session().submit("/compact")).done.wait(), 2)
    assert h.slack.stream_texts() == ["Compaction canceled."]
