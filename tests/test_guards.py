import copy
from typing import Any

import pytest

from code_with_slack import texts
from code_with_slack.guards import (
    ChannelGuard,
    Identity,
    interaction_actor,
    is_owner,
    is_prompt_message,
    message_actor,
)
from tests.fakes import (
    BOT,
    CHANNEL,
    FIXTURES,
    OTHER_TEAM,
    OWNER,
    STRANGER,
    TEAM,
    FakeSlack,
    slack_payload,
)

IDENTITY = Identity(owner_user_id=OWNER, team_id=TEAM, bot_user_id=BOT)


def recorded(kind: str) -> dict[str, Any]:
    name = next(p.stem for p in sorted((FIXTURES / "slack").glob(f"*-{kind}.json")))
    return slack_payload(name)


@pytest.mark.parametrize(
    ("user", "team", "allowed"),
    [
        (OWNER, TEAM, True),
        (STRANGER, TEAM, False),
        (OWNER, OTHER_TEAM, False),
        (None, TEAM, False),
        (OWNER, None, False),
        ("", "", False),
    ],
)
def test_is_owner_checks_user_and_team_separately(
    user: str | None, team: str | None, allowed: bool
) -> None:
    assert is_owner(IDENTITY, user, team) is allowed


def test_actors_read_the_recorded_payloads() -> None:
    assert message_actor(recorded("event_callback-message")["event"]) == (OWNER, TEAM)
    assert interaction_actor(recorded("block_actions")) == (OWNER, TEAM)


def test_interaction_from_a_user_of_another_home_team_is_not_the_owner() -> None:
    body = copy.deepcopy(recorded("block_actions"))
    body["user"]["team_id"] = OTHER_TEAM
    assert interaction_actor(body) == (OWNER, None)


def test_only_plain_human_messages_are_prompts() -> None:
    message = recorded("event_callback-message")["event"]
    assert is_prompt_message(message)
    assert not is_prompt_message(recorded("event_callback-message_changed")["event"])
    assert not is_prompt_message({**message, "bot_id": "B000BOT"})
    assert not is_prompt_message({**message, "subtype": "message_deleted"})
    assert not is_prompt_message({**message, "text": ""})


def channel_info(**flags: Any) -> dict[str, Any]:
    info = copy.deepcopy(slack_payload("api-conversations-info"))
    info["channel"].update(flags)
    return info


async def test_private_channel_with_owner_and_bot_is_allowed(slack: FakeSlack) -> None:
    assert await ChannelGuard(slack, IDENTITY).refusal(CHANNEL) is None


@pytest.mark.parametrize(
    ("flags", "reason"),
    [
        ({"is_private": False}, texts.REASON_NOT_PRIVATE),
        ({"is_ext_shared": True}, texts.REASON_SHARED),
        ({"is_shared": True}, texts.REASON_SHARED),
        ({"is_org_shared": True}, texts.REASON_SHARED),
        ({"is_mpim": True}, texts.REASON_NOT_PRIVATE),
        ({"is_im": True}, texts.REASON_NOT_PRIVATE),
    ],
)
async def test_channel_flags_are_refused(
    slack: FakeSlack, flags: dict[str, Any], reason: str
) -> None:
    slack.responses["conversations.info"] = channel_info(**flags)
    assert await ChannelGuard(slack, IDENTITY).refusal(CHANNEL) == reason


async def test_a_third_member_is_refused(slack: FakeSlack) -> None:
    slack.responses["conversations.members"] = {"ok": True, "members": [OWNER, BOT, STRANGER]}
    assert await ChannelGuard(slack, IDENTITY).refusal(CHANNEL) == texts.REASON_MEMBERS


async def test_a_missing_owner_is_refused(slack: FakeSlack) -> None:
    slack.responses["conversations.members"] = {"ok": True, "members": [BOT]}
    assert await ChannelGuard(slack, IDENTITY).refusal(CHANNEL) == texts.REASON_MEMBERS


async def test_a_paginated_member_list_is_refused(slack: FakeSlack) -> None:
    slack.responses["conversations.members"] = {
        "ok": True,
        "members": [OWNER, BOT],
        "response_metadata": {"next_cursor": "abc"},
    }
    assert await ChannelGuard(slack, IDENTITY).refusal(CHANNEL) == texts.REASON_MEMBERS


async def test_an_unreadable_channel_is_refused(slack: FakeSlack) -> None:
    slack.responses["conversations.info"] = {"ok": False, "error": "channel_not_found"}
    assert await ChannelGuard(slack, IDENTITY).refusal(CHANNEL) == texts.REASON_UNREADABLE


async def test_the_guard_asks_slack_every_time(slack: FakeSlack) -> None:
    guard = ChannelGuard(slack, IDENTITY)
    await guard.refusal(CHANNEL)
    slack.responses["conversations.members"] = {"ok": True, "members": [OWNER, BOT, STRANGER]}
    assert await guard.refusal(CHANNEL) == texts.REASON_MEMBERS
