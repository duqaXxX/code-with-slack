"""Claude Code's Chrome integration, for the sessions the daemon starts.

Claude Code turns the integration on in an interactive session when the owner chose "Enabled by
default" in `/chrome`: the key `claudeInChromeDefaultEnabled` of `~/.claude.json` (settings
reference, read 2026-10-09). A session the Agent SDK starts is not interactive, and there the key
connects nothing: the built-in `claude-in-chrome` server is listed only when Claude Code is
started with `--chrome` (measured 2026-10-09, CLI 2.1.292). The daemon passes the flag when the
key is on, so a session from Slack has the browser where the terminal has it.

Nothing else is the daemon's. Which sites Claude may act on is the extension's own setting, and
whether an action asks first is the session's permission mode, as in the terminal: a browser
call that asks reaches Slack through `can_use_tool` like any other tool (measured the same day,
SDK 0.2.164).
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _enabled(home: Path) -> bool:
    try:
        record: Any = json.loads((home / ".claude.json").read_text())
    except (OSError, ValueError) as exc:
        logger.warning("could not read Claude Code's Chrome setting: %s", type(exc).__name__)
        return False
    return isinstance(record, dict) and record.get("claudeInChromeDefaultEnabled") is True


async def chrome_enabled(home: Path | None = None) -> bool:
    """Whether the owner turned Chrome on by default in Claude Code; False when its record cannot
    be read. Read at every call, so a change in `/chrome` holds from a session's next start."""
    return await asyncio.to_thread(_enabled, home or Path.home())
