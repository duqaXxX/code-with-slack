import asyncio
import contextlib
import functools
import json
import logging
import time
from pathlib import Path

import pytest
from claude_agent_sdk import SDKSessionInfo

from code_with_slack import __main__ as entry
from code_with_slack import texts
from code_with_slack.home import Home
from code_with_slack.lock import single_instance
from code_with_slack.state import StateStore
from tests.fakes import CHANNEL, FakeSlack, slack_payload
from tests.test_sessions import until

ROOT = Path(__file__).resolve().parents[1]


def test_a_bad_config_exits_1_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(entry, "CONFIG_DIR", tmp_path)
    with pytest.raises(SystemExit) as exit_info, caplog.at_level(logging.ERROR):
        entry.main()
    assert exit_info.value.code == 1
    assert "docs/setup.md" in caplog.text


async def test_a_second_instance_stops_before_slack(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        f"SLACK_BOT_TOKEN={'xox' + 'b-1'}\nSLACK_APP_TOKEN={'xap' + 'p-1'}\n"
        f"SLACK_OWNER_USER_ID=U000ALICE\nALLOWED_ROOT={tmp_path}\n"
    )
    env.chmod(0o600)
    with single_instance(tmp_path), pytest.raises(entry.AlreadyRunning):
        await entry.run(tmp_path)


class _FakeHandler:
    """Stands in for AsyncSocketModeHandler: no real websocket, so `run` reaches `stop.wait()`."""

    def __init__(self, app: object, app_token: str) -> None:
        pass

    async def connect_async(self) -> None:
        return None

    async def close_async(self) -> None:
        return None


async def test_run_prunes_stale_threads_and_survives_alive_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    env = tmp_path / ".env"
    env.write_text(
        f"SLACK_BOT_TOKEN={'xox' + 'b-1'}\nSLACK_APP_TOKEN={'xap' + 'p-1'}\n"
        f"SLACK_OWNER_USER_ID=U000ALICE\nALLOWED_ROOT={tmp_path}\n"
    )
    env.chmod(0o600)
    # A bound channel with a stored session id: `run` must ask `_alive_sessions` about it before
    # connecting, and a broken transcript read there must not stop the daemon from starting.
    state = StateStore(tmp_path / "state.json")
    state.bind(CHANNEL, tmp_path)
    state.open_thread(CHANNEL, "1780000000.000001", session_id="some-id")

    def broken_list_sessions(*, directory: str, include_worktrees: bool) -> list[SDKSessionInfo]:
        raise PermissionError("transcripts unreadable")

    monkeypatch.setattr("code_with_slack.sessions.list_sessions", broken_list_sessions)
    monkeypatch.setattr(entry, "AsyncWebClient", lambda token, retry_handlers=None: FakeSlack())
    monkeypatch.setattr(entry, "AsyncSocketModeHandler", _FakeHandler)

    with caplog.at_level(logging.WARNING), pytest.raises(TimeoutError):
        await asyncio.wait_for(entry.run(tmp_path), timeout=1)
    assert "could not prune stale threads" in caplog.text


async def test_run_repairs_a_crashed_thread_before_pruning_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A thread with no session id, its root message older than a day (`state.ONE_DAY`): prune
    # drops it. Repair must still act on it first (issue #19 ruling 5), since it is exactly what
    # a crash mid-first-turn leaves.
    env = tmp_path / ".env"
    env.write_text(
        f"SLACK_BOT_TOKEN={'xox' + 'b-1'}\nSLACK_APP_TOKEN={'xap' + 'p-1'}\n"
        f"SLACK_OWNER_USER_ID=U000ALICE\nALLOWED_ROOT={tmp_path}\n"
    )
    env.chmod(0o600)
    state = StateStore(tmp_path / "state.json")
    state.bind(CHANNEL, tmp_path)
    state.open_thread(CHANNEL, "1000000000.000001")
    state.set_status_pending(CHANNEL, "1000000000.000001", "raised_hand")

    fake_slack = FakeSlack()
    monkeypatch.setattr(entry, "AsyncWebClient", lambda token, retry_handlers=None: fake_slack)
    monkeypatch.setattr(entry, "AsyncSocketModeHandler", _FakeHandler)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(entry.run(tmp_path), timeout=1)

    added = [a["name"] for a in fake_slack.calls_to("reactions.add")]
    assert added == ["x"]
    # Repaired, then pruned: nothing left of the thread at all.
    assert StateStore(tmp_path / "state.json").thread(CHANNEL, "1000000000.000001") is None


