import asyncio
import copy
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from slack_bolt.request.async_request import AsyncBoltRequest

from code_with_slack import texts
from code_with_slack.approvals import Approvals
from code_with_slack.config import Config
from code_with_slack.footer import UsageCache
from code_with_slack.guards import ChannelGuard, Identity
from code_with_slack.sessions import SessionDeps, SessionManager
from code_with_slack.slack_app import build_app, slack_unescape
from code_with_slack.state import StateStore
from tests.fakes import (
    BOT,
    CHANNEL,
    FIXTURES,
    OTHER_TEAM,
    OWNER,
    STRANGER,
    TEAM,
    FakeClaudeClient,
    FakeSlack,
    slack_payload,
)


def recorded(kind: str) -> dict[str, Any]:
    name = next(p.stem for p in sorted((FIXTURES / "slack").glob(f"*-{kind}.json")))
    return copy.deepcopy(slack_payload(name))


class World:
    def __init__(self, slack: FakeSlack, tmp_path: Path, *, bound: bool = True) -> None:
        self.slack = slack
        self.root = tmp_path / "root"
        (self.root / "app").mkdir(parents=True)
        self.state = StateStore(tmp_path / "state.json")
        if bound:
            self.state.bind(CHANNEL, self.root / "app")
        self.clients: list[FakeClaudeClient] = []
        self.approvals = Approvals()
        identity = Identity(OWNER, TEAM, BOT)

        async def no_usage() -> str:
            return ""

        def factory(options: ClaudeAgentOptions) -> FakeClaudeClient:
            client = FakeClaudeClient(options)
            self.clients.append(client)
            return client

        self.sessions = SessionManager(
            SessionDeps(
                slack=slack,
                identity=identity,
                state=self.state,
                approvals=self.approvals,
                usage=UsageCache(no_usage),
                client_factory=factory,
            )
        )
        config = Config(
            bot_token="xox" + "b-fake",
            app_token="xap" + "p-fake",
            owner_user_id=OWNER,
            allowed_root=self.root.resolve(),
            config_dir=tmp_path,
        )
        self.app = build_app(
            slack=slack,
            config=config,
            identity=identity,
            sessions=self.sessions,
            approvals=self.approvals,
            guard=ChannelGuard(slack, identity),
        )

    async def dispatch(self, body: dict[str, Any]) -> Any:
        response = await self.app.async_dispatch(AsyncBoltRequest(body=body, mode="socket_mode"))
        await asyncio.sleep(0.05)  # let the listener tasks run
        return response

    def queries(self) -> list[str]:
        return [q for c in self.clients for q in c.queries]

    def ephemerals(self) -> list[str]:
        return [a["text"] for a in self.slack.calls_to("chat.postEphemeral")]

    def posted_anything(self) -> bool:
        return any(m.startswith("chat.") for m, _ in self.slack.calls)


@pytest.fixture
async def world(slack: FakeSlack, tmp_path: Path) -> AsyncIterator[World]:
    made = World(slack, tmp_path)
    yield made
    await made.sessions.close_all()


def message(text: str = "hello", **event: Any) -> dict[str, Any]:
    body = recorded("event_callback-message")
    body["event"].update({"text": text, **event})
    return body


async def test_owner_message_becomes_a_prompt(world: World) -> None:
    body = message("list the files")
    await world.dispatch(body)
    assert world.queries() == ["list the files"]


async def test_a_message_in_a_thread_is_a_prompt_too(world: World) -> None:
    thread_bodies = [
        p.stem for p in sorted((FIXTURES / "slack").glob("*-event_callback-message.json"))
    ]
    replies = [slack_payload(n) for n in thread_bodies if "thread_ts" in slack_payload(n)["event"]]
    assert replies, "the recording holds a thread reply"
    await world.dispatch(copy.deepcopy(replies[0]))
    assert world.queries() == [replies[0]["event"]["text"]]


@pytest.mark.parametrize(("user", "team"), [(STRANGER, TEAM), (OWNER, OTHER_TEAM)])
async def test_a_message_from_anyone_else_does_nothing(world: World, user: str, team: str) -> None:
    await world.dispatch(message("rm -rf /", user=user, team=team))
    assert world.queries() == [] and not world.posted_anything()


async def test_edits_do_nothing(world: World) -> None:
    await world.dispatch(recorded("event_callback-message_changed"))
    assert world.queries() == []


async def test_a_refused_channel_tells_only_the_owner(world: World) -> None:
    world.slack.responses["conversations.members"] = {"ok": True, "members": [OWNER, BOT, STRANGER]}
    await world.dispatch(message())
    assert world.queries() == []
    assert world.ephemerals() == [texts.CHANNEL_REFUSED.format(reason=texts.REASON_MEMBERS)]
    assert world.slack.calls_to("chat.postEphemeral")[0]["user"] == OWNER


async def test_an_unbound_channel_explains_how_to_bind(slack: FakeSlack, tmp_path: Path) -> None:
    world = World(slack, tmp_path, bound=False)
    await world.dispatch(message())
    assert world.ephemerals() == [texts.UNBOUND]


async def test_bang_runs_a_known_command(world: World) -> None:
    await world.dispatch(message("!compact"))
    assert world.queries() == ["/compact"]


async def test_bang_leaves_other_text_alone(world: World) -> None:
    await world.dispatch(message("!important: read the notes"))
    assert world.queries() == ["!important: read the notes"]


