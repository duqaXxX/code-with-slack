import asyncio
import copy
import itertools
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ResultError, SDKSessionInfo
from slack_bolt.request.async_request import AsyncBoltRequest

from code_with_slack import texts
from code_with_slack.approvals import Answer, Approvals, Draft
from code_with_slack.attachments import DownloadFailed
from code_with_slack.config import Config
from code_with_slack.footer import UsageCache
from code_with_slack.guards import ChannelGuard, Identity
from code_with_slack.hold import HOLD_CANCEL, HOLD_CONTINUE, Holds
from code_with_slack.render.sinks import UpdateLimiter
from code_with_slack.render.status import Status
from code_with_slack.sessions import SessionDeps, SessionManager
from code_with_slack.slack_app import build_app, slack_unescape
from code_with_slack.state import StateStore
from tests.fakes import (
    BOT,
    CHANNEL,
    FIXTURES,
    OTHER_CHANNEL,
    OTHER_TEAM,
    OTHER_THREAD,
    OWNER,
    STRANGER,
    TEAM,
    THREAD,
    CanUseToolCall,
    FakeClaudeClient,
    FakeSlack,
    sdk_messages,
    slack_payload,
    split_turns,
)

# The click fixtures (000-005-block_actions.json) sit at their message's own ts, no thread_ts:
# `click_thread` reads it from `container.thread_ts`.
CLICK_THREAD = "1790192011.564799"
# form-open-click.json carries no thread_ts at all: `click_thread` falls back to `message.ts`.
FORM_THREAD = "1790285951.735939"


def recorded(kind: str) -> dict[str, Any]:
    name = next(p.stem for p in sorted((FIXTURES / "slack").glob(f"*-{kind}.json")))
    return copy.deepcopy(slack_payload(name))


async def always_trusted(directory: Path) -> bool:
    return True  # code_with_slack.trust has tests of its own


