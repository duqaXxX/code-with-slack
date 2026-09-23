import os
from pathlib import Path

import pytest

from code_with_slack.config import ConfigError, load_config

BOT = "xox" + "b-000-fake"
APP = "xap" + "p-000-fake"


def write_env(directory: Path, root: Path, *, mode: int = 0o600, **overrides: str) -> Path:
    values = {
        "SLACK_BOT_TOKEN": BOT,
        "SLACK_APP_TOKEN": APP,
        "SLACK_OWNER_USER_ID": "U000ALICE",
        "ALLOWED_ROOT": str(root),
        **overrides,
    }
    env = directory / ".env"
    env.write_text("".join(f"{k}={v}\n" for k, v in values.items() if v))
    env.chmod(mode)
    return env


def test_loads_a_private_env(tmp_path: Path) -> None:
    write_env(tmp_path, tmp_path)
    config = load_config(tmp_path)
    assert config.owner_user_id == "U000ALICE"
    assert config.allowed_root == tmp_path.resolve()
    assert config.config_dir == tmp_path


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o644])
def test_refuses_an_env_readable_by_others(tmp_path: Path, mode: int) -> None:
    write_env(tmp_path, tmp_path, mode=mode)
    with pytest.raises(ConfigError, match="chmod 600"):
        load_config(tmp_path)


def test_refuses_a_symlinked_env(tmp_path: Path) -> None:
    real = write_env(tmp_path, tmp_path)
    link_dir = tmp_path / "link"
    link_dir.mkdir()
    (link_dir / ".env").symlink_to(real)
    with pytest.raises(ConfigError, match="regular file"):
        load_config(link_dir)


def test_missing_env_points_to_the_setup_guide(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"docs/setup\.md"):
        load_config(tmp_path)


def test_names_every_missing_variable(tmp_path: Path) -> None:
    write_env(tmp_path, tmp_path, SLACK_APP_TOKEN="", SLACK_OWNER_USER_ID="")
    with pytest.raises(ConfigError, match="SLACK_APP_TOKEN, SLACK_OWNER_USER_ID"):
        load_config(tmp_path)


def test_rejects_swapped_tokens(tmp_path: Path) -> None:
    write_env(tmp_path, tmp_path, SLACK_BOT_TOKEN=APP)
    with pytest.raises(ConfigError, match="SLACK_BOT_TOKEN"):
        load_config(tmp_path)


def test_expands_a_tilde_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "code").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    write_env(tmp_path, Path("~/code"))
    assert load_config(tmp_path).allowed_root == (tmp_path / "code").resolve()


def test_rejects_a_root_that_is_not_a_directory(tmp_path: Path) -> None:
    write_env(tmp_path, tmp_path / "nowhere")
    with pytest.raises(ConfigError, match="ALLOWED_ROOT"):
        load_config(tmp_path)


def test_repr_never_shows_a_token(tmp_path: Path) -> None:
    write_env(tmp_path, tmp_path)
    text = repr(load_config(tmp_path))
    assert BOT not in text and APP not in text


def test_does_not_touch_the_process_environment(tmp_path: Path) -> None:
    write_env(tmp_path, tmp_path)
    load_config(tmp_path)
    assert "SLACK_BOT_TOKEN" not in os.environ
