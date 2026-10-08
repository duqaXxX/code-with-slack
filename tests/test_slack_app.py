import asyncio
import copy
import dataclasses
import itertools
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ResultError, SDKSessionInfo
from slack_bolt.request.async_request import AsyncBoltRequest

from code_with_slack import openfile as openfile_module
from code_with_slack import sessions as sessions_module
from code_with_slack import slack_app as slack_app_module
from code_with_slack import texts
from code_with_slack.approvals import Answer, Approvals, Draft
from code_with_slack.attachments import DownloadFailed
from code_with_slack.config import Config
from code_with_slack.footer import UsageCache
from code_with_slack.guards import ChannelGuard, Identity
from code_with_slack.hold import HOLD_CANCEL, HOLD_CONTINUE, Holds, hold_blocks
from code_with_slack.home import (
    CHANNEL_ACTION,
    CLEAN_ACTION,
    DELETE_ACTION,
    EDIT_ACTION,
    EDIT_OFF,
    EDIT_ON,
    FILTER_ACTIONS,
    FILTERS_BLOCK,
    NEW_THREAD_ACTION,
    SEARCH_ACTION,
    SEARCH_BLOCK,
    SHOW_ALL_ACTION,
    STATUS_ACTION,
    Home,
    HomeFilter,
)
from code_with_slack.openfile import (
    CHOICE_ACTION,
    CHOICE_BLOCK,
    OPEN_BUTTON_ACTION,
    OPEN_FORM,
    QUERY_ACTION,
    QUERY_BLOCK,
    Listings,
    Target,
    modal_view,
)
from code_with_slack.render.sinks import FALLBACK_LIMIT, UpdateLimiter
from code_with_slack.render.status import Status
from code_with_slack.sessions import SessionDeps, SessionManager
from code_with_slack.setup import SETUP_BYPASS, SETUP_EFFORT, SETUP_MODEL, SETUP_START, Choice
from code_with_slack.slack_app import action_key, build_app, slack_unescape
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
    any_repository,
    sdk_messages,
    slack_payload,
    split_turns,
)
from tests.git_layouts import commit_at, committed, git, git_init
from tests.test_sessions import until
from tests.test_setup import controls as setup_controls
from tests.test_setup import state as setup_state

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
        # Presses Start on every session setup that a dispatch leaves waiting, so the tests of
        # everything after the setup keep reading as they did; the setup's own tests turn it off.
        self.auto_start = True
        self._started: set[str] = set()
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
                trusted_repository=any_repository,
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
        self.deleted: list[tuple[str, str]] = []

        async def delete(channel_id: str, thread_ts: str) -> str | None:
            self.deleted.append((channel_id, thread_ts))
            return None

        self.cleaned: list[str] = []

        async def clean(channel_id: str) -> str | None:
            self.cleaned.append(channel_id)
            return None

        self.home = Home(
            slack,
            owner_user_id=OWNER,
            team_id=TEAM,
            state=self.state,
            sessions_of=lambda directory: self.stored_sessions,
            delete=delete,
            clean=clean,
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
            home=self.home,
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
        # Let the listener tasks run. In short waits, not one: a garbage collection can stop the
        # process for longer than the whole wait (57 ms measured over the full suite,
        # 2026-10-01), and one pause must not use it all up.
        for _ in range(5):
            await asyncio.sleep(0.01)
        if self.auto_start:
            await self.start_waiting_setups()
        return response

    def waiting_setups(self) -> list[tuple[str, str, str, str]]:
        """(setup id, channel, thread, message ts) of every setup message that was posted."""
        shown = {
            element["value"]: ts
            for ts, message in self.slack.messages.items()
            if not message.deleted
            for block in message.blocks
            for element in block.get("elements") or []
            if element.get("action_id") == SETUP_START
        }
        found = []
        for post in self.slack.calls_to("chat.postMessage"):
            for block in post.get("blocks") or []:
                for element in block.get("elements") or []:
                    if element.get("action_id") == SETUP_START and element["value"] in shown:
                        found.append(
                            (
                                element["value"],
                                post["channel"],
                                post["thread_ts"],
                                shown[element["value"]],
                            )
                        )
        return found

    async def settle(self, seconds: float) -> None:
        """Wait `seconds`, pressing Start on every setup that shows up meanwhile (a message whose
        download is slow posts its setup late)."""
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        while loop.time() < end:
            await self.start_waiting_setups()
            await asyncio.sleep(0.02)

    async def start_waiting_setups(self) -> None:
        for setup_id, channel, thread_ts, ts in self.waiting_setups():
            if setup_id not in self._started:
                self._started.add(setup_id)
                await self.dispatch(
                    click_in(SETUP_START, setup_id, channel, thread_ts, message_ts=ts)
                )

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
    await made.home.close()


def composed(text: str, **style: bool) -> list[dict[str, Any]]:
    """The blocks Slack's composer sends with a message of one run of text, as the recorded
    events hold them; a style as read back from Slack on 2026-10-07 (`code` for inline code)."""
    leaf: dict[str, Any] = {"type": "text", "text": text, **({"style": style} if style else {})}
    section = {"type": "rich_text_section", "elements": [leaf]}
    return [{"type": "rich_text", "block_id": "pHDTI", "elements": [section]}]


def message(text: str = "hello", **event: Any) -> dict[str, Any]:
    """A top-level message: its `thread_ts` defaults to its own `ts` (a fresh fixture ts unless
    `ts=` is given), so each call with a distinct `ts` opens an independent thread."""
    body = recorded("event_callback-message")
    body["event"].update({"text": text, "blocks": composed(text), **event})
    return body


_REPLY_SEQ = itertools.count(1)


def reply(text: str, thread_ts: str, **event: Any) -> dict[str, Any]:
    """A reply inside `thread_ts`'s thread: its own `ts` always differs from it."""
    body = recorded("event_callback-message")
    own_ts = f"179019{next(_REPLY_SEQ):04d}.900000"
    body["event"].update(
        {"text": text, "blocks": composed(text), "thread_ts": thread_ts, "ts": own_ts, **event}
    )
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


async def test_a_reply_in_a_thread_being_deleted_starts_nothing(world: World) -> None:
    # The thread still has its entry while its messages are deleted: a reply sent then must not
    # rebuild a session from it (the delete would leave it running on a thread that is gone).
    world.state.open_thread(CHANNEL, THREAD, session_id="68da9311-0000-4000-8000-00000000beef")
    assert await world.sessions.release(CHANNEL, THREAD)
    await world.dispatch(reply("one more thing", THREAD))
    assert world.queries() == [] and world.clients == []
    assert world.ephemerals() == [texts.NOT_A_SESSION]
    # The delete failed and let the thread go: it takes a prompt again.
    world.sessions.free(CHANNEL, THREAD)
    await world.dispatch(reply("one more thing", THREAD))
    await until(lambda: world.queries() == ["one more thing"])


async def test_a_reply_after_a_gone_session_starts_nothing(world: World) -> None:
    # D7 removes the thread's entry with its session, so the next reply finds a thread that
    # holds no session: it is refused, never treated as a fresh top-level message.
    world.state.open_thread(CHANNEL, THREAD, session_id="68da9311-0000-4000-8000-00000000dead")
    world.connect_error = ResultError("transcript missing")
    await world.dispatch(reply("hello", THREAD))
    assert world.state.thread(CHANNEL, THREAD) is None
    world.connect_error = None
    await world.dispatch(reply("hello again", THREAD))
    assert world.queries() == []
    assert world.ephemerals()[-1] == texts.NOT_A_SESSION


def test_the_thread_refusals_send_the_owner_to_the_channel() -> None:
    # Both are shown inside a thread, where `!bind` is refused and a reply meets the same
    # refusal again: each has to say that its way out is typed in the channel (issue #78).
    assert "in the channel, bind another folder with `!bind <path>`" in texts.DIRECTORY_MISSING
    assert "In the channel, send a new message" in texts.NOT_A_SESSION
    assert "`!resume`" in texts.NOT_A_SESSION
    # No cause: the daemon cannot tell a thread that never held a session from one whose
    # entry it dropped while Claude Code still has the session.
    assert "deleted" not in texts.NOT_A_SESSION


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
    assert said(world) == [texts.UNBOUND.format(root=world.root.resolve())]
    assert "thread_ts" not in world.slack.calls_to("chat.postMessage")[0]


async def test_bang_runs_a_known_command(world: World) -> None:
    await world.dispatch(message("!compact"))
    assert world.queries() == ["/compact"]


async def test_bang_leaves_other_text_alone(world: World) -> None:
    await world.dispatch(message("!important: read the notes"))
    assert world.queries() == ["!important: read the notes"]


async def test_slack_escapes_are_undone() -> None:
    assert slack_unescape("a &lt;b&gt; &amp;&amp; c") == "a <b> && c"


def reactions_on(world: World, ts: str) -> list[str]:
    """The reactions the daemon added to the message at `ts`, in order."""
    return [a["name"] for a in world.slack.calls_to("reactions.add") if a["timestamp"] == ts]


def said(world: World) -> list[str]:
    """What the bot posted in a thread (not ephemeral), in order; a session's setup message
    apart, which the setup's own tests read."""
    posts = world.slack.calls_to("chat.postMessage")
    return [a["text"] for a in posts if a["text"] != texts.SETUP_FALLBACK]


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
    assert world.ephemerals() == [texts.WORD_IN_THREAD.format(word="bind")]
    assert world.state.channel(CHANNEL).directory == (world.root / "app").resolve()


async def test_bang_resume_is_refused_inside_a_thread(world: World) -> None:
    await world.dispatch(message("hi", ts=THREAD))
    await world.dispatch(reply("!resume", THREAD))
    assert world.ephemerals() == [texts.WORD_IN_THREAD.format(word="resume")]


async def test_bang_bypass_is_refused_at_top_level(world: World) -> None:
    await world.dispatch(message("!bypass on"))
    assert said(world) == [texts.BYPASS_TOP_LEVEL]
    assert "thread_ts" not in world.slack.calls_to("chat.postMessage")[0]
    assert world.clients == []  # no session was even started


async def test_bang_clear_is_refused_inside_a_thread(world: World) -> None:
    await world.dispatch(message("hi", ts=THREAD))
    await world.dispatch(reply("!clear", THREAD))
    assert world.ephemerals() == [texts.CLEAR_IN_THREAD]
    assert world.queries() == ["hi"]  # the earlier prompt is the only one that reached Claude


@pytest.mark.parametrize("word", ["!reset", "!new", "!NEW keep this"])
async def test_an_alias_of_clear_is_refused_inside_a_thread(world: World, word: str) -> None:
    # `/reset` and `/new` are `/clear` under other names: commands reference and the recorded
    # list (SDK 0.2.163) both give them as its aliases.
    await world.dispatch(message("hi", ts=THREAD))
    await world.dispatch(reply(word, THREAD))
    assert world.ephemerals() == [texts.CLEAR_IN_THREAD]
    assert world.queries() == ["hi"]


@pytest.mark.parametrize(
    ("word", "answer"),
    [
        ("!login", texts.LOGIN_ON_HOST),
        ("!LOGIN now", texts.LOGIN_ON_HOST),
        ("!logout", texts.LOGOUT_ON_HOST),
    ],
)
async def test_login_and_logout_never_reach_claude_code_from_the_channel(
    world: World, word: str, answer: str
) -> None:
    # They act on the host's own login, which the daemon and every session run on.
    await world.dispatch(message(word))
    assert said(world) == [answer]
    assert "thread_ts" not in world.slack.calls_to("chat.postMessage")[0]
    assert world.clients == []  # no session was even started


@pytest.mark.parametrize(
    ("word", "answer"),
    [("!login", texts.LOGIN_ON_HOST), ("!logout", texts.LOGOUT_ON_HOST)],
)
async def test_login_and_logout_never_reach_claude_code_from_a_thread(
    world: World, word: str, answer: str
) -> None:
    await world.dispatch(message("hi", ts=THREAD))
    await world.dispatch(reply(word, THREAD))
    assert world.ephemerals() == [answer]
    assert world.queries() == ["hi"]


async def test_bang_clear_at_top_level_opens_a_session_like_any_other_word(world: World) -> None:
    await world.dispatch(message("!clear"))
    assert world.queries() == ["/clear"]


async def test_a_daemon_word_in_a_dead_thread_acts_as_top_level(world: World) -> None:
    # A reply inside the thread of an old request (never a session) still gets an answer there,
    # exactly as a top-level `!bind` would: the channel's own folder ("app") shows as current.
    await world.dispatch(reply("!bind", CLICK_THREAD))
    (post,) = world.slack.calls_to("chat.postMessage")
    assert "thread_ts" not in post  # answered in the channel, like a top-level `!bind`
    rows = [b["text"]["text"] for b in post["blocks"] if b.get("type") == "section"]
    assert any(row.startswith("`app`") and texts.BIND_CURRENT in row for row in rows)
    values = [b["accessory"]["value"] for b in post["blocks"] if "accessory" in b]
    assert values == ["."]  # the current folder ("app") shows with no button


async def test_bang_help_lists_the_session_commands(world: World) -> None:
    await world.dispatch(message("hi", ts=THREAD))
    await world.dispatch(reply("!help", THREAD))
    (text,) = world.ephemerals()  # inside a session's thread the owner alone sees the answer
    assert "`!compact" in text and "`!bypass" in text
    (asked,) = world.slack.calls_to("chat.postEphemeral")
    assert asked["thread_ts"] == THREAD and asked["user"] == OWNER


async def test_bang_help_top_level_lists_only_the_daemon_words(world: World) -> None:
    await world.dispatch(message("!help", ts=THREAD))
    (text,) = said(world)
    assert "`!bypass" in text and "`!compact" not in text
    assert texts.HELP_UNBOUND in text
    assert world.clients == []
    # Typed in the channel, the answer is a normal top-level post that survives a reload.
    assert "thread_ts" not in world.slack.calls_to("chat.postMessage")[0]
    assert not world.ephemerals()


async def test_bang_help_with_a_filter_lists_only_matches(world: World) -> None:
    await world.dispatch(message("hi", ts=THREAD))
    await world.dispatch(reply("!help compact", THREAD))
    (text,) = world.ephemerals()
    assert "`!compact" in text and "`!bypass" not in text


async def test_bang_help_works_in_an_unbound_channel(slack: FakeSlack, tmp_path: Path) -> None:
    world = World(slack, tmp_path, bound=False)
    await world.dispatch(message("!help"))
    (text,) = said(world)
    assert "`!bind" in text
    assert world.clients == []


async def test_bang_bypass_on_inside_a_thread_switches_the_live_client(world: World) -> None:
    await world.dispatch(message("hi", ts=THREAD))
    word = reply("!bypass on", THREAD)
    await world.dispatch(word)
    assert world.clients[0].modes == ["bypassPermissions"]
    # The answer says what changed, to the owner alone and under their word, and a ✅ stays on
    # the word after a reload takes the line away. Neither rings.
    assert reactions_on(world, word["event"]["ts"]) == ["white_check_mark"]
    (answer,) = world.slack.calls_to("chat.postEphemeral")
    assert answer["text"] == texts.BYPASS_ON_THREAD and answer["thread_ts"] == THREAD
    # No placeholder for the session's reply: nothing is posted until Claude has something to say.
    assert said(world) == []


async def test_bang_bypass_off_inside_a_thread_says_so_too(world: World) -> None:
    await world.dispatch(message("hi", ts=THREAD))
    await world.dispatch(reply("!bypass on", THREAD))
    word = reply("!bypass off", THREAD)
    await world.dispatch(word)
    assert world.clients[0].modes == ["bypassPermissions", "default"]
    assert reactions_on(world, word["event"]["ts"]) == ["white_check_mark"]
    assert world.ephemerals() == [texts.BYPASS_ON_THREAD, texts.BYPASS_OFF_THREAD]
    assert said(world) == []
    assert world.state.thread(CHANNEL, THREAD).bypass is False


async def test_a_notice_is_small_and_grey_a_reference_full_size(world: World) -> None:
    # The daemon's notices read apart from Claude's replies, as the footer does (the owner,
    # 2026-09-27); `!help`, `!guide` and `!status` stay full size, since they are read.
    await world.dispatch(message("hi", ts=THREAD))
    await world.dispatch(reply("!bind", THREAD))
    await world.dispatch(reply("!help", THREAD))
    notice, reference = world.slack.calls_to("chat.postEphemeral")
    refusal = texts.WORD_IN_THREAD.format(word="bind")
    assert notice["blocks"] == [
        {"type": "context", "elements": [{"type": "mrkdwn", "text": refusal}]}
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
    word = reply("!bypass on", THREAD)
    await world.dispatch(word)
    assert world.ephemerals() == [texts.SESSION_GONE]
    assert reactions_on(world, word["event"]["ts"]) == []  # nothing took effect: no ✅
    assert world.state.thread(CHANNEL, THREAD) is None


async def test_a_word_in_code_formatting_is_still_a_word(world: World) -> None:
    """Pasted from where it was shown as code, a word keeps the formatting: the event's text
    starts with a backtick, the composer's blocks hold the word (issue #178)."""
    await world.dispatch(message("`!stop`", blocks=composed("!stop", code=True)))
    assert said(world) == [texts.NOTHING_TO_STOP]
    assert world.queries() == []


async def test_a_command_in_code_formatting_runs_as_the_command(world: World) -> None:
    await world.dispatch(message("`!compact`", blocks=composed("!compact", code=True)))
    assert world.queries() == ["/compact"]


async def test_anything_before_the_bang_keeps_a_formatted_message_a_prompt(world: World) -> None:
    """The escape the setup doc gives, where the two readings differ: the text opens with a
    mark, the composer's run with the backslash."""
    await world.dispatch(message("`\\!stop`", blocks=composed("\\!stop", code=True)))
    assert world.queries() == ["`\\!stop`"]
    assert said(world) == []


async def test_a_formatted_command_keeps_its_arguments_as_sent(world: World) -> None:
    blocks = composed("!compact", code=True)
    blocks[0]["elements"][0]["elements"] += [
        {"type": "text", "text": " keep "},
        {"type": "text", "text": "the plan", "style": {"code": True}},
    ]
    await world.dispatch(message("`!compact` keep `the plan`", blocks=blocks))
    assert world.queries() == ["/compact keep `the plan`"]


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


async def test_bang_status_names_an_idle_session_by_its_title_and_last_activity(
    world: World,
) -> None:
    # Issue #76: five rows reading `Session: idle` told the owner nothing about which was which.
    two_hours_ago = int((time.time() - 7_200) * 1000)
    world.stored_sessions = [SDKSessionInfo(SESSION_A, "Fix the footer", two_hours_ago, 1)]
    await world.dispatch(message(f"!resume {SESSION_A}"))  # a live, idle session with that id
    await world.dispatch(message("!status", ts=OTHER_THREAD))
    assert said(world)[-1].splitlines()[1] == f"[Fix the footer]({PERMALINK}): idle · 2h ago"


async def test_bang_status_dates_an_idle_session_by_its_last_message_not_its_file(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seen live on 2026-10-07: a session idle for 16 minutes read `idle · just now`. Its file had
    # just been written to, by the entries with no timestamp Claude Code appends when a session
    # is connected again, so the file's time says nothing of when the owner last heard from it.
    now_ms = int(time.time() * 1000)
    other = "68da9311-0000-4000-8000-00000000000b"
    world.stored_sessions = [
        SDKSessionInfo(SESSION_A, "Fix the footer", now_ms, 1),  # its file: touched just now
        SDKSessionInfo(other, "Another session of the folder", now_ms, 1),  # no live thread
    ]
    asked: list[list[str]] = []

    def last_messages(directory: Path, found: list[SDKSessionInfo]) -> list[SDKSessionInfo]:
        asked.append([s.session_id for s in found])
        return [dataclasses.replace(s, last_modified=now_ms - 16 * 60_000) for s in found]

    monkeypatch.setattr(sessions_module, "by_last_activity", last_messages)
    await world.dispatch(message(f"!resume {SESSION_A}"))
    asked.clear()
    await world.dispatch(message("!status", ts=OTHER_THREAD))
    assert said(world)[-1].splitlines()[1] == f"[Fix the footer]({PERMALINK}): idle · 16m ago"
    assert asked == [[SESSION_A]]  # only the sessions the answer shows are read


async def test_bang_status_keeps_a_title_s_own_brackets_out_of_the_link(world: World) -> None:
    world.stored_sessions = [SDKSessionInfo(SESSION_A, "fix [urgent](x) *now*", 0, 1)]
    await world.dispatch(message(f"!resume {SESSION_A}"))
    await world.dispatch(message("!status", ts=OTHER_THREAD))
    row = said(world)[-1].splitlines()[1]
    assert row.startswith(rf"[fix \[urgent\]\(x\) \*now\*]({PERMALINK}): idle")


async def test_bang_status_falls_back_to_session_when_the_titles_cannot_be_read(
    world: World,
) -> None:
    world.stored_sessions = [SDKSessionInfo(SESSION_A, "Fix the footer", 0, 1)]
    await world.dispatch(message(f"!resume {SESSION_A}"))

    def broken(directory: Path) -> list[SDKSessionInfo]:
        raise OSError("unreadable")

    world.sessions._deps.sessions_of = broken
    await world.dispatch(message("!status", ts=OTHER_THREAD))
    assert said(world)[-1].splitlines()[1] == f"[Session]({PERMALINK}): idle"  # no title, no time


@pytest.mark.parametrize(
    ("seconds", "shown"),
    [
        (5, "just now"),
        (59, "just now"),
        (60, "1m ago"),
        (3_599, "59m ago"),
        (3_600, "1h ago"),
        (86_399, "23h ago"),
        (86_400, "1d ago"),
        (30 * 86_400, "30d ago"),
        (-30, "just now"),
    ],
)
def test_how_long_ago_reads_in_one_unit(seconds: int, shown: str) -> None:
    assert slack_app_module.ago(seconds) == shown


def test_the_hold_question_s_buttons_answer_it_in_its_own_words() -> None:
    # Issue #76: `Continue` beside the shorter `Cancel` made the second look the lesser choice,
    # and a button's width is its text. One `primary` in the set, as the button reference asks.
    _, actions = hold_blocks("hold-1", "<https://example.slack.com/x|Session>")
    send, keep = actions["elements"]
    assert (send["text"]["text"], send.get("style")) == ("Send anyway", "primary")
    assert (keep["text"]["text"], keep.get("style")) == ("Don't send", None)
    assert abs(len(send["text"]["text"]) - len(keep["text"]["text"])) <= 1


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
    (text,) = world.ephemerals()
    assert text.startswith("Directory:")
    assert "\nContext: `7%`" in text


async def test_an_answer_in_a_session_thread_sets_its_status_line_again(world: World) -> None:
    await world.dispatch(message("hi", ts=THREAD))
    told: list[tuple[str, str]] = []
    world.sessions.wrote = lambda channel, thread: told.append((channel, thread))  # type: ignore[method-assign]
    await world.dispatch(reply("!status", THREAD))
    assert told == [(CHANNEL, THREAD)]  # the answer cleared the status: it is set again
    await world.dispatch(message("!status"))  # top-level: no thread, no status line
    assert told == [(CHANNEL, THREAD)]


async def test_bang_stop_inside_a_thread_stops_only_that_session(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fake client's turn never ends, so the answer goes out once its wait is over.
    monkeypatch.setattr(sessions_module, "STOP_TAIL_WAIT", 0.05)
    await world.dispatch(message("hello", ts=THREAD))  # never ends
    await world.dispatch(message("hello", ts=OTHER_THREAD))  # D8: THREAD's session is busy
    hold_id = button_value(world.slack.calls_to("chat.postMessage")[-1]["blocks"], HOLD_CONTINUE)
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, OTHER_THREAD))
    posted = len(world.slack.calls_to("chat.postMessage"))
    await world.dispatch(reply("!stop", THREAD))
    # The answer follows the wait above, which lasts as long as `dispatch` lets listeners run:
    # the test waits for the post itself, not for that time to have been enough.
    await until(lambda: len(world.slack.calls_to("chat.postMessage")) > posted)
    # One line that stays in the thread: an ephemeral one is gone on reload (issue #85).
    (answer,) = world.slack.calls_to("chat.postMessage")[posted:]
    assert answer["text"] == texts.STOPPED_THREAD and answer["thread_ts"] == THREAD
    assert not world.ephemerals()
    assert world.clients[0].interrupts == 1
    assert world.clients[1].interrupts == 0


async def test_bang_stop_inside_an_idle_thread_says_nothing_is_running(world: World) -> None:
    await _idle_message(world, "hi", ts=THREAD)
    await world.dispatch(reply("!stop", THREAD))
    # A post in the thread, which stays: the owner can tell the stop was received (issue #85).
    answer = world.slack.calls_to("chat.postMessage")[-1]
    assert answer["text"] == texts.NOTHING_TO_STOP_THREAD and answer["thread_ts"] == THREAD
    assert not world.ephemerals()


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
    (text,) = world.ephemerals()
    assert "`!bypass" in text and "`!compact" in text
    assert texts.HELP_UNBOUND not in text


async def test_bang_from_anyone_else_does_nothing(world: World) -> None:
    await world.dispatch(message("!bypass on", user=STRANGER))
    assert world.clients == [] and not world.posted_anything()


def home_action(action: dict[str, Any], user: str = OWNER, **values: Any) -> dict[str, Any]:
    """A use of a Home tab control: no channel and no message, a `view` container, and the state
    of the page's controls in `view.state.values` (the block_actions payload reference's Home
    tab example, docs.slack.dev, read 2026-10-01)."""
    return {
        "type": "block_actions",
        "team": {"id": TEAM, "domain": "example"},
        "user": {"id": user, "username": "alice", "name": "alice", "team_id": TEAM},
        "api_app_id": "A000APP",
        "container": {"type": "view", "view_id": "V000HOME"},
        "trigger_id": "1.2.abc",
        "view": {
            "id": "V000HOME",
            "team_id": TEAM,
            "type": "home",
            "blocks": [],
            "state": {"values": values},
        },
        "actions": [{"block_id": "b1", "action_ts": "1790000000.000001", **action}],
    }


def chosen_option(value: str) -> dict[str, Any]:
    # A static_select's state, as 001-block_actions.json records its action.
    option = {"text": {"type": "plain_text", "text": value, "emoji": True}, "value": value}
    return {"type": "static_select", "selected_option": option}


@pytest.mark.parametrize("user", [OWNER, STRANGER])
async def test_a_click_on_new_thread_is_acknowledged_and_does_nothing(
    world: World, user: str
) -> None:
    # A link button still sends its click to the app, which must acknowledge it (button element
    # reference, read 2026-10-01); Slack itself opens the link.
    action = {"type": "button", "action_id": NEW_THREAD_ACTION}
    response = await world.dispatch(home_action(action, user))
    assert response.status == 200
    assert world.clients == [] and world.slack.calls == []


@pytest.mark.parametrize("action_id", FILTER_ACTIONS)
async def test_a_home_filter_is_read_from_the_pages_state_and_published(
    world: World, action_id: str
) -> None:
    # Whichever of the four controls was used, the payload carries the state of them all.
    values = {
        FILTERS_BLOCK: {
            CHANNEL_ACTION: chosen_option(CHANNEL),
            STATUS_ACTION: chosen_option("raised_hand"),
        },
        SEARCH_BLOCK: {SEARCH_ACTION: {"type": "plain_text_input", "value": "Footer"}},
    }
    action = {"type": "static_select", "action_id": action_id, **chosen_option("raised_hand")}
    response = await world.dispatch(home_action(action, **values))
    assert response.status == 200
    chosen = HomeFilter(channel=CHANNEL, status="raised_hand", search="Footer")
    await until(lambda: world.home.chosen == chosen and bool(world.slack.calls_to("views.publish")))
    assert {call["user_id"] for call in world.slack.calls_to("views.publish")} == {OWNER}


async def test_show_all_chooses_that_channel(world: World) -> None:
    action = {"type": "button", "action_id": SHOW_ALL_ACTION, "value": CHANNEL}
    await world.dispatch(home_action(action))
    await until(lambda: world.home.chosen == HomeFilter(channel=CHANNEL))
    # A channel that is not bound is no filter: the value of a click is untrusted.
    await world.dispatch(home_action({**action, "value": "C000NOPE"}))
    await until(lambda: world.home.chosen == HomeFilter())


@pytest.mark.parametrize(("user", "team"), [(STRANGER, TEAM), (OWNER, OTHER_TEAM)])
async def test_a_home_control_used_by_anyone_else_changes_nothing(
    world: World, user: str, team: str
) -> None:
    values = {FILTERS_BLOCK: {STATUS_ACTION: chosen_option("raised_hand")}}
    for action in (
        {"type": "static_select", "action_id": STATUS_ACTION, **chosen_option("raised_hand")},
        {"type": "button", "action_id": SHOW_ALL_ACTION, "value": CHANNEL},
    ):
        body = home_action(action, user, **values)
        body["team"]["id"] = team
        response = await world.dispatch(body)
        assert response.status == 200
    assert world.home.chosen == HomeFilter()
    assert world.slack.calls_to("views.publish") == []


HOME_THREAD = "1790000000.000001"


async def test_edit_and_delete_reach_the_home(world: World) -> None:
    edit = {"type": "button", "action_id": EDIT_ACTION, "value": EDIT_ON}
    delete = {"type": "button", "action_id": DELETE_ACTION, "value": f"{CHANNEL}:{HOME_THREAD}"}
    # Out of edit mode a Delete click is one the page did not offer.
    await world.dispatch(home_action(delete))
    await world.dispatch(home_action(edit))
    await until(lambda: bool(world.slack.calls_to("views.publish")))
    assert world.deleted == []
    await world.dispatch(home_action(delete))
    await until(lambda: world.deleted == [(CHANNEL, HOME_THREAD)])
    clean = {"type": "button", "action_id": CLEAN_ACTION, "value": CHANNEL}
    await world.dispatch(home_action(clean))
    await until(lambda: world.cleaned == [CHANNEL])
    await world.dispatch(home_action({**edit, "value": EDIT_OFF}))
    await world.dispatch(home_action(delete))
    await world.dispatch(home_action(clean))
    await asyncio.sleep(0.05)
    assert world.deleted == [(CHANNEL, HOME_THREAD)] and world.cleaned == [CHANNEL]


@pytest.mark.parametrize(("user", "team"), [(STRANGER, TEAM), (OWNER, OTHER_TEAM)])
async def test_edit_and_delete_from_anyone_else_do_nothing(
    world: World, user: str, team: str
) -> None:
    await world.home.edit(True)
    published = len(world.slack.calls_to("views.publish"))
    for action in (
        {"type": "button", "action_id": EDIT_ACTION, "value": EDIT_OFF},
        {"type": "button", "action_id": DELETE_ACTION, "value": f"{CHANNEL}:{HOME_THREAD}"},
        {"type": "button", "action_id": CLEAN_ACTION, "value": CHANNEL},
    ):
        body = home_action(action, user)
        body["team"]["id"] = team
        assert (await world.dispatch(body)).status == 200
    await asyncio.sleep(0.05)
    assert world.deleted == [] and world.cleaned == []
    assert len(world.slack.calls_to("views.publish")) == published  # still in edit mode


def click(action_id: str, value: str, **user: Any) -> dict[str, Any]:
    body = recorded("block_actions")
    body["actions"] = [{**body["actions"][0], "action_id": action_id, "value": value}]
    body["user"].update(user)
    return body


def resume_click(session_id: str, thread_ts: str = THREAD, **user: Any) -> dict[str, Any]:
    """A click on a Resume button of a picker posted at top level: the value names the session and
    the thread of the owner's `!resume` message (`thread_ts`); the click itself sits at the
    picker's own ts (CLICK_THREAD)."""
    return click("session_resume", f"{session_id}@{thread_ts}", **user)


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


async def test_a_complete_submit_answers_claude_and_leaves_the_request_to_the_session(
    world: World,
) -> None:
    approval_id, pending = world.approvals.open(CHANNEL, FORM_THREAD, "Colour", QUESTIONS)
    pending.message_ts = "1790000000.000009"
    draft = Draft(approval_id, CHANNEL, FORM_THREAD, active=1, picks={0: [1]})
    values = {
        "q1": {"answer": {"type": "checkboxes", "selected_options": [{"value": "0"}]}},
        "o1": {"other": {"type": "plain_text_input", "value": "xl"}},
    }
    await world.dispatch(form_body("view_submission", draft, values))
    assert pending.future.result() == Answer({"Colour?": "blue", "Sizes?": ["s", "xl"]})
    # The session that asked decides what becomes of the request (`_keep_answers`): its reply
    # keeps the answers and the request goes, or the request itself becomes the record.
    assert world.slack.calls_to("chat.delete") == []
    assert world.slack.calls_to("chat.update") == []


@pytest.mark.parametrize("user", [{"id": STRANGER}, {"team_id": OTHER_TEAM}])
async def test_nobody_else_can_submit_the_form(world: World, user: dict[str, str]) -> None:
    approval_id, pending = world.approvals.open(CHANNEL, FORM_THREAD, "Colour", QUESTIONS)
    draft = Draft(approval_id, CHANNEL, FORM_THREAD, picks={0: [0], 1: [0]})
    await world.dispatch(form_body("view_submission", draft, {}, **user))
    assert not pending.future.done()


async def test_a_bypass_inside_a_thread_fails_when_the_directory_is_gone(world: World) -> None:
    # A session that ran before: one that never did answers the word without connecting.
    world.state.open_thread(CHANNEL, THREAD, session_id="68da9311-0000-4000-8000-00000000beef")
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
    await world.dispatch(message("!resume", ts=THREAD))
    (post,) = world.slack.calls_to("chat.postMessage")
    values = [b["accessory"]["value"] for b in post["blocks"] if "accessory" in b]
    # The list is a top-level post; each button names the thread of the owner's `!resume`.
    assert values == [f"{SESSION_A}@{THREAD}", f"{SESSION_B}@{THREAD}"]
    assert "thread_ts" not in post and not world.ephemerals()
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
    assert world.slack.calls_to("chat.postMessage")[-1]["thread_ts"] == OTHER_THREAD


async def test_bang_resume_of_an_unknown_session_says_so(world: World) -> None:
    two_sessions(world)
    await world.dispatch(message("!resume nothing-like-it"))
    assert "`nothing-like-it`" in said(world)[-1]
    assert "thread_ts" not in world.slack.calls_to("chat.postMessage")[-1]
    assert not world.ephemerals()
    assert world.state.channel(CHANNEL).threads == {}


async def test_the_owner_resumes_from_the_list(world: World) -> None:
    two_sessions(world)
    body = resume_click(SESSION_B, THREAD)
    await world.dispatch(body)
    # The session lives in the thread of the owner's `!resume` message, not the picker's.
    assert world.state.thread(CHANNEL, THREAD).session_id == SESSION_B
    assert world.state.thread(CHANNEL, CLICK_THREAD) is None
    (resumed,) = world.slack.calls_to("chat.postMessage")
    assert resumed["thread_ts"] == THREAD and "Trust gate" in resumed["text"]
    # The picker has done its job: it is deleted, and the thread holds the one record (#70).
    (deleted,) = world.slack.calls_to("chat.delete")
    assert (deleted["channel"], deleted["ts"]) == (CHANNEL, body["message"]["ts"])
    assert world.slack.calls_to("chat.update") == [] and not world.ephemerals()


async def test_a_list_that_cannot_be_deleted_is_rewritten_without_its_buttons(
    world: World,
) -> None:
    two_sessions(world)
    world.slack.responses["chat.delete"] = RuntimeError("network down")
    body = resume_click(SESSION_B, THREAD)
    await world.dispatch(body)
    assert world.state.thread(CHANNEL, THREAD).session_id == SESSION_B
    (edited,) = world.slack.calls_to("chat.update")
    assert edited["ts"] == body["message"]["ts"]
    assert "Trust gate" in edited["text"] and PERMALINK in edited["text"]
    assert all("accessory" not in b for b in edited["blocks"])


async def test_a_resume_click_with_a_malformed_thread_changes_nothing(world: World) -> None:
    two_sessions(world)
    for value in (SESSION_B, f"{SESSION_B}@", f"{SESSION_B}@not-a-ts", f"@{THREAD}"):
        await world.dispatch(click("session_resume", value))
    assert world.state.channel(CHANNEL).threads == {}
    # A bare session id is a list posted before the value carried its thread.
    assert said(world) == [texts.RESUME_STALE] * 4


@pytest.mark.parametrize("user", [{"id": STRANGER}, {"team_id": OTHER_TEAM}])
async def test_nobody_else_can_resume(world: World, user: dict[str, str]) -> None:
    two_sessions(world)
    await world.dispatch(resume_click(SESSION_B, **user))
    assert world.state.channel(CHANNEL).threads == {}


async def test_a_resume_click_on_a_thread_stored_but_not_live_is_refused(world: World) -> None:
    # An idle close or a restart evicts the live object, not the entry: the click must not
    # report a resume that `open_thread` would silently not perform.
    two_sessions(world)
    world.state.open_thread(CHANNEL, THREAD, session_id=SESSION_A)
    await world.dispatch(resume_click(SESSION_B, THREAD))
    assert world.state.thread(CHANNEL, THREAD).session_id == SESSION_A
    assert said(world) == [texts.RESUME_HELD]
    assert world.slack.calls_to("chat.update") == []


async def test_a_typed_resume_in_a_non_session_thread_names_that_thread(world: World) -> None:
    two_sessions(world)
    await world.dispatch(reply("!resume", OTHER_THREAD))
    (post,) = world.slack.calls_to("chat.postMessage")
    values = [b["accessory"]["value"] for b in post["blocks"] if "accessory" in b]
    assert values == [f"{SESSION_A}@{OTHER_THREAD}", f"{SESSION_B}@{OTHER_THREAD}"]
    assert "thread_ts" not in post


async def test_a_button_is_never_trusted_for_a_session_of_another_directory(world: World) -> None:
    two_sessions(world)
    await world.dispatch(resume_click("68da9311-0000-4000-8000-0000000000ff"))
    assert world.state.channel(CHANNEL).threads == {}
    assert said(world) == [texts.RESUME_GONE] and not world.ephemerals()


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
    assert texts.RESUME_ELSEWHERE.format(link=link) in said(world)
    assert not world.ephemerals()


async def test_a_resume_click_of_a_session_held_by_another_thread_is_refused(world: World) -> None:
    two_sessions(world)
    await world.dispatch(message(f"!resume {SESSION_B}", ts=OTHER_THREAD))
    await world.dispatch(resume_click(SESSION_B, THREAD))  # a different thread
    assert world.state.thread(CHANNEL, THREAD) is None
    link = f"<{PERMALINK}|Session>"
    assert texts.RESUME_ELSEWHERE.format(link=link) in said(world)
    assert world.slack.calls_to("chat.update") == []  # the picker is left as it was


async def test_a_held_session_s_link_falls_back_when_the_permalink_fails(world: World) -> None:
    two_sessions(world)
    await world.dispatch(message(f"!resume {SESSION_B}", ts=OTHER_THREAD))
    world.slack.responses["chat.getPermalink"] = RuntimeError("network down")
    await world.dispatch(message(f"!resume {SESSION_B}", ts=THREAD))
    assert world.state.thread(CHANNEL, THREAD) is None
    fallback = texts.STATUS_CHANNEL_LINK_FALLBACK.format(thread_ts=OTHER_THREAD)
    assert texts.RESUME_ELSEWHERE.format(link=fallback) in said(world)


async def test_the_list_counts_a_session_held_elsewhere_and_gives_it_no_row(world: World) -> None:
    two_sessions(world)
    await world.dispatch(message(f"!resume {SESSION_B}", ts=OTHER_THREAD))
    await world.dispatch(message("!resume"))
    blocks = world.slack.calls_to("chat.postMessage")[-1]["blocks"]
    listed = [b["block_id"] for b in blocks if b.get("block_id")]
    assert listed == [f"session-{SESSION_A}"]  # the rows are the sessions that can be resumed
    assert blocks[-1]["elements"][0]["text"] == texts.RESUME_OPEN_ONE
    assert world.slack.calls_to("chat.getPermalink") == []  # no row links to a thread


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
    assert texts.RESUME_ELSEWHERE.format(link=link) in said(world)


async def test_resume_opens_an_independent_thread_while_another_is_busy(world: World) -> None:
    two_sessions(world)
    await world.dispatch(message("hello", ts=THREAD))  # the fake Claude Code never ends this turn
    await world.dispatch(message(f"!resume {SESSION_B}", ts=OTHER_THREAD))
    assert world.state.thread(CHANNEL, OTHER_THREAD).session_id == SESSION_B
    assert "Trust gate" in said(world)[-1]


async def test_a_top_level_word_whose_failure_report_fails_pushes_nothing(world: World) -> None:
    # The word's own failure and then the channel post that reports it both fail: the outer
    # handler must not fall back to a threaded `ERROR_REPLY` under the word (a push).
    def broken(directory: Path) -> list[SDKSessionInfo]:
        raise PermissionError("transcripts unreadable")

    world.sessions._deps.sessions_of = broken
    world.slack.responses["chat.postMessage"] = RuntimeError("network down")
    await world.dispatch(message("!resume"))
    posts = world.slack.calls_to("chat.postMessage")
    assert posts and all("thread_ts" not in p for p in posts)


async def test_a_failing_resume_click_tells_the_owner(world: World) -> None:
    def broken(directory: Path) -> list[SDKSessionInfo]:
        raise PermissionError("transcripts unreadable")

    world.sessions._deps.sessions_of = broken
    await world.dispatch(resume_click(SESSION_B))
    assert said(world) == [texts.ERROR_REPLY.format(error="PermissionError")]
    assert "thread_ts" not in world.slack.calls_to("chat.postMessage")[0]


async def test_bang_guide_works_in_an_unbound_channel(slack: FakeSlack, tmp_path: Path) -> None:
    world = World(slack, tmp_path, bound=False)
    await world.dispatch(message("!guide"))
    # The block holds the whole guide; `text` is its fallback, cut at FALLBACK_LIMIT.
    (post,) = world.slack.calls_to("chat.postMessage")
    assert post["blocks"] == [{"type": "markdown", "text": texts.GUIDE}]
    assert post["text"] == texts.GUIDE[:FALLBACK_LIMIT] and world.clients == []


async def test_bang_guide_inside_a_thread_is_for_the_owner_alone(world: World) -> None:
    await world.dispatch(message("hi", ts=THREAD))
    await world.dispatch(reply("!guide", THREAD))
    assert texts.GUIDE not in said(world)
    (guide,) = world.slack.calls_to("chat.postEphemeral")
    assert guide["thread_ts"] == THREAD and guide["text"].startswith("**code-with-slack**")


@pytest.mark.parametrize(
    "word", ["!help", "!guide", "!status", "!stop", "!bind", "!bind /", "!bypass on", "!resume"]
)
async def test_a_word_typed_in_the_channel_is_answered_by_a_top_level_post(
    world: World, word: str
) -> None:
    two_sessions(world)
    await world.dispatch(message(word, ts=THREAD))
    posts = world.slack.calls_to("chat.postMessage")
    assert posts and all("thread_ts" not in post for post in posts)
    assert world.ephemerals() == [] and world.clients == []


@pytest.mark.parametrize("word", ["!stop", "!resume", "!bind", "!status", "!bypass on"])
async def test_a_word_typed_in_a_thread_that_is_no_session_is_answered_top_level(
    world: World, word: str
) -> None:
    two_sessions(world)
    await world.dispatch(reply(word, CLICK_THREAD))
    posts = world.slack.calls_to("chat.postMessage")
    assert posts and all("thread_ts" not in post for post in posts)
    assert world.ephemerals() == []


async def test_a_failure_starting_a_prompt_lands_in_its_thread(world: World) -> None:
    # The turn's one push: the failure of a prompt that starts its session is posted in that
    # session's thread (a command needs the connection at once), not shown to the owner alone.
    world.connect_error = RuntimeError("boom")
    await world.dispatch(message("!compact", ts=THREAD))
    assert said(world) == [texts.ERROR_REPLY.format(error="RuntimeError")]
    assert world.slack.calls_to("chat.postMessage")[0]["thread_ts"] == THREAD
    assert world.ephemerals() == []


async def test_a_refusal_to_a_thread_message_is_for_the_owner_alone(world: World) -> None:
    await world.dispatch(_recorded_thread_reply())  # a thread that holds no session
    assert world.ephemerals() == [texts.NOT_A_SESSION] and said(world) == []


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
    assert (
        "thread_ts" not in world.slack.calls_to("chat.postMessage")[0]
    )  # top-level, like the list
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
    await world.dispatch(resume_click(SESSION_B))
    assert dated == []
    await world.dispatch(message("!resume"))
    assert dated == [(world.root / "app").resolve()]


async def test_a_second_resume_click_on_the_same_list_is_refused(world: World) -> None:
    two_sessions(world)
    await world.dispatch(resume_click(SESSION_A))
    await world.dispatch(resume_click(SESSION_B))  # a quick second click, same list
    assert world.state.thread(CHANNEL, THREAD).session_id == SESSION_A
    assert texts.RESUME_HELD in said(world)


async def test_a_resume_click_after_a_typed_resume_in_the_same_thread_is_refused(
    world: World,
) -> None:
    two_sessions(world)
    # A non-session thread that holds the picker: `!resume footer` there acts as top-level.
    await world.dispatch(reply("!resume footer", CLICK_THREAD))
    assert world.state.thread(CHANNEL, CLICK_THREAD).session_id == SESSION_A
    await world.dispatch(resume_click(SESSION_B, CLICK_THREAD))  # the same `!resume` thread
    assert world.state.thread(CHANNEL, CLICK_THREAD).session_id == SESSION_A
    assert texts.RESUME_HELD in said(world)


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
    click_task = asyncio.create_task(world.dispatch(resume_click(SESSION_B)))
    await listed.wait()
    await world.dispatch(message("!bind docs"))
    await click_task
    await asyncio.sleep(0.3)
    assert world.state.channel(CHANNEL).directory == (world.root / "docs").resolve()
    assert world.state.channel(CHANNEL).threads == {}
    assert texts.RESUME_GONE in said(world)


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
    assert texts.RESUME_GONE in said(world)


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


async def test_the_resumed_list_keeps_a_title_with_underscores_readable(world: World) -> None:
    title = "fix_the_parser"
    world.stored_sessions = [SDKSessionInfo(SESSION_A, title, 0, 1, title)]
    world.slack.responses["chat.delete"] = RuntimeError("network down")  # the list is rewritten
    body = resume_click(SESSION_A, THREAD)
    await world.dispatch(body)
    (edited,) = world.slack.calls_to("chat.update")
    assert title in edited["text"] and f"_{title}_" not in edited["text"]


async def test_a_failing_confirmation_still_updates_the_list(world: World) -> None:
    two_sessions(world)
    # The first post (the confirmation) fails; the failure report that follows goes through.
    world.slack.responses["chat.postMessage"] = [
        RuntimeError("network down"),
        {"ok": True, "ts": "1"},
    ]
    await world.dispatch(resume_click(SESSION_B, THREAD))
    assert world.state.thread(CHANNEL, THREAD).session_id == SESSION_B
    # No confirmation reached the thread: the list is kept, rewritten, as the record.
    (edited,) = world.slack.calls_to("chat.update")
    assert all("accessory" not in b for b in edited["blocks"])
    assert world.slack.calls_to("chat.delete") == []


async def test_a_failing_list_edit_does_not_block_the_confirmation(world: World) -> None:
    two_sessions(world)
    world.slack.responses["chat.update"] = RuntimeError("network down")
    await world.dispatch(resume_click(SESSION_B, THREAD))
    assert world.state.thread(CHANNEL, THREAD).session_id == SESSION_B
    assert "Trust gate" in said(world)[-1]


async def test_a_click_failure_whose_report_fails_raises_nothing(world: World) -> None:
    def broken(directory: Path) -> list[SDKSessionInfo]:
        raise PermissionError("transcripts unreadable")

    world.sessions._deps.sessions_of = broken
    world.slack.responses["chat.postMessage"] = RuntimeError("network down")
    await world.dispatch(resume_click(SESSION_B))  # would raise into Bolt if the report did


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
    assert world.ephemerals() == [texts.UPLOAD_FAILED.format(name="photo.png", reason=reason)]
    assert world.queries() == []


async def test_a_failed_download_sends_nothing_and_says_why(world: World) -> None:
    body = shared_file("image")
    world.downloads[body["event"]["files"][0]["url_private_download"]] = DownloadFailed("HTTP 404")
    await world.dispatch(body)
    reason = texts.UPLOAD_DOWNLOAD.format(error="HTTP 404")
    assert world.ephemerals() == [texts.UPLOAD_FAILED.format(name="photo.png", reason=reason)]
    assert world.queries() == []


async def test_a_file_in_an_unbound_channel_explains_how_to_bind(
    slack: FakeSlack, tmp_path: Path
) -> None:
    world = World(slack, tmp_path, bound=False)
    await world.dispatch(shared_file("image"))
    assert said(world) == [texts.UNBOUND.format(root=world.root.resolve())]
    assert "thread_ts" not in world.slack.calls_to("chat.postMessage")[0]
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
    assert world.ephemerals() == [
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
    await world.settle(0.3)
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
    assert world.ephemerals() == [texts.UPLOAD_TOO_MANY.format(count=6, limit=5)]


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
    await world.settle(0.3)
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
    await world.settle(0.3)
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
    await world.settle(0.3)  # let the slow download finish and the retried submit run
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
    await world.settle(0.3)
    assert world.queries() == []
    assert texts.SESSION_GONE in said(world)  # in the prompt's thread: the turn's one push


async def test_a_prompt_while_the_daemon_stops_is_refused_and_words_still_work(
    world: World,
) -> None:
    await world.sessions.drain(asyncio.Event())  # nothing runs: returns at once
    await world.dispatch(message("list the files"))
    await world.dispatch(message("!stop"))
    assert world.ephemerals() == [texts.RESTARTING]
    assert said(world) == [texts.NOTHING_TO_STOP]
    assert world.queries() == []


async def test_a_refusal_during_a_stop_names_the_thread_the_stop_waits_for(world: World) -> None:
    await world.dispatch(message("hello", ts=THREAD))  # never ends: it holds the stop
    cut_short = asyncio.Event()
    draining = asyncio.create_task(world.sessions.drain(cut_short))
    await asyncio.sleep(0.05)
    await world.dispatch(message("list the files", ts=OTHER_THREAD))
    cut_short.set()
    await draining
    assert world.ephemerals()[-1] == "\n".join(
        [
            texts.RESTARTING,
            texts.RESTART_WAITS_FOR,
            texts.RESTART_WAIT_ROW.format(
                channel=CHANNEL,
                link=f"<{PERMALINK}|{texts.RESTART_WAIT_SESSION}>",
                hold=texts.RESTART_HOLD_TURN,
            ),
            texts.RESTART_WAIT_STOP,
        ]
    )
    assert world.queries() == ["hello"]


async def test_a_channel_status_during_a_stop_lists_what_the_stop_waits_for(world: World) -> None:
    await world.dispatch(message("hello", ts=THREAD))  # never ends: it holds the stop
    cut_short = asyncio.Event()
    draining = asyncio.create_task(world.sessions.drain(cut_short))
    await asyncio.sleep(0.05)
    await world.dispatch(message("!status"))
    cut_short.set()
    await draining
    waits = said(world)[-1]
    assert waits.startswith(texts.RESTART_WAITS_HEADER + "\n• ")
    assert texts.RESTART_HOLD_TURN in waits and waits.endswith(texts.RESTART_WAIT_STOP)


async def test_a_channel_status_during_a_stop_counts_other_channels_without_naming_them(
    world: World,
) -> None:
    (world.root / "other").mkdir()  # another folder: no D8 hold between the two sessions
    world.state.bind(OTHER_CHANNEL, world.root / "other")
    await world.dispatch(message("hello", ts=THREAD))
    await world.dispatch(message("busy elsewhere", ts=OTHER_THREAD, channel=OTHER_CHANNEL))
    assert world.queries() == ["hello", "busy elsewhere"]
    cut_short = asyncio.Event()
    draining = asyncio.create_task(world.sessions.drain(cut_short))
    await asyncio.sleep(0.05)
    await world.dispatch(message("!status"))
    cut_short.set()
    await draining
    waits = said(world)[-1]
    # A post the channel's members read: the other channel's thread is counted, never named.
    assert OTHER_CHANNEL not in waits and waits.count("• ") == 1
    assert texts.RESTART_WAITS_ELSEWHERE.format(count=1) in waits


async def test_a_thread_that_only_waits_for_a_report_gets_no_stop_advice(world: World) -> None:
    await world.dispatch(message("hello", ts=THREAD))
    session = world.sessions.get(CHANNEL, THREAD)
    world.sessions.restart_holds = lambda: [(session, texts.RESTART_HOLD_REPORT)]  # type: ignore[method-assign]
    world.sessions.draining = True
    await world.dispatch(message("list the files", ts=OTHER_THREAD))
    refusal = world.ephemerals()[-1]
    assert texts.RESTART_HOLD_REPORT in refusal and texts.RESTART_WAIT_STOP not in refusal
    world.sessions.draining = False


async def test_a_long_list_of_waits_is_cut_between_rows(world: World) -> None:
    await world.dispatch(message("hello", ts=THREAD))
    session = world.sessions.get(CHANNEL, THREAD)
    world.sessions.restart_holds = lambda: [(session, texts.RESTART_HOLD_TURN)] * 11  # type: ignore[method-assign]
    world.sessions.draining = True
    await world.dispatch(message("list the files", ts=OTHER_THREAD))
    lines = world.ephemerals()[-1].split("\n")
    assert sum(line.startswith("• ") for line in lines) == 8
    assert lines[-2:] == [texts.RESTART_WAITS_MORE.format(count=3), texts.RESTART_WAIT_STOP]
    world.sessions.draining = False


async def test_a_channel_status_with_no_stop_under_way_says_nothing_of_a_restart(
    world: World,
) -> None:
    await world.dispatch(message("hello", ts=THREAD))
    await world.dispatch(message("!status"))
    assert not any(texts.RESTART_WAITS_FOR in text for text in said(world))


async def test_a_stop_during_the_downloads_sends_the_prompt_nowhere(world: World) -> None:
    body = shared_file("snippet")
    world.downloads[body["event"]["files"][0]["url_private_download"]] = b"hello\n"
    world.slow_downloads = 0.2
    first = asyncio.create_task(world.dispatch(body))
    await asyncio.sleep(0.05)
    await world.sessions.drain(asyncio.Event())
    await first
    await world.settle(0.3)
    assert world.queries() == [] and world.clients == []
    assert world.ephemerals()[-1] == texts.RESTARTING


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


async def test_a_reply_in_an_old_folder_thread_gets_the_notice_on_every_prompt(
    world: World,
) -> None:
    await _idle_message(world, "hi", ts=THREAD)  # opens a session in `app`
    (world.root / "docs").mkdir()
    await world.dispatch(message("!bind docs"))
    old = (world.root / "app").resolve()
    new = (world.root / "docs").resolve()
    expected = texts.OLD_THREAD_FOLDER.format(old=old, new=new)
    await world.dispatch(reply("go on", THREAD))
    assert world.ephemerals().count(expected) == 1
    assert expected not in said(world)  # for the owner alone: no push
    await world.dispatch(reply("again", THREAD))
    # An ephemeral vanishes on reload, so "once" could mean never: every prompt shows it.
    assert world.ephemerals().count(expected) == 2
    await world.sessions.close_all()


async def test_a_failed_old_folder_notice_still_submits_the_prompt(world: World) -> None:
    await _idle_message(world, "hi", ts=THREAD)  # opens a session in `app`
    (world.root / "docs").mkdir()
    await world.dispatch(message("!bind docs"))
    old = (world.root / "app").resolve()
    new = (world.root / "docs").resolve()
    expected = texts.OLD_THREAD_FOLDER.format(old=old, new=new)
    world.slack.responses["chat.postEphemeral"] = [RuntimeError("network down"), {"ok": True}]
    await world.dispatch(reply("go on", THREAD))
    assert world.clients[-1].queries[-1] == "go on"  # never dropped, despite the failed notice
    await world.dispatch(reply("again", THREAD))
    # The next reply shows it again (and this one succeeds), so its text was sent twice.
    assert world.ephemerals().count(expected) == 2
    await world.sessions.close_all()


async def test_a_thread_in_the_current_folder_never_gets_the_notice(world: World) -> None:
    await world.dispatch(message("hi", ts=THREAD))  # opens a session in `app`, still current
    await world.dispatch(reply("go on", THREAD))
    assert not any("Claude Code resumes a session only there" in t for t in world.ephemerals())
    await world.sessions.close_all()


def test_long_answers_fit_slack_s_limit() -> None:
    from code_with_slack.approvals import SECTION_LIMIT, answered_blocks

    questions = [{"question": "q" * 900} for _ in range(4)]
    answers: dict[str, str | list[str]] = {"q" * 900: "a" * 300}
    [block] = answered_blocks(questions, answers)
    assert len(block["elements"][0]["text"]) <= SECTION_LIMIT


# --- D8: two busy sessions in one folder (Phase 3, task 2) ---


def posted_blocks(world: World, index: int = -1) -> list[dict[str, Any]]:
    return world.slack.calls_to("chat.postMessage")[index]["blocks"]


def assert_held_unsent(world: World) -> None:
    """The held thread's client is connected (its setup needed the CLI's model list) but nothing
    was sent on it: only the other session, at `clients[0]`, ever got a prompt."""
    assert len(world.clients) == 2 and world.clients[-1].queries == []


async def start_a_hold(world: World, *, other_channel: str = CHANNEL) -> None:
    """`other_channel`'s thread at `OTHER_THREAD` never ends; a top-level message at THREAD,
    in the same folder, then holds and asks."""
    if other_channel != CHANNEL:
        world.state.bind(other_channel, world.root / "app")
    await world.dispatch(message("busy elsewhere", ts=OTHER_THREAD, channel=other_channel))
    await world.dispatch(message("hello", ts=THREAD))


async def test_a_busy_session_in_the_same_folder_holds_the_message(world: World) -> None:
    await start_a_hold(world)
    assert_held_unsent(world)  # held, not sent
    # mrkdwn's own `<url|label>` form (a `section` block, like approvals and the resume picker
    # use for their own buttons), not `thread_link`'s standard-Markdown form.
    link = "<https://example.slack.com/archives/C000CHAN/p1780000000000001|Session>"
    section = posted_blocks(world)[0]
    assert section["type"] == "section"
    assert section["text"] == {"type": "mrkdwn", "text": texts.HOLD_QUESTION.format(link=link)}
    # Crash repair (issue #19): a D8 hold is a request like an approval or a question.
    assert world.state.thread(CHANNEL, THREAD).requests == (world.slack.posted_ts[-1],)


async def test_a_failed_state_write_for_a_d8_hold_still_lets_it_proceed(world: World) -> None:
    # Fix round 2 item 1: `state.add_request` sits outside `hold_before_sending`'s own
    # try/finally on purpose, so a write failure here can never skip `hold_start`/`hold_end` and
    # leave the hold itself undiscarded.
    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("disk full")

    world.state.add_request = boom  # type: ignore[method-assign]
    await start_a_hold(world)
    question_ts = world.slack.posted_ts[-1]
    hold_id = button_value(posted_blocks(world), HOLD_CONTINUE)
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, THREAD, message_ts=question_ts))
    assert world.clients[-1].queries == ["hello"]


async def test_continue_sends_the_held_message(world: World) -> None:
    await start_a_hold(world)
    question_ts = world.slack.posted_ts[-1]
    hold_id = button_value(posted_blocks(world), HOLD_CONTINUE)
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, THREAD, message_ts=question_ts))
    assert world.clients[-1].queries == ["hello"]
    deleted = [a["ts"] for a in world.slack.calls_to("chat.delete")]
    assert deleted == [question_ts]  # the question's own message
    assert world.state.thread(CHANNEL, THREAD).requests == ()