class World:
    def __init__(
        self,
        slack: FakeSlack,
        tmp_path: Path,
        *,
        bound: bool = True,
        update_limiter: UpdateLimiter | None = None,
    ) -> None:
        self.slack = slack
        self.root = tmp_path / "root"
        (self.root / "app").mkdir(parents=True)
        self.state = StateStore(tmp_path / "state.json")
        if bound:
            self.state.bind(CHANNEL, self.root / "app")
        self.clients: list[FakeClaudeClient] = []
        self.approvals = Approvals()
        self.holds = Holds()
        # What list_sessions returns for the channel's directory (SDK SDKSessionInfo, newest first).
        self.stored_sessions: list[SDKSessionInfo] = []
        # What each file URL downloads to: bytes, or the failure the download raises.
        self.downloads: dict[str, bytes | Exception] = {}
        self.uploads = tmp_path / "uploads"
        self.fetched: list[str] = []
        self.slow_downloads = 0.0
        # Holds every client's connect until set, as a CLI that is still starting.
        self.connect_gate: asyncio.Event | None = None
        # Every client factored from here on fails to connect with this, as a session whose
        # stored id no longer resumes (D7).
        self.connect_error: Exception | None = None
        identity = Identity(OWNER, TEAM, BOT)

        async def no_usage() -> str:
            return ""

        def factory(options: ClaudeAgentOptions) -> FakeClaudeClient:
            client = FakeClaudeClient(
                options, connect_gate=self.connect_gate, connect_error=self.connect_error
            )
            self.clients.append(client)
            return client

        self.sessions = SessionManager(
            SessionDeps(
                slack=slack,
                identity=identity,
                state=self.state,
                approvals=self.approvals,
                holds=self.holds,
                usage=UsageCache(no_usage),
                client_factory=factory,
                workspace_trusted=always_trusted,
                sessions_of=lambda directory: self.stored_sessions,
                update_limiter=update_limiter or UpdateLimiter(),
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
            holds=self.holds,
            guard=ChannelGuard(slack, identity),
            state=self.state,
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
    """A top-level message: its `thread_ts` defaults to its own `ts` (a fresh fixture ts unless
    `ts=` is given), so each call with a distinct `ts` opens an independent thread."""
    body = recorded("event_callback-message")
    body["event"].update({"text": text, **event})
    return body


_REPLY_SEQ = itertools.count(1)


def reply(text: str, thread_ts: str, **event: Any) -> dict[str, Any]:
    """A reply inside `thread_ts`'s thread: its own `ts` always differs from it."""
    body = recorded("event_callback-message")
    own_ts = f"179019{next(_REPLY_SEQ):04d}.900000"
    body["event"].update({"text": text, "thread_ts": thread_ts, "ts": own_ts, **event})
    return body


async def test_owner_message_becomes_a_prompt(world: World) -> None:
    body = message("list the files")
    await world.dispatch(body)
    assert world.queries() == ["list the files"]


def _recorded_thread_reply() -> dict[str, Any]:
    """A recorded reply inside a thread (its own shape, faithfully reused): 010 or 012."""
    return next(
        slack_payload(p.stem)
        for p in sorted((FIXTURES / "slack").glob("*-event_callback-message.json"))
        if "thread_ts" in slack_payload(p.stem)["event"]
    )


async def test_a_reply_in_a_known_session_s_thread_is_a_prompt_too(world: World) -> None:
    thread_body = _recorded_thread_reply()
    thread_ts = thread_body["event"]["thread_ts"]
    world.sessions.open(CHANNEL, thread_ts)  # a session already lives in this thread
    await world.dispatch(copy.deepcopy(thread_body))
    assert world.queries() == [thread_body["event"]["text"]]


async def test_a_reply_in_a_thread_that_holds_no_session_is_refused(world: World) -> None:
    # Same recorded shape as above, but its thread was never opened: nothing to continue.
    thread_body = _recorded_thread_reply()
    await world.dispatch(copy.deepcopy(thread_body))
    assert world.queries() == []
    assert world.ephemerals() == [texts.NOT_A_SESSION]


@pytest.mark.parametrize(("user", "team"), [(STRANGER, TEAM), (OWNER, OTHER_TEAM)])
async def test_a_message_from_anyone_else_does_nothing(world: World, user: str, team: str) -> None:
    await world.dispatch(message("rm -rf /", user=user, team=team))
    assert world.queries() == [] and not world.posted_anything()


@pytest.mark.parametrize(("user", "team"), [(STRANGER, TEAM), (OWNER, OTHER_TEAM)])
async def test_a_reply_from_anyone_else_does_nothing(world: World, user: str, team: str) -> None:
    await world.dispatch(message("hi", ts=THREAD))  # the owner opens the session
    await world.dispatch(reply("rm -rf /", THREAD, user=user, team=team))
    assert world.queries() == ["hi"] and not world.ephemerals()


async def test_edits_do_nothing(world: World) -> None:
    await world.dispatch(recorded("event_callback-message_changed"))
    assert world.queries() == []


async def test_a_refused_channel_tells_only_the_owner(world: World) -> None:
    world.slack.responses["conversations.members"] = {"ok": True, "members": [OWNER, BOT, STRANGER]}
    await world.dispatch(message())
    assert world.queries() == []
    assert world.ephemerals() == [texts.CHANNEL_REFUSED.format(reason=texts.REASON_MEMBERS)]
    (refused,) = world.slack.calls_to("chat.postEphemeral")
    assert refused["user"] == OWNER
    assert refused["blocks"][0]["type"] == "context"


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
    """What the bot posted in a thread (not ephemeral), in order."""
    return [a["text"] for a in world.slack.calls_to("chat.postMessage")]


async def test_bang_bind_inside_and_outside_the_root(world: World) -> None:
    await world.dispatch(message(f"!bind {world.root / 'app'}"))
    assert world.state.channel(CHANNEL).directory == (world.root / "app").resolve()
    await world.dispatch(message("!bind /"))
    assert said(world)[-1] == texts.BIND_OUTSIDE.format(path="/", root=world.root.resolve())
    await world.dispatch(message("!bind app"))  # relative to the allowed root, as the text says
    assert world.state.channel(CHANNEL).directory == (world.root / "app").resolve()


async def test_bang_bind_works_in_an_unbound_channel(slack: FakeSlack, tmp_path: Path) -> None:
    world = World(slack, tmp_path, bound=False)
    await world.dispatch(message(f"!bind {world.root / 'app'}"))
    assert world.state.channel(CHANNEL).directory == (world.root / "app").resolve()
    assert world.queries() == []
    await world.sessions.close_all()


async def test_bang_bind_is_refused_inside_a_thread(world: World) -> None:
    await world.dispatch(message("hi", ts=THREAD))  # opens a session at THREAD
    await world.dispatch(reply(f"!bind {world.root / 'docs'}", THREAD))
    assert said(world)[-1] == texts.WORD_IN_THREAD.format(word="bind")
    assert world.state.channel(CHANNEL).directory == (world.root / "app").resolve()


async def test_bang_resume_is_refused_inside_a_thread(world: World) -> None:
    await world.dispatch(message("hi", ts=THREAD))
    await world.dispatch(reply("!resume", THREAD))
    assert said(world)[-1] == texts.WORD_IN_THREAD.format(word="resume")


async def test_bang_bypass_is_refused_at_top_level(world: World) -> None:
    await world.dispatch(message("!bypass on"))
    assert said(world) == [texts.BYPASS_TOP_LEVEL]
    assert world.clients == []  # no session was even started


async def test_bang_clear_is_refused_inside_a_thread(world: World) -> None:
    await world.dispatch(message("hi", ts=THREAD))
    await world.dispatch(reply("!clear", THREAD))
    assert said(world)[-1] == texts.CLEAR_IN_THREAD
    assert world.queries() == ["hi"]  # the earlier prompt is the only one that reached Claude


async def test_bang_clear_at_top_level_opens_a_session_like_any_other_word(world: World) -> None:
    await world.dispatch(message("!clear"))
    assert world.queries() == ["/clear"]


async def test_a_daemon_word_in_a_dead_thread_acts_as_top_level(world: World) -> None:
    # A reply inside the thread of an old request (never a session) still gets an answer there,
    # exactly as a top-level `!bind` would: the channel's own folder ("app") shows as current.
    await world.dispatch(reply("!bind", CLICK_THREAD))
    (post,) = world.slack.calls_to("chat.postMessage")
    assert post["thread_ts"] == CLICK_THREAD
    rows = [b["text"]["text"] for b in post["blocks"] if b.get("type") == "section"]
    assert any(row.startswith("`app`") and texts.BIND_CURRENT in row for row in rows)
    values = [b["accessory"]["value"] for b in post["blocks"] if "accessory" in b]
    assert values == ["."]  # the current folder ("app") shows with no button


async def test_bang_help_lists_the_session_commands(world: World) -> None:
    await world.dispatch(message("hi", ts=THREAD))
    await world.dispatch(reply("!help", THREAD))
    (text,) = said(world)[1:]
    assert "`!compact" in text and "`!bypass" in text


async def test_bang_help_top_level_lists_only_the_daemon_words(world: World) -> None:
    await world.dispatch(message("!help"))
    (text,) = said(world)
    assert "`!bypass" in text and "`!compact" not in text
    assert texts.HELP_UNBOUND in text
    assert world.clients == []


async def test_bang_help_with_a_filter_lists_only_matches(world: World) -> None:
    await world.dispatch(message("hi", ts=THREAD))
    await world.dispatch(reply("!help compact", THREAD))
    (text,) = said(world)[1:]
    assert "`!compact" in text and "`!bypass" not in text


async def test_bang_help_works_in_an_unbound_channel(slack: FakeSlack, tmp_path: Path) -> None:
    world = World(slack, tmp_path, bound=False)
    await world.dispatch(message("!help"))
    (text,) = said(world)
    assert "`!bind" in text
    assert world.clients == []


async def test_bang_bypass_on_inside_a_thread_switches_the_live_client(world: World) -> None:
    await world.dispatch(message("hi", ts=THREAD))
    await world.dispatch(reply("!bypass on", THREAD))
    assert world.clients[0].modes == ["bypassPermissions"]
    assert said(world)[-1] == texts.BYPASS_ON


async def test_a_notice_is_small_and_grey_a_reference_full_size(world: World) -> None:
    # The daemon's notices read apart from Claude's replies, as the footer does (the owner,
    # 2026-09-27); `!help`, `!guide` and `!status` stay full size, since they are read.
    await world.dispatch(message("hi", ts=THREAD))
    await world.dispatch(reply("!bypass on", THREAD))
    await world.dispatch(reply("!help", THREAD))
    _, notice, reference = world.slack.calls_to("chat.postMessage")
    assert notice["blocks"] == [
        {"type": "context", "elements": [{"type": "mrkdwn", "text": texts.BYPASS_ON}]}
    ]
    assert [b["type"] for b in reference["blocks"]] == ["markdown"]


async def test_a_bound_folder_is_shown_as_written_in_the_notice(world: World) -> None:
    folder = world.root / "R&D"
    folder.mkdir()
    await world.dispatch(message(f"!bind {folder}"))
    (notice,) = world.slack.calls_to("chat.postMessage")
    assert "R&amp;D" in notice["blocks"][0]["elements"][0]["text"]


async def test_a_bind_never_touches_an_existing_thread_s_bypass(world: World) -> None:
    (world.root / "docs").mkdir()
    await world.dispatch(message("hi", ts=THREAD))
    await world.dispatch(reply("!bypass on", THREAD))
    await world.dispatch(message("!bind docs"))  # a new default-folder thread from here on
    assert world.state.thread(CHANNEL, THREAD).bypass is True
    assert world.clients[0].connected is True  # never closed by the bind


async def test_a_gone_session_answers_bypass_with_no_new_session(world: World) -> None:
    world.state.open_thread(CHANNEL, THREAD, session_id="68da9311-0000-4000-8000-00000000dead")
    world.connect_error = ResultError("transcript missing")
    await world.dispatch(reply("!bypass on", THREAD))
    assert world.ephemerals() == [texts.SESSION_GONE]
    assert world.state.thread(CHANNEL, THREAD) is None


async def test_bang_status_and_stop_top_level_summarize_the_channel(world: World) -> None:
    await world.dispatch(message("!stop"))
    await world.dispatch(message("!status"))
    assert said(world)[0] == texts.NOTHING_TO_STOP
    assert said(world)[1] == "\n".join(
        [
            texts.STATUS_CHANNEL_HEADER.format(directory=(world.root / "app").resolve()),
            texts.STATUS_CHANNEL_EMPTY,
        ]
    )
    assert world.queries() == []


async def test_bang_status_lists_every_live_session_with_a_link(world: World) -> None:
    await world.dispatch(message("hello", ts=THREAD))  # never ends: stays busy
    await world.dispatch(message("!status", ts=OTHER_THREAD))
    text = said(world)[-1]
    lines = text.splitlines()
    directory = (world.root / "app").resolve()
    assert lines[0] == texts.STATUS_CHANNEL_HEADER.format(directory=directory)
    # `say` posts a markdown block: a standard Markdown link, not mrkdwn's `<url|label>`.
    link = f"https://example.slack.com/archives/{CHANNEL}/p1780000000000001"
    assert lines[1] == f"[Session]({link}): busy"


async def test_bang_status_fetches_permalinks_concurrently(world: World) -> None:
    await world.dispatch(message("hello", ts=THREAD))
    await world.dispatch(message("hello", ts=OTHER_THREAD))
    await world.dispatch(message("hello", ts=CLICK_THREAD))

    in_flight = 0
    max_in_flight = 0
    real = world.slack.chat_getPermalink

    async def tracked(**kwargs: Any) -> Any:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        try:
            return await real(**kwargs)
        finally:
            in_flight -= 1

    world.slack.chat_getPermalink = tracked  # type: ignore[method-assign]
    await world.dispatch(message("!status", ts="1790300000.000001"))
    assert max_in_flight == 3  # all three fetched at once, not one after another
    text = said(world)[-1]
    assert len(text.splitlines()) == 4  # header + one row per session, order kept


async def test_bang_status_shows_waiting_for_the_owner(world: World) -> None:
    await world.dispatch(message("hi", ts=THREAD))
    world.clients[0].inject([CanUseToolCall("Bash", {"command": "ls"})])
    async with asyncio.timeout(2):
        while not world.approvals._pending:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    await world.dispatch(message("!status", ts=OTHER_THREAD))
    line = said(world)[-1].splitlines()[1]
    assert f": {texts.STATUS_CHANNEL_WAITING}" in line


async def test_bang_status_shows_a_background_only_session_as_busy_not_idle(world: World) -> None:
    # A task that outlived its turn: `session.busy` is False, yet it is not idle either.
    first = split_turns(sdk_messages("background"))[0]
    await world.dispatch(message("start it", ts=THREAD))
    world.clients[0].inject(first)
    session = world.sessions.get(CHANNEL, THREAD)
    assert session is not None
    async with asyncio.timeout(2):
        while session.busy or not session.running_kinds:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    await world.dispatch(message("!status", ts=OTHER_THREAD))
    line = said(world)[-1].splitlines()[1]
    assert f": {texts.STATUS_CHANNEL_BUSY}" in line
    assert "⏳" in line


async def test_bang_status_inside_a_thread_shows_that_session(world: World) -> None:
    await world.dispatch(message("hi", ts=THREAD))
    await world.dispatch(reply("!status", THREAD))
    text = said(world)[-1]
    assert text.startswith("Directory:")
    assert "\nContext: `7%`" in text


async def test_bang_stop_inside_a_thread_stops_only_that_session(world: World) -> None:
    await world.dispatch(message("hello", ts=THREAD))  # never ends
    await world.dispatch(message("hello", ts=OTHER_THREAD))  # D8: THREAD's session is busy
    hold_id = button_value(world.slack.calls_to("chat.postMessage")[-1]["blocks"], HOLD_CONTINUE)
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, OTHER_THREAD))
    await world.dispatch(reply("!stop", THREAD))
    assert said(world)[-1] == texts.STOPPED_THREAD
    assert world.clients[0].interrupts == 1
    assert world.clients[1].interrupts == 0


