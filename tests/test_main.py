import asyncio
import json
import logging
import time
from pathlib import Path

import pytest
from claude_agent_sdk import SDKSessionInfo

from code_with_slack import __main__ as entry
from code_with_slack import texts
from code_with_slack.lock import single_instance
from code_with_slack.state import StateStore
from tests.fakes import CHANNEL, FakeSlack

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
    monkeypatch.setattr(entry, "AsyncWebClient", lambda token: FakeSlack())
    monkeypatch.setattr(entry, "AsyncSocketModeHandler", _FakeHandler)

    with caplog.at_level(logging.WARNING), pytest.raises(TimeoutError):
        await asyncio.wait_for(entry.run(tmp_path), timeout=1)
    assert "could not prune stale threads" in caplog.text


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
    ]
    assert manifest["settings"]["event_subscriptions"]["bot_events"] == ["message.groups"]
    assert manifest["settings"]["is_mcp_enabled"] is False
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