async def test_bypass_typed_after_start_while_held_switches_the_session(world: World) -> None:
    # Start was applied (the world's setup starts on its own), then D8 asks: the setup's box is
    # gone, so the word works as in a session that ran, and holds after Continue.
    await start_a_hold(world)
    question_ts = world.slack.posted_ts[-1]
    word = reply("!bypass on", THREAD)
    await world.dispatch(word)
    assert world.ephemerals() == [texts.BYPASS_ON_THREAD]
    assert reactions_on(world, word["event"]["ts"]) == ["white_check_mark"]
    hold_id = button_value(posted_blocks(world, -1), HOLD_CONTINUE)
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, THREAD, message_ts=question_ts))
    client = world.clients[-1]
    assert client.queries == ["hello"]
    assert client.modes == ["bypassPermissions"]
    assert world.state.thread(CHANNEL, THREAD).bypass is True


async def test_cancel_drops_the_message_and_says_so(world: World) -> None:
    await start_a_hold(world)
    question_ts = world.slack.posted_ts[-1]
    hold_id = button_value(posted_blocks(world), HOLD_CANCEL)
    await world.dispatch(click_in(HOLD_CANCEL, hold_id, CHANNEL, THREAD, message_ts=question_ts))
    assert_held_unsent(world)  # never sent
    assert world.ephemerals()[-1] == texts.NOT_SENT
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
    assert_held_unsent(world)
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
    assert_held_unsent(world)  # held: the background task still counts as working


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
    assert_held_unsent(world)
    assert not world.ephemerals()