async def test_a_malformed_daemon_word_at_top_level_shows_only_the_daemon_words(
    world: World,
) -> None:
    await world.dispatch(message("!bypass maybe"))
    text = said(world)[-1]
    assert "`!bypass" in text and "`!compact" not in text
    assert texts.HELP_UNBOUND in text
    assert world.queries() == []


async def test_a_malformed_daemon_word_inside_a_thread_shows_the_session_s_help_too(
    world: World,
) -> None:
    await world.dispatch(message("hi", ts=THREAD))
    await world.dispatch(reply("!bypass maybe", THREAD))
    text = said(world)[-1]
    assert "`!bypass" in text and "`!compact" in text
    assert texts.HELP_UNBOUND not in text


async def test_bang_from_anyone_else_does_nothing(world: World) -> None:
    await world.dispatch(message("!bypass on", user=STRANGER))
    assert world.clients == [] and not world.posted_anything()


def click(action_id: str, value: str, **user: Any) -> dict[str, Any]:
    body = recorded("block_actions")
    body["actions"] = [{**body["actions"][0], "action_id": action_id, "value": value}]
    body["user"].update(user)
    return body


def click_in(
    action_id: str,
    value: str,
    channel: str,
    thread_ts: str,
    *,
    message_ts: str | None = None,
    **user: Any,
) -> dict[str, Any]:
    """`click`, but on a message posted in `channel`/`thread_ts` rather than the fixed
    CLICK_THREAD fixture: for a button whose message a test itself made the daemon post.
    `message_ts` is that message's own ts (`remove_request` deletes it), when it matters to the
    test; left out, it stays the fixture's own (a click that is refused before it is read)."""
    body = click(action_id, value, **user)
    body["channel"]["id"] = channel
    body["container"]["thread_ts"] = thread_ts
    body["message"]["thread_ts"] = thread_ts
    if message_ts is not None:
        body["container"]["message_ts"] = message_ts
        body["message"]["ts"] = message_ts
    return body


def button_value(blocks: list[dict[str, Any]], action_id: str) -> str:
    """The value a posted message's own button carries, read back as a click would send it."""
    for block in blocks:
        for element in block.get("elements") or []:
            if element.get("action_id") == action_id:
                return str(element["value"])
    raise AssertionError(f"no {action_id} button in the posted blocks")


async def open_request(world: World, thread_ts: str = CLICK_THREAD) -> tuple[str, Any]:
    approval_id, pending = world.approvals.open(CHANNEL, thread_ts, "Bash: ls")
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
    approval_id, pending = world.approvals.open("C000ELSEWHERE", CLICK_THREAD, "Bash: ls")
    await world.dispatch(click("approval_allow", approval_id))
    assert not pending.future.done()
    assert world.ephemerals() == [texts.APPROVAL_GONE]


async def test_a_request_from_another_thread_cannot_be_answered_here(world: World) -> None:
    approval_id, pending = world.approvals.open(CHANNEL, OTHER_THREAD, "Bash: ls")
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
    approval_id, _ = world.approvals.open(CHANNEL, FORM_THREAD, "Colour", QUESTIONS)
    body = recorded("open-click")  # the Answer click, recorded 2026-09-24
    body["actions"][0]["value"] = approval_id
    body["trigger_id"] = "0000000000.0000000000.fake"  # real clicks carry one; the scrub drops it
    await world.dispatch(body)
    (opened,) = world.slack.calls_to("views.open")
    assert opened["trigger_id"] == body["trigger_id"]
    view = json.loads(opened["view"]) if isinstance(opened["view"], str) else opened["view"]
    assert view["callback_id"] == "question_form"
    assert Draft.load(view["private_metadata"]) == Draft(approval_id, CHANNEL, FORM_THREAD)


@pytest.mark.parametrize("user", [{"id": STRANGER}, {"team_id": OTHER_TEAM}])
async def test_nobody_else_can_open_the_form(world: World, user: dict[str, str]) -> None:
    approval_id, _ = world.approvals.open(CHANNEL, FORM_THREAD, "Colour", QUESTIONS)
    await world.dispatch(click("question_open", approval_id, **user))
    assert not world.slack.calls_to("views.open") and not world.posted_anything()


async def test_submit_with_the_open_question_unanswered_shows_an_error(world: World) -> None:
    approval_id, pending = world.approvals.open(CHANNEL, FORM_THREAD, "Colour", QUESTIONS)
    draft = Draft(approval_id, CHANNEL, FORM_THREAD)
    response = await world.dispatch(form_body("view_submission", draft, {}))
    assert json.loads(response.body) == {
        "response_action": "errors",
        "errors": {"q0": texts.QUESTION_MISSING},
    }
    assert not pending.future.done()


async def test_next_with_an_answer_moves_to_the_next_question(world: World) -> None:
    approval_id, pending = world.approvals.open(CHANNEL, FORM_THREAD, "Colour", QUESTIONS)
    draft = Draft(approval_id, CHANNEL, FORM_THREAD)
    response = await world.dispatch(form_body("view_submission", draft, picked(0, "0")))
    answer = json.loads(response.body)
    assert answer["response_action"] == "update"
    assert Draft.load(answer["view"]["private_metadata"]) == Draft(
        approval_id, CHANNEL, FORM_THREAD, 1, {0: [0]}
    )
    assert not pending.future.done()


async def test_a_complete_submit_answers_claude_and_keeps_the_answers(world: World) -> None:
    approval_id, pending = world.approvals.open(CHANNEL, FORM_THREAD, "Colour", QUESTIONS)
    pending.message_ts = "1790000000.000009"
    world.state.open_thread(CHANNEL, FORM_THREAD)
    world.state.add_request(CHANNEL, FORM_THREAD, pending.message_ts)
    draft = Draft(approval_id, CHANNEL, FORM_THREAD, active=1, picks={0: [1]})
    values = {
        "q1": {"answer": {"type": "checkboxes", "selected_options": [{"value": "0"}]}},
        "o1": {"other": {"type": "plain_text_input", "value": "xl"}},
    }
    await world.dispatch(form_body("view_submission", draft, values))
    assert pending.future.result() == Answer({"Colour?": "blue", "Sizes?": ["s", "xl"]})
    # The request stays as the terminal's record of the answers (terminal, CLI 2.1.283:
    # `User answered Claude's questions:` then `⎿ · Which colour do you prefer? → Red`).
    assert world.slack.calls_to("chat.delete") == []
    [update] = world.slack.calls_to("chat.update")
    assert update["ts"] == "1790000000.000009"
    assert update["blocks"][0]["elements"][0]["text"] == (
        f"{texts.ANSWERED}\n{texts.NESTED}· Colour? → blue\n{texts.NESTED}· Sizes? → s, xl"
    )
    # Crash repair (issue #19): answered without a delete, so it is no longer tracked either.
    assert world.state.thread(CHANNEL, FORM_THREAD).requests == ()


async def test_show_answered_draws_from_the_process_s_shared_update_limiter(
    slack: FakeSlack, tmp_path: Path
) -> None:
    limiter = UpdateLimiter(limit=1, window=0.3, burst=1)
    world = World(slack, tmp_path, update_limiter=limiter)
    await limiter.acquire()  # spent, as a busy ReplySink's own chat.update already would have
    approval_id, pending = world.approvals.open(CHANNEL, FORM_THREAD, "Colour", QUESTIONS)
    pending.message_ts = "1790000000.000009"
    draft = Draft(approval_id, CHANNEL, FORM_THREAD, active=1, picks={0: [1]})
    values = {
        "q1": {"answer": {"type": "checkboxes", "selected_options": [{"value": "0"}]}},
        "o1": {"other": {"type": "plain_text_input", "value": "xl"}},
    }
    start = time.monotonic()
    await world.dispatch(form_body("view_submission", draft, values))  # runs the listener task
    for _ in range(50):  # the listener task runs in the background: poll for its write
        if world.slack.calls_to("chat.update"):
            break
        await asyncio.sleep(0.02)
    # waits for the same budget a busy reply had already spent, not a free pass of its own.
    assert world.slack.calls_to("chat.update")
    assert time.monotonic() - start >= 0.2


@pytest.mark.parametrize("user", [{"id": STRANGER}, {"team_id": OTHER_TEAM}])
async def test_nobody_else_can_submit_the_form(world: World, user: dict[str, str]) -> None:
    approval_id, pending = world.approvals.open(CHANNEL, FORM_THREAD, "Colour", QUESTIONS)
    draft = Draft(approval_id, CHANNEL, FORM_THREAD, picks={0: [0], 1: [0]})
    await world.dispatch(form_body("view_submission", draft, {}, **user))
    assert not pending.future.done()


