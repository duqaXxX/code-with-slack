from pathlib import Path

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from code_with_slack import texts
from code_with_slack.render.sinks import UpdateLimiter
from code_with_slack.render.status import Status
from code_with_slack.repair import repair_crash
from code_with_slack.state import StateStore
from tests.fakes import CHANNEL, OTHER_THREAD, THREAD, FakeSlack

STOPPED_LINE = texts.ENDED.format(reason=texts.ENDED_SHUTDOWN)


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


async def test_an_open_reply_is_rewritten_keeping_its_body_and_ending_with_the_stopped_line(
    tmp_path: Path, slack: FakeSlack
) -> None:
    state = make_state(tmp_path)
    body_blocks = [{"type": "markdown", "text": "Looking at the files."}]
    slack.responses["conversations.replies"] = {
        "ok": True,
        "messages": [
            {
                "ts": "1790000000.000001",
                "blocks": [
                    *body_blocks,
                    # the daemon's own transient status line: a context block, no block_id.
                    {
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": "Claude is writing…"}],
                    },
                ],
            }
        ],
    }
    state.set_open_reply(CHANNEL, THREAD, "1790000000.000001")
    await repair_crash(slack, state, UpdateLimiter())
    [update] = slack.calls_to("chat.update")
    assert update["ts"] == "1790000000.000001"
    assert update["blocks"] == [*body_blocks, {"type": "markdown", "text": STOPPED_LINE}]
    assert state.thread(CHANNEL, THREAD).open_reply is None


async def test_a_reply_with_no_status_line_still_gets_the_stopped_line_appended(
    tmp_path: Path, slack: FakeSlack
) -> None:
    # Already in its final form (past `finish`, not yet `close_out`): nothing to drop, since
    # the last block is a markdown block, not the shape a status line has.
    state = make_state(tmp_path)
    body_blocks = [{"type": "markdown", "text": "Done."}]
    slack.responses["conversations.replies"] = {
        "ok": True,
        "messages": [{"ts": "1790000000.000001", "blocks": body_blocks}],
    }
    state.set_open_reply(CHANNEL, THREAD, "1790000000.000001")
    await repair_crash(slack, state, UpdateLimiter())
    [update] = slack.calls_to("chat.update")
    assert update["blocks"] == [*body_blocks, {"type": "markdown", "text": STOPPED_LINE}]


async def test_a_deleted_reply_message_is_left_alone_and_the_field_still_clears(
    tmp_path: Path, slack: FakeSlack
) -> None:
    state = make_state(tmp_path)
    slack.responses["conversations.replies"] = {"ok": True, "messages": []}
    state.set_open_reply(CHANNEL, THREAD, "1790000000.000001")
    await repair_crash(slack, state, UpdateLimiter())
    assert slack.calls_to("chat.update") == []
    assert state.thread(CHANNEL, THREAD).open_reply is None


async def test_a_failed_read_leaves_the_message_untouched_and_still_clears_the_field(
    tmp_path: Path, slack: FakeSlack
) -> None:
    state = make_state(tmp_path)
    slack.responses["conversations.replies"] = RuntimeError("network down")
    state.set_open_reply(CHANNEL, THREAD, "1790000000.000001")
    await repair_crash(slack, state, UpdateLimiter())
    assert slack.calls_to("chat.update") == []
    assert state.thread(CHANNEL, THREAD).open_reply is None


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
    removed = [a["name"] for a in slack.calls_to("reactions.remove")]
    added = [a["name"] for a in slack.calls_to("reactions.add")]
    assert removed == [Status.WAITING.value]
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
    state.set_open_reply(CHANNEL, THREAD, "1790000000.000001")
    state.set_status_pending(CHANNEL, OTHER_THREAD, Status.WORKING.value)
    slack.responses["conversations.replies"] = RuntimeError("boom")
    await repair_crash(slack, state, UpdateLimiter())
    assert state.thread(CHANNEL, THREAD).open_reply is None
    assert state.thread(CHANNEL, OTHER_THREAD).status is None
    added = [a["name"] for a in slack.calls_to("reactions.add")]
    assert added == [Status.ERROR.value]


async def test_repair_never_stores_or_sends_message_content(
    tmp_path: Path, slack: FakeSlack
) -> None:
    state = make_state(tmp_path)
    state.set_open_reply(CHANNEL, THREAD, "1790000000.000001")
    secret_block = {"type": "markdown", "text": "secret"}
    slack.responses["conversations.replies"] = {
        "ok": True,
        "messages": [{"ts": "1790000000.000001", "blocks": [secret_block]}],
    }
    await repair_crash(slack, state, UpdateLimiter())
    raw = (tmp_path / "state.json").read_text()
    assert "secret" not in raw
