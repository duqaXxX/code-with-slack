import asyncio
import copy
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, SDKSessionInfo
from slack_bolt.request.async_request import AsyncBoltRequest

from code_with_slack import texts
from code_with_slack.approvals import Answer, Approvals, Draft
from code_with_slack.attachments import DownloadFailed
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


async def always_trusted(directory: Path) -> bool:
    return True  # code_with_slack.trust has tests of its own


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
        # What list_sessions returns for the channel's directory (SDK SDKSessionInfo, newest first).
        self.stored_sessions: list[SDKSessionInfo] = []
        # What each file URL downloads to: bytes, or the failure the download raises.
        self.downloads: dict[str, bytes | Exception] = {}
        self.uploads = tmp_path / "uploads"
        self.fetched: list[str] = []
        self.slow_downloads = 0.0
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
                workspace_trusted=always_trusted,
                sessions_of=lambda directory: self.stored_sessions,
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
            fetch=self.fetch,
            uploads=self.uploads,
        )

    async def fetch(self, *, url: str, mimetype: str, limit: int) -> bytes:
        self.fetched.append(url)
        await asyncio.sleep(self.slow_downloads)
        found = self.downloads[url]
        if isinstance(found, Exception):
            raise found
        return found

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
    assert world.ephemerals() == [texts.UNBOUND.format(root=world.root.resolve())]


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
    assert said(world)[-1] == texts.BIND_OUTSIDE.format(path="/", root=world.root.resolve())
    await world.dispatch(message("!bind app"))  # relative to the allowed root, as the text says
    assert world.state.get(CHANNEL).directory == (world.root / "app").resolve()


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
    # The tool's line in the reply records the call: the request message goes away.
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


QUESTIONS = [
    {
        "question": "Colour?",
        "header": "Colour",
        "options": [{"label": "red"}, {"label": "blue"}],
        "multiSelect": False,
    },
    {
        "question": "Sizes?",
        "header": "Sizes",
        "options": [{"label": "s"}, {"label": "l"}],
        "multiSelect": True,
    },
]


def form_body(kind: str, draft: Draft, values: dict[str, Any], **user: Any) -> dict[str, Any]:
    """The form's recorded Submit (view_submission, 2026-09-24), carrying this draft and state."""
    body = recorded("submit")
    body["type"] = kind
    body["user"].update(user)
    body["view"]["private_metadata"] = draft.dump()
    body["view"]["state"] = {"values": values}
    return body


def picked(index: int, value: str) -> dict[str, Any]:
    return {f"q{index}": {"answer": {"type": "radio_buttons", "selected_option": {"value": value}}}}


async def test_answer_opens_the_form(world: World) -> None:
    approval_id, _ = world.approvals.open(CHANNEL, "Colour", QUESTIONS)
    body = recorded("open-click")  # the Answer click, recorded 2026-09-24
    body["actions"][0]["value"] = approval_id
    body["trigger_id"] = "0000000000.0000000000.fake"  # real clicks carry one; the scrub drops it
    await world.dispatch(body)
    (opened,) = world.slack.calls_to("views.open")
    assert opened["trigger_id"] == body["trigger_id"]
    view = json.loads(opened["view"]) if isinstance(opened["view"], str) else opened["view"]
    assert view["callback_id"] == "question_form"
    assert Draft.load(view["private_metadata"]) == Draft(approval_id, CHANNEL)


async def test_nobody_else_can_open_the_form(world: World) -> None:
    approval_id, _ = world.approvals.open(CHANNEL, "Colour", QUESTIONS)
    await world.dispatch(click("question_open", approval_id, id=STRANGER))
    assert not world.slack.calls_to("views.open") and not world.posted_anything()


async def test_submit_with_the_open_question_unanswered_shows_an_error(world: World) -> None:
    approval_id, pending = world.approvals.open(CHANNEL, "Colour", QUESTIONS)
    response = await world.dispatch(form_body("view_submission", Draft(approval_id, CHANNEL), {}))
    assert json.loads(response.body) == {
        "response_action": "errors",
        "errors": {"q0": texts.QUESTION_MISSING},
    }
    assert not pending.future.done()