async def test_a_bypass_inside_a_thread_fails_when_the_directory_is_gone(world: World) -> None:
    world.sessions.open(CHANNEL, THREAD)
    (world.root / "app").rmdir()
    await world.dispatch(reply("!bypass on", THREAD))
    assert world.ephemerals() == [texts.DIRECTORY_MISSING.format(directory=world.root / "app")]


async def test_a_form_that_cannot_open_tells_the_owner(world: World) -> None:
    approval_id, _ = world.approvals.open(CHANNEL, FORM_THREAD, "Colour", QUESTIONS)
    world.slack.responses["views.open"] = {"ok": False, "error": "expired_trigger_id"}
    body = recorded("open-click")
    body["actions"][0]["value"] = approval_id
    body["trigger_id"] = "0000000000.0000000000.fake"
    await world.dispatch(body)
    assert world.ephemerals() == [texts.QUESTION_NOT_OPENED.format(error="expired_trigger_id")]


async def test_a_submit_that_needs_more_answers_makes_no_slack_call_first(world: World) -> None:
    approval_id, _ = world.approvals.open(CHANNEL, FORM_THREAD, "Colour", QUESTIONS)
    draft = Draft(approval_id, CHANNEL, FORM_THREAD)
    await world.dispatch(form_body("view_submission", draft, {}))
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


async def test_bang_resume_by_name_or_id_opens_a_new_thread_on_it(world: World) -> None:
    two_sessions(world)
    await world.dispatch(message("!resume footer", ts=THREAD))
    assert world.state.thread(CHANNEL, THREAD).session_id == SESSION_A
    await world.dispatch(message(f"!resume {SESSION_B}", ts=OTHER_THREAD))
    assert world.state.thread(CHANNEL, OTHER_THREAD).session_id == SESSION_B
    confirmation = said(world)[-1]
    assert "Trust gate" in confirmation and "\n" not in confirmation.split("**")[1]


async def test_bang_resume_of_an_unknown_session_says_so(world: World) -> None:
    two_sessions(world)
    await world.dispatch(message("!resume nothing-like-it"))
    assert "`nothing-like-it`" in said(world)[-1]
    assert world.state.channel(CHANNEL).threads == {}


async def test_the_owner_resumes_from_the_list(world: World) -> None:
    two_sessions(world)
    body = click("session_resume", SESSION_B)
    await world.dispatch(body)
    assert world.state.thread(CHANNEL, CLICK_THREAD).session_id == SESSION_B
    deleted = [(a["channel"], a["ts"]) for a in world.slack.calls_to("chat.delete")]
    assert deleted == [(CHANNEL, body["message"]["ts"])]


@pytest.mark.parametrize("user", [{"id": STRANGER}, {"team_id": OTHER_TEAM}])
async def test_nobody_else_can_resume(world: World, user: dict[str, str]) -> None:
    two_sessions(world)
    await world.dispatch(click("session_resume", SESSION_B, **user))
    assert world.state.channel(CHANNEL).threads == {}


async def test_a_button_is_never_trusted_for_a_session_of_another_directory(world: World) -> None:
    two_sessions(world)
    await world.dispatch(click("session_resume", "68da9311-0000-4000-8000-0000000000ff"))
    assert world.state.channel(CHANNEL).threads == {}
    assert world.ephemerals() == [texts.RESUME_GONE]


# The permalink FakeSlack answers with by default (tests/fixtures/slack/api-chat-getPermalink.json).
PERMALINK = "https://example.slack.com/archives/C000CHAN/p1780000000000001"


async def test_a_typed_resume_of_a_session_held_by_another_thread_is_refused(world: World) -> None:
    # D6 minimum: two live processes on one transcript is never reachable.
    two_sessions(world)
    await world.dispatch(message(f"!resume {SESSION_B}", ts=OTHER_THREAD))
    assert world.state.thread(CHANNEL, OTHER_THREAD).session_id == SESSION_B
    await world.dispatch(message(f"!resume {SESSION_B}", ts=THREAD))
    assert world.state.thread(CHANNEL, THREAD) is None
    link = f"<{PERMALINK}|Session>"
    assert texts.RESUME_ELSEWHERE.format(link=link) in world.ephemerals()


async def test_a_resume_click_of_a_session_held_by_another_thread_is_refused(world: World) -> None:
    two_sessions(world)
    await world.dispatch(message(f"!resume {SESSION_B}", ts=OTHER_THREAD))
    await world.dispatch(click("session_resume", SESSION_B))  # a different thread (CLICK_THREAD)
    assert world.state.thread(CHANNEL, CLICK_THREAD) is None
    link = f"<{PERMALINK}|Session>"
    assert texts.RESUME_ELSEWHERE.format(link=link) in world.ephemerals()


async def test_a_held_session_s_link_falls_back_when_the_permalink_fails(world: World) -> None:
    two_sessions(world)
    await world.dispatch(message(f"!resume {SESSION_B}", ts=OTHER_THREAD))
    world.slack.responses["chat.getPermalink"] = RuntimeError("network down")
    await world.dispatch(message(f"!resume {SESSION_B}", ts=THREAD))
    assert world.state.thread(CHANNEL, THREAD) is None
    fallback = texts.STATUS_CHANNEL_LINK_FALLBACK.format(thread_ts=OTHER_THREAD)
    assert texts.RESUME_ELSEWHERE.format(link=fallback) in world.ephemerals()


async def test_the_list_marks_a_session_held_elsewhere_with_its_permalink(world: World) -> None:
    two_sessions(world)
    await world.dispatch(message(f"!resume {SESSION_B}", ts=OTHER_THREAD))
    await world.dispatch(message("!resume"))
    post = world.slack.calls_to("chat.postMessage")[-1]
    held_row = next(b for b in post["blocks"] if b.get("block_id") == f"session-{SESSION_B}")
    free_row = next(b for b in post["blocks"] if b.get("block_id") == f"session-{SESSION_A}")
    assert "accessory" not in held_row and "accessory" in free_row
    link = f"<{PERMALINK}|open elsewhere>"
    assert held_row["text"]["text"].endswith(texts.RESUME_ELSEWHERE_ROW.format(link=link))
    permalinks = world.slack.calls_to("chat.getPermalink")
    assert [(c["channel"], c["message_ts"]) for c in permalinks] == [(CHANNEL, OTHER_THREAD)]


async def test_a_still_running_first_turn_already_holds_its_session_id(world: World) -> None:
    # D6: the session id is recorded as soon as Claude Code reports it (the init message), not
    # only at the turn's ResultMessage, so a still-running first turn is already this session
    # id's holder and cannot be resumed a second time into another thread.
    await world.dispatch(message("hello", ts=THREAD))
    world.clients[0].inject(sdk_messages("tools")[:-1])  # no ResultMessage: still running
    async with asyncio.timeout(2):
        stored = world.state.thread(CHANNEL, THREAD)
        while stored is None or stored.session_id is None:
            await asyncio.sleep(0.01)
            stored = world.state.thread(CHANNEL, THREAD)
    held_id = stored.session_id
    world.stored_sessions = [SDKSessionInfo(held_id, "tools", 0, 1)]
    await world.dispatch(message(f"!resume {held_id}", ts=OTHER_THREAD))
    assert world.state.thread(CHANNEL, OTHER_THREAD) is None
    link = f"<{PERMALINK}|Session>"
    assert texts.RESUME_ELSEWHERE.format(link=link) in world.ephemerals()


async def test_resume_opens_an_independent_thread_while_another_is_busy(world: World) -> None:
    two_sessions(world)
    await world.dispatch(message("hello", ts=THREAD))  # the fake Claude Code never ends this turn
    await world.dispatch(message(f"!resume {SESSION_B}", ts=OTHER_THREAD))
    assert world.state.thread(CHANNEL, OTHER_THREAD).session_id == SESSION_B
    assert "Trust gate" in said(world)[-1]


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
    assert world.state.channel(CHANNEL).directory == (world.root / "app").resolve()
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
    assert world.state.channel(CHANNEL) is None and not world.posted_anything()


async def test_a_bind_button_is_never_trusted_for_a_folder_outside_the_root(world: World) -> None:
    await world.dispatch(click("folder_bind", "../.."))
    assert world.state.channel(CHANNEL).directory == (world.root / "app").resolve()
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
    await world.dispatch(click("folder_bind", "app"))
    assert said(world) == [texts.BIND_ALREADY.format(directory=(world.root / "app").resolve())]
    assert world.state.channel(CHANNEL).directory == (world.root / "app").resolve()


