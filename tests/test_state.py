import json
import os
import stat
from pathlib import Path

import pytest

from code_with_slack.state import ChannelState, StateError, StateStore


def test_a_new_store_is_empty(tmp_path: Path) -> None:
    assert StateStore(tmp_path / "state.json").get("C000CHAN") is None


def test_bind_then_session_survive_a_reload(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.bind("C000CHAN", tmp_path / "project")
    store.set_session("C000CHAN", "session-1")
    assert StateStore(path).get("C000CHAN") == ChannelState(tmp_path / "project", "session-1")


def test_rebinding_drops_the_session(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.bind("C000CHAN", tmp_path / "a")
    store.set_session("C000CHAN", "session-1")
    store.bind("C000CHAN", tmp_path / "b")
    assert store.get("C000CHAN") == ChannelState(tmp_path / "b", None)


def test_file_is_private_and_nothing_else_is_left_behind(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.bind("C000CHAN", tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]
    assert stat.S_IMODE((tmp_path / "state.json").stat().st_mode) == 0o600


def test_a_failed_write_keeps_the_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.bind("C000CHAN", tmp_path / "a")
    before = path.read_text()

    def crash(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        store.set_session("C000CHAN", "session-2")
    assert path.read_text() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]


def test_an_unchanged_session_does_not_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.bind("C000CHAN", tmp_path)
    store.set_session("C000CHAN", "s")
    mtime = path.stat().st_mtime_ns
    store.set_session("C000CHAN", "s")
    assert path.stat().st_mtime_ns == mtime


def test_a_corrupt_file_is_refused_not_discarded(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json")
    with pytest.raises(StateError, match=r"state\.json"):
        StateStore(path)
    assert path.read_text() == "{not json"


def test_the_file_holds_directory_and_session_only(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.bind("C000CHAN", tmp_path)
    store.set_session("C000CHAN", "s")
    data = json.loads(path.read_text())
    entry = {"directory": str(tmp_path), "session_id": "s"}
    assert data == {"version": 1, "channels": {"C000CHAN": entry}}
