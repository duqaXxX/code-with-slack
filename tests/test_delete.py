from pathlib import Path
from typing import Any

import pytest

from code_with_slack import texts
from code_with_slack.delete import PAGE, ThreadDeleter
from code_with_slack.state import StateStore
from tests.fakes import FakeSlack
from tests.test_repair import slack_error

CHANNEL = "C000CHAN"
ROOT = "1790000000.000001"
OWNER = "U000ALICE"
BOT = "U000BOT"
SESSION = "11111111-1111-4111-8111-111111111111"


def message(ts: str, user: str) -> dict[str, Any]:
    """A thread's message as `conversations.replies` returns it, the fields the delete reads
    (api-conversations-replies-by-ts.json: the bot's own carries `user` and `bot_id`)."""
    found = {"type": "message", "ts": ts, "user": user, "thread_ts": ROOT, "text": "x"}
    return {**found, "bot_id": "B000BOT"} if user == BOT else found


def paged(*pages: list[dict[str, Any]]) -> Any:
    """`conversations.replies` answering page by page: cursor pagination, the next page named
    in `response_metadata.next_cursor` (docs.slack.dev/reference/methods/conversations.replies,
    read 2026-10-05)."""

    def answer(args: dict[str, Any]) -> dict[str, Any]:
        index = int(args.get("cursor") or 0)
        more = index + 1 < len(pages)
        return {
            "ok": True,
            "messages": pages[index],
            "has_more": more,
            "response_metadata": {"next_cursor": str(index + 1) if more else ""},
        }

    return answer


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.bot = FakeSlack()
        self.owner = FakeSlack()
        self.state = StateStore(tmp_path / "state.json")
        self.state.bind(CHANNEL, tmp_path)
        self.state.open_thread(CHANNEL, ROOT, session_id=SESSION)
        self.free = True
        self.released: list[tuple[str, str]] = []
        self.deleter = ThreadDeleter(
            self.bot, self.owner, bot_user_id=BOT, state=self.state, release=self.release
        )

    async def release(self, channel_id: str, thread_ts: str) -> bool:
        self.released.append((channel_id, thread_ts))
        return self.free

    def deleted(self, client: FakeSlack) -> list[str]:
        return [str(args["ts"]) for args in client.calls_to("chat.delete")]


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


async def test_each_message_is_deleted_by_its_author_s_token_and_the_root_last(
    world: World,
) -> None:
    root = message(ROOT, OWNER)
    world.bot.responses["conversations.replies"] = paged(
        [root, message("1790000001.000001", BOT), message("1790000002.000001", OWNER)],
        # Slack returns the root at the head of every page.
        [root, message("1790000003.000001", BOT)],
    )
    assert await world.deleter.delete(CHANNEL, ROOT) is None
    asked = world.bot.calls_to("conversations.replies")
    assert [(a["ts"], a["limit"], a.get("cursor")) for a in asked] == [
        (ROOT, PAGE, None),
        (ROOT, PAGE, "1"),
    ]
    assert world.deleted(world.bot) == ["1790000001.000001", "1790000003.000001"]
    assert world.deleted(world.owner) == ["1790000002.000001", ROOT]
    assert world.released == [(CHANNEL, ROOT)]
    assert world.state.thread(CHANNEL, ROOT) is None  # forgotten once the thread is gone


async def test_a_thread_in_use_is_not_deleted(world: World) -> None:
    world.free = False
    assert await world.deleter.delete(CHANNEL, ROOT) == texts.HOME_DELETE_BUSY
    assert world.bot.calls == [] and world.owner.calls == []
    assert world.state.thread(CHANNEL, ROOT) is not None


async def test_a_thread_the_daemon_does_not_hold_is_not_touched(world: World) -> None:
    # The value of a click is untrusted: only a thread `state.json` holds is ever deleted.
    assert await world.deleter.delete(CHANNEL, "1790000009.000009") is None
    assert await world.deleter.delete("C000NOPE", ROOT) is None
    assert world.released == []
    assert world.bot.calls == [] and world.owner.calls == []
    assert world.state.thread(CHANNEL, ROOT) is not None


async def test_a_message_already_gone_does_not_stop_the_delete(world: World) -> None:
    world.bot.responses["conversations.replies"] = paged(
        [message(ROOT, OWNER), message("1790000001.000001", BOT)]
    )
    world.bot.responses["chat.delete"] = slack_error("message_not_found")
    assert await world.deleter.delete(CHANNEL, ROOT) is None
    assert world.deleted(world.owner) == [ROOT]
    assert world.state.thread(CHANNEL, ROOT) is None


async def test_a_delete_slack_refuses_stops_there_and_keeps_the_thread(world: World) -> None:
    world.bot.responses["conversations.replies"] = paged(
        [message(ROOT, OWNER), message("1790000001.000001", BOT), message("1790000002.000001", BOT)]
    )
    world.bot.responses["chat.delete"] = slack_error("cant_delete_message")
    notice = await world.deleter.delete(CHANNEL, ROOT)
    assert notice == texts.HOME_DELETE_FAILED.format(error="cant_delete_message")
    assert world.deleted(world.bot) == ["1790000001.000001"]  # nothing after the refusal
    assert world.deleted(world.owner) == []  # the root stays while a reply does
    assert world.state.thread(CHANNEL, ROOT) is not None  # asked again, it continues


async def test_a_failure_that_is_not_slack_s_answer_is_a_notice_too(world: World) -> None:
    world.bot.responses["conversations.replies"] = OSError("network down")
    notice = await world.deleter.delete(CHANNEL, ROOT)
    assert notice is not None and notice.startswith("Could not delete every message")
    assert world.state.thread(CHANNEL, ROOT) is not None


async def test_a_thread_whose_root_is_gone_is_forgotten(world: World) -> None:
    world.bot.responses["conversations.replies"] = slack_error("thread_not_found")
    assert await world.deleter.delete(CHANNEL, ROOT) is None
    assert world.deleted(world.bot) == [] and world.deleted(world.owner) == []
    assert world.state.thread(CHANNEL, ROOT) is None
