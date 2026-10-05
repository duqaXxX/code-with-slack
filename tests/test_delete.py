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
            self.bot,
            self.owner,
            bot_user_id=BOT,
            owner_user_id=OWNER,
            state=self.state,
            release=self.release,
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


async def test_threads_are_deleted_one_at_a_time(world: World, tmp_path: Path) -> None:
    import asyncio

    other = "1790000100.000001"
    world.state.open_thread(CHANNEL, other, session_id=SESSION)
    world.bot.responses["conversations.replies"] = lambda args: {
        "ok": True,
        "messages": [message(str(args["ts"]), OWNER)],
        "has_more": False,
    }
    world.owner.gate = asyncio.Event()
    world.owner.gate_method = "chat.delete"
    first = asyncio.create_task(world.deleter.delete(CHANNEL, ROOT))
    await world.owner.gated.wait()  # the first delete is inside its call to Slack
    second = asyncio.create_task(world.deleter.delete(CHANNEL, other))
    again = asyncio.create_task(world.deleter.delete(CHANNEL, ROOT))  # the same thread, twice
    await asyncio.sleep(0.05)
    assert world.released == [(CHANNEL, ROOT)]  # the others have not started
    world.owner.gate.set()
    assert await asyncio.gather(first, second, again) == [None, None, None]
    assert world.deleted(world.owner) == [ROOT, other]  # the repeated one found nothing left


def loose(ts: str, user: str, text: str, **fields: Any) -> dict[str, Any]:
    """A channel's message as `conversations.history` returns it: the message object of
    006-event_callback-message.json, and for a thread's parent the `thread_ts` and
    `reply_count` of api-conversations-replies-root.json (a threaded message is detected "by
    looking for a `thread_ts` value", docs.slack.dev/messaging/retrieving-messages, read
    2026-10-05)."""
    found = {"type": "message", "ts": ts, "user": user, "text": text, **fields}
    return {**found, "bot_id": "B000BOT"} if user == BOT else found


async def test_a_clean_up_deletes_what_has_no_reply_outside_a_thread(world: World) -> None:
    waiting = "1790000050.000001"
    emptied = "1790000013.000001"
    orphan = "1790000014.000001"  # a thread with replies whose session is gone
    world.state.open_thread(CHANNEL, waiting)  # a thread of the daemon's, no reply yet
    pages = (
        [
            loose("1790000010.000001", OWNER, "!stop"),
            loose("1790000011.000001", BOT, "Stopped what was running in this channel."),
            loose("1790000012.000001", OWNER, "a prompt nobody answered"),
            loose(ROOT, OWNER, "fix the footer", thread_ts=ROOT, reply_count=19),
            # A parent keeps `thread_ts` once every reply is deleted.
            loose(emptied, OWNER, "a prompt whose replies are gone", thread_ts=emptied),
        ],
        [
            loose(orphan, OWNER, "an old thread", thread_ts=orphan, reply_count=4),
            loose("1790000015.000001", "U000BOB", "!stop"),
            loose("1790000016.000001", BOT, "joined", subtype="channel_join"),
            loose(waiting, OWNER, "a prompt whose setup is open"),
            # A reply also sent to the channel belongs to its thread.
            loose("1790000017.000001", BOT, "done", thread_ts=ROOT, subtype="thread_broadcast"),
        ],
    )
    world.bot.responses["conversations.history"] = paged(*pages)
    # Asked of the thread itself, a root with a reply carries `reply_count` and one with none
    # does not (api-conversations-replies-root.json, and the Home's measure of 2026-10-01).
    roots = {
        orphan: loose(orphan, OWNER, "an old thread", thread_ts=orphan, reply_count=4),
        emptied: loose(emptied, OWNER, "a prompt whose replies are gone", thread_ts=emptied),
    }
    world.bot.responses["conversations.replies"] = lambda args: {
        "ok": True,
        "messages": [roots[str(args["ts"])]],
        "has_more": False,
    }
    assert await world.deleter.clean(CHANNEL) is None
    asked = world.bot.calls_to("conversations.history")
    assert [(a["channel"], a["limit"], a.get("cursor")) for a in asked] == [
        (CHANNEL, PAGE, None),
        (CHANNEL, PAGE, "1"),
    ]
    # Only a message that carries `thread_ts` and that `state.json` does not hold is asked
    # about, its root alone.
    checked = world.bot.calls_to("conversations.replies")
    assert [(a["ts"], a["limit"]) for a in checked] == [(emptied, 1), (orphan, 1)]
    assert world.deleted(world.owner) == ["1790000010.000001", "1790000012.000001", emptied]
    assert world.deleted(world.bot) == ["1790000011.000001"]
    assert world.state.thread(CHANNEL, ROOT) is not None
    assert world.released == []


@pytest.mark.parametrize(
    "answer",
    [
        {"ok": True, "messages": [], "has_more": False},  # no root in the answer: no proof
        slack_error("thread_not_found"),  # gone meanwhile
    ],
)
async def test_a_message_with_thread_ts_is_kept_unless_its_thread_is_known_empty(
    world: World, answer: Any
) -> None:
    world.state.remove_thread(CHANNEL, ROOT)
    world.bot.responses["conversations.history"] = paged(
        [loose(ROOT, OWNER, "fix the footer", thread_ts=ROOT)]
    )
    world.bot.responses["conversations.replies"] = answer
    assert await world.deleter.clean(CHANNEL) is None
    assert world.deleted(world.owner) == [] and world.deleted(world.bot) == []


async def test_a_thread_that_cannot_be_asked_about_stops_the_clean_up(world: World) -> None:
    world.state.remove_thread(CHANNEL, ROOT)
    world.bot.responses["conversations.history"] = paged(
        [loose(ROOT, OWNER, "fix the footer", thread_ts=ROOT)]
    )
    world.bot.responses["conversations.replies"] = slack_error("ratelimited")
    assert await world.deleter.clean(CHANNEL) == texts.HOME_CLEAN_FAILED.format(error="ratelimited")
    assert world.deleted(world.owner) == []


async def test_a_clean_up_of_a_channel_that_is_not_bound_reads_nothing(world: World) -> None:
    assert await world.deleter.clean("C000NOPE") is None
    assert world.bot.calls == [] and world.owner.calls == []


async def test_a_clean_up_that_slack_stops_says_so(world: World) -> None:
    world.bot.responses["conversations.history"] = paged(
        [loose("1790000010.000001", OWNER, "!stop"), loose("1790000011.000001", BOT, "Stopped.")]
    )
    world.owner.responses["chat.delete"] = slack_error("cant_delete_message")
    notice = await world.deleter.clean(CHANNEL)
    assert notice == texts.HOME_CLEAN_FAILED.format(error="cant_delete_message")
    assert world.deleted(world.bot) == []  # it stopped at the first refusal
