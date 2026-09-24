"""Load the daemon's configuration from ~/.config/code-with-slack/.env."""

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

CONFIG_DIR = Path.home() / ".config" / "code-with-slack"
REQUIRED = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_OWNER_USER_ID", "ALLOWED_ROOT")


class ConfigError(Exception):
    """The configuration is missing, unsafe or invalid; the message says what to fix."""


@dataclass(frozen=True)
class Config:
    bot_token: str = field(repr=False)
    app_token: str = field(repr=False)
    owner_user_id: str
    allowed_root: Path
    config_dir: Path


def load_config(config_dir: Path = CONFIG_DIR) -> Config:
    """Read and validate `.env`; raises ConfigError, never touches os.environ."""
    env_path = config_dir / ".env"
    try:
        st = env_path.lstat()
    except FileNotFoundError:
        raise ConfigError(f"{env_path} does not exist; see docs/setup.md, Part 2") from None
    if not stat.S_ISREG(st.st_mode):
        raise ConfigError(f"{env_path} must be a regular file, not a link")
    if st.st_uid != os.getuid():
        raise ConfigError(f"{env_path} must belong to the user running code-with-slack")
    # The tokens drive a shell on this machine: nobody but the owner may read them.
    if st.st_mode & 0o077:
        raise ConfigError(f"{env_path} is readable by others; run: chmod 600 {env_path}")

    values = {k: v.strip() for k, v in dotenv_values(env_path).items() if v and v.strip()}
    missing = [k for k in REQUIRED if k not in values]
    if missing:
        raise ConfigError(f"missing in {env_path}: {', '.join(missing)}")
    if not values["SLACK_BOT_TOKEN"].startswith("xoxb-"):
        raise ConfigError("SLACK_BOT_TOKEN must be the Bot User OAuth Token (xoxb-...)")
    if not values["SLACK_APP_TOKEN"].startswith("xapp-"):
        raise ConfigError("SLACK_APP_TOKEN must be the app-level token (xapp-...)")
    root = Path(values["ALLOWED_ROOT"]).expanduser().resolve()
    if not root.is_dir():
        raise ConfigError(f"ALLOWED_ROOT is not a directory: {root}")
    return Config(
        bot_token=values["SLACK_BOT_TOKEN"],
        app_token=values["SLACK_APP_TOKEN"],
        owner_user_id=values["SLACK_OWNER_USER_ID"],
        allowed_root=root,
        config_dir=config_dir,
    )