async def test_run_publishes_the_session_index_once_started_and_after_a_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text(
        f"SLACK_BOT_TOKEN={'xox' + 'b-1'}\nSLACK_APP_TOKEN={'xap' + 'p-1'}\n"
        f"SLACK_OWNER_USER_ID=U000ALICE\nALLOWED_ROOT={tmp_path}\n"
    )
    env.chmod(0o600)
    thread_ts = "1780000000.000001"
    state = StateStore(tmp_path / "state.json")
    state.bind(CHANNEL, tmp_path)
    state.open_thread(CHANNEL, thread_ts, session_id="some-id")
    state.set_status_pending(CHANNEL, thread_ts, "hourglass_flowing_sand")  # a crash left it

    def one_session(*, directory: str, include_worktrees: bool) -> list[SDKSessionInfo]:
        now_ms = int(time.time() * 1000)  # inside the 48 hours the page starts on
        return [
            SDKSessionInfo(session_id="some-id", summary="Fix the footer", last_modified=now_ms)
        ]

    fake_slack = FakeSlack()
    # The thread's root as Slack returns it, its last reply now: inside the page's 48 hours.
    root = {**slack_payload("api-conversations-replies-root")["messages"][0], "ts": thread_ts}
    root = {**root, "thread_ts": thread_ts, "latest_reply": f"{time.time():.6f}", "reactions": []}
    fake_slack.responses["conversations.replies"] = {"ok": True, "messages": [root]}
    stores: list[StateStore] = []

    def recording_store(path: Path) -> StateStore:
        stores.append(StateStore(path))
        return stores[-1]

    monkeypatch.setattr("code_with_slack.sessions.list_sessions", one_session)
    monkeypatch.setattr(entry, "AsyncWebClient", lambda token, retry_handlers=None: fake_slack)
    monkeypatch.setattr(entry, "AsyncSocketModeHandler", _FakeHandler)
    monkeypatch.setattr(entry, "StateStore", recording_store)
    monkeypatch.setattr(entry, "Home", functools.partial(Home, debounce=0.01))

    def first_lines() -> list[str]:
        return [
            block["text"]["text"].splitlines()[0]
            for args in fake_slack.calls_to("views.publish")
            for block in args["view"]["blocks"]
            if block["type"] == "section" and "accessory" not in block
        ]

    running = asyncio.create_task(entry.run(tmp_path))
    try:
        # Published after the repair: the root a crash left ⏳ already reads ❌.
        await until(lambda: first_lines() == [":x:  *Fix the footer*"])
        assert fake_slack.calls_to("views.publish")[0]["user_id"] == "U000ALICE"
        stores[0].set_status_pending(CHANNEL, thread_ts, "raised_hand")
        await until(lambda: first_lines()[-1] == ":raised_hand:  *Fix the footer*")
    finally:
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running