async def test_a_bind_click_while_a_turn_runs_is_refused_and_the_list_stays(world: World) -> None:
    (world.root / "docs").mkdir()
    await world.dispatch(message("hello"))  # the fake Claude Code never ends this turn
    await world.dispatch(click("folder_bind", "docs"))
    assert world.state.channel(CHANNEL).directory == (world.root / "app").resolve()
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


async def test_a_second_resume_click_on_the_same_list_is_refused(world: World) -> None:
    two_sessions(world)
    await world.dispatch(click("session_resume", SESSION_A))
    await world.dispatch(click("session_resume", SESSION_B))  # a quick second click, same list
    assert world.state.thread(CHANNEL, CLICK_THREAD).session_id == SESSION_A
    assert texts.RESUME_HELD in world.ephemerals()


async def test_a_resume_click_after_a_typed_resume_in_the_same_thread_is_refused(
    world: World,
) -> None:
    two_sessions(world)
    # A non-session thread that holds the picker: `!resume footer` there acts as top-level.
    await world.dispatch(reply("!resume footer", CLICK_THREAD))
    assert world.state.thread(CHANNEL, CLICK_THREAD).session_id == SESSION_A
    await world.dispatch(click("session_resume", SESSION_B))  # the picker's own thread
    assert world.state.thread(CHANNEL, CLICK_THREAD).session_id == SESSION_A
    assert texts.RESUME_HELD in world.ephemerals()


async def test_a_resume_click_never_stores_a_session_of_a_folder_bound_meanwhile(
    world: World,
) -> None:
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
    assert world.state.channel(CHANNEL).directory == (world.root / "docs").resolve()
    assert world.state.channel(CHANNEL).threads == {}
    assert texts.RESUME_GONE in world.ephemerals()


async def test_a_typed_resume_never_stores_a_session_of_a_folder_bound_meanwhile(
    world: World,
) -> None:
    two_sessions(world)
    (world.root / "docs").mkdir()
    listed = asyncio.Event()

    def slow(directory: Path) -> list[SDKSessionInfo]:
        listed.set()
        time.sleep(0.2)
        return world.stored_sessions

    world.sessions._deps.sessions_of = slow
    resume_task = asyncio.create_task(world.dispatch(message(f"!resume {SESSION_B}", ts=THREAD)))
    await listed.wait()
    await world.dispatch(message("!bind docs"))
    await resume_task
    await asyncio.sleep(0.3)
    assert world.state.channel(CHANNEL).directory == (world.root / "docs").resolve()
    assert world.state.thread(CHANNEL, THREAD) is None
    assert texts.RESUME_GONE in world.ephemerals()


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
    # Downloads take time: a reply sent right after must not enter the queue first.
    body = shared_file("snippet")
    file_thread = str(body["event"]["ts"])
    world.downloads[body["event"]["files"][0]["url_private_download"]] = b"hello\n"
    world.slow_downloads = 0.2
    queued: list[Any] = []
    first = asyncio.create_task(world.dispatch(body))
    async with asyncio.timeout(2):
        session = None
        while session is None:
            session = world.sessions.get(CHANNEL, file_thread)
            await asyncio.sleep(0.01)
    submit = session.submit

    async def spy(prompt: Any) -> Any:
        queued.append(prompt)
        return await submit(prompt)

    monkeypatch.setattr(session, "submit", spy)
    await asyncio.sleep(0.05)
    await world.dispatch(reply("focus on the errors", file_thread))
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


async def test_a_bind_during_the_downloads_never_moves_the_thread_already_opened(
    world: World,
) -> None:
    # The file's own thread keeps the folder it opened in: a bind only affects new threads.
    (world.root / "docs").mkdir()
    body = shared_file("snippet")
    world.downloads[body["event"]["files"][0]["url_private_download"]] = b"hello\n"
    world.slow_downloads = 0.2
    first = asyncio.create_task(world.dispatch(body))
    await asyncio.sleep(0.05)
    await world.dispatch(message("!bind docs"))
    await first
    await asyncio.sleep(0.3)
    assert world.queries() != []
    assert world.state.channel(CHANNEL).directory == (world.root / "docs").resolve()