async def test_a_click_for_another_thread_is_refused(world: World) -> None:
    await start_a_hold(world)
    hold_id = button_value(posted_blocks(world), HOLD_CONTINUE)
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, OTHER_THREAD))
    assert_held_unsent(world)  # never sent: the click did not match the hold
    assert world.ephemerals()[-1] == texts.HOLD_GONE


async def test_a_click_for_another_channel_is_refused(world: World) -> None:
    await start_a_hold(world)
    hold_id = button_value(posted_blocks(world), HOLD_CONTINUE)
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, OTHER_CHANNEL, THREAD))
    assert_held_unsent(world)  # never sent: the click did not match the hold
    assert world.ephemerals()[-1] == texts.HOLD_GONE


async def test_a_drain_starting_right_after_continue_does_not_leave_a_stale_raised_hand(
    world: World,
) -> None:
    await start_a_hold(world)
    hold_id = button_value(posted_blocks(world), HOLD_CONTINUE)
    world.sessions.draining = True  # as if a drain's own cancellation pass had just run
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, THREAD))
    assert_held_unsent(world)
    session = world.sessions.get(CHANNEL, THREAD)
    assert session is not None
    assert session._status.current is None  # restored, not left on ✋: this thread never ran


async def test_a_directory_gone_unavailable_after_continue_reacts_error_not_a_raised_hand(
    world: World,
) -> None:
    await world.dispatch(message("busy elsewhere", ts=OTHER_THREAD))
    await world.dispatch(message("!compact", ts=THREAD))  # a Passthrough: held, `ensure_connected`
    hold_id = button_value(posted_blocks(world), HOLD_CONTINUE)
    session = world.sessions.get(CHANNEL, THREAD)
    assert session is not None
    # The setup connected it already; a client the CLI lost since makes Continue connect again.
    await session._drop_client()

    async def untrusted(directory: Path) -> bool:
        return False

    world.sessions._deps.workspace_trusted = untrusted
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, THREAD))
    assert world.clients[-1].queries == []  # never sent: its own connect failed
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
    session = world.sessions.get(CHANNEL, THREAD)
    assert session is not None
    await session._drop_client()  # the setup connected it; a lost client makes Continue reconnect
    world.connect_error = RuntimeError("logged out")
    await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, THREAD))
    assert world.clients[-1].queries == []  # its own connect failed: nothing was ever sent
    assert world.clients[0].queries == ["busy elsewhere"]  # the other session, unaffected
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
    try:
        await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, THREAD))
        assert world.clients[-1].queries == []  # never sent
        assert session._status.current is Status.ERROR
        assert texts.SESSION_GONE in said(world)
    finally:
        # `_closed` was set directly above, bypassing the real teardown (`cancel_hold` would
        # answer Cancel, not Continue, for a session real `close()` reaches): finished properly
        # here even when an assertion fails, or the fixture's own `close_all` hangs forever
        # behind this object's never-fired `done_closing` and the failure is never reported.
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
    try:
        await world.dispatch(click_in(HOLD_CONTINUE, hold_id, CHANNEL, THREAD))
        fresh = world.sessions.get(CHANNEL, THREAD)
        assert fresh is not None and fresh is not session
        assert world.clients[-1].queries == ["hello"]  # sent, on a freshly rebuilt client
        reacted = {a["name"] for a in world.slack.calls_to("reactions.add")}
        assert Status.ERROR.value not in reacted
    finally:
        # `_closed` was set directly above, bypassing the real teardown: finished properly here
        # even when an assertion fails, or the fixture's own `close_all` hangs behind this
        # object's never-cancelled background tasks.
        await session.close()