async def test_next_with_an_answer_moves_to_the_next_question(world: World) -> None:
    approval_id, pending = world.approvals.open(CHANNEL, "Colour", QUESTIONS)
    response = await world.dispatch(
        form_body("view_submission", Draft(approval_id, CHANNEL), picked(0, "0"))
    )
    answer = json.loads(response.body)
    assert answer["response_action"] == "update"
    assert Draft.load(answer["view"]["private_metadata"]) == Draft(
        approval_id, CHANNEL, 1, {0: [0]}
    )
    assert not pending.future.done()


async def test_a_complete_submit_answers_claude_and_removes_the_request(world: World) -> None:
    approval_id, pending = world.approvals.open(CHANNEL, "Colour", QUESTIONS)
    pending.message_ts = "1790000000.000009"
    draft = Draft(approval_id, CHANNEL, active=1, picks={0: [1]})
    values = {
        "q1": {"answer": {"type": "checkboxes", "selected_options": [{"value": "0"}]}},
        "o1": {"other": {"type": "plain_text_input", "value": "xl"}},
    }
    await world.dispatch(form_body("view_submission", draft, values))
    assert pending.future.result() == Answer({"Colour?": "blue", "Sizes?": ["s", "xl"]})
    assert [a["ts"] for a in world.slack.calls_to("chat.delete")] == ["1790000000.000009"]


async def test_nobody_else_can_submit_the_form(world: World) -> None:
    approval_id, pending = world.approvals.open(CHANNEL, "Colour", QUESTIONS)
    draft = Draft(approval_id, CHANNEL, picks={0: [0], 1: [0]})
    await world.dispatch(form_body("view_submission", draft, {}, id=STRANGER))
    assert not pending.future.done()


async def test_a_failing_command_tells_the_owner(world: World) -> None:
    (world.root / "app").rmdir()
    await world.dispatch(message("!bypass on"))
    assert world.ephemerals() == [texts.DIRECTORY_MISSING.format(directory=world.root / "app")]


async def test_a_form_that_cannot_open_tells_the_owner(world: World) -> None:
    approval_id, _ = world.approvals.open(CHANNEL, "Colour", QUESTIONS)
    world.slack.responses["views.open"] = {"ok": False, "error": "expired_trigger_id"}
    body = recorded("open-click")
    body["actions"][0]["value"] = approval_id
    body["trigger_id"] = "0000000000.0000000000.fake"
    await world.dispatch(body)
    assert world.ephemerals() == [texts.QUESTION_NOT_OPENED.format(error="expired_trigger_id")]


async def test_a_submit_that_needs_more_answers_makes_no_slack_call_first(world: World) -> None:
    approval_id, _ = world.approvals.open(CHANNEL, "Colour", QUESTIONS)
    await world.dispatch(form_body("view_submission", Draft(approval_id, CHANNEL), {}))
    assert not [m for m, _ in world.slack.calls if m.startswith("conversations.")]


def test_slack_links_reach_claude_as_typed() -> None:
    # Message formatting reference (read 2026-09-25): Slack sends a link as <url|label> or <url>.
    assert slack_unescape("open <http://main.py|main.py> now") == "open main.py now"
    assert slack_unescape("see <https://example.com/a?b=1&amp;c=2>") == (
        "see https://example.com/a?b=1&c=2"
    )
    assert slack_unescape("mail <mailto:bob@example.com|bob@example.com>") == "mail bob@example.com"
    assert slack_unescape("hi <@U000BOB>") == "hi <@U000BOB>"  # a mention stays as Slack sent it
    # A link the owner named keeps its address: Claude could not open the label alone.
    assert slack_unescape("read <https://example.com/x|the docs>") == (
        "read the docs (https://example.com/x)"
    )


SESSION_A = "68da9311-0000-4000-8000-00000000000a"
SESSION_B = "68da9311-0000-4000-8000-00000000000b"


def two_sessions(world: World) -> None:
    now = int(time.time() * 1000)
    world.stored_sessions = [
        # A titled session: its summary is the title (SDKSessionInfo, SDK 0.2.158).
        SDKSessionInfo(SESSION_A, "footer", now - 7_200_000, 412_000, "footer", git_branch="main"),
        # No title: the summary is the first prompt, here long and on several lines.
        SDKSessionInfo(
            SESSION_B, "Trust gate\n" + "why " * 60, now - 90_000_000, 1_100_000, git_branch="main"
        ),
    ]