async def test_a_command_after_a_message_with_files_waits_its_turn(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = shared_file("snippet")
    file_thread = str(body["event"]["ts"])
    world.downloads[body["event"]["files"][0]["url_private_download"]] = b"hello\n"
    world.slow_downloads = 0.2
    queued: list[Any] = []
    first = asyncio.create_task(world.dispatch(body))
    async with asyncio.timeout(2):
        session = None
        while session is None:
            session = world.sessions.get(CHANNEL, file_thread)
            await asyncio.sleep(0.01)
    submit = session.submit

    async def spy(prompt: Any) -> Any:
        queued.append(prompt)
        return await submit(prompt)

    monkeypatch.setattr(session, "submit", spy)
    await asyncio.sleep(0.05)
    await world.dispatch(reply("!compact", file_thread))
    await first
    await asyncio.sleep(0.3)
    assert [str(p).startswith(body["event"]["text"]) for p in queued] == [True, False]


async def test_a_session_closed_during_a_slow_download_is_retried_on_a_fresh_one(
    world: World,
) -> None:
    # D9: whatever closed the session handed to this call (an idle close, most likely, though
    # `touch()` at the lookup already guards that common case) during the slow step below must
    # not lose the prompt: submit_to_session retries once against a freshly looked-up session.
    body = shared_file("snippet")
    file_thread = str(body["event"]["ts"])
    world.downloads[body["event"]["files"][0]["url_private_download"]] = b"hello\n"
    world.slow_downloads = 0.2
    dispatching = asyncio.create_task(world.dispatch(body))
    async with asyncio.timeout(2):
        session = None
        while session is None:
            session = world.sessions.get(CHANNEL, file_thread)
            await asyncio.sleep(0.01)
    await session.close()  # something else closes it while the download is still running
    await dispatching
    await asyncio.sleep(0.3)  # let the slow download finish and the retried submit run
    queries = world.queries()
    assert len(queries) == 1  # the retry queues the prompt once, never twice
    assert str(queries[0]).startswith(body["event"]["text"])
    rebuilt = world.sessions.get(CHANNEL, file_thread)
    assert rebuilt is not None and rebuilt is not session


async def test_a_retry_that_finds_the_thread_gone_answers_session_gone(world: World) -> None:
    # D7: a `SessionGone` close (unlike an idle close) also removes the thread's own entry, so
    # the retry's fresh lookup finds nothing: answering the stale `SessionClosed` text, which
    # promises a retry will help, would be wrong.
    body = shared_file("snippet")
    file_thread = str(body["event"]["ts"])
    world.downloads[body["event"]["files"][0]["url_private_download"]] = b"hello\n"
    world.slow_downloads = 0.2
    dispatching = asyncio.create_task(world.dispatch(body))
    async with asyncio.timeout(2):
        session = None
        while session is None:
            session = world.sessions.get(CHANNEL, file_thread)
            await asyncio.sleep(0.01)
    await session.close()  # closed, and its thread's entry gone, as SessionGone leaves it
    world.state.remove_thread(CHANNEL, file_thread)
    await dispatching
    await asyncio.sleep(0.3)
    assert world.queries() == []
    assert texts.SESSION_GONE in world.ephemerals()


async def test_a_prompt_while_the_daemon_stops_is_refused_and_words_still_work(
    world: World,
) -> None:
    await world.sessions.drain(asyncio.Event())  # nothing runs: returns at once
    await world.dispatch(message("list the files"))
    await world.dispatch(message("!stop"))
    assert said(world) == [texts.RESTARTING, texts.NOTHING_TO_STOP]
    assert world.queries() == []


async def test_a_stop_during_the_downloads_sends_the_prompt_nowhere(world: World) -> None:
    body = shared_file("snippet")
    world.downloads[body["event"]["files"][0]["url_private_download"]] = b"hello\n"
    world.slow_downloads = 0.2
    first = asyncio.create_task(world.dispatch(body))
    await asyncio.sleep(0.05)
    await world.sessions.drain(asyncio.Event())
    await first
    await asyncio.sleep(0.3)
    assert world.queries() == [] and world.clients == []
    assert said(world)[-1] == texts.RESTARTING


@pytest.mark.parametrize("how", ["typed", "clicked"])
async def test_a_bind_to_an_untrusted_folder_says_no_session_can_start_yet(
    world: World, how: str
) -> None:
    (world.root / "docs").mkdir()
    docs = (world.root / "docs").resolve()

    async def only_app(directory: Path) -> bool:
        return directory != docs

    world.sessions._deps.workspace_trusted = only_app
    if how == "typed":
        await world.dispatch(message("!bind docs"))
    else:
        await world.dispatch(click("folder_bind", "docs"))
    reason = texts.DIRECTORY_UNTRUSTED.format(directory=docs)
    assert said(world)[-1] == texts.BIND_UNAVAILABLE.format(directory=docs, reason=reason)
    assert world.state.channel(CHANNEL).directory == docs
    await world.sessions.close_all()


async def _idle_message(world: World, text: str, *, ts: str) -> None:
    """A message that opens or continues a thread and lets its turn finish, so the session is
    idle again (D5's `bind` refuses one that is still busy)."""
    await world.dispatch(message(text, ts=ts))
    world.clients[-1].inject(sdk_messages("tools"))  # a full turn, ResultMessage included
    await asyncio.sleep(0.05)


async def test_bind_names_the_old_folder_a_thread_keeps(world: World) -> None:
    # D5: the thread stays in `app`, the folder it was created in; the bind answer names it.
    await _idle_message(world, "hi", ts=THREAD)
    (world.root / "docs").mkdir()
    await world.dispatch(message("!bind docs"))
    old = (world.root / "app").resolve()
    new = (world.root / "docs").resolve()
    assert said(world)[-1] == texts.BIND_OK_ELSEWHERE.format(directory=new, old=f"`{old}`")
    await world.sessions.close_all()


async def test_bind_names_every_old_folder_still_in_use(world: World) -> None:
    await _idle_message(world, "hi", ts=THREAD)  # a thread in `app`
    (world.root / "docs").mkdir()
    await world.dispatch(message("!bind docs"))
    await _idle_message(world, "hi", ts=OTHER_THREAD)  # a thread in `docs`
    (world.root / "notes").mkdir()
    await world.dispatch(message("!bind notes"))
    app = (world.root / "app").resolve()
    docs = (world.root / "docs").resolve()
    notes = (world.root / "notes").resolve()
    old = ", ".join(f"`{f}`" for f in sorted((app, docs), key=str))
    assert said(world)[-1] == texts.BIND_OK_ELSEWHERE.format(directory=notes, old=old)
    await world.sessions.close_all()


async def test_bind_stays_plain_with_no_thread_in_another_folder(world: World) -> None:
    (world.root / "docs").mkdir()
    await world.dispatch(message("!bind docs"))
    new = (world.root / "docs").resolve()
    assert said(world) == [texts.BIND_OK.format(directory=new)]


async def test_a_reply_in_an_old_folder_thread_gets_the_notice_once(world: World) -> None:
    await _idle_message(world, "hi", ts=THREAD)  # opens a session in `app`
    (world.root / "docs").mkdir()
    await world.dispatch(message("!bind docs"))
    old = (world.root / "app").resolve()
    new = (world.root / "docs").resolve()
    expected = texts.OLD_THREAD_FOLDER.format(old=old, new=new)
    await world.dispatch(reply("go on", THREAD))
    assert said(world).count(expected) == 1
    await world.dispatch(reply("again", THREAD))
    assert said(world).count(expected) == 1  # a second reply in the same thread says it no more
    await world.sessions.close_all()


async def test_a_failed_old_folder_notice_still_submits_the_prompt(world: World) -> None:
    await _idle_message(world, "hi", ts=THREAD)  # opens a session in `app`
    (world.root / "docs").mkdir()
    await world.dispatch(message("!bind docs"))
    old = (world.root / "app").resolve()
    new = (world.root / "docs").resolve()
    expected = texts.OLD_THREAD_FOLDER.format(old=old, new=new)
    world.slack.responses["chat.postMessage"] = [
        RuntimeError("network down"),
        {"ok": True, "ts": "1790000000.000099"},
    ]
    await world.dispatch(reply("go on", THREAD))
    assert world.clients[-1].queries[-1] == "go on"  # never dropped, despite the failed notice
    world.slack.responses["chat.postMessage"] = {"ok": True}
    await world.dispatch(reply("again", THREAD))
    # Not marked notified on the failed attempt: the next reply tries the notice again (and this
    # one succeeds), so its text was sent twice in total.
    assert said(world).count(expected) == 2
    await world.sessions.close_all()


async def test_a_thread_in_the_current_folder_never_gets_the_notice(world: World) -> None:
    await world.dispatch(message("hi", ts=THREAD))  # opens a session in `app`, still current
    await world.dispatch(reply("go on", THREAD))
    assert not any("Claude Code resumes a session only there" in t for t in said(world))
    await world.sessions.close_all()


def test_long_answers_fit_slack_s_limit() -> None:
    from code_with_slack.approvals import SECTION_LIMIT, answered_blocks

    questions = [{"question": "q" * 900} for _ in range(4)]
    answers: dict[str, str | list[str]] = {"q" * 900: "a" * 300}
    [block] = answered_blocks(questions, answers)
    assert len(block["elements"][0]["text"]) <= SECTION_LIMIT


async def test_an_answer_slack_will_not_record_removes_the_request(world: World) -> None:
    approval_id, pending = world.approvals.open(CHANNEL, FORM_THREAD, "Colour", QUESTIONS)
    pending.message_ts = "1790000000.000009"
    draft = Draft(approval_id, CHANNEL, FORM_THREAD, active=1, picks={0: [1]})
    values = {
        "q1": {"answer": {"type": "checkboxes", "selected_options": [{"value": "0"}]}},
        "o1": {"other": {"type": "plain_text_input", "value": "xl"}},
    }
    world.slack.responses["chat.update"] = {"ok": False, "error": "msg_too_long"}
    await world.dispatch(form_body("view_submission", draft, values))
    assert pending.future.done()
    # Its buttons would no longer work: the request goes, as before this record existed.
    assert [a["ts"] for a in world.slack.calls_to("chat.delete")] == ["1790000000.000009"]


# --- D8: two busy sessions in one folder (Phase 3, task 2) ---


def posted_blocks(world: World, index: int = -1) -> list[dict[str, Any]]:
    return world.slack.calls_to("chat.postMessage")[index]["blocks"]


async def start_a_hold(world: World, *, other_channel: str = CHANNEL) -> None:
    """`other_channel`'s thread at `OTHER_THREAD` never ends; a top-level message at THREAD,
    in the same folder, then holds and asks."""
    if other_channel != CHANNEL:
        world.state.bind(other_channel, world.root / "app")
    await world.dispatch(message("busy elsewhere", ts=OTHER_THREAD, channel=other_channel))
    await world.dispatch(message("hello", ts=THREAD))


async def test_a_busy_session_in_the_same_folder_holds_the_message(world: World) -> None:
    await start_a_hold(world)
    assert len(world.clients) == 1  # held, not sent
    # mrkdwn's own `<url|label>` form (a `section` block, like approvals and the resume picker
    # use for their own buttons), not `thread_link`'s standard-Markdown form.
    link = "<https://example.slack.com/archives/C000CHAN/p1780000000000001|Session>"
    section = posted_blocks(world)[0]
    assert section["type"] == "section"
    assert section["text"] == {"type": "mrkdwn", "text": texts.HOLD_QUESTION.format(link=link)}
    # Crash repair (issue #19): a D8 hold is a request like an approval or a question.
    assert world.state.thread(CHANNEL, THREAD).requests == (world.slack.posted_ts[-1],)


async def test_continue_sends_the_held_message(world: World) -> None:
    await start_a_hold(world)
    question_ts = world.slack.posted_ts[-1]
    hold_id = button_value(posted_blocks(world), HOLD_CONTINUE)
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, THREAD, message_ts=question_ts))
    assert world.clients[-1].queries == ["hello"]
    deleted = [a["ts"] for a in world.slack.calls_to("chat.delete")]
    assert deleted == [question_ts]  # the question's own message
    assert world.state.thread(CHANNEL, THREAD).requests == ()


async def test_cancel_drops_the_message_and_says_so(world: World) -> None:
    await start_a_hold(world)
    question_ts = world.slack.posted_ts[-1]
    hold_id = button_value(posted_blocks(world), HOLD_CANCEL)
    await world.dispatch(click_in(HOLD_CANCEL, hold_id, CHANNEL, THREAD, message_ts=question_ts))
    assert len(world.clients) == 1  # never sent
    assert said(world)[-1] == texts.NOT_SENT
    deleted = [a["ts"] for a in world.slack.calls_to("chat.delete")]
    assert deleted == [question_ts]  # the question, not the `Not sent.` notice
    assert world.state.thread(CHANNEL, THREAD).requests == ()


async def test_no_hold_when_the_other_session_is_idle(world: World) -> None:
    await world.dispatch(message("hi", ts=OTHER_THREAD))
    session = world.sessions.get(CHANNEL, OTHER_THREAD)
    assert session is not None
    world.clients[0].inject(sdk_messages("tools"))
    async with asyncio.timeout(2):
        while not session.idle:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    await world.dispatch(message("hello", ts=THREAD))
    assert world.clients[-1].queries == ["hello"]  # sent at once, no question


async def test_no_hold_when_the_other_session_is_in_another_folder(world: World) -> None:
    (world.root / "other").mkdir()
    world.state.bind(OTHER_CHANNEL, world.root / "other")
    await world.dispatch(message("busy elsewhere", ts=OTHER_THREAD, channel=OTHER_CHANNEL))
    await world.dispatch(message("hello", ts=THREAD))
    assert world.clients[-1].queries == ["hello"]


