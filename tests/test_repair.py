from pathlib import Path

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from code_with_slack import texts
from code_with_slack.render.sinks import BLOCKS_LIMIT, UpdateLimiter
from code_with_slack.render.status import Status
from code_with_slack.repair import repair_crash
from code_with_slack.state import StateStore
from tests.fakes import CHANNEL, OTHER_THREAD, THREAD, FakeSlack, slack_payload

STOPPED_LINE = texts.ENDED.format(reason=texts.ENDED_SHUTDOWN)
STOPPED_BLOCK = {"type": "markdown", "text": STOPPED_LINE}


def make_state(tmp_path: Path) -> StateStore:
    state = StateStore(tmp_path / "state.json")
    state.bind(CHANNEL, tmp_path)
    state.open_thread(CHANNEL, THREAD)
    return state


def slack_error(code: str) -> SlackApiError:
    response = AsyncSlackResponse(
        client=None,  # type: ignore[arg-type]
        http_verb="POST",
        api_url="x",
        req_args={},
        data={"ok": False, "error": code},
        headers={},
        status_code=200,
    )
    return SlackApiError(f"error: {code}", response)


async def test_a_second_start_repairs_nothing(tmp_path: Path, slack: FakeSlack) -> None:
    state = make_state(tmp_path)
    await repair_crash(slack, state, UpdateLimiter())
    assert slack.calls == []


async def test_an_open_reply_is_read_by_its_own_ts_and_rewritten_dropping_only_the_status_block(
    tmp_path: Path, slack: FakeSlack
) -> None:
    # Recorded from a real `conversations.replies` response on 2026-09-28 (slack-sdk 3.44.1),
    # scrubbed, as other fixtures are. A posted `markdown` block reads back as `rich_text`; a
    # block posted with an explicit block_id keeps it; one without gets a Slack-assigned id,
    # which is why the status line's own fixed block_id (`sinks.STATUS_BLOCK_ID`) is what repair
    # looks for, not "no block_id".
    state = make_state(tmp_path)
    fixture = slack_payload("api-conversations-replies-by-ts")
    message_ts = fixture["messages"][0]["ts"]
    slack.responses["conversations.replies"] = fixture
    state.replace_open_reply(CHANNEL, THREAD, None, message_ts)
    await repair_crash(slack, state, UpdateLimiter())
    [call] = slack.calls_to("conversations.replies")
    assert call["channel"] == CHANNEL
    assert call["ts"] == message_ts
    assert call["limit"] == 1
    assert call["oldest"] is None and call["latest"] is None and call["inclusive"] is None
    [update] = slack.calls_to("chat.update")
    assert update["ts"] == message_ts
    original = fixture["messages"][0]["blocks"]
    assert update["blocks"] == [*original[:-1], STOPPED_BLOCK]  # only the "status" block dropped
    assert any(b["block_id"] == "Pq6Je" for b in update["blocks"])  # an unrelated context block
    assert state.thread(CHANNEL, THREAD).open_replies == ()


async def test_a_reply_with_no_status_block_still_gets_the_stopped_line_appended(
    tmp_path: Path, slack: FakeSlack
) -> None:
    # Already in its final form (past `finish`, not yet `close_out`): nothing to drop, since the
    # last block's id is not the status line's.
    state = make_state(tmp_path)
    body_blocks = [{"type": "markdown", "block_id": "auto1", "text": "Done."}]
    slack.responses["conversations.replies"] = {
        "ok": True,
        "messages": [{"ts": "1790000000.000001", "blocks": body_blocks}],
    }
    state.replace_open_reply(CHANNEL, THREAD, None, "1790000000.000001")
    await repair_crash(slack, state, UpdateLimiter())
    [update] = slack.calls_to("chat.update")
    assert update["blocks"] == [*body_blocks, STOPPED_BLOCK]


async def test_a_full_reply_replaces_its_last_block_instead_of_exceeding_the_limit(
    tmp_path: Path, slack: FakeSlack
) -> None:
    state = make_state(tmp_path)
    body_blocks = [
        {"type": "markdown", "block_id": f"b{i}", "text": f"x{i}"} for i in range(BLOCKS_LIMIT)
    ]
    slack.responses["conversations.replies"] = {
        "ok": True,
        "messages": [{"ts": "1790000000.000001", "blocks": body_blocks}],
    }
    state.replace_open_reply(CHANNEL, THREAD, None, "1790000000.000001")
    await repair_crash(slack, state, UpdateLimiter())
    [update] = slack.calls_to("chat.update")
    assert len(update["blocks"]) == BLOCKS_LIMIT
    assert update["blocks"][:-1] == body_blocks[:-1]
    assert update["blocks"][-1] == STOPPED_BLOCK


async def test_a_deleted_reply_message_is_left_alone_and_the_field_still_clears(
    tmp_path: Path, slack: FakeSlack
) -> None:
    state = make_state(tmp_path)
    slack.responses["conversations.replies"] = {"ok": True, "messages": []}
    state.replace_open_reply(CHANNEL, THREAD, None, "1790000000.000001")
    await repair_crash(slack, state, UpdateLimiter())
    assert slack.calls_to("chat.update") == []
    assert state.thread(CHANNEL, THREAD).open_replies == ()