async def test_bang_resume_lists_the_directory_s_sessions_with_buttons(world: World) -> None:
    two_sessions(world)
    await world.dispatch(message("!resume"))
    (post,) = world.slack.calls_to("chat.postMessage")
    values = [b["accessory"]["value"] for b in post["blocks"] if "accessory" in b]
    assert values == [SESSION_A, SESSION_B]
    assert post["unfurl_links"] is False and post["unfurl_media"] is False
    assert world.clients == []  # listing starts no Claude Code process


async def test_bang_resume_by_name_or_id_points_the_channel_at_it(world: World) -> None:
    two_sessions(world)
    await world.dispatch(message("!resume footer"))
    assert world.state.get(CHANNEL).session_id == SESSION_A
    await world.dispatch(message(f"!resume {SESSION_B}"))
    assert world.state.get(CHANNEL).session_id == SESSION_B
    confirmation = said(world)[-1]
    assert "Trust gate" in confirmation and "\n" not in confirmation.split("**")[1]


async def test_bang_resume_of_an_unknown_session_says_so(world: World) -> None:
    two_sessions(world)
    await world.dispatch(message("!resume nothing-like-it"))
    assert "`nothing-like-it`" in said(world)[-1]
    assert world.state.get(CHANNEL).session_id is None


async def test_the_owner_resumes_from_the_list(world: World) -> None:
    two_sessions(world)
    body = click("session_resume", SESSION_B)
    await world.dispatch(body)
    assert world.state.get(CHANNEL).session_id == SESSION_B
    deleted = [(a["channel"], a["ts"]) for a in world.slack.calls_to("chat.delete")]
    assert deleted == [(CHANNEL, body["message"]["ts"])]


@pytest.mark.parametrize("user", [{"id": STRANGER}, {"team_id": OTHER_TEAM}])
async def test_nobody_else_can_resume(world: World, user: dict[str, str]) -> None:
    two_sessions(world)
    await world.dispatch(click("session_resume", SESSION_B, **user))
    assert world.state.get(CHANNEL).session_id is None and not world.posted_anything()


async def test_a_button_is_never_trusted_for_a_session_of_another_directory(world: World) -> None:
    two_sessions(world)
    await world.dispatch(click("session_resume", "68da9311-0000-4000-8000-0000000000ff"))
    assert world.state.get(CHANNEL).session_id is None
    assert world.ephemerals() == [texts.RESUME_GONE]


async def test_resume_while_a_turn_runs_is_refused_and_the_list_stays(world: World) -> None:
    two_sessions(world)
    await world.dispatch(message("hello"))  # the fake Claude Code never ends this turn
    await world.dispatch(click("session_resume", SESSION_B))
    assert world.state.get(CHANNEL).session_id != SESSION_B
    assert said(world)[-1] == texts.RESUME_BUSY
    assert world.slack.calls_to("chat.delete") == []


async def test_a_failing_resume_click_tells_the_owner(world: World) -> None:
    def broken(directory: Path) -> list[SDKSessionInfo]:
        raise PermissionError("transcripts unreadable")

    world.sessions._deps.sessions_of = broken
    await world.dispatch(click("session_resume", SESSION_B))
    assert world.ephemerals() == [texts.ERROR_REPLY.format(error="PermissionError")]


async def test_bang_guide_works_in_an_unbound_channel(slack: FakeSlack, tmp_path: Path) -> None:
    world = World(slack, tmp_path, bound=False)
    await world.dispatch(message("!guide"))
    assert said(world) == [texts.GUIDE] and world.clients == []


async def test_bang_bind_alone_lists_the_folders_with_buttons(
    slack: FakeSlack, tmp_path: Path
) -> None:
    world = World(slack, tmp_path, bound=False)
    (world.root / "docs").mkdir()
    await world.dispatch(message("!bind"))
    (post,) = world.slack.calls_to("chat.postMessage")
    values = [b["accessory"]["value"] for b in post["blocks"] if "accessory" in b]
    assert values == [".", "app", "docs"]  # the root first: the fake trusts every folder
    assert post["unfurl_links"] is False and post["unfurl_media"] is False
    assert world.clients == []  # listing starts no Claude Code process
    await world.sessions.close_all()