def _hold_questions(world: World) -> list[dict[str, Any]]:
    prefix = texts.HOLD_QUESTION.split("{")[0]
    return [m for m in world.slack.calls_to("chat.postMessage") if m["text"].startswith(prefix)]


async def test_no_hold_when_the_target_itself_is_busy(world: World) -> None:
    await start_a_hold(world)  # THREAD's own first message is held (it is not busy yet)
    question_ts = world.slack.posted_ts[-1]
    hold_id = button_value(posted_blocks(world), HOLD_CONTINUE)
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, THREAD, message_ts=question_ts))
    await world.dispatch(reply("again", THREAD))  # THREAD is busy now: queues, asks nothing
    assert [c.queries for c in world.clients] == [["busy elsewhere"], ["hello"]]
    assert len(_hold_questions(world)) == 1  # only the first message was ever held


async def test_another_channel_bound_to_the_same_folder_also_holds(world: World) -> None:
    await start_a_hold(world, other_channel=OTHER_CHANNEL)
    assert len(world.clients) == 1
    hold_id = button_value(posted_blocks(world), HOLD_CONTINUE)
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, THREAD))
    assert world.clients[-1].queries == ["hello"]


async def test_a_background_only_session_counts_as_working(world: World) -> None:
    first = split_turns(sdk_messages("background"))[0]
    await world.dispatch(message("start it", ts=OTHER_THREAD))
    world.clients[0].inject(first)
    session = world.sessions.get(CHANNEL, OTHER_THREAD)
    assert session is not None
    async with asyncio.timeout(2):
        while session.busy or not session.running_kinds:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    await world.dispatch(message("hello", ts=THREAD))
    assert len(world.clients) == 1  # held: the background task still counts as working


async def test_a_double_click_finds_no_hold(world: World) -> None:
    await start_a_hold(world)
    hold_id = button_value(posted_blocks(world), HOLD_CONTINUE)
    body = click_in(HOLD_CONTINUE, hold_id, CHANNEL, THREAD)
    await world.dispatch(body)
    await world.dispatch(body)
    assert world.ephemerals()[-1] == texts.HOLD_GONE


@pytest.mark.parametrize("user", [{"id": STRANGER}, {"team_id": OTHER_TEAM}])
async def test_a_click_from_someone_else_is_ignored(world: World, user: dict[str, str]) -> None:
    await start_a_hold(world)
    hold_id = button_value(posted_blocks(world), HOLD_CONTINUE)
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, THREAD, **user))
    assert len(world.clients) == 1
    assert not world.ephemerals()


async def test_a_click_for_another_thread_is_refused(world: World) -> None:
    await start_a_hold(world)
    hold_id = button_value(posted_blocks(world), HOLD_CONTINUE)
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, OTHER_THREAD))
    assert len(world.clients) == 1  # never sent: the click did not match the hold
    assert world.ephemerals()[-1] == texts.HOLD_GONE


async def test_a_click_for_another_channel_is_refused(world: World) -> None:
    await start_a_hold(world)
    hold_id = button_value(posted_blocks(world), HOLD_CONTINUE)
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, OTHER_CHANNEL, THREAD))
    assert len(world.clients) == 1  # never sent: the click did not match the hold
    assert world.ephemerals()[-1] == texts.HOLD_GONE


async def test_a_drain_starting_right_after_continue_does_not_leave_a_stale_raised_hand(
    world: World,
) -> None:
    await start_a_hold(world)
    hold_id = button_value(posted_blocks(world), HOLD_CONTINUE)
    world.sessions.draining = True  # as if a drain's own cancellation pass had just run
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, THREAD))
    assert world.clients[-1].queries == ["busy elsewhere"]  # never sent: no second client
    session = world.sessions.get(CHANNEL, THREAD)
    assert session is not None
    assert session._status.current is None  # restored, not left on ✋: this thread never ran


async def test_a_directory_gone_unavailable_after_continue_reacts_error_not_a_raised_hand(
    world: World,
) -> None:
    await world.dispatch(message("busy elsewhere", ts=OTHER_THREAD))
    await world.dispatch(message("!compact", ts=THREAD))  # a Passthrough: held, `ensure_connected`
    hold_id = button_value(posted_blocks(world), HOLD_CONTINUE)

    async def untrusted(directory: Path) -> bool:
        return False

    world.sessions._deps.workspace_trusted = untrusted
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, THREAD))
    assert world.clients[-1].queries == ["busy elsewhere"]  # never sent: no second client
    session = world.sessions.get(CHANNEL, THREAD)
    assert session is not None
    assert session._status.current is Status.ERROR


async def test_a_generic_connect_error_after_continue_reacts_error_not_a_raised_hand(
    world: World,
) -> None:
    # `submitted`, not a fixed set of exception types: any failure before `submit()` (a
    # logged-out CLI raising something `ensure_connected` does not special-case, most likely)
    # must not leave the hold's ✋ standing forever either.
    await world.dispatch(message("busy elsewhere", ts=OTHER_THREAD))
    await world.dispatch(message("!compact", ts=THREAD))  # a Passthrough: held, `ensure_connected`
    hold_id = button_value(posted_blocks(world), HOLD_CONTINUE)
    world.connect_error = RuntimeError("logged out")
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, THREAD))
    assert world.clients[-1].queries == []  # its own connect failed: nothing was ever sent
    assert world.clients[0].queries == ["busy elsewhere"]  # the other session, unaffected
    session = world.sessions.get(CHANNEL, THREAD)
    assert session is not None
    assert session._status.current is Status.ERROR


async def test_a_session_gone_at_continue_time_reacts_error_not_a_raised_hand(
    world: World,
) -> None:
    # The held session itself closed (its thread's own entry gone too, as a SessionGone close
    # leaves it) in the gap between Continue and `submit()`: the retry's own fresh lookup finds
    # nothing, so `SessionGone` propagates instead of a plain `SessionClosed`.
    await start_a_hold(world)
    hold_id = button_value(posted_blocks(world), HOLD_CONTINUE)
    session = world.sessions.get(CHANNEL, THREAD)
    assert session is not None
    session._closed = True  # as if something else had closed it while the hold was open
    world.state.remove_thread(CHANNEL, THREAD)
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, THREAD))
    assert world.clients[-1].queries == ["busy elsewhere"]  # never sent: no second client
    assert session._status.current is Status.ERROR
    assert texts.SESSION_GONE in world.ephemerals()
    # `_closed` was set directly above, bypassing the real teardown (`cancel_hold` would answer
    # Cancel, not Continue, for a session real `close()` reaches): finished properly here, or
    # the fixture's own `close_all` hangs behind this object's never-fired `done_closing`.
    await session.close()


async def test_a_session_closed_at_continue_time_retries_and_sends_without_reacting_error(
    world: World,
) -> None:
    # The held session closed (its thread's own entry intact, unlike D7's `SessionGone` close)
    # in the gap between Continue and `submit()`: the retry's own fresh lookup rebuilds a live
    # session and sends normally, so the original object's ✋ must not be turned into ❌.
    await start_a_hold(world)
    hold_id = button_value(posted_blocks(world), HOLD_CONTINUE)
    session = world.sessions.get(CHANNEL, THREAD)
    assert session is not None
    session._closed = True  # a D9 idle close, most likely; the thread's own entry survives
    session.done_closing.set()  # what the real teardown this stands in for always fires
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, THREAD))
    fresh = world.sessions.get(CHANNEL, THREAD)
    assert fresh is not None and fresh is not session
    assert world.clients[-1].queries == ["hello"]  # sent, on a freshly rebuilt client
    reacted = {a["name"] for a in world.slack.calls_to("reactions.add")}
    assert Status.ERROR.value not in reacted
    # `_closed` was set directly above, bypassing the real teardown: finished properly here, or
    # the fixture's own `close_all` hangs behind this object's never-cancelled background tasks.
    await session.close()


async def test_stop_in_the_held_thread_cancels_it(world: World) -> None:
    await start_a_hold(world)
    await world.dispatch(reply("!stop", THREAD))
    assert len(world.clients) == 1
    # `!stop` cancelled the hold: `Not sent.` alone, from the waiter, since nothing Claude Code
    # itself was doing stopped (no separate "Nothing is running..." on top of it).
    assert said(world).count(texts.NOT_SENT) == 1
    assert texts.NOTHING_TO_STOP_THREAD not in said(world)
    assert texts.STOPPED_THREAD not in said(world)


async def test_a_top_level_stop_of_the_channel_cancels_the_hold(world: World) -> None:
    await start_a_hold(world)
    await world.dispatch(message("!stop"))
    assert len(world.clients) == 1
    assert texts.NOT_SENT in said(world)