async def test_a_stop_answer_that_cannot_be_posted_is_not_the_word_s_failure(
    world: World,
) -> None:
    await _idle_message(world, "hi", ts=THREAD)
    world.slack.responses["chat.postMessage"] = RuntimeError("network down")
    await world.dispatch(reply("!stop", THREAD))
    # One attempt at the answer, and no `ERROR_REPLY` for a stop that did what it was asked.
    assert [p["text"] for p in world.slack.calls_to("chat.postMessage")][-1] == (
        texts.NOTHING_TO_STOP_THREAD
    )
    assert not world.ephemerals()


async def test_stop_in_the_held_thread_cancels_it(world: World) -> None:
    await start_a_hold(world)
    await world.dispatch(reply("!stop", THREAD))
    assert_held_unsent(world)
    # `!stop` cancelled the hold: `Not sent.` alone, from the waiter, since nothing Claude Code
    # itself was doing stopped (no separate "Nothing is running..." on top of it).
    assert world.ephemerals() == [texts.NOT_SENT]
    assert texts.NOT_SENT not in said(world)  # for the owner alone: no push


async def test_a_top_level_stop_of_the_channel_cancels_the_hold(world: World) -> None:
    await start_a_hold(world)
    await world.dispatch(message("!stop"))
    assert_held_unsent(world)
    assert texts.NOT_SENT in world.ephemerals()


async def test_a_top_level_stop_with_only_a_cancelled_hold_says_nothing_else(
    world: World,
) -> None:
    # The busy session lives in ANOTHER channel here, so this channel's own `!stop` cancels the
    # hold and stops nothing else: `stop_channel`'s own `None` must not read as "nothing
    # stopped" and add a second, contradicting notice on top of `Not sent.`.
    await start_a_hold(world, other_channel=OTHER_CHANNEL)
    await world.dispatch(message("!stop"))
    assert texts.NOT_SENT in world.ephemerals()
    assert texts.NOTHING_TO_STOP not in said(world)
    assert texts.STOPPED_CHANNEL not in said(world)


async def test_a_drain_cancels_the_hold(world: World) -> None:
    await start_a_hold(world)
    cut_short = asyncio.Event()
    cut_short.set()  # returns as soon as the per-session cancellation pass is done
    await world.sessions.drain(cut_short)
    await asyncio.sleep(0.05)
    assert_held_unsent(world)
    assert texts.NOT_SENT in world.ephemerals()


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
    assert_held_unsent(world)  # the hold still waits: nobody re-checked on its own
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
    assert_held_unsent(world)
    assert world.ephemerals()[-1] == texts.HOLD_UNPOSTED


async def test_a_message_during_a_drain_is_refused_not_held(world: World) -> None:
    await world.dispatch(message("busy elsewhere", ts=OTHER_THREAD))
    cut_short = asyncio.Event()
    drain = asyncio.create_task(world.sessions.drain(cut_short))
    await asyncio.sleep(0.02)  # the drain has set its flags before this message arrives
    await world.dispatch(message("hello", ts=THREAD))
    # The busy session of the other thread holds the stop: the refusal names it (issue #119).
    assert world.ephemerals()[-1].startswith(texts.RESTARTING + "\n" + texts.RESTART_WAITS_FOR)
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
    assert_held_unsent(world)  # neither "hello" nor "again" was ever sent
    assert texts.NOT_SENT in world.ephemerals()  # "hello", cancelled by the drain
    # "again", refused once draining had begun; the session busy elsewhere still holds the stop.
    assert world.ephemerals()[-1].startswith(texts.RESTARTING)


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
    await world.settle(0.2)  # Start on the setup; the question is then posted and gated
    world.holds.cancel(CHANNEL, THREAD)  # exactly what `!stop` would do
    gate.set()
    await asyncio.wait_for(task, 2)
    await asyncio.sleep(0.1)  # the handler itself outlives the dispatch call
    assert texts.NOT_SENT in world.ephemerals()
    added = [a["name"] for a in world.slack.calls_to("reactions.add")]
    # Only each setup's own ✋ (two threads opened): hold_start/hold_end never ran for the
    # question, so it added none.
    assert added.count(Status.WAITING.value) == 2


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


# Session setup (issue #74): model, effort and bypass, asked before a new session's first prompt.


def setup_click(
    world: World,
    action_id: str = SETUP_START,
    *,
    model: str | None = None,
    effort: str | None = None,
    bypass: bool = False,
    **where: Any,
) -> dict[str, Any]:
    """A click on the (only) waiting setup, carrying `state.values` as Slack would."""
    ((setup_id, channel, thread_ts, ts),) = world.waiting_setups()
    body = click_in(
        action_id,
        setup_id,
        where.get("channel", channel),
        where.get("thread_ts", thread_ts),
        message_ts=ts,
        **where.get("user", {}),
    )
    body["state"] = {"values": setup_state(model, effort, bypass)}
    return body


@pytest.fixture
def manual(world: World) -> Iterator[World]:
    world.auto_start = False
    yield world
    if world.connect_gate is not None:  # a failed test must not leave the teardown waiting on it
        world.connect_gate.set()


async def test_a_plain_prompt_waits_for_start(manual: World) -> None:
    await manual.dispatch(message("hello", ts=THREAD))
    assert manual.queries() == []
    ((_, channel, thread_ts, _),) = manual.waiting_setups()
    assert (channel, thread_ts) == (CHANNEL, THREAD)
    session = manual.sessions.get(CHANNEL, THREAD)
    assert session is not None and session.waiting_for_owner  # ✋, idle timer paused
    assert reactions_on(manual, THREAD) == ["raised_hand"]
    assert manual.state.thread(CHANNEL, THREAD).requests == (manual.slack.posted_ts[-1],)


async def test_a_message_with_files_waits_for_start_too(manual: World) -> None:
    body = shared_file("snippet")
    manual.downloads[body["event"]["files"][0]["url_private_download"]] = b"hello\n"
    await manual.dispatch(body)
    await asyncio.sleep(0.2)
    assert len(manual.waiting_setups()) == 1 and manual.queries() == []
    await manual.dispatch(setup_click(manual))
    await asyncio.sleep(0.2)
    (query,) = manual.queries()
    assert str(query).startswith(body["event"]["text"])


async def test_a_top_level_passthrough_waits_for_start_too(manual: World) -> None:
    await manual.dispatch(message("!compact", ts=THREAD))
    assert len(manual.waiting_setups()) == 1 and manual.queries() == []
    await manual.dispatch(setup_click(manual))
    assert manual.queries() == ["/compact"] or manual.queries() == ["!compact"]


async def test_daemon_words_and_thread_replies_show_no_setup(manual: World) -> None:
    await manual.dispatch(message("!status"))
    await manual.dispatch(message("!help"))
    assert manual.waiting_setups() == []
    await manual.dispatch(message("hi", ts=THREAD))
    await manual.dispatch(setup_click(manual))
    await manual.dispatch(reply("second", THREAD))
    posted = [a["text"] for a in manual.slack.calls_to("chat.postMessage")]
    assert posted.count(texts.SETUP_FALLBACK) == 1  # the top-level prompt's, not the reply's


async def test_start_with_defaults_changes_nothing(manual: World) -> None:
    await manual.dispatch(message("hello", ts=THREAD))
    await manual.dispatch(setup_click(manual))
    client = manual.clients[-1]
    assert client.queries == ["hello"] and len(manual.clients) == 1
    assert client.models_set == [] and client.modes == []
    assert client.options.effort is None
    summary = manual.slack.calls_to("chat.update")[-1]["text"]
    assert summary == "Model: Default (recommended) · Effort: Default · Bypass: off"
    assert manual.state.thread(CHANNEL, THREAD).requests == ()  # kept, no longer a request
    assert manual.slack.calls_to("chat.delete") == []
    assert reactions_on(manual, THREAD)[-1] == "hourglass_flowing_sand"


async def test_start_applies_model_effort_and_bypass(manual: World) -> None:
    await manual.dispatch(message("hello", ts=THREAD))
    await manual.dispatch(setup_click(manual, model="opus", effort="high", bypass=True))
    assert len(manual.clients) == 2  # reconnected once, for the effort
    client = manual.clients[-1]
    assert client.options.effort == "high"
    assert client.models_set == ["opus"]
    assert client.modes == ["bypassPermissions"]
    assert client.queries == ["hello"] and manual.clients[0].queries == []
    assert manual.clients[0].connected is False  # the effort reconnect closed the first client
    stored = manual.state.thread(CHANNEL, THREAD)
    assert stored.bypass is True and stored.effort == "high"
    summary = manual.slack.calls_to("chat.update")[-1]["text"]
    assert summary == "Model: Opus 5.5 · Effort: high · Bypass: on"


async def test_changing_the_model_rewrites_the_efforts(manual: World) -> None:
    await manual.dispatch(message("hello", ts=THREAD))
    await manual.dispatch(setup_click(manual, SETUP_MODEL, model="haiku", effort="high"))
    update = manual.slack.calls_to("chat.update")[-1]
    found = setup_controls(update["blocks"])
    assert [o["value"] for o in found[SETUP_EFFORT]["options"]] == ["default"]
    assert found[SETUP_MODEL]["initial_option"]["value"] == "haiku"
    assert "initial_options" not in found[SETUP_BYPASS]  # the tick state is kept
    assert manual.queries() == []  # still waiting


async def test_an_effort_or_bypass_change_only_acks(manual: World) -> None:
    await manual.dispatch(message("hello", ts=THREAD))
    await manual.dispatch(setup_click(manual, SETUP_EFFORT, effort="low"))
    await manual.dispatch(setup_click(manual, SETUP_BYPASS, bypass=True))
    assert manual.slack.calls_to("chat.update") == [] and manual.queries() == []


@pytest.mark.parametrize("user", [{"id": STRANGER}, {"team_id": OTHER_TEAM}])
async def test_a_setup_click_from_someone_else_is_ignored(
    manual: World, user: dict[str, str]
) -> None:
    await manual.dispatch(message("hello", ts=THREAD))
    await manual.dispatch(setup_click(manual, user=user))
    assert manual.queries() == [] and not manual.ephemerals()


async def test_a_setup_click_for_another_thread_or_channel_is_refused(manual: World) -> None:
    # Sessions exist at both places, so the refusal is `Holds.resolve`'s own channel/thread check,
    # not merely a lookup that finds no session.
    manual.auto_start = True
    manual.state.bind(OTHER_CHANNEL, manual.root / "app")
    await manual.dispatch(message("elsewhere", ts=OTHER_THREAD))
    await manual.dispatch(message("elsewhere", ts=THREAD, channel=OTHER_CHANNEL))
    manual.auto_start = False
    await manual.dispatch(message("hello", ts=THREAD))
    before = len(manual.queries())
    await manual.dispatch(setup_click(manual, thread_ts=OTHER_THREAD))
    await manual.dispatch(setup_click(manual, channel=OTHER_CHANNEL))
    assert len(manual.queries()) == before
    assert len(manual.waiting_setups()) == 1  # still waiting
    assert manual.ephemerals()[-1] == texts.HOLD_GONE


async def test_a_second_start_is_stale(manual: World) -> None:
    await manual.dispatch(message("hello", ts=THREAD))
    body = setup_click(manual)
    await manual.dispatch(body)
    await manual.dispatch(body)
    assert manual.queries() == ["hello"]
    assert manual.ephemerals()[-1] == texts.HOLD_GONE


async def test_stop_in_the_thread_cancels_a_waiting_setup(manual: World) -> None:
    await manual.dispatch(message("hello", ts=THREAD))
    await manual.dispatch(reply("!stop", THREAD))
    assert manual.queries() == [] and manual.ephemerals() == [texts.NOT_SENT]
    assert len(manual.slack.calls_to("chat.delete")) == 1
    assert manual.state.thread(CHANNEL, THREAD).requests == ()


async def test_a_top_level_stop_cancels_a_waiting_setup(manual: World) -> None:
    await manual.dispatch(message("hello", ts=THREAD))
    await manual.dispatch(message("!stop"))
    assert manual.queries() == [] and texts.NOT_SENT in manual.ephemerals()
    assert len(manual.slack.calls_to("chat.delete")) == 1


