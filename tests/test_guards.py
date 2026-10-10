import copy
from typing import Any

import pytest

from awaydesk import texts
from awaydesk.guards import (
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
    plain = recorded("event_callback-message")
    assert message_actor(plain["event"], plain) == (OWNER, TEAM)
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
    assert not is_prompt_message({**message, "subtype": None})
    assert not is_prompt_message({**message, "text": ""})


def test_a_message_with_a_file_is_a_prompt_even_with_no_text() -> None:
    # Measured 2026-09-25: a file arrives as subtype file_share, although Slack's reference calls
    # that subtype legacy.
    shared = recorded("event_callback-file_share-image")["event"]
    assert is_prompt_message(shared)
    assert is_prompt_message({**shared, "text": ""})
    assert not is_prompt_message({**shared, "bot_id": "B000BOT"})


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


async def test_a_network_failure_reading_the_channel_is_a_refusal(slack: FakeSlack) -> None:
    import aiohttp

    slack.responses["conversations.info"] = aiohttp.ClientConnectionError("network down")
    assert await ChannelGuard(slack, IDENTITY).refusal(CHANNEL) == texts.REASON_UNREADABLE


def test_a_file_share_names_its_workspace_in_the_file() -> None:
    # Measured 2026-09-25: a file_share event has no `team`; each file carries `user_team`.
    body = recorded("event_callback-file_share-image")
    shared = body["event"]
    assert "team" not in shared
    assert message_actor(shared, body) == (OWNER, TEAM)
    other = {**shared["files"][0], "user_team": OTHER_TEAM}
    assert message_actor({**shared, "files": [*shared["files"], other]}, body) == (OWNER, None)


def test_a_reply_also_sent_to_the_channel_is_a_prompt() -> None:
    # Recorded 2026-10-09: a thread reply sent with "Also send to #channel" arrives as subtype
    # thread_broadcast, followed by a hidden message_changed that wraps the same reply.
    event = recorded("event_callback-thread_broadcast")["event"]
    assert event["subtype"] == "thread_broadcast"
    assert is_prompt_message(event)
    assert not is_prompt_message({**event, "bot_id": "B000BOT"})
    followed = recorded("event_callback-message_changed-thread_broadcast")["event"]
    assert followed["message"]["subtype"] == "thread_broadcast"
    assert not is_prompt_message(followed)


def test_a_reply_also_sent_to_the_channel_takes_its_workspace_from_the_envelope() -> None:
    # Recorded 2026-10-09: the event has no `team` (its `root` names the root author's); the
    # envelope's `team_id` is where the event happened.
    body = recorded("event_callback-thread_broadcast")
    event = body["event"]
    assert "team" not in event
    assert message_actor(event, body) == (OWNER, TEAM)
    assert message_actor(event, {**body, "team_id": OTHER_TEAM}) == (OWNER, OTHER_TEAM)
    # A team the event names is never replaced by the envelope's.
    assert message_actor({**event, "team": OTHER_TEAM}, body) == (OWNER, OTHER_TEAM)


def test_the_envelope_stands_in_only_in_a_channel_not_shared_outside() -> None:
    body = recorded("event_callback-thread_broadcast")
    event = body["event"]
    assert body["is_ext_shared_channel"] is False
    assert message_actor(event, {**body, "is_ext_shared_channel": True}) == (OWNER, None)
    unsaid = {key: value for key, value in body.items() if key != "is_ext_shared_channel"}
    assert message_actor(event, unsaid) == (OWNER, None)
    # Only the JSON `false` counts: nothing that merely reads as false.
    for unclear in (None, 0, "false"):
        assert message_actor(event, {**body, "is_ext_shared_channel": unclear}) == (OWNER, None)


def test_a_reply_also_sent_to_the_channel_with_files_follows_the_files() -> None:
    # Not recorded: whether Slack sends a broadcast reply with a file as thread_broadcast. If it
    # does, the files decide, as for any message that carries them, and the envelope never does.
    body = recorded("event_callback-thread_broadcast")
    files = recorded("event_callback-file_share-image")["event"]["files"]
    event = {**body["event"], "files": files}
    assert message_actor(event, body) == (OWNER, TEAM)
    mixed = [*files, {**files[0], "user_team": OTHER_TEAM}]
    assert message_actor({**event, "files": mixed}, body) == (OWNER, None)
    unnamed = [{key: value for key, value in files[0].items() if key != "user_team"}]
    assert message_actor({**event, "files": unnamed}, body) == (OWNER, None)


def test_the_envelope_stands_in_for_no_other_kind_of_message() -> None:
    body = recorded("event_callback-thread_broadcast")
    plain = recorded("event_callback-message")["event"]
    teamless = {key: value for key, value in plain.items() if key != "team"}
    assert message_actor(teamless, body) == (OWNER, None)
    assert message_actor({**teamless, "subtype": "file_share"}, body) == (OWNER, None)