async def test_the_owner_binds_from_the_list(slack: FakeSlack, tmp_path: Path) -> None:
    world = World(slack, tmp_path, bound=False)
    body = click("folder_bind", "app")
    await world.dispatch(body)
    assert world.state.get(CHANNEL).directory == (world.root / "app").resolve()
    assert said(world) == [texts.BIND_OK.format(directory=(world.root / "app").resolve())]
    deleted = [(a["channel"], a["ts"]) for a in world.slack.calls_to("chat.delete")]
    assert deleted == [(CHANNEL, body["message"]["ts"])]
    await world.sessions.close_all()


@pytest.mark.parametrize("user", [{"id": STRANGER}, {"team_id": OTHER_TEAM}])
async def test_nobody_else_can_bind_from_the_list(
    slack: FakeSlack, tmp_path: Path, user: dict[str, str]
) -> None:
    world = World(slack, tmp_path, bound=False)
    await world.dispatch(click("folder_bind", "app", **user))
    assert world.state.get(CHANNEL) is None and not world.posted_anything()


async def test_a_bind_button_is_never_trusted_for_a_folder_outside_the_root(world: World) -> None:
    await world.dispatch(click("folder_bind", "../.."))
    assert world.state.get(CHANNEL).directory == (world.root / "app").resolve()
    assert said(world) == [texts.BIND_OUTSIDE.format(path="../..", root=world.root.resolve())]
    assert world.slack.calls_to("chat.delete") == []


async def test_no_trusted_folder_says_so_in_the_notification_too(
    slack: FakeSlack, tmp_path: Path
) -> None:
    world = World(slack, tmp_path, bound=False)

    async def never(directory: Path) -> bool:
        return False

    world.sessions._deps.workspace_trusted = never
    await world.dispatch(message("!bind"))
    (post,) = world.slack.calls_to("chat.postMessage")
    assert post["text"] == texts.BIND_EMPTY.format(root=world.root.resolve())
    await world.sessions.close_all()


async def test_a_bind_click_on_the_channel_s_own_folder_changes_nothing(world: World) -> None:
    world.state.set_session(CHANNEL, SESSION_A)
    await world.dispatch(click("folder_bind", "app"))
    assert world.state.get(CHANNEL).session_id == SESSION_A
    assert said(world) == [texts.BIND_ALREADY.format(directory=(world.root / "app").resolve())]


async def test_a_bind_click_while_a_turn_runs_is_refused_and_the_list_stays(world: World) -> None:
    (world.root / "docs").mkdir()
    await world.dispatch(message("hello"))  # the fake Claude Code never ends this turn
    await world.dispatch(click("folder_bind", "docs"))
    assert world.state.get(CHANNEL).directory == (world.root / "app").resolve()
    assert said(world)[-1] == texts.BIND_BUSY
    assert world.slack.calls_to("chat.delete") == []


