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


def test_bypass_survives_a_reload_and_a_new_session_but_not_a_rebind(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.bind("C000CHAN", tmp_path / "a")
    store.set_bypass("C000CHAN", True)
    store.set_session("C000CHAN", "session-1")
    assert StateStore(path).get("C000CHAN") == ChannelState(tmp_path / "a", "session-1", True)
    store.bind("C000CHAN", tmp_path / "b")
    assert StateStore(path).get("C000CHAN") == ChannelState(tmp_path / "b", None, False)


@pytest.mark.parametrize("value", ["true", "false", 1, None])
def test_only_a_literal_true_turns_bypass_on(tmp_path: Path, value: object) -> None:
    path = tmp_path / "state.json"
    entry = {"directory": str(tmp_path), "session_id": None, "bypass": value}
    path.write_text(json.dumps({"version": 1, "channels": {"C000CHAN": entry}}))
    assert StateStore(path).get("C000CHAN") == ChannelState(tmp_path, None, False)


def test_a_file_written_before_bypass_was_stored_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    entry = {"directory": str(tmp_path), "session_id": "s"}
    path.write_text(json.dumps({"version": 1, "channels": {"C000CHAN": entry}}))
    assert StateStore(path).get("C000CHAN") == ChannelState(tmp_path, "s", False)


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


def test_the_file_holds_directory_session_and_bypass_only(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.bind("C000CHAN", tmp_path)
    store.set_session("C000CHAN", "s")
    store.set_bypass("C000CHAN", True)
    data = json.loads(path.read_text())
    entry = {"directory": str(tmp_path), "session_id": "s", "bypass": True}
    assert data == {"version": 1, "channels": {"C000CHAN": entry}}


def test_a_session_for_an_unbound_channel_is_not_recorded(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.set_session("C000NONE", "68da9311-c5e1-4465-a7e5-75d74e30aaa4")
    assert store.get("C000NONE") is None
