import copy
from pathlib import Path

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from code_with_slack import texts
from code_with_slack.render.sinks import UpdateLimiter
from code_with_slack.render.status import Status
from code_with_slack.repair import repair_crash
from code_with_slack.state import StateStore
from tests.fakes import CHANNEL, OTHER_THREAD, THREAD, FakeSlack, slack_payload

STOPPED_BLOCK = {
    "type": "context",
    "elements": [{"type": "mrkdwn", "text": texts.STOPPED_BEFORE_ANSWER}],
}


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


async def test_an_open_reply_is_stopped_then_rewritten_closing_its_running_cards(
    tmp_path: Path, slack: FakeSlack
) -> None:
    # Recorded from a real `conversations.replies` response on 2026-09-29 (slack-sdk 3.44.1) of
    # a stopped stream, scrubbed as other fixtures are, one card set back to `in_progress` as
    # an update-phase message a crash cut off would still show it. A card left running in a
    # stopped stream is stored as an error (M33); a card in a message that was updated is not.
    state = make_state(tmp_path)
    fixture = slack_payload("api-conversations-replies-stream")
    message_ts = fixture["messages"][0]["ts"]
    slack.responses["conversations.replies"] = fixture
    state.replace_open_reply(CHANNEL, THREAD, None, message_ts)
    await repair_crash(slack, state, UpdateLimiter())
    assert [m for m, _ in slack.calls if m.startswith("chat.")] == [
        "chat.stopStream",
        "chat.update",
    ]
    [stop] = slack.calls_to("chat.stopStream")
    assert stop["channel"] == CHANNEL and stop["ts"] == message_ts and "blocks" not in stop
    [call] = slack.calls_to("conversations.replies")
    assert call["channel"] == CHANNEL and call["ts"] == message_ts and call["limit"] == 1
    assert call["oldest"] is None and call["latest"] is None and call["inclusive"] is None
    [update] = slack.calls_to("chat.update")
    assert update["ts"] == message_ts and update["text"] == texts.STOPPED_BEFORE_ANSWER
    original = fixture["messages"][0]["blocks"]
    assert [b["status"] for b in update["blocks"] if b["type"] == "task_card"] == [
        "complete",
        "error",
        "complete",
    ]
    # everything else as Slack read it back, and the line that says it stopped, last
    assert update["blocks"][:-1] == [
        {**b, "status": "error"} if b.get("task_id") == "t2" else b for b in original
    ]
    assert update["blocks"][-1] == STOPPED_BLOCK
    assert slack.calls_to("chat.postMessage") == []  # nothing is posted: the stop is the push
    assert state.thread(CHANNEL, THREAD).open_replies == ()


async def test_a_crashed_reply_loses_its_line_of_what_still_runs(
    tmp_path: Path, slack: FakeSlack
) -> None:
    # The line as Slack reads it back from a stream (measured 2026-10-02, slack-sdk 3.44.1): a
    # context block with an id of Slack's own, the hourglass as its name.
    state = make_state(tmp_path)
    body_blocks = [{"type": "rich_text", "block_id": "auto1", "elements": []}]
    line = {
        "type": "context",
        "block_id": "auto2",
        "elements": [
            {
                "type": "mrkdwn",
                "text": ":hourglass_flowing_sand: 1 shell still running",
                "verbatim": False,
            }
        ],
    }
    slack.responses["conversations.replies"] = {
        "ok": True,
        "messages": [{"ts": "1790000000.000001", "blocks": [*body_blocks, line]}],
    }
    state.replace_open_reply(CHANNEL, THREAD, None, "1790000000.000001")
    await repair_crash(slack, state, UpdateLimiter())
    [update] = slack.calls_to("chat.update")
    assert update["blocks"] == [*body_blocks, STOPPED_BLOCK]  # the tasks went with the process