async def test_a_failed_read_leaves_the_message_untouched_and_still_clears_the_field(
    tmp_path: Path, slack: FakeSlack
) -> None:
    state = make_state(tmp_path)
    slack.responses["conversations.replies"] = RuntimeError("network down")
    state.replace_open_reply(CHANNEL, THREAD, None, "1790000000.000001")
    await repair_crash(slack, state, UpdateLimiter())
    assert slack.calls_to("chat.update") == []
    assert state.thread(CHANNEL, THREAD).open_replies == ()


async def test_more_than_one_open_reply_in_a_thread_are_all_repaired(
    tmp_path: Path, slack: FakeSlack
) -> None:
    # A background task's own reply can outlive the turn that started it: two open at once.
    state = make_state(tmp_path)
    slack.responses["conversations.replies"] = {"ok": True, "messages": []}
    state.replace_open_reply(CHANNEL, THREAD, None, "1790000000.000001")
    state.replace_open_reply(CHANNEL, THREAD, None, "1790000000.000002")
    await repair_crash(slack, state, UpdateLimiter())
    read = [c["ts"] for c in slack.calls_to("conversations.replies")]
    assert read == ["1790000000.000001", "1790000000.000002"]
    assert state.thread(CHANNEL, THREAD).open_replies == ()


async def test_stale_requests_are_deleted_and_a_gone_one_still_counts_as_done(
    tmp_path: Path, slack: FakeSlack
) -> None:
    state = make_state(tmp_path)
    state.add_request(CHANNEL, THREAD, "1790000000.000002")
    state.add_request(CHANNEL, THREAD, "1790000000.000003")
    slack.responses["chat.delete"] = [{"ok": True}, slack_error("message_not_found")]
    await repair_crash(slack, state, UpdateLimiter())
    deleted = [a["ts"] for a in slack.calls_to("chat.delete")]
    assert deleted == ["1790000000.000002", "1790000000.000003"]
    assert state.thread(CHANNEL, THREAD).requests == ()


async def test_a_root_left_waiting_or_working_gets_x_and_the_field_clears(
    tmp_path: Path, slack: FakeSlack
) -> None:
    state = make_state(tmp_path)
    state.set_status_pending(CHANNEL, THREAD, Status.WAITING.value)
    await repair_crash(slack, state, UpdateLimiter())
    # Through `StatusReaction` (fix round item 9): a fresh instance's first `show` also strips
    # every other stray reaction on the root, not just the one name state.json recorded.
    removed = {a["name"] for a in slack.calls_to("reactions.remove")}
    added = [a["name"] for a in slack.calls_to("reactions.add")]
    assert removed == {s.value for s in Status if s is not Status.ERROR}
    assert added == [Status.ERROR.value]
    assert state.thread(CHANNEL, THREAD).status is None


async def test_already_reacted_and_no_reaction_count_as_done(
    tmp_path: Path, slack: FakeSlack
) -> None:
    state = make_state(tmp_path)
    state.set_status_pending(CHANNEL, THREAD, Status.WORKING.value)
    slack.responses["reactions.remove"] = slack_error("no_reaction")
    slack.responses["reactions.add"] = slack_error("already_reacted")
    await repair_crash(slack, state, UpdateLimiter())
    assert state.thread(CHANNEL, THREAD).status is None


async def test_one_threads_failure_does_not_stop_the_others(
    tmp_path: Path, slack: FakeSlack
) -> None:
    state = make_state(tmp_path)
    state.open_thread(CHANNEL, OTHER_THREAD)
    state.replace_open_reply(CHANNEL, THREAD, None, "1790000000.000001")
    state.set_status_pending(CHANNEL, OTHER_THREAD, Status.WORKING.value)
    slack.responses["conversations.replies"] = RuntimeError("boom")
    await repair_crash(slack, state, UpdateLimiter())
    assert state.thread(CHANNEL, THREAD).open_replies == ()
    assert state.thread(CHANNEL, OTHER_THREAD).status is None
    added = [a["name"] for a in slack.calls_to("reactions.add")]
    assert added == [Status.ERROR.value]


async def test_repair_never_stores_or_sends_message_content(
    tmp_path: Path, slack: FakeSlack
) -> None:
    state = make_state(tmp_path)
    state.replace_open_reply(CHANNEL, THREAD, None, "1790000000.000001")
    secret_block = {"type": "markdown", "text": "secret"}
    slack.responses["conversations.replies"] = {
        "ok": True,
        "messages": [{"ts": "1790000000.000001", "blocks": [secret_block]}],
    }
    await repair_crash(slack, state, UpdateLimiter())
    raw = (tmp_path / "state.json").read_text()
    assert "secret" not in raw


async def test_a_failed_state_write_is_logged_and_swallowed(
    tmp_path: Path, slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = make_state(tmp_path)
    state.replace_open_reply(CHANNEL, THREAD, None, "1790000000.000001")
    slack.responses["conversations.replies"] = {"ok": True, "messages": []}

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(state, "replace_open_reply", boom)
    await repair_crash(slack, state, UpdateLimiter())  # must not raise