async def test_a_drain_cancels_a_waiting_setup(manual: World) -> None:
    await manual.dispatch(message("hello", ts=THREAD))
    cut_short = asyncio.Event()
    cut_short.set()
    await manual.sessions.drain(cut_short)
    await asyncio.sleep(0.05)
    assert manual.queries() == [] and texts.NOT_SENT in manual.ephemerals()


def answer_inside_post(
    world: World, text_starts: str, answer: Any, action: str | None = None
) -> None:
    """Resolve the hold a message shows before `chat.postMessage` has answered, as a fast click
    does when it arrives ahead of the HTTP answer."""
    original = world.slack.api_call

    async def api_call(method: str, **kwargs: Any) -> Any:
        result = await original(method, **kwargs)
        args = {**(kwargs.get("json") or {}), **(kwargs.get("params") or {})}
        if method == "chat.postMessage" and str(args.get("text", "")).startswith(text_starts):
            for block in args["blocks"]:
                for element in block.get("elements") or []:
                    if element.get("action_id") == action:
                        world.holds.resolve(element["value"], CHANNEL, THREAD, answer)
        return result

    world.slack.api_call = api_call  # type: ignore[method-assign]


async def test_a_start_decided_before_the_post_returns_is_still_applied(manual: World) -> None:
    answer_inside_post(manual, texts.SETUP_FALLBACK, Choice("opus", "high", True), SETUP_START)
    await manual.dispatch(message("hello", ts=THREAD))
    await manual.settle(0.3)
    client = manual.clients[-1]
    assert manual.queries() == ["hello"]
    assert client.models_set == ["opus"] and client.options.effort == "high"
    assert client.modes == ["bypassPermissions"]
    assert manual.slack.calls_to("chat.delete") == []  # the record stays
    summary = manual.slack.calls_to("chat.update")[-1]["text"]
    assert summary == "Model: Opus 5.5 · Effort: high · Bypass: on"
    assert manual.state.thread(CHANNEL, THREAD).requests == ()


async def test_a_continue_decided_before_the_post_returns_still_sends(world: World) -> None:
    await world.dispatch(message("busy elsewhere", ts=OTHER_THREAD))
    answer_inside_post(world, texts.HOLD_QUESTION.split("{")[0], True, HOLD_CONTINUE)
    await world.dispatch(message("hello", ts=THREAD))
    await world.settle(0.3)
    assert world.clients[-1].queries == ["hello"]


async def start_with_a_gated_reconnect(manual: World) -> None:
    """Start with an effort, while the reconnect's CLI is slow to come up: the window in which
    the answer is applied but nothing is sent yet."""
    await manual.dispatch(message("hello", ts=THREAD))
    manual.connect_gate = asyncio.Event()
    await manual.dispatch(setup_click(manual, effort="high", bypass=True))
    await asyncio.sleep(0.1)


async def assert_cancelled_while_settling(manual: World) -> None:
    assert manual.connect_gate is not None
    manual.connect_gate.set()
    await manual.settle(0.3)
    assert manual.queries() == []
    assert texts.NOT_SENT in manual.ephemerals()
    assert manual.state.thread(CHANNEL, THREAD).requests == ()
    assert len(manual.slack.calls_to("chat.delete")) == 1