async def test_a_top_level_stop_with_only_a_cancelled_hold_says_nothing_else(
    world: World,
) -> None:
    # The busy session lives in ANOTHER channel here, so this channel's own `!stop` cancels the
    # hold and stops nothing else: `stop_channel`'s own `None` must not read as "nothing
    # stopped" and add a second, contradicting notice on top of `Not sent.`.
    await start_a_hold(world, other_channel=OTHER_CHANNEL)
    await world.dispatch(message("!stop"))
    assert texts.NOT_SENT in said(world)
    assert texts.NOTHING_TO_STOP not in said(world)
    assert texts.STOPPED_CHANNEL not in said(world)


async def test_a_drain_cancels_the_hold(world: World) -> None:
    await start_a_hold(world)
    cut_short = asyncio.Event()
    cut_short.set()  # returns as soon as the per-session cancellation pass is done
    await world.sessions.drain(cut_short)
    await asyncio.sleep(0.05)
    assert len(world.clients) == 1
    assert texts.NOT_SENT in said(world)


async def test_the_idle_close_timer_does_not_fire_while_held(world: World) -> None:
    await start_a_hold(world)
    session = world.sessions.get(CHANNEL, THREAD)
    assert session is not None
    assert session.waiting_for_owner
    names = {t.get_name() for t in asyncio.all_tasks()}
    assert f"idle-close-{CHANNEL}-{THREAD}" not in names  # never armed while held (D9)


async def test_the_raised_hand_shows_while_held_and_clears_on_cancel(world: World) -> None:
    await start_a_hold(world)
    session = world.sessions.get(CHANNEL, THREAD)
    assert session is not None
    assert session._status.current is Status.WAITING
    hold_id = button_value(posted_blocks(world), HOLD_CANCEL)
    await world.dispatch(click_in(HOLD_CANCEL, hold_id, CHANNEL, THREAD))
    assert session._status.current is None  # nothing to go back to: a session that never ran


async def test_the_other_session_finishing_does_not_skip_the_question(world: World) -> None:
    await start_a_hold(world)
    hold_id = button_value(posted_blocks(world), HOLD_CONTINUE)
    other = world.sessions.get(CHANNEL, OTHER_THREAD)
    assert other is not None
    world.clients[0].inject(sdk_messages("tools"))
    async with asyncio.timeout(2):
        while not other.idle:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    assert len(world.clients) == 1  # the hold still waits: nobody re-checked on its own
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, THREAD))
    assert world.clients[-1].queries == ["hello"]


async def test_an_unpostable_question_fails_closed(world: World) -> None:
    real = world.slack.chat_postMessage
    prefix = texts.HOLD_QUESTION.split("{")[0]

    async def flaky(**kwargs: Any) -> Any:
        if str(kwargs.get("text", "")).startswith(prefix):
            raise Exception("boom")
        return await real(**kwargs)

    world.slack.chat_postMessage = flaky  # type: ignore[method-assign]
    await world.dispatch(message("busy elsewhere", ts=OTHER_THREAD))
    await world.dispatch(message("hello", ts=THREAD))
    assert len(world.clients) == 1
    assert world.ephemerals()[-1] == texts.HOLD_UNPOSTED


async def test_a_message_during_a_drain_is_refused_not_held(world: World) -> None:
    await world.dispatch(message("busy elsewhere", ts=OTHER_THREAD))
    cut_short = asyncio.Event()
    drain = asyncio.create_task(world.sessions.drain(cut_short))
    await asyncio.sleep(0.02)  # the drain has set its flags before this message arrives
    await world.dispatch(message("hello", ts=THREAD))
    assert said(world)[-1] == texts.RESTARTING
    assert _hold_questions(world) == []  # never held: a hold opened now would wait forever
    cut_short.set()
    await asyncio.wait_for(drain, 2)


async def test_a_message_queued_behind_a_drain_cancelled_hold_is_also_refused(
    world: World,
) -> None:
    await start_a_hold(world)  # THREAD's own "hello" is held
    queued = asyncio.create_task(world.dispatch(reply("again", THREAD)))
    await asyncio.sleep(0.02)  # "again" now waits behind "hello" for the thread's arrival lock
    cut_short = asyncio.Event()
    cut_short.set()
    await world.sessions.drain(cut_short)  # cancels "hello"'s hold
    await asyncio.wait_for(queued, 2)
    assert len(world.clients) == 1  # neither "hello" nor "again" was ever sent
    assert texts.NOT_SENT in said(world)  # "hello", cancelled by the drain
    assert said(world)[-1] == texts.RESTARTING  # "again", refused once draining had begun


async def test_a_cancelled_wait_does_not_leak_the_hold(world: World) -> None:
    await world.dispatch(message("busy elsewhere", ts=OTHER_THREAD))
    before = set(asyncio.all_tasks())
    await world.dispatch(message("hello", ts=THREAD))  # posts the question, then waits
    new_tasks = set(asyncio.all_tasks()) - before
    assert new_tasks, "expected a task still waiting on the hold's future"
    for task in new_tasks:
        task.cancel()
    await asyncio.sleep(0.05)
    session = world.sessions.get(CHANNEL, THREAD)
    assert session is not None
    assert not session.waiting_for_owner  # `_HOLD_MARKER` did not stay in `_waiting`
    assert world.holds._pending == {}  # no leaked entry


async def test_a_hold_decided_before_its_message_ts_is_known_does_not_flicker(
    world: World,
) -> None:
    await world.dispatch(message("busy elsewhere", ts=OTHER_THREAD))
    real = world.slack.chat_postMessage
    prefix = texts.HOLD_QUESTION.split("{")[0]
    gate = asyncio.Event()

    async def gated(**kwargs: Any) -> Any:
        result = await real(**kwargs)
        if str(kwargs.get("text", "")).startswith(prefix):
            await gate.wait()  # the question is posted, but not yet recorded in `holds`
        return result

    world.slack.chat_postMessage = gated  # type: ignore[method-assign]
    task = asyncio.create_task(world.dispatch(message("hello", ts=THREAD)))
    await asyncio.sleep(0.05)
    world.holds.cancel(CHANNEL, THREAD)  # exactly what `!stop` would do
    gate.set()
    await asyncio.wait_for(task, 2)
    assert texts.NOT_SENT in said(world)
    added = [a["name"] for a in world.slack.calls_to("reactions.add")]
    assert Status.WAITING.value not in added  # hold_start/hold_end never ran: no ✋ flicker


async def test_a_report_turn_during_a_hold_keeps_the_raised_hand(world: World) -> None:
    turns = split_turns(sdk_messages("background"))
    await world.dispatch(message("start it", ts=THREAD))
    target = world.sessions.get(CHANNEL, THREAD)
    assert target is not None
    world.clients[0].inject(turns[0])
    async with asyncio.timeout(2):
        while target.busy or not target.running_kinds:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    # THREAD already has a running task, so OTHER_THREAD's own first message is held too:
    # Continue lets it become genuinely busy (never completes) before the real case below.
    await world.dispatch(message("busy elsewhere", ts=OTHER_THREAD))
    hold_id = button_value(posted_blocks(world), HOLD_CONTINUE)
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, OTHER_THREAD))
    assert len(world.clients) == 2
    await world.dispatch(reply("more", THREAD))  # held: OTHER_THREAD busy, THREAD is not
    assert target.waiting_for_owner
    for turn in turns[1:]:
        world.clients[0].inject(turn)
    await asyncio.sleep(0.2)  # the background task's own report turn runs during the hold
    assert target._status.current is Status.WAITING  # the report turn did not show ⏳ over it


async def test_cancel_after_a_finished_report_turn_shows_done_not_a_stale_reaction(
    world: World,
) -> None:
    turns = split_turns(sdk_messages("background"))
    await world.dispatch(message("start it", ts=THREAD))
    target = world.sessions.get(CHANNEL, THREAD)
    assert target is not None
    world.clients[0].inject(turns[0])
    async with asyncio.timeout(2):
        while target.busy or not target.running_kinds:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    await world.dispatch(message("busy elsewhere", ts=OTHER_THREAD))  # held too: see test above
    hold_id = button_value(posted_blocks(world), HOLD_CONTINUE)
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, OTHER_THREAD))
    assert len(world.clients) == 2
    await world.dispatch(reply("more", THREAD))  # held: OTHER_THREAD busy, THREAD is not
    for turn in turns[1:]:
        world.clients[0].inject(turn)
    async with asyncio.timeout(2):
        while target.running_kinds:  # noqa: ASYNC110  # the background task finishes reporting
            await asyncio.sleep(0.01)
    hold_id = button_value(_hold_questions(world)[-1]["blocks"], HOLD_CANCEL)
    await world.dispatch(click_in(HOLD_CANCEL, hold_id, CHANNEL, THREAD))
    await asyncio.sleep(0.05)
    # Not the stale snapshot from `hold_start` (WAITING, its own ✋): the session actually
    # finished its report turn during the hold, so cancelling now shows done.
    assert target._status.current is Status.DONE