async def test_repair_and_prune_run_before_the_socket_mode_connection_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fix round 2 item 6: the exact order named in the fix brief, not just repair-before-prune.
    env = tmp_path / ".env"
    env.write_text(
        f"SLACK_BOT_TOKEN={'xox' + 'b-1'}\nSLACK_APP_TOKEN={'xap' + 'p-1'}\n"
        f"SLACK_OWNER_USER_ID=U000ALICE\nALLOWED_ROOT={tmp_path}\n"
    )
    env.chmod(0o600)
    monkeypatch.setattr(entry, "AsyncWebClient", lambda token, retry_handlers=None: FakeSlack())

    order: list[str] = []

    async def fake_repair_crash(slack: object, state: object, limiter: object) -> None:
        order.append("repair")

    class OrderedFakeHandler(_FakeHandler):
        async def connect_async(self) -> None:
            order.append("connect")

    monkeypatch.setattr(entry, "repair_crash", fake_repair_crash)
    monkeypatch.setattr(entry, "AsyncSocketModeHandler", OrderedFakeHandler)
    real_prune = entry.StateStore.prune

    def recording_prune(self: object, *a: object, **k: object) -> int:
        order.append("prune")
        return real_prune(self, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(entry.StateStore, "prune", recording_prune)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(entry.run(tmp_path), timeout=1)
    assert order == ["repair", "prune", "connect"]


async def test_signal_handlers_are_installed_before_a_long_repair_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fix round 2 item 5: installed before repair, not after, so a SIGTERM arriving during a long
    # repair is caught instead of terminating the process under Python's default disposition,
    # which would skip every `finally` (the single-instance lock, Socket Mode, every session).
    env = tmp_path / ".env"
    env.write_text(
        f"SLACK_BOT_TOKEN={'xox' + 'b-1'}\nSLACK_APP_TOKEN={'xap' + 'p-1'}\n"
        f"SLACK_OWNER_USER_ID=U000ALICE\nALLOWED_ROOT={tmp_path}\n"
    )
    env.chmod(0o600)
    monkeypatch.setattr(entry, "AsyncWebClient", lambda token, retry_handlers=None: FakeSlack())
    monkeypatch.setattr(entry, "AsyncSocketModeHandler", _FakeHandler)

    order: list[str] = []
    loop = asyncio.get_running_loop()

    def recording_add_signal_handler(sig: object, callback: object, *args: object) -> None:
        order.append("signal")

    monkeypatch.setattr(loop, "add_signal_handler", recording_add_signal_handler)

    async def fake_repair_crash(slack: object, state: object, limiter: object) -> None:
        order.append("repair")

    monkeypatch.setattr(entry, "repair_crash", fake_repair_crash)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(entry.run(tmp_path), timeout=1)
    assert order == ["signal", "signal", "repair"]  # SIGTERM and SIGINT, both before repair


def test_no_name_carries_claude_code() -> None:
    manifest = json.loads((ROOT / "slack-app-manifest.json").read_text())
    names = [
        manifest["display_information"]["name"],
        manifest["features"]["bot_user"]["display_name"],
    ]
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert all("claude code" not in n.lower() for n in names)
    assert 'name = "code-with-slack"' in pyproject


def test_the_manifest_asks_for_the_minimum() -> None:
    manifest = json.loads((ROOT / "slack-app-manifest.json").read_text())
    assert sorted(manifest["oauth_config"]["scopes"]["bot"]) == [
        "chat:write",
        "files:read",  # downloading the files attached to a message (the maintainer, 2026-09-25)
        "groups:history",
        "groups:read",
        "reactions:write",  # the status reaction on a session's root message (D10)
    ]
    assert manifest["settings"]["event_subscriptions"]["bot_events"] == ["message.groups"]
    assert manifest["settings"]["is_mcp_enabled"] is False
    # The session index is the app's Home tab: published with no scope and no event
    # (views.publish reference, read 2026-10-01). Nobody writes to the app in its Messages tab.
    assert manifest["features"]["app_home"] == {
        "home_tab_enabled": True,
        "messages_tab_enabled": True,
        "messages_tab_read_only_enabled": True,
    }
    # Replies are plain messages in the main window: no agent view, no task cards.
    assert "agent_view" not in manifest["features"]
    # Commands are typed as `!word` messages: the app registers no slash command.
    assert "slash_commands" not in manifest["features"]


def _v1_state(tmp_path: Path, channel: str = CHANNEL) -> StateStore:
    """A v1 `state.json`, migrated on load: each of its channels gets a pending D7 notice."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 1, "channels": {channel: {"directory": str(tmp_path)}}}))
    return StateStore(path)


async def test_the_upgrade_notice_posts_top_level_and_clears_the_flag(tmp_path: Path) -> None:
    state = _v1_state(tmp_path)
    assert state.pending_notices() == [CHANNEL]
    slack = FakeSlack()
    await entry._post_upgrade_notices(slack, state)
    (post,) = slack.calls_to("chat.postMessage")
    assert post["channel"] == CHANNEL
    assert post.get("thread_ts") is None  # not a reply to any message
    assert post["text"] == texts.UPGRADE_NOTICE
    assert state.pending_notices() == []


async def test_a_failed_upgrade_notice_keeps_the_flag_for_next_start(tmp_path: Path) -> None:
    state = _v1_state(tmp_path)
    slack = FakeSlack()
    slack.responses["chat.postMessage"] = RuntimeError("network down")
    await entry._post_upgrade_notices(slack, state)
    assert state.pending_notices() == [CHANNEL]


def test_alive_sessions_reads_directory_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_list_sessions(*, directory: str, include_worktrees: bool) -> list[SDKSessionInfo]:
        assert directory == str(tmp_path) and include_worktrees is False
        return [SDKSessionInfo("sid-1", "", 0, 1), SDKSessionInfo("sid-2", "", 0, 1)]

    monkeypatch.setattr("code_with_slack.sessions.list_sessions", fake_list_sessions)
    assert entry._alive_sessions(tmp_path) == {"sid-1", "sid-2"}


def test_alive_sessions_keeps_a_transcript_list_sessions_filters_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `list_sessions` skips sidechain and metadata-only sessions (claude-agent-sdk 0.2.160,
    # `_internal/sessions.py`, read 2026-09-28): a session real enough to be stored in a thread
    # must never be pruned just because it has not built up a title yet.
    from claude_agent_sdk._internal.sessions import _canonicalize_path, _get_project_dir

    config, project = tmp_path / "config", tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    folder = _get_project_dir(_canonicalize_path(str(project)))
    folder.mkdir(parents=True)
    (folder / "68da9311-0000-4000-8000-000000000001.jsonl").write_text('{"type": "system"}\n')

    def fake_list_sessions(*, directory: str, include_worktrees: bool) -> list[SDKSessionInfo]:
        return []  # filtered out by the SDK's own listing rules, not actually gone

    monkeypatch.setattr("code_with_slack.sessions.list_sessions", fake_list_sessions)
    assert entry._alive_sessions(project) == {"68da9311-0000-4000-8000-000000000001"}


def test_prune_uses_alive_sessions_to_drop_a_gone_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = StateStore(tmp_path / "state.json")
    state.bind(CHANNEL, tmp_path)
    state.open_thread(CHANNEL, "1780000000.000001", session_id="gone")

    def fake_list_sessions(*, directory: str, include_worktrees: bool) -> list[SDKSessionInfo]:
        return []

    monkeypatch.setattr("code_with_slack.sessions.list_sessions", fake_list_sessions)
    removed = state.prune(entry._alive_sessions, time.time())
    assert removed == 1
    assert state.thread(CHANNEL, "1780000000.000001") is None


def test_alive_sessions_cannot_decide_a_long_folder_it_does_not_find(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Past 200 sanitized characters the CLI names the project folder with a hash the SDK does
    # not reproduce (`_find_project_dir` docstring, claude-agent-sdk 0.2.160): a missing folder
    # there proves nothing, so `_alive_sessions` answers None and prune keeps the threads.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
    project = tmp_path / ("p" * 220)
    monkeypatch.setattr("code_with_slack.sessions.list_sessions", lambda **_: [])
    assert entry._alive_sessions(project) is None


def test_alive_sessions_decides_a_short_folder_it_does_not_find(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("code_with_slack.sessions.list_sessions", lambda **_: [])
    assert entry._alive_sessions(tmp_path / "project") == set()


def test_only_the_reply_client_skips_the_connection_retry_of_a_post() -> None:
    import asyncio as aio

    import aiohttp
    from slack_sdk.http_retry.request import HttpRequest
    from slack_sdk.http_retry.state import RetryState

    shared, replies = entry.make_clients("xoxb-" + "fake")
    error = aiohttp.ClientOSError(104, "Connection reset by peer")
    post = HttpRequest(
        method="POST",
        url="https://slack.com/api/chat.postMessage",
        headers={},
        body_params={},
        data={},
    )

    async def retried(client: object) -> bool:
        handlers = client.retry_handlers  # type: ignore[attr-defined]
        return any(
            [
                await h.can_retry_async(state=RetryState(), request=post, error=error)
                for h in handlers
            ]
        )

    # an approval or a question posted through the shared client is still retried after a reset
    assert aio.run(retried(shared)) is True
    assert aio.run(retried(replies)) is False