async def test_bypass_typed_while_start_is_applied_wins_over_the_box(
    manual: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The word came after the click: it waits for Start to finish, then switches the session,
    # so what its answer says is what runs. Start is held inside `set_model`, after the point
    # where it is marked as applied and before it compares the box with the live client.
    gate = asyncio.Event()

    async def slow(self: FakeClaudeClient, model: str | None = None) -> None:
        self.models_set.append(model)
        await gate.wait()

    monkeypatch.setattr(FakeClaudeClient, "set_model", slow)
    await manual.dispatch(message("hello", ts=THREAD))
    await manual.dispatch(setup_click(manual, model="opus"))  # bypass unticked
    await asyncio.sleep(0.1)
    word = reply("!bypass on", THREAD)
    await manual.dispatch(word)
    await asyncio.sleep(0.1)
    assert texts.BYPASS_ON_THREAD not in manual.ephemerals()  # nothing is said before it holds
    gate.set()
    await manual.settle(0.3)
    assert manual.clients[-1].modes == ["bypassPermissions"]
    assert manual.state.thread(CHANNEL, THREAD).bypass is True
    assert texts.BYPASS_ON_THREAD in manual.ephemerals()
    assert reactions_on(manual, word["event"]["ts"]) == ["white_check_mark"]
    assert manual.queries() == ["hello"]


async def test_stop_while_start_settles_cancels(manual: World) -> None:
    await start_with_a_gated_reconnect(manual)
    await manual.dispatch(reply("!stop", THREAD))
    await asyncio.sleep(0.1)
    assert texts.NOTHING_TO_STOP not in said(manual) + manual.ephemerals()
    await assert_cancelled_while_settling(manual)


async def test_a_channel_stop_while_start_settles_cancels(manual: World) -> None:
    await start_with_a_gated_reconnect(manual)
    await manual.dispatch(message("!stop"))
    await asyncio.sleep(0.1)
    assert texts.NOTHING_TO_STOP not in said(manual) + manual.ephemerals()
    await assert_cancelled_while_settling(manual)


async def test_a_drain_while_start_settles_cancels(manual: World) -> None:
    await start_with_a_gated_reconnect(manual)
    cut_short = asyncio.Event()
    cut_short.set()
    await manual.sessions.drain(cut_short)
    await assert_cancelled_while_settling(manual)


def assert_a_fresh_setup(world: World) -> None:
    """The setup shown again: bypass unticked, effort Default, nothing left from the aborted
    Start in `state.json` or on the client that will be used."""
    ((_, _, _, ts),) = world.waiting_setups()
    blocks = world.slack.messages[ts].blocks
    found = setup_controls(blocks)
    assert found[SETUP_EFFORT]["initial_option"]["value"] == "default"
    assert "initial_options" not in found[SETUP_BYPASS]
    stored = world.state.thread(CHANNEL, THREAD)
    assert stored.bypass is None and stored.effort is None
    client = world.clients[-1]
    assert client.options.effort is None and client.models_set == [] and client.modes == []


async def test_a_reply_after_a_stopped_setup_asks_the_setup_again(manual: World) -> None:
    await start_with_a_gated_reconnect(manual)
    await manual.dispatch(reply("!stop", THREAD))
    await assert_cancelled_while_settling(manual)
    manual.connect_gate = None
    await manual.dispatch(reply("again", THREAD))
    assert manual.queries() == []  # held, not sent with what the aborted Start left
    assert_a_fresh_setup(manual)
    await manual.dispatch(setup_click(manual))
    assert manual.clients[-1].queries == ["again"]


async def test_a_reply_after_a_cancelled_d8_hold_asks_the_setup_again(manual: World) -> None:
    manual.auto_start = True
    await manual.dispatch(message("busy elsewhere", ts=OTHER_THREAD))
    manual.auto_start = False
    await manual.dispatch(message("hello", ts=THREAD))
    await manual.dispatch(setup_click(manual, model="opus", effort="high", bypass=True))
    hold_id = button_value(posted_blocks(manual), HOLD_CANCEL)
    await manual.dispatch(click_in(HOLD_CANCEL, hold_id, CHANNEL, THREAD))
    assert texts.NOT_SENT in manual.ephemerals()
    await manual.dispatch(reply("again", THREAD))
    assert_a_fresh_setup(manual)


async def test_a_reply_while_the_first_turn_runs_is_not_asked_again(manual: World) -> None:
    await manual.dispatch(message("hello", ts=THREAD))
    await manual.dispatch(setup_click(manual))
    await manual.dispatch(reply("more", THREAD))
    await manual.settle(0.3)
    posted = [a["text"] for a in manual.slack.calls_to("chat.postMessage")]
    assert posted.count(texts.SETUP_FALLBACK) == 1  # the first prompt's, not the reply's
    assert manual.waiting_setups() == []


async def test_a_failing_start_leaves_no_controls_and_no_leftovers(
    manual: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(self: FakeClaudeClient, model: str | None = None) -> None:
        raise RuntimeError("bad model")

    monkeypatch.setattr(FakeClaudeClient, "set_model", boom)
    await manual.dispatch(message("hello", ts=THREAD))
    await manual.dispatch(setup_click(manual, model="opus", effort="high", bypass=True))
    await manual.settle(0.3)
    assert manual.queries() == []
    assert len(manual.slack.calls_to("chat.delete")) == 1  # the controls are gone
    stored = manual.state.thread(CHANNEL, THREAD)
    assert stored.requests == () and stored.effort is None and stored.bypass is None
    assert manual.ephemerals() or any("RuntimeError" in t for t in said(manual))


async def test_a_model_change_cannot_overwrite_the_summary(manual: World) -> None:
    await manual.dispatch(message("hello", ts=THREAD))
    gate = asyncio.Event()
    acquire = manual.sessions.update_limiter.acquire
    slow = [True]

    async def gated() -> None:
        if slow:
            slow.pop()
            await gate.wait()
        await acquire()

    manual.sessions.update_limiter.acquire = gated  # type: ignore[method-assign]
    body = setup_click(manual, SETUP_MODEL, model="haiku")
    start = setup_click(manual)
    await manual.dispatch(body)  # waits on the limiter
    await manual.dispatch(start)
    await manual.settle(0.2)
    gate.set()
    await manual.settle(0.2)
    last = manual.slack.calls_to("chat.update")[-1]
    assert last["text"].startswith("Model:")


def gate_limiter(world: World) -> asyncio.Event:
    """Hold every `chat.update` behind the process-wide limiter until the event is set."""
    gate = asyncio.Event()
    acquire = world.sessions.update_limiter.acquire

    async def gated() -> None:
        await gate.wait()
        await acquire()

    world.sessions.update_limiter.acquire = gated  # type: ignore[method-assign]
    return gate


async def test_stop_while_the_summary_waits_on_the_limiter_cancels(manual: World) -> None:
    await manual.dispatch(message("hello", ts=THREAD))
    gate = gate_limiter(manual)
    await manual.dispatch(setup_click(manual))
    await asyncio.sleep(0.1)
    assert manual.queries() == []  # Start is still being settled: the summary waits
    await manual.dispatch(reply("!stop", THREAD))
    await asyncio.sleep(0.1)
    assert texts.NOTHING_TO_STOP not in said(manual) + manual.ephemerals()
    gate.set()
    await manual.settle(0.3)
    assert manual.queries() == []
    assert texts.NOT_SENT in manual.ephemerals()
    # The cancel deleted the message; the summary edit is skipped rather than sent to it.
    assert len(manual.slack.calls_to("chat.delete")) == 1
    assert manual.slack.calls_to("chat.update") == []
    assert manual.state.thread(CHANNEL, THREAD).requests == ()


async def test_a_restart_during_start_leaves_nothing_for_the_next_start(manual: World) -> None:
    await manual.dispatch(message("hello", ts=THREAD))
    gate = gate_limiter(manual)
    await manual.dispatch(setup_click(manual, effort="high", bypass=True))
    await asyncio.sleep(0.1)
    manual.sessions.draining = True  # a restart begins before the prompt is sent
    gate.set()
    await manual.settle(0.2)
    assert manual.queries() == [] and texts.RESTARTING in manual.ephemerals()
    stored = manual.state.thread(CHANNEL, THREAD)
    assert stored.bypass is True and stored.effort == "high"  # what the new process finds
    # The restart: a new ThreadSession rebuilt from state.json.
    manual.sessions.draining = False
    old = manual.sessions.get(CHANNEL, THREAD)
    assert old is not None
    await old.close()
    await manual.dispatch(reply("again", THREAD))
    await asyncio.sleep(0.1)
    assert len(manual.waiting_setups()) == 1  # asked the setup again
    await manual.dispatch(setup_click(manual))  # defaults: effort Default, bypass off
    await manual.settle(0.3)
    client = manual.clients[-1]
    assert client.queries == ["again"]
    assert client.options.effort is None and client.modes in ([], ["default"])
    stored = manual.state.thread(CHANNEL, THREAD)
    # Start's unticked box is an explicit off.
    assert stored.bypass is False and stored.effort is None
    assert manual.slack.calls_to("chat.update")[-1]["text"].endswith(
        "Effort: Default · Bypass: off"
    )


@pytest.mark.parametrize("word", ["!bypass on", "!bypass off"])
async def test_bypass_typed_while_the_setup_waits_points_at_the_setup(
    manual: World, word: str
) -> None:
    await manual.dispatch(message("hello", ts=THREAD))
    typed = reply(word, THREAD)
    await manual.dispatch(typed)
    # Start alone sets bypass before the first prompt: the word changes nothing and says so.
    assert manual.clients[-1].modes == []
    assert manual.state.thread(CHANNEL, THREAD).bypass is None
    assert manual.ephemerals() == [texts.BYPASS_BEFORE_START]
    assert reactions_on(manual, typed["event"]["ts"]) == []  # nothing took effect: no ✅
    await manual.dispatch(setup_click(manual))  # bypass unticked
    await manual.settle(0.3)
    client = manual.clients[-1]
    assert client.modes == []  # never left the folder's own mode
    assert manual.state.thread(CHANNEL, THREAD).bypass is False
    assert client.queries == ["hello"]
    assert manual.slack.calls_to("chat.update")[-1]["text"].endswith("Bypass: off")


async def test_a_failing_forget_setup_does_not_hide_the_original_error(
    manual: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(self: FakeClaudeClient, model: str | None = None) -> None:
        raise RuntimeError("bad model")

    async def worse(self: Any) -> None:
        raise OSError("cannot drop")

    monkeypatch.setattr(FakeClaudeClient, "set_model", boom)
    monkeypatch.setattr("code_with_slack.sessions.ThreadSession._drop_client", worse)
    await manual.dispatch(message("hello", ts=THREAD))
    await manual.dispatch(setup_click(manual, model="opus"))
    await manual.settle(0.3)
    assert manual.queries() == []
    assert any("RuntimeError" in t for t in said(manual) + manual.ephemerals())


def native_bypass_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    """A folder whose own Claude Code settings start the process in bypassPermissions."""
    original = FakeClaudeClient.__init__

    def init(self: FakeClaudeClient, options: Any, **kw: Any) -> None:
        original(self, options, **kw)
        self._server_info = dict(self._server_info, current_permission_mode="bypassPermissions")

    monkeypatch.setattr(FakeClaudeClient, "__init__", init)


def auto_mode_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """An owner whose own Claude Code settings start the process in auto mode."""
    original = FakeClaudeClient.__init__

    def init(self: FakeClaudeClient, options: Any, **kw: Any) -> None:
        original(self, options, **kw)
        self._server_info = dict(self._server_info, current_permission_mode="auto")

    monkeypatch.setattr(FakeClaudeClient, "__init__", init)


async def test_bang_bypass_off_in_an_auto_mode_thread_returns_to_auto_mode(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    auto_mode_settings(monkeypatch)
    await world.dispatch(message("hi", ts=THREAD))
    await world.dispatch(reply("!bypass on", THREAD))
    await world.dispatch(reply("!bypass off", THREAD))
    assert world.clients[0].modes == ["bypassPermissions", "auto"]
    assert world.ephemerals() == [texts.BYPASS_ON_THREAD, texts.BYPASS_OFF_THREAD]


def bypass_box(world: World) -> dict[str, Any]:
    ((_, _, _, ts),) = world.waiting_setups()
    return setup_controls(world.slack.messages[ts].blocks)[SETUP_BYPASS]


async def test_a_native_bypass_folder_starts_the_box_ticked(
    manual: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    native_bypass_folder(monkeypatch)
    await manual.dispatch(message("hello", ts=THREAD))
    assert [o["value"] for o in bypass_box(manual)["initial_options"]] == ["on"]
    await manual.dispatch(setup_click(manual, bypass=True))  # the ticked default
    await manual.settle(0.3)
    client = manual.clients[-1]
    session = manual.sessions.get(CHANNEL, THREAD)
    assert session is not None and session.native_mode == "bypassPermissions"
    assert client.queries == ["hello"] and client.modes[-1:] in ([], ["bypassPermissions"])
    assert manual.slack.calls_to("chat.update")[-1]["text"].endswith("Bypass: on")


async def test_unticking_in_a_native_bypass_folder_turns_bypass_off(
    manual: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    native_bypass_folder(monkeypatch)
    await manual.dispatch(message("hello", ts=THREAD))
    await manual.dispatch(setup_click(manual))  # unticked: an explicit off, as `!bypass off`
    await manual.settle(0.3)
    client = manual.clients[-1]
    session = manual.sessions.get(CHANNEL, THREAD)
    assert session is not None and session.bypass is False
    assert client.modes[-1] == "default" and client.queries == ["hello"]
    assert manual.slack.calls_to("chat.update")[-1]["text"].endswith("Bypass: off")


async def test_a_typed_bypass_does_not_change_what_the_summary_says(
    manual: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    native_bypass_folder(monkeypatch)
    await manual.dispatch(message("hello", ts=THREAD))
    await manual.dispatch(reply("!bypass on", THREAD))
    await manual.dispatch(setup_click(manual))  # unticked
    await manual.settle(0.3)
    client = manual.clients[-1]
    assert client.modes[-1] == "default"
    assert manual.slack.calls_to("chat.update")[-1]["text"].endswith("Bypass: off")


async def test_a_model_change_keeps_the_bypass_tick(
    manual: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    native_bypass_folder(monkeypatch)
    await manual.dispatch(message("hello", ts=THREAD))
    await manual.dispatch(setup_click(manual, SETUP_MODEL, model="haiku"))  # unticked by the owner
    assert "initial_options" not in bypass_box(manual)
    await manual.dispatch(setup_click(manual, SETUP_MODEL, model="haiku", bypass=True))
    assert bypass_box(manual)["initial_options"]


async def test_a_connect_racing_start_does_not_record_bypass_as_off(
    manual: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await manual.dispatch(message("hello", ts=THREAD))
    session = manual.sessions.get(CHANNEL, THREAD)
    assert session is not None
    await session._drop_client()  # the client is gone while the setup waits
    manual.state.set_bypass(CHANNEL, THREAD, True)  # what a restart left
    gate = asyncio.Event()
    original = FakeClaudeClient.set_permission_mode

    async def gated(self: FakeClaudeClient, mode: str) -> None:
        await original(self, mode)
        await gate.wait()

    monkeypatch.setattr(FakeClaudeClient, "set_permission_mode", gated)
    status = asyncio.create_task(manual.dispatch(reply("!status", THREAD)))
    await asyncio.sleep(0.1)
    assert manual.clients[-1].modes == ["bypassPermissions"]  # connect blocked mid-switch
    click = asyncio.create_task(manual.dispatch(setup_click(manual)))  # bypass off
    await asyncio.sleep(0.1)
    gate.set()
    await status
    await click
    await manual.settle(0.3)
    client = manual.clients[-1]
    assert client.queries == ["hello"]
    assert client.modes[-1] == "default"  # what Start said, not the client's first mode


async def test_a_native_bypass_folder_asks_again_with_the_box_ticked_after_a_stop(
    manual: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    native_bypass_folder(monkeypatch)
    await manual.dispatch(message("hello", ts=THREAD))
    gate = gate_limiter(manual)
    await manual.dispatch(setup_click(manual))  # unticked: native mode moves to default
    await asyncio.sleep(0.1)
    await manual.dispatch(reply("!stop", THREAD))
    gate.set()
    await manual.settle(0.3)
    await manual.dispatch(reply("again", THREAD))
    assert [o["value"] for o in bypass_box(manual)["initial_options"]] == ["on"]


async def ran_in_a_native_bypass_folder(
    manual: World, monkeypatch: pytest.MonkeyPatch, *, unticked: bool = True
) -> Any:
    """A native-bypass folder's thread that started with an explicit off and ran a turn."""
    native_bypass_folder(monkeypatch)
    await manual.dispatch(message("hello", ts=THREAD))
    await manual.dispatch(setup_click(manual, bypass=not unticked))
    await manual.settle(0.3)
    manual.state.set_session(CHANNEL, THREAD, "sess-ran")  # a thread that ran
    session = manual.sessions.get(CHANNEL, THREAD)
    assert session is not None
    return session


async def test_an_unticked_off_survives_an_idle_close(
    manual: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await ran_in_a_native_bypass_folder(manual, monkeypatch)
    await session.close()
    await manual.dispatch(reply("second", THREAD))
    await manual.settle(0.3)
    fresh = manual.sessions.get(CHANNEL, THREAD)
    assert fresh is not None and fresh is not session
    assert manual.clients[-1].queries == ["second"]
    assert manual.clients[-1].modes[-1] == "default"  # not back in the folder's own bypass
    assert fresh.bypass is False


async def test_an_unticked_off_survives_a_lost_client(
    manual: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await ran_in_a_native_bypass_folder(manual, monkeypatch)
    await session._drop_client()  # the reader-crash path
    await manual.dispatch(reply("second", THREAD))
    await manual.settle(0.3)
    assert manual.clients[-1].modes[-1] == "default" and session.bypass is False


async def test_bang_bypass_off_in_a_native_folder_survives_a_restart(
    manual: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await ran_in_a_native_bypass_folder(manual, monkeypatch, unticked=False)
    assert session.bypass is True
    await manual.dispatch(reply("!bypass off", THREAD))
    assert manual.clients[-1].modes[-1] == "default"
    await session.close()  # the restart: a new object from state.json
    await manual.dispatch(reply("second", THREAD))
    await manual.settle(0.3)
    assert manual.clients[-1].modes[-1] == "default"


async def test_a_thread_that_never_chose_keeps_the_folders_own_bypass(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    native_bypass_folder(monkeypatch)
    await world.dispatch(message("hello", ts=THREAD))  # the harness presses Start, ticked
    await world.dispatch(reply("!bypass off", THREAD))
    world.state.set_bypass(CHANNEL, THREAD, None)  # never chosen
    session = world.sessions.get(CHANNEL, THREAD)
    assert session is not None
    await session._drop_client()
    await session.ensure_connected()
    assert world.clients[-1].modes == []  # left as Claude Code started it
    assert session.bypass is True


async def test_the_channel_status_row_reads_the_effective_bypass(
    manual: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await ran_in_a_native_bypass_folder(manual, monkeypatch)
    await manual.dispatch(message("!status"))
    assert texts.STATUS_CHANNEL_BYPASS not in said(manual)[-1]
    manual.state.set_bypass(CHANNEL, THREAD, None)  # never chosen: the folder's bypass runs
    await manual.dispatch(message("!status"))
    assert texts.STATUS_CHANNEL_BYPASS in said(manual)[-1]


async def test_a_model_change_keeps_a_supported_effort_and_the_tick(manual: World) -> None:
    await manual.dispatch(message("hello", ts=THREAD))
    await manual.dispatch(setup_click(manual, SETUP_MODEL, model="opus", effort="low", bypass=True))
    found = setup_controls(manual.slack.calls_to("chat.update")[-1]["blocks"])
    assert found[SETUP_MODEL]["initial_option"]["value"] == "opus"
    assert found[SETUP_EFFORT]["initial_option"]["value"] == "low"
    assert [o["value"] for o in found[SETUP_BYPASS]["initial_options"]] == ["on"]


# --- `!open` ---

# The payloads of the picker's modal follow the Slack reference, read 2026-10-05 on docs.slack.dev:
# block_actions payload (an action from a view: `container.type` "view" with `view_id`, `view.id`,
# `view.hash`, `view.private_metadata`, `view.state.values`, `actions[].action_ts`, and no
# `channel`), view interaction payloads (`view_submission`, and the `response_action` "errors"),
# views.open and views.update (`hash`, optional on an update, and the `hash_conflict` error of a
# hash that is not the view's current one; read again 2026-10-06). The submit is the recorded
# form-submit.json with the picker's view in it.
VIEW_ID = "V000PICK"
OPEN_TARGET = Target(CHANNEL, THREAD)


class ModalSlack:
    """Slack's side of one modal as the references describe it: `views.open` answers the view's
    id and hash, `views.update` answers the new hash and rejects, with `hash_conflict`, a hash
    that is not the view's current one (the daemon sends none). `view` is the view as it stands,
    `written` every update that was accepted, `calls` every one that was made, with the hash
    each carried."""

    def __init__(self, slack: FakeSlack) -> None:
        self.hash = "1790000000.h0"
        self.view: dict[str, Any] = {}
        self.written: list[dict[str, Any]] = []
        self.calls: list[tuple[str | None, dict[str, Any]]] = []
        self._count = 0
        slack.responses["views.open"] = self._open
        slack.responses["views.update"] = self._update

    def _next(self) -> str:
        self._count += 1
        self.hash = f"1790000000.h{self._count}"
        return self.hash

    @staticmethod
    def _view(args: dict[str, Any]) -> dict[str, Any]:
        view = args["view"]
        return json.loads(view) if isinstance(view, str) else view

    def _open(self, args: dict[str, Any]) -> dict[str, Any]:
        self.view = self._view(args)
        return {"ok": True, "view": {"id": VIEW_ID, "hash": self._next()}}

    def _update(self, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((args.get("hash"), self._view(args)))
        if args.get("hash") not in (None, self.hash) or args.get("view_id") != VIEW_ID:
            return {"ok": False, "error": "hash_conflict"}
        self.view = self._view(args)
        self.written.append(self.view)
        return {"ok": True, "view": {"id": VIEW_ID, "hash": self._next()}}

    def labels(self) -> list[str]:
        """What the view says about its rows: the radio group's label, else its context lines."""
        found = []
        for block in self.view["blocks"][1:]:
            if block["type"] == "input":
                found.append(block["label"]["text"])
            else:
                found.append(block["elements"][0]["text"])
        return found

    def rows(self, view: dict[str, Any] | None = None) -> list[str]:
        """The paths of the radio group's rows, in order."""
        for block in (view or self.view)["blocks"]:
            if block.get("block_id", "").startswith(CHOICE_BLOCK):
                return [o["value"] for o in block["element"]["options"]]
        return []


@pytest.fixture
def modal(world: World) -> ModalSlack:
    return ModalSlack(world.slack)


def opened(world: World) -> list[dict[str, Any]]:
    """The files the bot shared, as `files.completeUploadExternal` was asked to."""
    return world.slack.calls_to("files.completeUploadExternal")


def picker_posts(world: World) -> list[dict[str, Any]]:
    """The messages the bot posted that hold the button of `!open`'s modal."""
    return [
        post
        for post in world.slack.calls_to("chat.postMessage")
        if any(
            element.get("action_id") == OPEN_BUTTON_ACTION
            for block in post.get("blocks") or []
            for element in block.get("elements") or []
        )
    ]


def made_before_the_thread(path: Path) -> Path:
    """A repository with one commit, `README`, made long before THREAD began: what a session
    started in an existing repository sees. (A repository made after the thread began counts
    every file as changed.)"""
    git_init(path)
    commit_at(path, "README", int(THREAD.split(".")[0]) - 1000)
    return path.resolve()


@pytest.fixture
def project(world: World) -> Path:
    """The channel's folder as a repository with one commit, `README`."""
    return made_before_the_thread(world.root / "app")


def put(root: Path, name: str, text: str = "x\n") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def choose_click(words: str = "", **user: Any) -> dict[str, Any]:
    """A click on the `Choose a file` button of a message in THREAD: the recorded button click
    (`000-block_actions.json` shape), with the `trigger_id` a real click carries."""
    body = click_in(OPEN_BUTTON_ACTION, words, CHANNEL, THREAD, **user)
    body["actions"][0].pop("selected_option", None)
    body["actions"][0]["type"] = "button"
    if not words:
        body["actions"][0].pop("value")
    body["trigger_id"] = "0000000000.0000000000.fake"
    return body


def view_event(
    kind: str,
    values: dict[str, Any],
    *,
    metadata: str | None = None,
    user: str = OWNER,
    team: str = TEAM,
    view_hash: str | None = None,
    blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """What Slack sends for the picker's modal: `view_submission` is the recorded Submit with the
    picker's view in it, `block_actions` is built from the reference (no recording of one from a
    modal exists)."""
    metadata = OPEN_TARGET.dump() if metadata is None else metadata
    view = {
        "id": VIEW_ID,
        "team_id": TEAM,
        "type": "modal",
        "blocks": blocks if blocks is not None else modal_view(OPEN_TARGET, "", ["a.py"])["blocks"],
        "private_metadata": metadata,
        "callback_id": OPEN_FORM,
        "state": {"values": values},
        "hash": view_hash or "1790000000.h0",
    }
    if kind == "view_submission":
        body = recorded("submit")
        body["view"].update(view)
    else:
        body = {
            "type": "block_actions",
            "team": {"id": TEAM, "domain": "example"},
            "user": {"id": OWNER, "username": "alice", "name": "alice", "team_id": TEAM},
            "api_app_id": "A000APP",
            "container": {"type": "view", "view_id": VIEW_ID},
            "trigger_id": "1.2.abc",
            "view": view,
        }
    body["user"]["id"] = user
    body["team"]["id"] = team
    return body


def typing(
    text: str, at: float = 1790000100.0, *, hash_: str | None = None, **event: Any
) -> dict[str, Any]:
    """One character typed in the search field (`on_character_entered`): the action carries the
    text and its `action_ts`, and the view's state carries the same text."""
    values = {QUERY_BLOCK: {QUERY_ACTION: {"type": "plain_text_input", "value": text}}}
    body = view_event("block_actions", values, view_hash=hash_, **event)
    body["actions"] = [
        {
            "type": "plain_text_input",
            "block_id": QUERY_BLOCK,
            "action_id": QUERY_ACTION,
            "value": text,
            "action_ts": f"{at:.6f}",
        }
    ]
    return body


def shown_choice_id(blocks: list[dict[str, Any]]) -> str:
    return next(b["block_id"] for b in blocks if b.get("block_id", "").startswith(CHOICE_BLOCK))


def submitted(
    path: str | None, *, rows: list[str] | None = None, under: str | None = None, **event: Any
) -> dict[str, Any]:
    """The Submit of the picker's modal with `path` chosen (a radio group's state as
    form-submit.json records it), or nothing chosen. The view holds `rows` (just `path` by
    default); the choice is in the state of the block `under` (the rows' own block by default:
    Slack keeps the state of a block id from one update to the next)."""
    shown = rows if rows is not None else ([] if path is None else [path])
    blocks = event.pop("blocks", None) or modal_view(OPEN_TARGET, "", shown)["blocks"]
    option = None if path is None else {"text": {"type": "plain_text", "text": path}, "value": path}
    block_id = under or next(
        (b["block_id"] for b in blocks if b.get("block_id", "").startswith(CHOICE_BLOCK)), None
    )
    values: dict[str, Any] = {
        QUERY_BLOCK: {QUERY_ACTION: {"type": "plain_text_input", "value": None}}
    }
    if block_id is not None:
        values[block_id] = {CHOICE_ACTION: {"type": "radio_buttons", "selected_option": option}}
    return view_event("view_submission", values, blocks=blocks, **event)


async def asked_open(world: World, text: str) -> None:
    """`text` (an `!open` word) sent in THREAD, and the time its git commands need: `dispatch`
    waits 50 ms, a listing runs several processes."""
    await world.dispatch(reply(text, THREAD))
    await world.settle(0.3)


async def in_a_thread(world: World) -> None:
    """A session in THREAD, whose first sight is now (its start commit)."""
    await world.dispatch(message("hello", ts=THREAD))


async def test_two_replies_sent_together_reach_the_queue_in_the_order_they_were_sent(
    world: World, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing of `!open` runs when a prompt arrives: a slow repository lookup (a cold cache, a
    # slow git) must not hold a prompt, or let a later one overtake it. With a lookup awaited
    # before the arrival lock, the first reply here is overtaken by the second.
    await in_a_thread(world)
    session = world.sessions.get(CHANNEL, THREAD)
    assert session is not None
    queued: list[str] = []
    submit = session.submit

    async def spy(prompt: Any) -> Any:
        queued.append(str(prompt))
        return await submit(prompt)

    monkeypatch.setattr(session, "submit", spy)
    asked: list[Path] = []
    real = world.sessions._deps.trusted_repository
    delays = iter([0.15])

    async def slow_lookup(directory: Path, session_folder: Path) -> Any:
        asked.append(directory)
        await asyncio.sleep(next(delays, 0.0))
        return await real(directory, session_folder)

    world.sessions._deps.trusted_repository = slow_lookup
    first = asyncio.create_task(world.dispatch(reply("first", THREAD)))
    await asyncio.sleep(0.03)
    await world.dispatch(reply("second", THREAD))
    await first
    await world.settle(0.3)
    assert queued == ["first", "second"]
    looked_up = len(asked)  # the replies' own footers look a repository up when they end
    await asked_open(world, "!open zzz")
    assert len(asked) > looked_up  # the control: the lookup is counted, and a name makes one


async def test_open_alone_posts_a_button_and_reads_nothing_of_the_folder(
    world: World, project: Path
) -> None:
    await in_a_thread(world)
    asked: list[Path] = []
    real = world.sessions._deps.trusted_repository

    async def counted(directory: Path, session_folder: Path) -> Any:
        asked.append(directory)
        return await real(directory, session_folder)

    world.sessions._deps.trusted_repository = counted
    await asked_open(world, "!open")
    (post,) = picker_posts(world)
    assert post["thread_ts"] == THREAD and post["channel"] == CHANNEL
    section, actions, context = post["blocks"]
    assert section["text"]["text"] == "*Open a file*"
    (button,) = actions["elements"]
    assert button["type"] == "button" and button["text"]["text"] == "Choose a file"
    assert "value" not in button
    assert context["elements"][0]["text"] == "Or type `!open setup` to open a file by name."
    # The rows are read when the button is clicked, not when the message is posted.
    assert asked == [] and world.slack.calls_to("views.open") == []


async def test_the_button_opens_the_modal_with_the_files_changed_in_the_session(
    world: World, project: Path, modal: ModalSlack
) -> None:
    await in_a_thread(world)
    committed_since = put(project, "docs/guide.md")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "docs")
    older = put(project, "notes.txt")
    os.utime(committed_since, (1_790_000_100, 1_790_000_100))
    os.utime(older, (1_790_000_000, 1_790_000_000))
    body = choose_click()
    await world.dispatch(body)
    await world.settle(0.3)
    (asked,) = world.slack.calls_to("views.open")
    assert asked["trigger_id"] == body["trigger_id"]
    assert modal.rows() == ["docs/guide.md", "notes.txt"]
    assert modal.labels() == ["Changed in this session (2), newest first"]
    assert modal.view["callback_id"] == OPEN_FORM
    assert Target.load(modal.view["private_metadata"]) == OPEN_TARGET
    assert modal.view["blocks"][0]["element"]["focus_on_load"] is True
    assert "initial_value" not in modal.view["blocks"][0]["element"]
    assert modal.calls == []  # the rows were ready: no update after it
    assert world.ephemerals() == []


async def test_a_session_that_changed_nothing_gets_the_line_that_says_to_type(
    world: World, project: Path, modal: ModalSlack
) -> None:
    await in_a_thread(world)
    await world.dispatch(choose_click())
    await world.settle(0.3)
    assert modal.rows() == []
    assert modal.labels() == [texts.OPEN_TYPE_A_NAME]


async def test_the_changes_are_counted_from_where_head_was_when_the_thread_started(
    world: World, modal: ModalSlack
) -> None:
    # Nothing is remembered by the daemon: HEAD's log says where the repository stood at THREAD's
    # time (an epoch second), so a daemon restarted since gives the same answer.
    folder = world.root / "app"
    start = int(THREAD.split(".")[0])
    repo = git_init(folder / "workspace").resolve()
    commit_at(repo, "base.py", start - 1000)
    commit_at(repo, "before.py", start - 500)  # moved HEAD before the thread began
    commit_at(repo, "made_by_claude.py", start + 500)
    put(repo, "uncommitted.py")
    await in_a_thread(world)
    await world.dispatch(choose_click())
    await world.settle(0.3)
    assert sorted(modal.rows()) == ["workspace/made_by_claude.py", "workspace/uncommitted.py"]


async def test_a_repository_made_after_the_thread_began_lists_every_file_as_changed(
    world: World, modal: ModalSlack
) -> None:
    await in_a_thread(world)
    made = committed(world.root / "app")  # THREAD is long past: the first commit is the session's
    put(made, "new.py")
    await world.dispatch(choose_click())
    await world.settle(0.3)
    assert sorted(modal.rows()) == ["README", "new.py"]


async def test_a_folder_that_is_not_a_repository_has_no_changed_files(
    world: World, modal: ModalSlack
) -> None:
    await in_a_thread(world)
    put(world.root / "app", "plain.txt")
    await world.dispatch(choose_click())
    await world.settle(0.3)
    assert modal.rows() == [] and modal.labels() == [texts.OPEN_TYPE_A_NAME]


async def test_the_repositories_of_a_folder_are_looked_up_once_for_a_short_while(
    world: World, modal: ModalSlack
) -> None:
    folder = world.root / "app"
    made_before_the_thread(folder / "workspace")
    await in_a_thread(world)
    asked: list[Path] = []
    real = world.sessions._deps.trusted_repository

    async def counted(directory: Path, session_folder: Path) -> Any:
        asked.append(directory)
        return await real(directory, session_folder)

    world.sessions._deps.trusted_repository = counted
    await world.dispatch(choose_click())
    await world.settle(0.3)
    first = len(asked)
    assert first > 0
    await world.dispatch(choose_click())
    await world.settle(0.3)
    assert len(asked) == first  # the second click found them kept


async def test_the_changes_of_a_repository_inside_the_folder_are_listed(
    world: World, project: Path, modal: ModalSlack
) -> None:
    # The channel's folder is a plain folder that holds a repository one level down.
    folder = project.parent / "plain"
    folder.mkdir()
    world.state.bind(CHANNEL, folder)
    nested = made_before_the_thread(folder / "workspace")
    await in_a_thread(world)
    put(nested, "docs/new.md")
    put(folder, "notes.txt")  # outside any repository: not a change
    await world.dispatch(choose_click())
    await world.settle(0.3)
    assert modal.rows() == ["workspace/docs/new.md"]
    assert modal.labels() == ["Changed in this session (1), newest first"]


async def test_a_name_that_matches_several_files_posts_the_count_and_the_button(
    world: World, project: Path
) -> None:
    await in_a_thread(world)
    for name in ("docs/setup.md", "src/setup.py", "setup/readme.md"):
        put(project, name)
    await asked_open(world, "!open setup")
    assert opened(world) == []
    (post,) = picker_posts(world)
    assert post["thread_ts"] == THREAD
    section, actions = post["blocks"]
    assert section["text"]["text"] == "*3 files match* `setup`"
    (button,) = actions["elements"]
    assert button["text"]["text"] == "Choose a file" and button["value"] == "setup"
    assert world.slack.calls_to("views.open") == []  # the rows come when it is clicked


async def test_the_button_of_several_matches_opens_the_modal_on_them(
    world: World, project: Path, modal: ModalSlack
) -> None:
    await in_a_thread(world)
    for name in ("docs/setup.md", "src/setup.py", "setup/readme.md"):
        put(project, name)
    await world.dispatch(choose_click("setup"))
    await world.settle(0.3)
    field = modal.view["blocks"][0]["element"]
    assert field["initial_value"] == "setup"
    # The file names first, then the folder's file.
    assert modal.rows() == ["src/setup.py", "docs/setup.md", "setup/readme.md"]
    assert modal.labels() == ["3 files match"]


async def test_the_modal_of_a_plain_folder_lists_what_its_disk_holds(
    world: World, modal: ModalSlack
) -> None:
    await in_a_thread(world)
    for name in ("a/setup.md", "b/setup.py"):
        put(world.root / "app", name)
    await asked_open(world, "!open setup")
    (post,) = picker_posts(world)
    await world.dispatch(choose_click(post["blocks"][1]["elements"][0]["value"]))
    await world.settle(0.3)
    assert sorted(modal.rows()) == ["a/setup.md", "b/setup.py"]


async def test_more_than_ten_matches_list_ten_and_say_so(
    world: World, project: Path, modal: ModalSlack
) -> None:
    await in_a_thread(world)
    for i in range(130):
        put(project, f"data/part{i:03}.csv")
    await world.dispatch(choose_click("part"))
    await world.settle(0.3)
    assert len(modal.rows()) == 10
    assert modal.labels() == [
        "130 files match",
        texts.OPEN_MATCHES_CAPPED.format(shown=10, count=130),
    ]


async def test_a_listing_that_comes_late_fills_the_modal_after_it_opens(
    world: World, project: Path, modal: ModalSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The click's trigger_id lives 3 seconds (views.open reference): the modal opens without
    # the rows when they are not ready, and an update fills it.
    await in_a_thread(world)
    put(project, "src/parser.py")
    monkeypatch.setattr(slack_app_module, "OPEN_WAIT", 0.05)
    real = world.sessions._deps.trusted_repository

    async def slow(directory: Path, session_folder: Path) -> Any:
        await asyncio.sleep(0.4)
        return await real(directory, session_folder)

    world.sessions._deps.trusted_repository = slow
    await world.dispatch(choose_click("parser"))
    await world.settle(0.2)
    assert modal.labels() == [texts.OPEN_LOADING] and modal.written == []
    assert modal.view["blocks"][0]["element"]["initial_value"] == "parser"
    await world.settle(0.8)
    assert modal.rows() == ["src/parser.py"] and len(modal.written) == 1
    assert modal.calls[0][0] is None  # no hash: the daemon is the view's only writer
    assert "initial_value" not in modal.view["blocks"][0]["element"]  # what was typed stays


async def test_a_keystroke_typed_while_the_modal_is_still_filling_is_not_overwritten(
    world: World, project: Path, modal: ModalSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    await in_a_thread(world)
    put(project, "src/parser.py")
    put(project, "src/lexer.py")
    monkeypatch.setattr(slack_app_module, "OPEN_WAIT", 0.05)
    real = world.sessions._deps.trusted_repository

    async def slow(directory: Path, session_folder: Path) -> Any:
        await asyncio.sleep(0.4)
        return await real(directory, session_folder)

    world.sessions._deps.trusted_repository = slow
    await world.dispatch(choose_click("src"))
    await world.settle(0.1)
    await world.dispatch(typing("lex", hash_=modal.hash))
    await world.settle(1.0)
    assert modal.rows() == ["src/lexer.py"]
    assert [modal.rows(view) for _, view in modal.calls] == [["src/lexer.py"]]


async def test_the_files_are_listed_while_the_channel_is_checked(
    world: World, project: Path, modal: ModalSlack
) -> None:
    # The checks before the modal make two calls to Slack; the listing does not wait for them.
    await in_a_thread(world)
    listing_started = asyncio.Event()
    real = world.sessions._deps.trusted_repository

    async def watching(directory: Path, session_folder: Path) -> Any:
        listing_started.set()
        return await real(directory, session_folder)

    world.sessions._deps.trusted_repository = watching
    world.slack.gate = asyncio.Event()
    world.slack.gate_method = "conversations.info"
    await world.dispatch(choose_click())
    await asyncio.wait_for(world.slack.gated.wait(), 2)
    await asyncio.wait_for(listing_started.wait(), 2)  # while the channel check is still waiting
    assert world.slack.calls_to("views.open") == []
    world.slack.gate.set()
    await world.settle(0.3)
    assert modal.rows() == []  # nothing changed in this session
    (asked,) = world.slack.calls_to("views.open")
    assert modal.labels() == [texts.OPEN_TYPE_A_NAME] and asked["trigger_id"]


async def test_the_wait_for_the_rows_counts_from_the_click_and_not_from_the_channel_check(
    world: World, project: Path, modal: ModalSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The click's trigger_id lives 3 seconds: the check, the wait and views.open share them.
    await in_a_thread(world)
    put(project, "src/parser.py")
    monkeypatch.setattr(slack_app_module, "OPEN_WAIT", 0.5)
    real = world.sessions._deps.trusted_repository

    async def slow(directory: Path, session_folder: Path) -> Any:
        await asyncio.sleep(1.5)
        return await real(directory, session_folder)

    world.sessions._deps.trusted_repository = slow
    loop = asyncio.get_running_loop()
    opened_at: list[float] = []

    def stamped(args: dict[str, Any]) -> dict[str, Any]:
        opened_at.append(loop.time())
        return modal._open(args)

    world.slack.responses["views.open"] = stamped
    world.slack.gate = asyncio.Event()
    world.slack.gate_method = "conversations.info"
    clicked = loop.time()
    await world.dispatch(choose_click("parser"))
    await asyncio.sleep(0.35)  # the channel check takes this long
    world.slack.gate.set()
    await world.settle(2.0)
    assert opened_at and opened_at[0] - clicked < 0.7  # the check, then the rest of the wait
    assert modal.labels() == ["1 file matches"] and modal.rows() == ["src/parser.py"]


async def test_a_form_that_cannot_open_tells_the_owner_of_the_picker(
    world: World, project: Path, modal: ModalSlack
) -> None:
    await in_a_thread(world)
    world.slack.responses["views.open"] = {"ok": False, "error": "expired_trigger_id"}
    await world.dispatch(choose_click())
    await world.settle(0.2)
    assert world.ephemerals() == [texts.OPEN_FORM_NOT_OPENED.format(error="expired_trigger_id")]


@pytest.mark.parametrize(("user", "team"), [(STRANGER, TEAM), (OWNER, OTHER_TEAM)])
async def test_the_button_clicked_by_anyone_else_opens_nothing(
    world: World, project: Path, modal: ModalSlack, user: str, team: str
) -> None:
    await in_a_thread(world)
    asked: list[Path] = []
    real = world.sessions._deps.trusted_repository

    async def counted(directory: Path, session_folder: Path) -> Any:
        asked.append(directory)
        return await real(directory, session_folder)

    world.sessions._deps.trusted_repository = counted
    body = choose_click(id=user)
    body["team"]["id"] = team
    await world.dispatch(body)
    await world.settle(0.2)
    assert world.slack.calls_to("views.open") == [] and not world.ephemerals()
    assert asked == []  # the listing that starts early is the owner's alone


async def test_the_button_in_a_thread_that_holds_no_session_opens_nothing(
    world: World, project: Path, modal: ModalSlack
) -> None:
    await world.dispatch(choose_click())
    assert world.slack.calls_to("views.open") == []
    assert world.ephemerals() == [texts.NOT_A_SESSION]


async def test_the_button_in_a_refused_channel_opens_nothing(
    world: World, project: Path, modal: ModalSlack
) -> None:
    await in_a_thread(world)
    world.slack.responses["conversations.members"] = {"ok": True, "members": [OWNER, BOT, STRANGER]}
    await world.dispatch(choose_click())
    assert world.slack.calls_to("views.open") == []
    assert world.ephemerals() == [texts.CHANNEL_REFUSED.format(reason=texts.REASON_MEMBERS)]


async def test_typing_puts_the_files_that_match_into_the_rows(
    world: World, project: Path, modal: ModalSlack
) -> None:
    await in_a_thread(world)
    put(project, "src/parser.py")
    put(project, "src/lexer.py")
    put(project, ".gitignore", "*.log\n")
    put(project, "trace.log")
    shown = modal.hash
    await world.dispatch(typing("PARS", hash_=shown))
    await world.settle(0.3)
    assert modal.rows() == ["src/parser.py"] and modal.labels() == ["1 file matches"]
    assert modal.calls[0][0] is None  # the hash is optional, and the daemon's ordering decides
    # What the update sends keeps the field's ids and does not restate its text.
    field = modal.view["blocks"][0]
    assert (field["block_id"], field["element"]["action_id"]) == (QUERY_BLOCK, QUERY_ACTION)
    assert "initial_value" not in field["element"]
    await world.dispatch(typing("trace", 1790000101.0, hash_=modal.hash))  # there, and ignored
    await world.settle(0.3)
    assert modal.rows() == [] and modal.labels() == ["0 files match"]


async def test_the_typed_text_is_read_from_the_state_when_the_action_has_none(
    world: World, project: Path, modal: ModalSlack
) -> None:
    await in_a_thread(world)
    put(project, "src/parser.py")
    body = typing("pars", hash_=modal.hash)
    body["actions"][0].pop("value")
    await world.dispatch(body)
    await world.settle(0.3)
    assert modal.rows() == ["src/parser.py"]


async def test_an_emptied_field_lists_the_changed_files_again(
    world: World, project: Path, modal: ModalSlack
) -> None:
    await in_a_thread(world)
    put(project, "new.py")
    await world.dispatch(typing("", hash_=modal.hash))
    await world.settle(0.3)
    assert modal.rows() == ["new.py"]
    assert modal.labels() == ["Changed in this session (1), newest first"]


async def test_typing_lists_at_most_ten_files(
    world: World, project: Path, modal: ModalSlack
) -> None:
    await in_a_thread(world)
    for i in range(130):
        put(project, f"data/part{i:03}.csv")
    await world.dispatch(typing("part", hash_=modal.hash))
    await world.settle(0.3)
    assert len(modal.rows()) == 10 and modal.labels()[0] == "130 files match"


async def test_typing_checks_the_disk_only_until_the_rows_are_full(
    world: World, project: Path, modal: ModalSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    await in_a_thread(world)
    for i in range(300):
        put(project, f"data/part{i:03}.csv")
    checked: list[str] = []
    real = openfile_module._locate

    def counting(real_folder: Path, relative: str) -> Any:
        checked.append(relative)
        return real(real_folder, relative)

    monkeypatch.setattr(openfile_module, "_locate", counting)
    await world.dispatch(typing("part", 1790000100.0))
    await world.settle(0.3)
    assert len(modal.rows()) == 10
    assert len(checked) == 10  # not one for each of the 300 matches
    # The count is the matches by name once ten files are there to show.
    assert modal.labels() == [
        "300 files match",
        texts.OPEN_MATCHES_CAPPED.format(shown=10, count=300),
    ]


async def test_a_count_below_the_rows_is_exact_even_when_a_match_is_gone(
    world: World, project: Path, modal: ModalSlack
) -> None:
    # `git ls-files` still names a tracked file deleted from the work tree.
    await in_a_thread(world)
    for name in ("keep-a.py", "keep-b.py", "gone.py"):
        put(project, name)
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "three")
    (project / "gone.py").unlink()
    await world.dispatch(typing(".py", 1790000100.0))
    await world.settle(0.3)
    assert sorted(modal.rows()) == ["keep-a.py", "keep-b.py"] and modal.labels() == [
        "2 files match"
    ]


async def test_typing_leaves_out_a_file_whose_path_is_too_long_for_a_value(
    world: World, project: Path, modal: ModalSlack
) -> None:
    await in_a_thread(world)
    long_name = "z" * 140
    put(project, f"{long_name}/{long_name}.py")
    put(project, "short_z.py")
    await world.dispatch(typing("z", hash_=modal.hash))
    await world.settle(0.3)
    assert modal.rows() == ["short_z.py"]


async def test_typing_in_a_folder_with_no_repository_reads_the_disk(
    world: World, modal: ModalSlack
) -> None:
    await in_a_thread(world)
    plain = world.root / "app"
    put(plain, "plain.txt")
    put(plain, "sub/deep/plain-too.txt")
    await world.dispatch(typing("plain", hash_=modal.hash))
    await world.settle(0.3)
    assert sorted(modal.rows()) == ["plain.txt", "sub/deep/plain-too.txt"]


async def test_typing_in_a_repository_inside_the_folder_leaves_out_what_git_ignores(
    world: World, modal: ModalSlack
) -> None:
    folder = world.root / "app"
    nested = committed(folder / "workspace").resolve()
    put(nested, ".gitignore", ".venv/\n")
    put(nested, "docs/setup.md")
    put(nested, ".venv/lib/setup_tools.py")
    put(folder, "setup-notes.txt")
    await in_a_thread(world)
    await world.dispatch(typing("setup", hash_=modal.hash))
    await world.settle(0.3)
    assert sorted(modal.rows()) == ["setup-notes.txt", "workspace/docs/setup.md"]


async def test_typing_makes_no_call_to_slack_about_the_channel(
    world: World, project: Path, modal: ModalSlack
) -> None:
    # Each character comes here: the checks are the owner and the workspace alone.
    await in_a_thread(world)
    before = len(world.slack.calls)
    await world.dispatch(typing("read", hash_=modal.hash))
    await world.settle(0.3)
    assert modal.rows() == ["README"]
    methods = [m for m, _ in world.slack.calls[before:]]
    assert methods == ["views.update"]


@pytest.mark.parametrize(("user", "team"), [(STRANGER, TEAM), (OWNER, OTHER_TEAM)])
async def test_typing_by_anyone_else_updates_nothing(
    world: World, project: Path, modal: ModalSlack, user: str, team: str
) -> None:
    await in_a_thread(world)
    before = len(world.slack.calls)
    await world.dispatch(typing("READ", user=user, team=team, hash_=modal.hash))
    await world.settle(0.2)
    assert modal.calls == [] and len(world.slack.calls) == before


@pytest.mark.parametrize(
    "metadata",
    [
        "",
        "not json",
        '{"c": 1}',
        Target("C000ELSEWHERE", THREAD).dump(),  # a thread of another channel
        Target(CHANNEL, "1790000000.999999").dump(),  # a thread that is no session
    ],
)
async def test_typing_in_a_modal_whose_thread_is_not_a_session_updates_nothing(
    world: World, project: Path, modal: ModalSlack, metadata: str
) -> None:
    await in_a_thread(world)
    await world.dispatch(typing("READ", metadata=metadata, hash_=modal.hash))
    await world.settle(0.2)
    assert modal.calls == []


async def test_typing_resolves_in_the_folder_of_the_thread_s_own_session(
    world: World, project: Path, modal: ModalSlack
) -> None:
    # D5: the channel was bound to another folder after the thread began.
    await in_a_thread(world)
    elsewhere = world.root / "elsewhere"
    put(elsewhere, "only_there.py")
    put(project, "only_here.py")
    world.state.bind(CHANNEL, elsewhere)
    await world.dispatch(typing("only", hash_=modal.hash))
    await world.settle(0.3)
    assert modal.rows() == ["only_here.py"]


def three_files(project: Path) -> None:
    for name in ("one.py", "onetwo.py", "onetwothree.py"):
        put(project, name)


async def test_a_slow_update_is_followed_by_the_newest_text_and_never_by_an_older_one(
    world: World, project: Path, modal: ModalSlack
) -> None:
    # Two keystrokes whose payloads carry the same hash: the first update is slow, the second
    # waits behind it and lands after it, and the view's last word is the newest text. No update
    # is rejected: none carries a hash.
    await in_a_thread(world)
    three_files(project)
    world.slack.gate = asyncio.Event()
    world.slack.gate_method = "views.update"
    shown = modal.hash
    await world.dispatch(typing("one", 1790000100.0, hash_=shown))
    await asyncio.wait_for(world.slack.gated.wait(), 2)
    await world.dispatch(typing("onetwot", 1790000100.5, hash_=shown))
    await world.settle(0.1)
    assert modal.calls == []  # the first is still in flight
    world.slack.gate.set()
    await world.settle(0.4)
    assert [modal.rows(view) for _, view in modal.calls] == [
        ["one.py", "onetwo.py", "onetwothree.py"],
        ["onetwothree.py"],
    ]
    assert modal.rows() == ["onetwothree.py"] and len(modal.written) == 2
    assert {sent for sent, _ in modal.calls} == {None}


async def test_an_update_waiting_behind_a_newer_keystroke_is_never_made(
    world: World, project: Path, modal: ModalSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    await in_a_thread(world)
    three_files(project)
    listed: list[Path] = []
    listing = Listings.of

    async def watched(self: Listings, folder: Path, **kwargs: Any) -> Any:
        listed.append(folder)
        return await listing(self, folder, **kwargs)

    monkeypatch.setattr(Listings, "of", watched)
    world.slack.gate = asyncio.Event()
    world.slack.gate_method = "views.update"
    shown = modal.hash
    await world.dispatch(typing("one", 1790000100.0, hash_=shown))
    await asyncio.wait_for(world.slack.gated.wait(), 2)
    await world.dispatch(typing("onet", 1790000100.5, hash_=shown))
    await world.dispatch(typing("onetwot", 1790000101.0, hash_=shown))
    await world.settle(0.1)
    world.slack.gate.set()
    await world.settle(0.4)
    shown_rows = [modal.rows(view) for _, view in modal.calls]
    # The rows of "onet" (two files) were never written, nor even looked for.
    assert ["onetwo.py", "onetwothree.py"] not in shown_rows
    assert modal.rows() == ["onetwothree.py"]
    assert len(listed) == 2  # the first keystroke and the last


async def test_a_keystroke_that_arrives_after_a_newer_one_is_dropped(
    world: World, project: Path, modal: ModalSlack
) -> None:
    await in_a_thread(world)
    three_files(project)
    await world.dispatch(typing("onetwot", 1790000101.0, hash_=modal.hash))
    await world.settle(0.3)
    assert modal.rows() == ["onetwothree.py"]
    await world.dispatch(typing("one", 1790000100.0, hash_=modal.hash))  # older, delivered late
    await world.settle(0.3)
    assert len(modal.calls) == 1 and modal.rows() == ["onetwothree.py"]


async def test_a_keystroke_delivered_twice_is_written_once(
    world: World, project: Path, modal: ModalSlack
) -> None:
    # Slack sends an interaction again when its acknowledgement was missed.
    await in_a_thread(world)
    three_files(project)
    await world.dispatch(typing("onetwo", 1790000100.0, hash_=modal.hash))
    await world.dispatch(typing("onetwo", 1790000100.0, hash_=modal.hash))
    await world.settle(0.3)
    assert len(modal.calls) == 1


async def test_an_update_that_slack_rejects_for_any_other_reason_is_dropped_quietly(
    world: World, project: Path, modal: ModalSlack
) -> None:
    await in_a_thread(world)
    world.slack.responses["views.update"] = {"ok": False, "error": "not_found"}
    await world.dispatch(typing("read", hash_=modal.hash))
    await world.settle(0.2)
    assert len(world.slack.calls_to("views.update")) == 1 and world.ephemerals() == []


def test_an_interaction_is_dated_by_its_action_ts() -> None:
    assert action_key({"action_ts": "1790000100.500000"}) == 1790000100.5
    arrival = time.time()
    for action in ({}, {"action_ts": None}, {"action_ts": "soon"}):
        assert action_key(action) >= arrival


async def test_a_keystroke_that_arrives_after_the_modal_was_submitted_updates_nothing(
    world: World, project: Path, modal: ModalSlack
) -> None:
    await in_a_thread(world)
    put(project, "src/app.py")
    await world.dispatch(typing("src", 1790000100.0))
    await world.settle(0.3)
    assert len(modal.calls) == 1
    await world.dispatch(submitted("src/app.py"))
    await world.dispatch(typing("src/a", 1790000101.0))  # sent before the submit, delivered after
    await world.settle(0.3)
    assert len(modal.calls) == 1  # no update of a view that is closed


async def test_open_shares_the_chosen_file_and_closes_the_modal(
    world: World, project: Path, modal: ModalSlack
) -> None:
    await in_a_thread(world)
    put(project, "src/app.py")
    response = await world.dispatch(submitted("src/app.py"))
    assert response.status == 200 and "response_action" not in (response.body or "")
    (done,) = opened(world)
    assert done["thread_ts"] == THREAD and done["channel_id"] == CHANNEL
    assert json.loads(done["files"])[0]["title"] == "src/app.py"
    assert world.ephemerals() == []


async def test_open_with_no_row_chosen_shows_the_error_on_the_rows_and_shares_nothing(
    world: World, project: Path
) -> None:
    await in_a_thread(world)
    before = len(world.slack.calls)
    response = await world.dispatch(submitted(None, rows=["a.py"]))
    rows_block = shown_choice_id(modal_view(OPEN_TARGET, "", ["a.py"])["blocks"])
    assert json.loads(response.body) == {
        "response_action": "errors",
        "errors": {rows_block: texts.OPEN_NONE_CHOSEN},
    }
    assert opened(world) == []
    # The answer is the first thing sent, and nothing is sent after it.
    assert len(world.slack.calls) == before


async def test_open_with_no_rows_to_choose_from_shows_the_error_on_the_search_field(
    world: World, project: Path
) -> None:
    await in_a_thread(world)
    blocks = modal_view(OPEN_TARGET, "zz", [])["blocks"]  # no radio group in the view
    response = await world.dispatch(submitted(None, blocks=blocks))
    assert json.loads(response.body) == {
        "response_action": "errors",
        "errors": {QUERY_BLOCK: texts.OPEN_NONE_CHOSEN},
    }
    assert opened(world) == []


async def test_a_row_chosen_before_the_rows_changed_is_never_opened(
    world: World, project: Path
) -> None:
    # Slack keeps the state of an input block whose ids do not change across an update, so the
    # Submit can carry a row the view no longer shows. Both ways it can: under the id the older
    # rows had, and under the id of the rows now shown with a value that is not among them.
    await in_a_thread(world)
    put(project, "old.py")
    put(project, "new.py")
    now = ["new.py"]
    older_id = shown_choice_id(modal_view(OPEN_TARGET, "", ["old.py", "other.py"])["blocks"])
    now_id = shown_choice_id(modal_view(OPEN_TARGET, "", now)["blocks"])
    assert older_id != now_id
    for under in (older_id, now_id):
        before = len(world.slack.calls)
        response = await world.dispatch(submitted("old.py", rows=now, under=under))
        assert json.loads(response.body) == {
            "response_action": "errors",
            "errors": {now_id: texts.OPEN_NONE_CHOSEN},
        }
        assert opened(world) == [] and len(world.slack.calls) == before
    # The control: a row of the rows shown, chosen under their id, is opened.
    await world.dispatch(submitted("new.py", rows=now))
    assert len(opened(world)) == 1


@pytest.mark.parametrize(("user", "team"), [(STRANGER, TEAM), (OWNER, OTHER_TEAM)])
async def test_open_by_anyone_else_shares_nothing(
    world: World, project: Path, user: str, team: str
) -> None:
    await in_a_thread(world)
    put(project, "a.py")
    assert (await world.dispatch(submitted("a.py", user=user, team=team))).status == 200
    assert opened(world) == [] and world.slack.uploaded == [] and not world.ephemerals()
    # With nothing chosen they get no error to read either: a plain acknowledgement.
    unanswered = await world.dispatch(submitted(None, user=user, team=team))
    assert "response_action" not in (unanswered.body or "")


async def test_open_in_a_modal_with_metadata_that_is_not_ours_shares_nothing(
    world: World, project: Path
) -> None:
    await in_a_thread(world)
    put(project, "a.py")
    for metadata in ("", "not json", '{"c": "C000CHAN"}'):
        assert (await world.dispatch(submitted("a.py", metadata=metadata))).status == 200
    assert opened(world) == [] and world.slack.uploaded == []


async def test_open_in_a_thread_that_holds_no_session_shares_nothing(
    world: World, project: Path
) -> None:
    put(project, "a.py")
    await world.dispatch(submitted("a.py"))
    assert opened(world) == []
    assert world.ephemerals() == [texts.NOT_A_SESSION]


async def test_open_in_a_refused_channel_shares_nothing(world: World, project: Path) -> None:
    await in_a_thread(world)
    put(project, "a.py")
    world.slack.responses["conversations.members"] = {"ok": True, "members": [OWNER, BOT, STRANGER]}
    await world.dispatch(submitted("a.py"))
    assert opened(world) == []
    assert world.ephemerals() == [texts.CHANNEL_REFUSED.format(reason=texts.REASON_MEMBERS)]


@pytest.mark.parametrize(
    "value", ["../outside.txt", "link.txt", "dir-link/secret.txt", "/etc/hosts", "docs"]
)
async def test_a_value_that_leaves_the_folder_or_is_no_file_is_refused(
    world: World, project: Path, tmp_path: Path, value: str
) -> None:
    await in_a_thread(world)
    secret = put(tmp_path, "outside.txt")
    (project / "link.txt").symlink_to(secret)
    (project / "dir-link").symlink_to(tmp_path)
    (project / "docs").mkdir()
    await world.dispatch(submitted(value))
    assert opened(world) == [] and world.slack.uploaded == []
    assert world.ephemerals() == [texts.OPEN_NOT_A_FILE.format(path=value)]


async def test_open_resolves_in_the_folder_of_the_thread_s_own_session(
    world: World, project: Path
) -> None:
    # D5: the channel was bound to another folder after the thread began.
    await in_a_thread(world)
    elsewhere = world.root / "elsewhere"
    put(elsewhere, "only_there.py")
    put(project, "only_here.py")
    world.state.bind(CHANNEL, elsewhere)
    await world.dispatch(submitted("only_there.py"))
    assert opened(world) == []
    await world.dispatch(submitted("only_here.py"))
    assert len(opened(world)) == 1


async def test_the_message_of_open_stays_so_another_file_can_be_chosen(
    world: World, project: Path, modal: ModalSlack
) -> None:
    await in_a_thread(world)
    put(project, "a.py")
    put(project, "b.py")
    await asked_open(world, "!open")
    (post,) = picker_posts(world)
    await world.dispatch(submitted("a.py"))
    await world.dispatch(submitted("b.py"))
    assert len(opened(world)) == 2
    # Left as it was: neither deleted nor rewritten (the session's setup message is rewritten).
    assert world.slack.calls_to("chat.delete") == []
    rewritten = {call["ts"] for call in world.slack.calls_to("chat.update")}
    assert world.slack.posted_ts[-1] not in rewritten
    assert post["blocks"]


async def test_open_with_the_path_of_a_file_shares_it_into_the_thread(
    world: World, project: Path
) -> None:
    await in_a_thread(world)
    put(project, "docs/guide.md", "# Guide\n")
    posts = len(world.slack.calls_to("chat.postMessage"))
    await asked_open(world, "!open docs/guide.md")
    (asked,) = world.slack.calls_to("files.getUploadURLExternal")
    assert asked["filename"] == "guide.md" and asked["length"] == len(b"# Guide\n")
    assert [data for _, data in world.slack.uploaded] == [b"# Guide\n"]
    (done,) = opened(world)
    assert done["channel_id"] == CHANNEL and done["thread_ts"] == THREAD
    assert json.loads(done["files"]) == [{"id": "F000FILE", "title": "docs/guide.md"}]
    # No line of its own on success: the file is the answer.
    assert len(world.slack.calls_to("chat.postMessage")) == posts
    assert world.ephemerals() == []


async def test_a_path_needs_no_git(world: World) -> None:
    await in_a_thread(world)
    put(world.root / "app", "plain.txt")
    await asked_open(world, "!open plain.txt")
    assert len(opened(world)) == 1


async def test_a_name_that_matches_one_file_opens_it(world: World, project: Path) -> None:
    await in_a_thread(world)
    put(project, "src/Parser.py")
    put(project, "src/lexer.py")
    await asked_open(world, "!open PARS")
    (done,) = opened(world)
    assert json.loads(done["files"])[0]["title"] == "src/Parser.py"


async def test_a_file_made_after_a_name_matched_nothing_is_found_by_the_next_search(
    world: World, project: Path
) -> None:
    # A kept listing is never the reason for "no match": it is made again first.
    await in_a_thread(world)
    await asked_open(world, "!open zzz")
    assert world.ephemerals() == [texts.OPEN_NO_MATCH.format(words="zzz")]
    put(project, "src/zzz-made-since.py")
    await asked_open(world, "!open zzz")
    (done,) = opened(world)
    assert json.loads(done["files"])[0]["title"] == "src/zzz-made-since.py"


async def test_typing_finds_a_file_made_after_the_search_matched_nothing(
    world: World, project: Path, modal: ModalSlack
) -> None:
    await in_a_thread(world)
    await world.dispatch(typing("qqq", 1790000100.0))
    await world.settle(0.3)
    assert modal.rows() == [] and modal.labels() == ["0 files match"]
    put(project, "qqq-made-since.py")
    await world.dispatch(typing("qqq-", 1790000101.0))
    await world.settle(0.3)
    assert modal.rows() == ["qqq-made-since.py"]


def cut_walk(monkeypatch: pytest.MonkeyPatch, found: list[str]) -> None:
    """A walk of a plain folder that ran out of time with `found` in hand."""
    monkeypatch.setattr(openfile_module, "walk_files", lambda root, skip, expired: (found, False))


async def test_a_name_that_matches_nothing_in_a_listing_that_was_cut_says_so(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await in_a_thread(world)
    cut_walk(monkeypatch, ["a/other.py"])
    await asked_open(world, "!open nothing-like-it")
    assert opened(world) == []
    assert world.ephemerals() == [texts.OPEN_NO_MATCH_PARTIAL.format(words="nothing-like-it")]


async def test_the_only_match_of_a_listing_that_was_cut_is_not_opened_on_its_own(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await in_a_thread(world)
    put(world.root / "app", "a/one.py")
    cut_walk(monkeypatch, ["a/one.py"])
    await asked_open(world, "!open one")
    assert opened(world) == []
    (post,) = picker_posts(world)
    section, actions, context = post["blocks"]
    assert section["text"]["text"] == texts.OPEN_MATCHES_ONE.format(words="one")
    assert context["elements"][0]["text"] == texts.OPEN_PARTIAL
    assert actions["elements"][0]["value"] == "one"


async def test_a_listing_that_was_cut_is_said_in_the_modal_too(
    world: World, modal: ModalSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    await in_a_thread(world)
    put(world.root / "app", "a/one.py")
    cut_walk(monkeypatch, ["a/one.py"])
    await world.dispatch(typing("zzz", 1790000100.0))
    await world.settle(0.3)
    assert modal.labels() == ["0 files match", texts.OPEN_PARTIAL]
    await world.dispatch(typing("one", 1790000101.0))
    await world.settle(0.3)
    assert modal.rows() == ["a/one.py"] and modal.labels() == ["1 file matches", texts.OPEN_PARTIAL]


async def test_a_name_that_matches_nothing_says_so(world: World, project: Path) -> None:
    await in_a_thread(world)
    await asked_open(world, "!open nothing-like-it")
    assert opened(world) == []
    assert world.ephemerals() == [texts.OPEN_NO_MATCH.format(words="nothing-like-it")]


async def test_a_file_over_one_megabyte_is_refused(world: World, project: Path) -> None:
    await in_a_thread(world)
    (project / "big.log").write_bytes(b"x" * ((1 << 20) + 1))
    await asked_open(world, "!open big.log")
    assert world.slack.calls_to("files.getUploadURLExternal") == []
    assert world.ephemerals() == [texts.OPEN_TOO_LARGE.format(path="big.log")]


async def test_an_empty_file_is_refused_before_anything_is_uploaded(
    world: World, project: Path
) -> None:
    await in_a_thread(world)
    put(project, "empty.txt", "")
    await asked_open(world, "!open empty.txt")
    assert world.slack.calls_to("files.getUploadURLExternal") == [] and world.slack.uploaded == []
    assert world.ephemerals() == [texts.OPEN_EMPTY.format(path="empty.txt")]
    # Chosen in the modal it is the same file, with the same answer.
    await world.dispatch(submitted("empty.txt"))
    assert world.slack.uploaded == []
    assert world.ephemerals() == [texts.OPEN_EMPTY.format(path="empty.txt")] * 2


async def test_without_the_files_write_scope_the_owner_is_told_what_to_do(
    world: World, project: Path
) -> None:
    await in_a_thread(world)
    put(project, "a.md")
    world.slack.responses["files.getUploadURLExternal"] = {"ok": False, "error": "missing_scope"}
    await asked_open(world, "!open a.md")
    assert world.ephemerals() == [texts.OPEN_NO_SCOPE]
    assert "files:write" in texts.OPEN_NO_SCOPE and "slack-app-manifest.json" in texts.OPEN_NO_SCOPE


async def test_another_slack_failure_names_its_code_and_no_file_content(
    world: World, project: Path
) -> None:
    await in_a_thread(world)
    put(project, "a.md", "secret words\n")
    world.slack.responses["files.completeUploadExternal"] = {"ok": False, "error": "ratelimited"}
    await asked_open(world, "!open a.md")
    assert world.ephemerals() == [texts.OPEN_FAILED.format(path="a.md", error="ratelimited")]


async def test_a_word_outside_a_session_s_thread_is_answered_as_bypass_is(
    world: World, project: Path
) -> None:
    await world.dispatch(message("!open"))
    await world.dispatch(reply("!open", OTHER_THREAD))  # a thread that holds no session
    assert said(world) == [texts.OPEN_TOP_LEVEL, texts.OPEN_TOP_LEVEL]
    assert picker_posts(world) == [] and opened(world) == []


async def test_a_name_is_looked_up_in_a_folder_with_no_repository(world: World) -> None:
    await in_a_thread(world)
    plain = world.root / "app"
    put(plain, "docs/Guide.md")
    put(plain, "other.txt")
    await asked_open(world, "!open guide")
    (done,) = opened(world)
    assert json.loads(done["files"])[0]["title"] == "docs/Guide.md"
    assert world.ephemerals() == []


# Issue #142: which path answered `texts.HOLD_GONE` is read from the log, ids and flags only.
def _app_log(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "code_with_slack.slack_app"]


async def test_an_accepted_setup_start_is_logged(
    manual: World, caplog: pytest.LogCaptureFixture
) -> None:
    await manual.dispatch(message("SECRET-PROMPT-CONTENT", ts=THREAD))
    body = setup_click(manual)
    with caplog.at_level(logging.INFO, logger="code_with_slack"):
        await manual.dispatch(body)
    assert f"accepted a setup start in {CHANNEL}/{THREAD} on message {body['message']['ts']}" in (
        _app_log(caplog)
    )
    assert "SECRET-PROMPT-CONTENT" not in caplog.text


async def test_a_second_start_is_logged_as_accepted_then_refused(
    manual: World, caplog: pytest.LogCaptureFixture
) -> None:
    await manual.dispatch(message("SECRET-PROMPT-CONTENT", ts=THREAD))
    body = setup_click(manual)
    with caplog.at_level(logging.INFO, logger="code_with_slack"):
        await manual.dispatch(body)
        await manual.dispatch(body)
    lines = [m for m in _app_log(caplog) if "setup start" in m]
    assert lines[0].startswith("accepted a setup start")
    assert lines[1].startswith("refused a click (setup start, not ")
    assert (
        f"action {SETUP_START} in {CHANNEL}/{THREAD} on message {body['message']['ts']}"
        in (lines[1])
    )
    assert "SECRET-PROMPT-CONTENT" not in caplog.text


async def test_a_start_for_a_setup_no_longer_held_is_logged(
    manual: World, caplog: pytest.LogCaptureFixture
) -> None:
    await manual.dispatch(message("SECRET-PROMPT-CONTENT", ts=THREAD))
    body = setup_click(manual)
    body["actions"][0]["value"] = "no-such-setup"
    with caplog.at_level(logging.INFO, logger="code_with_slack"):
        await manual.dispatch(body)
    (line,) = [m for m in _app_log(caplog) if "refused a click" in m]
    assert "(setup start, not held)" in line and "held=False" in line
    assert "no-such-setup" not in caplog.text and "SECRET-PROMPT-CONTENT" not in caplog.text


async def test_a_start_that_resolve_refuses_is_logged_with_what_differs(
    manual: World, caplog: pytest.LogCaptureFixture
) -> None:
    await manual.dispatch(message("SECRET-PROMPT-CONTENT", ts=THREAD))
    with caplog.at_level(logging.INFO, logger="code_with_slack"):
        await manual.dispatch(setup_click(manual, thread_ts=OTHER_THREAD))
    (line,) = [m for m in _app_log(caplog) if "refused a click" in m]
    assert "(setup start, not resolved)" in line
    assert "held=True, decided=False, same_thread=False" in line
    assert "SECRET-PROMPT-CONTENT" not in caplog.text


async def test_a_model_change_after_start_is_logged(
    manual: World, caplog: pytest.LogCaptureFixture
) -> None:
    await manual.dispatch(message("SECRET-PROMPT-CONTENT", ts=THREAD))
    model = setup_click(manual, SETUP_MODEL, model="haiku")
    await manual.dispatch(setup_click(manual))
    await manual.settle(0.2)
    with caplog.at_level(logging.INFO, logger="code_with_slack"):
        await manual.dispatch(model)
    (line,) = [m for m in _app_log(caplog) if "refused a click" in m]
    assert "(setup model, no open setup)" in line
    assert f"action {SETUP_MODEL} in {CHANNEL}/{THREAD}" in line
    assert "open_at_message=False, held=False" in line
    assert "SECRET-PROMPT-CONTENT" not in caplog.text


async def test_a_second_hold_click_is_logged(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    await start_a_hold(world)
    hold_id = button_value(posted_blocks(world), HOLD_CONTINUE)
    body = click_in(HOLD_CONTINUE, hold_id, CHANNEL, THREAD)
    await world.dispatch(body)
    with caplog.at_level(logging.INFO, logger="code_with_slack"):
        await world.dispatch(body)
    (line,) = [m for m in _app_log(caplog) if "refused a click" in m]
    assert "(hold decision)" in line and f"action {HOLD_CONTINUE} in {CHANNEL}/{THREAD}" in line
    assert hold_id not in caplog.text and "hello" not in caplog.text
