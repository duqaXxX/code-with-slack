"""`claudeInChromeDefaultEnabled` is a Boolean key at the top level of `~/.claude.json` (Claude
Code settings reference, read 2026-10-09; seen as `true` in a real record on Claude Code 2.1.294
after `/chrome`, "Enabled by default")."""

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from awaydesk.chrome import chrome_enabled


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def record(home: Path, content: Any) -> None:
    (home / ".claude.json").write_text(json.dumps(content))


async def test_chrome_is_on_when_the_owner_enabled_it_by_default(home: Path) -> None:
    record(home, {"claudeInChromeDefaultEnabled": True, "projects": {}})
    assert await chrome_enabled(home)


@pytest.mark.parametrize(
    "content",
    [
        {"claudeInChromeDefaultEnabled": False},
        {"projects": {}},  # the key is unset until the owner chooses in `/chrome`
        {"claudeInChromeDefaultEnabled": "true"},
        {"claudeInChromeDefaultEnabled": 1},
        {"projects": {"/code/app": {"claudeInChromeDefaultEnabled": True}}},
        ["claudeInChromeDefaultEnabled"],
    ],
)
async def test_chrome_is_off_for_anything_but_the_key_set_to_true(home: Path, content: Any) -> None:
    record(home, content)
    assert not await chrome_enabled(home)


async def test_a_record_that_cannot_be_read_leaves_chrome_off(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        assert not await chrome_enabled(home)  # no file at all
        (home / ".claude.json").write_text("{not json")
        assert not await chrome_enabled(home)
    assert [r.getMessage() for r in caplog.records] == [
        "could not read Claude Code's Chrome setting: FileNotFoundError",
        "could not read Claude Code's Chrome setting: JSONDecodeError",
    ]


async def test_a_change_holds_from_the_next_read(home: Path) -> None:
    record(home, {"claudeInChromeDefaultEnabled": False})
    assert not await chrome_enabled(home)
    record(home, {"claudeInChromeDefaultEnabled": True})
    assert await chrome_enabled(home)