async def test_a_stream_that_slack_already_closed_is_rewritten_all_the_same(
    tmp_path: Path, slack: FakeSlack
) -> None:
    # Slack ends a stream 5 minutes after it started: the stop then answers that it is over.
    state = make_state(tmp_path)
    body_blocks = [{"type": "rich_text", "block_id": "auto1", "elements": []}]
    slack.responses["chat.stopStream"] = slack_error("message_not_in_streaming_state")
    slack.responses["conversations.replies"] = {
        "ok": True,
        "messages": [{"ts": "1790000000.000001", "blocks": body_blocks}],
    }
    state.replace_open_reply(CHANNEL, THREAD, None, "1790000000.000001")
    await repair_crash(slack, state, UpdateLimiter())
    [update] = slack.calls_to("chat.update")
    assert update["blocks"] == [*body_blocks, STOPPED_BLOCK]


async def test_a_stop_that_fails_is_logged_and_the_rewrite_is_still_tried(
    tmp_path: Path, slack: FakeSlack, caplog: pytest.LogCaptureFixture
) -> None:
    state = make_state(tmp_path)
    slack.responses["chat.stopStream"] = RuntimeError("network down")
    slack.responses["conversations.replies"] = {
        "ok": True,
        "messages": [{"ts": "1790000000.000001", "blocks": []}],
    }
    state.replace_open_reply(CHANNEL, THREAD, None, "1790000000.000001")
    with caplog.at_level("WARNING"):
        await repair_crash(slack, state, UpdateLimiter())
    assert "could not stop" in caplog.text
    assert len(slack.calls_to("chat.update")) == 1


async def repaired_blocks(
    tmp_path: Path, slack: FakeSlack, body_blocks: list[dict[str, object]]
) -> list[dict[str, object]]:
    state = make_state(tmp_path)
    slack.responses["conversations.replies"] = {
        "ok": True,
        "messages": [{"ts": "1790000000.000001", "blocks": body_blocks}],
    }
    state.replace_open_reply(CHANNEL, THREAD, None, "1790000000.000001")
    await repair_crash(slack, state, UpdateLimiter())
    [update] = slack.calls_to("chat.update")
    blocks: list[dict[str, object]] = update["blocks"]
    return blocks


def content(n: int) -> list[dict[str, object]]:
    return [{"type": "rich_text", "block_id": f"b{i}", "elements": []} for i in range(n)]


async def test_a_message_up_to_slacks_cap_less_one_still_gets_the_stopped_line(
    tmp_path: Path, slack: FakeSlack
) -> None:
    # Slack's cap is 50 blocks, not the sink's 45: a read-back message (streamed markdown reads
    # back as several blocks) at 49 has room for the line.
    body = content(49)
    blocks = await repaired_blocks(tmp_path, slack, body)
    assert blocks == [*body, STOPPED_BLOCK]


async def test_a_message_at_the_cap_puts_the_line_in_its_last_context_block(
    tmp_path: Path, slack: FakeSlack
) -> None:
    footer = {"type": "context", "elements": [{"type": "mrkdwn", "text": "main · 12% ctx"}]}
    body = [*content(49), footer]
    blocks = await repaired_blocks(tmp_path, slack, body)
    assert blocks[:49] == body[:49] and len(blocks) == 50  # no content block lost
    assert blocks[-1]["elements"][0]["text"] == f"main · 12% ctx · {texts.STOPPED_BEFORE_ANSWER}"


async def test_a_message_at_the_cap_with_no_context_block_loses_no_content(
    tmp_path: Path, slack: FakeSlack
) -> None:
    body = content(50)
    blocks = await repaired_blocks(tmp_path, slack, body)
    assert blocks == body  # the line is dropped: the edit's `text` still says it stopped
    [update] = slack.calls_to("chat.update")
    assert update["text"] == texts.STOPPED_BEFORE_ANSWER


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
    # What the Home tab shows for it from now on.
    assert state.thread(CHANNEL, THREAD).ended == Status.ERROR.value


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


async def test_a_context_block_that_leads_with_an_image_is_left_as_it_is(
    tmp_path: Path, slack: FakeSlack
) -> None:
    image = {"type": "image", "image_url": "https://example.com/a.png", "alt_text": "a"}
    footer = {"type": "context", "elements": [image]}
    body = [*content(49), footer]
    before = copy.deepcopy(body)
    blocks = await repaired_blocks(tmp_path, slack, body)
    assert blocks == before  # only text is joined to; the edit's `text` still says it stopped