async def test_only_the_list_reads_the_transcripts_for_dates(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Matching an id or a title, or checking a click, needs no dates: they cost a file read each.
    dated: list[Path] = []

    def spy(directory: Path, stored: list[SDKSessionInfo]) -> list[SDKSessionInfo]:
        dated.append(directory)
        return stored

    monkeypatch.setattr("code_with_slack.sessions.by_last_activity", spy)
    two_sessions(world)
    await world.dispatch(message("!resume footer"))
    await world.dispatch(click("session_resume", SESSION_B))
    assert dated == []
    await world.dispatch(message("!resume"))
    assert dated == [(world.root / "app").resolve()]


@pytest.mark.parametrize(
    ("title", "shown"),
    [
        # Measured live 2026-09-25: an unescaped lone `*` paired with the bold's own asterisks.
        ("Clean up *.pyc files", r"Clean up \*.pyc files"),
        ("a_b [x](y) `c` {d} & e\\f", r"a\_b \[x\]\(y\) \`c\` \{d\} \& e\\f"),
        # Measured live 2026-09-25: `~~old~~` rendered as strikethrough; `<x>` stayed text.
        ("fix ~~old~~ <example.com>", r"fix \~\~old\~\~ <example.com>"),
    ],
)
async def test_a_resumed_title_keeps_its_characters_inside_the_bold(
    world: World, title: str, shown: str
) -> None:
    world.stored_sessions = [SDKSessionInfo(SESSION_A, title, 0, 1, title)]
    await world.dispatch(message(f"!resume {SESSION_A}"))
    assert said(world) == [texts.RESUME_OK.format(title=shown)]


async def test_a_resumed_session_with_no_title_shows_its_id(world: World) -> None:
    world.stored_sessions = [SDKSessionInfo(SESSION_A, "", 0, 1)]
    await world.dispatch(message(f"!resume {SESSION_A}"))
    assert said(world) == [texts.RESUME_OK.format(title=SESSION_A)]


async def test_resuming_the_channel_s_own_session_changes_nothing(world: World) -> None:
    two_sessions(world)
    world.state.set_session(CHANNEL, SESSION_A)
    await world.dispatch(message("!help"))  # an idle, connected client, which a resume closes
    await world.dispatch(message("!resume footer"))
    assert said(world)[-1] == texts.RESUME_ALREADY
    assert world.clients[0].connected


async def test_a_resume_never_stores_a_session_of_a_folder_bound_meanwhile(world: World) -> None:
    two_sessions(world)
    (world.root / "docs").mkdir()
    listed = asyncio.Event()

    def slow(directory: Path) -> list[SDKSessionInfo]:
        listed.set()
        time.sleep(0.2)  # list_sessions reading the old folder's transcripts
        return world.stored_sessions

    world.sessions._deps.sessions_of = slow
    click_task = asyncio.create_task(world.dispatch(click("session_resume", SESSION_B)))
    await listed.wait()
    await world.dispatch(message("!bind docs"))
    await click_task
    await asyncio.sleep(0.3)
    stored = world.state.get(CHANNEL)
    assert stored.directory == (world.root / "docs").resolve() and stored.session_id is None
    assert texts.RESUME_GONE in world.ephemerals()


def shared_file(kind: str, **fields: Any) -> dict[str, Any]:
    """A recorded file_share message (Slack, 2026-09-25), its file changed by `fields`."""
    body = recorded(f"event_callback-file_share-{kind}")
    body["event"]["files"][0].update(fields)
    return body


async def test_an_image_reaches_claude_as_an_image_block(world: World) -> None:
    body = shared_file("image")
    world.downloads[body["event"]["files"][0]["url_private_download"]] = b"\x89PNG"
    await world.dispatch(body)
    ((message,),) = world.queries()
    content = message["message"]["content"]
    assert content[0] == {"type": "text", "text": body["event"]["text"]}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["media_type"] == "image/png"


async def test_a_file_reaches_claude_as_a_path_to_its_saved_copy(world: World) -> None:
    body = shared_file("snippet")
    world.downloads[body["event"]["files"][0]["url_private_download"]] = b"hello\n"
    await world.dispatch(body)
    (prompt,) = world.queries()
    saved = world.uploads / "F000FILE-notes.txt"
    assert saved.read_bytes() == b"hello\n"
    assert prompt == f"{body['event']['text']}\n\nAttached files:\n- {saved}"


async def test_a_refused_file_sends_nothing_and_says_why(world: World) -> None:
    await world.dispatch(shared_file("image", mimetype="image/heic"))
    reason = texts.UPLOAD_IMAGE_TYPE.format(mimetype="image/heic")
    assert said(world) == [texts.UPLOAD_FAILED.format(name="photo.png", reason=reason)]
    assert world.queries() == []


async def test_a_failed_download_sends_nothing_and_says_why(world: World) -> None:
    body = shared_file("image")
    world.downloads[body["event"]["files"][0]["url_private_download"]] = DownloadFailed("HTTP 404")
    await world.dispatch(body)
    reason = texts.UPLOAD_DOWNLOAD.format(error="HTTP 404")
    assert said(world) == [texts.UPLOAD_FAILED.format(name="photo.png", reason=reason)]
    assert world.queries() == []


async def test_a_file_in_an_unbound_channel_explains_how_to_bind(
    slack: FakeSlack, tmp_path: Path
) -> None:
    world = World(slack, tmp_path, bound=False)
    await world.dispatch(shared_file("image"))
    assert world.ephemerals() == [texts.UNBOUND.format(root=world.root.resolve())]
    await world.sessions.close_all()


@pytest.mark.parametrize("user", [{"user": STRANGER}, {"team": OTHER_TEAM}])
async def test_a_file_from_anyone_else_is_never_downloaded(
    world: World, user: dict[str, str]
) -> None:
    body = shared_file("image")
    if "team" in user:
        body["event"]["files"][0]["user_team"] = OTHER_TEAM
    else:
        body["event"].update(user)
    await world.dispatch(body)
    assert world.fetched == [] and not world.posted_anything()


async def test_a_file_on_another_host_is_never_downloaded(world: World) -> None:
    await world.dispatch(shared_file("image", url_private_download="https://evil.example/x.png"))
    assert world.fetched == []
    assert said(world) == [
        texts.UPLOAD_FAILED.format(name="photo.png", reason=texts.UPLOAD_NOT_SHARED)
    ]


async def test_a_message_with_files_keeps_its_place_in_the_queue(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Downloads take time: a text sent right after must not enter the queue first.
    body = shared_file("snippet")
    world.downloads[body["event"]["files"][0]["url_private_download"]] = b"hello\n"
    world.slow_downloads = 0.2
    session = world.sessions.get(CHANNEL)
    assert session is not None
    queued: list[Any] = []
    submit = session.submit

    async def spy(prompt: Any) -> Any:
        queued.append(prompt)
        return await submit(prompt)

    monkeypatch.setattr(session, "submit", spy)
    first = asyncio.create_task(world.dispatch(body))
    await asyncio.sleep(0.05)
    await world.dispatch(message("focus on the errors"))
    await first
    await asyncio.sleep(0.3)
    assert [str(p).startswith(body["event"]["text"]) for p in queued] == [True, False]


async def test_a_failed_download_leaves_no_saved_copy(world: World) -> None:
    body = shared_file("snippet")
    good = copy.deepcopy(body["event"]["files"][0])
    bad = {**good, "id": "F000FAIL", "url_private_download": good["url_private_download"] + "x"}
    body["event"]["files"] = [good, bad]
    world.downloads[good["url_private_download"]] = b"hello\n"
    world.downloads[bad["url_private_download"]] = DownloadFailed("HTTP 404")
    await world.dispatch(body)
    assert world.queries() == [] and not any(world.uploads.glob("*"))


async def test_too_many_images_send_nothing_and_download_nothing(world: World) -> None:
    body = shared_file("image")
    body["event"]["files"] = body["event"]["files"] * 6
    await world.dispatch(body)
    assert world.fetched == [] and world.queries() == []
    assert said(world) == [texts.UPLOAD_TOO_MANY.format(count=6, limit=5)]


async def test_a_rebind_during_the_downloads_sends_the_prompt_nowhere(world: World) -> None:
    # The session read before the downloads is closed by the rebind: nothing may reach it.
    (world.root / "docs").mkdir()
    body = shared_file("snippet")
    world.downloads[body["event"]["files"][0]["url_private_download"]] = b"hello\n"
    world.slow_downloads = 0.2
    first = asyncio.create_task(world.dispatch(body))
    await asyncio.sleep(0.05)
    await world.dispatch(message("!bind docs"))
    await first
    await asyncio.sleep(0.3)
    assert world.queries() == [] and world.clients == []
    assert said(world)[-1] == texts.PROMPT_REBOUND


async def test_a_command_after_a_message_with_files_waits_its_turn(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = shared_file("snippet")
    world.downloads[body["event"]["files"][0]["url_private_download"]] = b"hello\n"
    world.slow_downloads = 0.2
    session = world.sessions.get(CHANNEL)
    assert session is not None
    queued: list[Any] = []
    submit = session.submit

    async def spy(prompt: Any) -> Any:
        queued.append(prompt)
        return await submit(prompt)

    monkeypatch.setattr(session, "submit", spy)
    first = asyncio.create_task(world.dispatch(body))
    await asyncio.sleep(0.05)
    await world.dispatch(message("!compact"))
    await first
    await asyncio.sleep(0.3)
    assert [str(p).startswith(body["event"]["text"]) for p in queued] == [True, False]
