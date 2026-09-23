import json
import logging
from pathlib import Path

import pytest

from code_with_slack import __main__ as entry
from code_with_slack.lock import single_instance

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
        "commands",
        "groups:history",
        "groups:read",
    ]
    assert manifest["settings"]["event_subscriptions"]["bot_events"] == ["message.groups"]
    assert manifest["settings"]["is_mcp_enabled"] is False
    # Replies are plain messages in the main window: no agent view, no task cards.
    assert "agent_view" not in manifest["features"]