async def test_slack_escapes_are_undone() -> None:
    assert slack_unescape("a &lt;b&gt; &amp;&amp; c") == "a <b> && c"


def said(world: World) -> list[str]:
    """What the bot posted in the channel (not ephemeral), in order."""
    return [a["text"] for a in world.slack.calls_to("chat.postMessage")]


async def test_bang_bind_inside_and_outside_the_root(world: World) -> None:
    await world.dispatch(message(f"!bind {world.root / 'app'}"))
    assert world.state.get(CHANNEL).directory == (world.root / "app").resolve()
    await world.dispatch(message("!bind /"))
    assert said(world)[-1].startswith("`/` is not a directory under")


async def test_bang_bind_works_in_an_unbound_channel(slack: FakeSlack, tmp_path: Path) -> None:
    world = World(slack, tmp_path, bound=False)
    await world.dispatch(message(f"!bind {world.root / 'app'}"))
    assert world.state.get(CHANNEL).directory == (world.root / "app").resolve()
    assert world.queries() == []
    await world.sessions.close_all()


async def test_bang_help_lists_the_session_commands(world: World) -> None:
    await world.dispatch(message("!help"))
    (text,) = said(world)
    assert "`!compact" in text and "`!bypass" in text
    assert world.queries() == []


async def test_bang_help_with_a_filter_lists_only_matches(world: World) -> None:
    await world.dispatch(message("!help compact"))
    (text,) = said(world)
    assert "`!compact" in text and "`!bypass" not in text


async def test_bang_help_works_in_an_unbound_channel(slack: FakeSlack, tmp_path: Path) -> None:
    world = World(slack, tmp_path, bound=False)
    await world.dispatch(message("!help"))
    (text,) = said(world)
    assert "`!bind" in text
    assert world.clients == []


async def test_bang_bypass_on_switches_the_live_client(world: World) -> None:
    await world.dispatch(message("!bypass on"))
    assert world.clients[0].modes == ["bypassPermissions"]
    assert said(world) == [texts.BYPASS_ON]


async def test_bang_status_and_stop_answer_in_the_channel(world: World) -> None:
    await world.dispatch(message("!stop"))
    await world.dispatch(message("!status"))
    assert said(world)[0] == texts.NOTHING_TO_STOP
    assert said(world)[1].startswith("Directory:")
    assert world.queries() == []


async def test_a_malformed_daemon_word_shows_the_help(world: World) -> None:
    await world.dispatch(message("!bypass maybe"))
    text = said(world)[-1]
    assert "`!bypass" in text and "`!compact" in text
    assert texts.HELP_UNBOUND not in text
    assert world.queries() == []


async def test_bang_from_anyone_else_does_nothing(world: World) -> None:
    await world.dispatch(message("!bypass on", user=STRANGER))
    assert world.clients == [] and not world.posted_anything()


def click(action_id: str, value: str, **user: Any) -> dict[str, Any]:
    body = recorded("block_actions")
    body["actions"] = [{**body["actions"][0], "action_id": action_id, "value": value}]
    body["user"].update(user)
    return body


async def open_request(world: World) -> tuple[str, Any]:
    approval_id, pending = world.approvals.open(CHANNEL, "Bash: ls")
    return approval_id, pending


async def test_the_owner_approves(world: World) -> None:
    approval_id, pending = await open_request(world)
    body = click("approval_allow", approval_id)
    await world.dispatch(body)
    assert pending.future.done()
    # The tool's card in the reply records the call: the request message goes away.
    deleted = [(a["channel"], a["ts"]) for a in world.slack.calls_to("chat.delete")]
    assert deleted == [(CHANNEL, body["message"]["ts"])]
    assert not world.slack.calls_to("chat.update")


@pytest.mark.parametrize("user", [{"id": STRANGER}, {"team_id": OTHER_TEAM}])
async def test_nobody_else_can_approve(world: World, user: dict[str, str]) -> None:
    approval_id, pending = await open_request(world)
    await world.dispatch(click("approval_allow", approval_id, **user))
    assert not pending.future.done() and not world.posted_anything()


async def test_stale_approval_click_gets_a_note(world: World) -> None:
    await world.dispatch(click("approval_allow", "no-such-request"))
    assert world.ephemerals() == [texts.APPROVAL_GONE]


async def test_a_request_from_another_channel_cannot_be_answered_here(world: World) -> None:
    approval_id, pending = world.approvals.open("C000ELSEWHERE", "Bash: ls")
    await world.dispatch(click("approval_allow", approval_id))
    assert not pending.future.done()
    assert world.ephemerals() == [texts.APPROVAL_GONE]


async def test_an_incomplete_answer_is_refused(world: World) -> None:
    approval_id, pending = world.approvals.open(
        CHANNEL,
        "Colour",
        [{"question": "Colour?", "options": [{"label": "red"}], "multiSelect": False}],
    )
    body = click("question_submit", approval_id)
    body["state"] = {"values": {}}
    await world.dispatch(body)
    assert not pending.future.done()
    assert world.ephemerals() == [texts.QUESTION_INCOMPLETE]


async def test_a_failing_command_tells_the_owner(world: World) -> None:
    (world.root / "app").rmdir()
    await world.dispatch(message("!bypass on"))
    assert world.ephemerals() == [texts.DIRECTORY_MISSING.format(directory=world.root / "app")]
