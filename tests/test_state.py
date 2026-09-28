import json
import os
import stat
from pathlib import Path

import pytest

from code_with_slack.state import ChannelRecord, StateError, StateStore, ThreadState

CHANNEL = "C000CHAN"
OTHER_CHANNEL = "C000OTHR"
THREAD_TS = "1790549806.565369"
OTHER_THREAD_TS = "1790549900.100000"
SESSION = "4e8c1111-2222-3333-4444-555566667777"


def test_a_new_store_is_empty(tmp_path: Path) -> None:
    assert StateStore(tmp_path / "state.json").channel(CHANNEL) is None


def test_bind_creates_a_channel_with_no_threads(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.bind(CHANNEL, tmp_path / "project")
    assert store.channel(CHANNEL) == ChannelRecord(tmp_path / "project")


def test_open_thread_stores_the_channels_current_folder(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.bind(CHANNEL, tmp_path / "project")
    thread = store.open_thread(CHANNEL, THREAD_TS)
    assert thread == ThreadState(tmp_path / "project")
    assert store.thread(CHANNEL, THREAD_TS) == thread


def test_open_thread_on_an_unbound_channel_raises(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    with pytest.raises(KeyError):
        store.open_thread(CHANNEL, THREAD_TS)


def test_open_thread_is_idempotent(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.bind(CHANNEL, tmp_path / "project")
    first = store.open_thread(CHANNEL, THREAD_TS, session_id="ignored-on-repeat")
    second = store.open_thread(CHANNEL, THREAD_TS, session_id="also-ignored")
    assert first == second
    assert first.session_id == "ignored-on-repeat"


def test_a_thread_keeps_its_folder_across_a_rebind(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.bind(CHANNEL, tmp_path / "a")
    store.open_thread(CHANNEL, THREAD_TS)
    store.bind(CHANNEL, tmp_path / "b")
    assert store.channel(CHANNEL) == ChannelRecord(
        tmp_path / "b", threads={THREAD_TS: ThreadState(tmp_path / "a")}
    )
    assert store.thread(CHANNEL, THREAD_TS) == ThreadState(tmp_path / "a")


def test_set_session_bypass_and_effort_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.bind(CHANNEL, tmp_path / "project")
    store.open_thread(CHANNEL, THREAD_TS)
    store.set_session(CHANNEL, THREAD_TS, SESSION)
    store.set_bypass(CHANNEL, THREAD_TS, True)
    store.set_effort(CHANNEL, THREAD_TS, "high")

    reloaded = StateStore(path)
    assert reloaded.thread(CHANNEL, THREAD_TS) == ThreadState(
        tmp_path / "project", session_id=SESSION, bypass=True, effort="high"
    )


def test_set_session_bypass_and_effort_are_a_no_op_for_an_unknown_thread(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.bind(CHANNEL, tmp_path)
    store.set_session(CHANNEL, THREAD_TS, SESSION)
    store.set_bypass(CHANNEL, THREAD_TS, True)
    store.set_effort(CHANNEL, THREAD_TS, "high")
    assert store.thread(CHANNEL, THREAD_TS) is None


def test_set_session_does_not_rewrite_when_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.bind(CHANNEL, tmp_path)
    store.open_thread(CHANNEL, THREAD_TS)
    store.set_session(CHANNEL, THREAD_TS, SESSION)
    mtime = path.stat().st_mtime_ns
    store.set_session(CHANNEL, THREAD_TS, SESSION)
    assert path.stat().st_mtime_ns == mtime


def test_remove_thread(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.bind(CHANNEL, tmp_path)
    store.open_thread(CHANNEL, THREAD_TS)
    store.remove_thread(CHANNEL, THREAD_TS)
    assert store.thread(CHANNEL, THREAD_TS) is None
    # Removing again, or removing from an unbound channel, is a no-op.
    store.remove_thread(CHANNEL, THREAD_TS)
    store.remove_thread(OTHER_CHANNEL, THREAD_TS)


def test_holder_finds_a_session_across_channels(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.bind(CHANNEL, tmp_path / "a")
    store.bind(OTHER_CHANNEL, tmp_path / "b")
    store.open_thread(CHANNEL, THREAD_TS)
    store.open_thread(OTHER_CHANNEL, OTHER_THREAD_TS)
    store.set_session(OTHER_CHANNEL, OTHER_THREAD_TS, SESSION)
    assert store.holder(SESSION) == (OTHER_CHANNEL, OTHER_THREAD_TS)
    assert store.holder("no-such-session") is None


@pytest.mark.parametrize("value", ["true", "false", 1, None])
def test_only_a_literal_true_turns_bypass_on(tmp_path: Path, value: object) -> None:
    path = tmp_path / "state.json"
    thread = {"directory": str(tmp_path), "session_id": None, "bypass": value, "effort": None}
    channel = {"directory": str(tmp_path), "notice_pending": False, "threads": {THREAD_TS: thread}}
    path.write_text(json.dumps({"version": 2, "channels": {CHANNEL: channel}}))
    assert StateStore(path).thread(CHANNEL, THREAD_TS) == ThreadState(tmp_path, bypass=False)


def test_v2_round_trips_every_field(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.bind(CHANNEL, tmp_path / "project")
    store.open_thread(CHANNEL, THREAD_TS)
    store.set_session(CHANNEL, THREAD_TS, SESSION)
    store.set_bypass(CHANNEL, THREAD_TS, True)
    store.set_effort(CHANNEL, THREAD_TS, "low")

    data = json.loads(path.read_text())
    assert data == {
        "version": 2,
        "channels": {
            CHANNEL: {
                "directory": str(tmp_path / "project"),
                "notice_pending": False,
                "threads": {
                    THREAD_TS: {
                        "directory": str(tmp_path / "project"),
                        "session_id": SESSION,
                        "bypass": True,
                        "effort": "low",
                    }
                },
            }
        },
    }


def test_a_v1_file_migrates_folder_kept_session_and_bypass_dropped(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    v1 = {
        "version": 1,
        "channels": {
            CHANNEL: {"directory": str(tmp_path / "project"), "session_id": "old", "bypass": True}
        },
    }
    path.write_text(json.dumps(v1))

    store = StateStore(path)
    assert store.channel(CHANNEL) == ChannelRecord(tmp_path / "project", notice_pending=True)
    assert store.pending_notices() == [CHANNEL]

    # The migration is written back at once, as v2.
    on_disk = json.loads(path.read_text())
    assert on_disk == {
        "version": 2,
        "channels": {
            CHANNEL: {
                "directory": str(tmp_path / "project"),
                "notice_pending": True,
                "threads": {},
            }
        },
    }


def test_clear_notice(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "channels": {CHANNEL: {"directory": str(tmp_path), "session_id": None}},
            }
        )
    )
    store = StateStore(path)
    assert store.pending_notices() == [CHANNEL]
    store.clear_notice(CHANNEL)
    assert store.pending_notices() == []
    # A no-op for an unbound channel or one already clear.
    store.clear_notice(OTHER_CHANNEL)
    store.clear_notice(CHANNEL)


def test_an_unknown_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 3, "channels": {}}))
    with pytest.raises(StateError, match=r"state\.json"):
        StateStore(path)


def test_a_corrupt_file_is_refused_not_discarded(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json")
    with pytest.raises(StateError, match=r"state\.json"):
        StateStore(path)
    assert path.read_text() == "{not json"


def test_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert StateStore(tmp_path / "state.json").channel(CHANNEL) is None


def test_file_is_private_and_nothing_else_is_left_behind(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.bind(CHANNEL, tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]
    assert stat.S_IMODE((tmp_path / "state.json").stat().st_mode) == 0o600


def test_a_failed_write_keeps_the_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.bind(CHANNEL, tmp_path / "a")
    before = path.read_text()

    def crash(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        store.bind(CHANNEL, tmp_path / "b")
    assert path.read_text() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]


class TestPrune:
    """`prune` calls `alive` once per distinct folder and reports how many entries it removed."""

    def test_removes_a_thread_whose_session_is_gone(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "state.json")
        store.bind(CHANNEL, tmp_path / "project")
        store.open_thread(CHANNEL, THREAD_TS)
        store.set_session(CHANNEL, THREAD_TS, SESSION)

        removed = store.prune(alive=lambda _directory: [], now=1790549806.565369)
        assert removed == 1
        assert store.thread(CHANNEL, THREAD_TS) is None

    def test_keeps_a_thread_whose_session_is_alive(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "state.json")
        store.bind(CHANNEL, tmp_path / "project")
        store.open_thread(CHANNEL, THREAD_TS)
        store.set_session(CHANNEL, THREAD_TS, SESSION)

        removed = store.prune(alive=lambda _directory: [SESSION], now=1790549806.565369)
        assert removed == 0
        assert store.thread(CHANNEL, THREAD_TS) is not None

    def test_removes_a_no_session_thread_older_than_a_day(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "state.json")
        store.bind(CHANNEL, tmp_path / "project")
        store.open_thread(CHANNEL, THREAD_TS)

        root_time = float(THREAD_TS)
        removed = store.prune(alive=lambda _directory: [], now=root_time + 86_401)
        assert removed == 1
        assert store.thread(CHANNEL, THREAD_TS) is None

    def test_keeps_a_young_no_session_thread(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "state.json")
        store.bind(CHANNEL, tmp_path / "project")
        store.open_thread(CHANNEL, THREAD_TS)

        root_time = float(THREAD_TS)
        removed = store.prune(alive=lambda _directory: [], now=root_time + 3_600)
        assert removed == 0
        assert store.thread(CHANNEL, THREAD_TS) is not None

    def test_calls_alive_once_per_distinct_folder(self, tmp_path: Path) -> None:
        calls: list[Path] = []

        def alive(directory: Path) -> list[str]:
            calls.append(directory)
            return [SESSION]

        store = StateStore(tmp_path / "state.json")
        store.bind(CHANNEL, tmp_path / "project")
        store.open_thread(CHANNEL, THREAD_TS)
        store.set_session(CHANNEL, THREAD_TS, SESSION)
        store.open_thread(CHANNEL, OTHER_THREAD_TS)
        store.set_session(CHANNEL, OTHER_THREAD_TS, SESSION)

        removed = store.prune(alive=alive, now=1790549806.565369)
        assert removed == 0
        assert calls == [tmp_path / "project"]

    def test_alive_raising_leaves_the_file_untouched(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        store = StateStore(path)
        store.bind(CHANNEL, tmp_path / "project")
        store.open_thread(CHANNEL, THREAD_TS)
        store.set_session(CHANNEL, THREAD_TS, SESSION)
        before = path.read_text()

        def boom(_directory: Path) -> list[str]:
            raise RuntimeError("folder is gone")

        with pytest.raises(RuntimeError):
            store.prune(alive=boom, now=1790549806.565369)
        assert path.read_text() == before
        assert store.thread(CHANNEL, THREAD_TS) is not None
