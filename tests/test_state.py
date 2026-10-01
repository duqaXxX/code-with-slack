import json
import os
import stat
from pathlib import Path

import pytest

from code_with_slack.state import ChannelRecord, StateError, StateStore, ThreadState, _parse_thread

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


@pytest.mark.parametrize("value", ["true", "false", 1, None, False])
def test_only_a_literal_true_turns_bypass_on_and_the_rest_is_unset(
    tmp_path: Path, value: object
) -> None:
    path = tmp_path / "state.json"
    thread = {"directory": str(tmp_path), "session_id": None, "bypass": value, "effort": None}
    channel = {"directory": str(tmp_path), "notice_pending": False, "threads": {THREAD_TS: thread}}
    path.write_text(json.dumps({"version": 2, "channels": {CHANNEL: channel}}))
    assert StateStore(path).thread(CHANNEL, THREAD_TS) == ThreadState(tmp_path, bypass=None)


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
                        "open_replies": [],
                        "requests": [],
                        "status": None,
                        "ended": None,
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


def test_a_v2_file_with_no_repair_fields_loads_with_them_empty(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "channels": {
                    CHANNEL: {
                        "directory": str(tmp_path / "project"),
                        "notice_pending": False,
                        "threads": {
                            THREAD_TS: {
                                "directory": str(tmp_path / "project"),
                                "session_id": SESSION,
                                "bypass": False,
                                "effort": None,
                            }
                        },
                    }
                },
            }
        )
    )
    thread = StateStore(path).thread(CHANNEL, THREAD_TS)
    assert thread == ThreadState(tmp_path / "project", session_id=SESSION)
    assert thread is not None
    assert thread.open_replies == ()
    assert thread.requests == ()
    assert thread.status is None


def test_a_file_with_1304c5e_s_single_open_reply_field_loads_as_nothing_open(
    tmp_path: Path,
) -> None:
    # That field never shipped past the fix round: a file written by it is read exactly like one
    # written before crash repair existed at all.
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "channels": {
                    CHANNEL: {
                        "directory": str(tmp_path / "project"),
                        "notice_pending": False,
                        "threads": {
                            THREAD_TS: {
                                "directory": str(tmp_path / "project"),
                                "session_id": None,
                                "bypass": False,
                                "effort": None,
                                "open_reply": "1790000000.000001",
                            }
                        },
                    }
                },
            }
        )
    )
    thread = StateStore(path).thread(CHANNEL, THREAD_TS)
    assert thread is not None
    assert thread.open_replies == ()


def test_repair_fields_round_trip_and_are_ignored_by_code_that_predates_them(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.bind(CHANNEL, tmp_path / "project")
    store.open_thread(CHANNEL, THREAD_TS)
    store.replace_open_reply(CHANNEL, THREAD_TS, None, "1790000000.000001")
    store.add_request(CHANNEL, THREAD_TS, "1790000000.000002")
    store.add_request(CHANNEL, THREAD_TS, "1790000000.000003")
    store.set_status_pending(CHANNEL, THREAD_TS, "hourglass_flowing_sand")

    reloaded = StateStore(path).thread(CHANNEL, THREAD_TS)
    assert reloaded == ThreadState(
        tmp_path / "project",
        open_replies=("1790000000.000001",),
        requests=("1790000000.000002", "1790000000.000003"),
        status="hourglass_flowing_sand",
    )
    # Code that only knows `_parse_thread`'s old fields ignores the extra keys.
    raw = json.loads(path.read_text())
    old_shape = {
        "directory": raw["channels"][CHANNEL]["threads"][THREAD_TS]["directory"],
        "session_id": raw["channels"][CHANNEL]["threads"][THREAD_TS]["session_id"],
        "bypass": raw["channels"][CHANNEL]["threads"][THREAD_TS]["bypass"],
        "effort": raw["channels"][CHANNEL]["threads"][THREAD_TS]["effort"],
    }
    assert _parse_thread(old_shape) == ThreadState(tmp_path / "project")


def test_replace_open_reply_tracks_two_sinks_independently(tmp_path: Path) -> None:
    # The bug the fix round measured: two replies open at once (a background task's own reply
    # outliving the turn that started it) must never step on each other's entry.
    store = StateStore(tmp_path / "state.json")
    store.bind(CHANNEL, tmp_path)
    store.open_thread(CHANNEL, THREAD_TS)
    store.replace_open_reply(CHANNEL, THREAD_TS, None, "a-1")  # sink A's first message
    store.replace_open_reply(CHANNEL, THREAD_TS, None, "b-1")  # sink B's first message
    assert set(store.thread(CHANNEL, THREAD_TS).requests) == set()  # sanity: unrelated field
    assert sorted(store.thread(CHANNEL, THREAD_TS).open_replies) == ["a-1", "b-1"]
    store.replace_open_reply(CHANNEL, THREAD_TS, "a-1", None)  # sink A settles; B untouched
    assert store.thread(CHANNEL, THREAD_TS).open_replies == ("b-1",)
    store.replace_open_reply(CHANNEL, THREAD_TS, "b-1", "b-2")  # sink B's continuation
    assert store.thread(CHANNEL, THREAD_TS).open_replies == ("b-2",)
    store.replace_open_reply(CHANNEL, THREAD_TS, "b-2", None)
    assert store.thread(CHANNEL, THREAD_TS).open_replies == ()


def test_replace_open_reply_is_a_no_op_that_changes_nothing_for_an_unknown_thread(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.json")
    store.bind(CHANNEL, tmp_path)
    store.replace_open_reply(CHANNEL, THREAD_TS, None, "ts-1")
    assert store.thread(CHANNEL, THREAD_TS) is None


def test_remove_request_drops_only_the_named_one(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.bind(CHANNEL, tmp_path / "project")
    store.open_thread(CHANNEL, THREAD_TS)
    store.add_request(CHANNEL, THREAD_TS, "ts-1")
    store.add_request(CHANNEL, THREAD_TS, "ts-2")
    store.remove_request(CHANNEL, THREAD_TS, "ts-1")
    thread = store.thread(CHANNEL, THREAD_TS)
    assert thread is not None
    assert thread.requests == ("ts-2",)
    store.remove_request(CHANNEL, THREAD_TS, "ts-1")  # already gone: a no-op
    assert store.thread(CHANNEL, THREAD_TS) == thread


def test_repair_setters_are_a_no_op_for_an_unknown_thread(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.bind(CHANNEL, tmp_path)
    store.replace_open_reply(CHANNEL, THREAD_TS, None, "ts-1")
    store.add_request(CHANNEL, THREAD_TS, "ts-1")
    store.set_status_pending(CHANNEL, THREAD_TS, "x")
    assert store.thread(CHANNEL, THREAD_TS) is None


def test_repairs_pending_lists_only_threads_with_something_open(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.bind(CHANNEL, tmp_path / "project")
    store.open_thread(CHANNEL, THREAD_TS)
    store.open_thread(CHANNEL, OTHER_THREAD_TS)
    store.replace_open_reply(CHANNEL, OTHER_THREAD_TS, None, "ts-1")
    assert store.repairs_pending() == [
        (CHANNEL, OTHER_THREAD_TS, store.thread(CHANNEL, OTHER_THREAD_TS))
    ]


def test_clear_repair_fields_clears_all_three_and_is_a_no_op_after(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.bind(CHANNEL, tmp_path / "project")
    store.open_thread(CHANNEL, THREAD_TS)
    store.replace_open_reply(CHANNEL, THREAD_TS, None, "ts-1")
    store.add_request(CHANNEL, THREAD_TS, "ts-2")
    store.set_status_pending(CHANNEL, THREAD_TS, "x")
    store.clear_repair(CHANNEL, THREAD_TS)
    assert store.thread(CHANNEL, THREAD_TS) == ThreadState(tmp_path / "project")
    assert StateStore(path).thread(CHANNEL, THREAD_TS) == ThreadState(tmp_path / "project")
    store.clear_repair(CHANNEL, THREAD_TS)  # already clear: a no-op, no extra write
    store.clear_repair("unknown-channel", THREAD_TS)  # unknown thread: a no-op too


def test_clear_repair_keeps_what_an_unlanded_answer_still_needs(tmp_path: Path) -> None:
    # An answer that never reached Slack leaves its open stream and the root's status for the
    # next start's repair; the requests, which a close deletes itself, are cleared.
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.bind(CHANNEL, tmp_path / "project")
    store.open_thread(CHANNEL, THREAD_TS)
    store.replace_open_reply(CHANNEL, THREAD_TS, None, "ts-1")
    store.add_request(CHANNEL, THREAD_TS, "ts-2")
    store.set_status_pending(CHANNEL, THREAD_TS, "x")
    store.clear_repair(CHANNEL, THREAD_TS, keep_open=True)
    kept = StateStore(path).thread(CHANNEL, THREAD_TS)
    assert kept == ThreadState(tmp_path / "project", open_replies=("ts-1",), status="x")


def test_prune_keeps_every_thread_of_a_folder_it_cannot_decide(tmp_path: Path) -> None:
    # `alive` returns None when it cannot tell which sessions a folder holds: pruning errs on
    # keeping, so none of that folder's threads is removed.
    store = StateStore(tmp_path / "state.json")
    store.bind("C000CHAN", tmp_path / "undecided")
    store.open_thread("C000CHAN", "1700000000.000100", session_id="kept-1")
    store.bind("C000CHAN", tmp_path / "decided")
    store.open_thread("C000CHAN", "1700000000.000200", session_id="gone-1")

    def alive(directory: Path) -> set[str] | None:
        return None if directory.name == "undecided" else set()

    assert store.prune(alive, 1700000000.0) == 1
    assert store.thread("C000CHAN", "1700000000.000100") is not None
    assert store.thread("C000CHAN", "1700000000.000200") is None


def written(path: Path) -> dict[str, object]:
    return json.loads(path.read_text())["channels"][CHANNEL]["threads"][THREAD_TS]


def test_an_explicit_off_is_kept_apart_from_never_chosen(tmp_path: Path) -> None:
    """`bypass` keeps its old meaning (`true` on, `false` not on); an explicit off (`!bypass off`
    or an unticked Start) adds `bypass_off: true`, which must survive a restart. The two keys are
    never both set, and clearing to unset removes `bypass_off`."""
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.bind(CHANNEL, tmp_path / "project")
    store.open_thread(CHANNEL, THREAD_TS)
    assert store.thread(CHANNEL, THREAD_TS).bypass is None
    assert written(path)["bypass"] is False and "bypass_off" not in written(path)
    store.set_bypass(CHANNEL, THREAD_TS, False)
    assert StateStore(path).thread(CHANNEL, THREAD_TS).bypass is False
    assert written(path)["bypass"] is False and written(path)["bypass_off"] is True
    store.set_bypass(CHANNEL, THREAD_TS, True)
    assert StateStore(path).thread(CHANNEL, THREAD_TS).bypass is True
    assert written(path)["bypass"] is True and "bypass_off" not in written(path)
    store.set_bypass(CHANNEL, THREAD_TS, None)
    assert StateStore(path).thread(CHANNEL, THREAD_TS).bypass is None
    assert written(path)["bypass"] is False and "bypass_off" not in written(path)
    assert json.loads(path.read_text())["version"] == 2


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ({"bypass": True}, True),
        ({"bypass": False}, None),  # an old file: a thread that never chose
        ({}, None),
        ({"bypass": False, "bypass_off": True}, False),
        ({"bypass_off": True}, False),
    ],
)
def test_bypass_is_read_from_the_two_keys(
    tmp_path: Path, keys: dict[str, object], expected: bool | None
) -> None:
    path = tmp_path / "state.json"
    thread = {"directory": str(tmp_path), "session_id": None, "effort": None, **keys}
    channel = {"directory": str(tmp_path), "notice_pending": False, "threads": {THREAD_TS: thread}}
    path.write_text(json.dumps({"version": 2, "channels": {CHANNEL: channel}}))
    assert StateStore(path).thread(CHANNEL, THREAD_TS).bypass is expected


def test_a_written_off_reads_as_not_on_through_bypass_alone(tmp_path: Path) -> None:
    """The old code looked at `bypass is True` and ignored every other key."""
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.bind(CHANNEL, tmp_path / "project")
    store.open_thread(CHANNEL, THREAD_TS)
    store.set_bypass(CHANNEL, THREAD_TS, False)
    assert written(path).get("bypass") is not True


def test_the_ended_reaction_round_trips_and_a_pending_one_replaces_it(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.bind(CHANNEL, tmp_path / "project")
    store.open_thread(CHANNEL, THREAD_TS)

    store.set_status_pending(CHANNEL, THREAD_TS, None, "white_check_mark")
    assert written(path)["status"] is None
    assert written(path)["ended"] == "white_check_mark"
    assert StateStore(path).thread(CHANNEL, THREAD_TS).ended == "white_check_mark"

    store.set_status_pending(CHANNEL, THREAD_TS, "hourglass_flowing_sand")
    assert written(path)["status"] == "hourglass_flowing_sand"
    assert written(path)["ended"] is None


def test_a_cross_shown_over_a_kept_status_keeps_both(tmp_path: Path) -> None:
    """An answer that never reached Slack: the root shows ❌ and crash repair still owes it."""
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.bind(CHANNEL, tmp_path / "project")
    store.open_thread(CHANNEL, THREAD_TS)
    store.set_status_pending(CHANNEL, THREAD_TS, "hourglass_flowing_sand", "x")
    assert (written(path)["status"], written(path)["ended"]) == ("hourglass_flowing_sand", "x")
    assert [ts for _, ts, _ in store.repairs_pending()] == [THREAD_TS]


def test_an_observer_that_raises_does_not_break_the_write(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = StateStore(tmp_path / "state.json")
    store.bind(CHANNEL, tmp_path / "project")

    def broken() -> None:
        raise RuntimeError("no running event loop")

    store.on_sessions_change = broken
    with caplog.at_level("WARNING"):
        store.open_thread(CHANNEL, THREAD_TS)
    assert store.thread(CHANNEL, THREAD_TS) is not None
    assert "RuntimeError" in caplog.text


def test_an_ended_reaction_is_nothing_for_crash_repair_to_find(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.bind(CHANNEL, tmp_path / "project")
    store.open_thread(CHANNEL, THREAD_TS)
    store.set_status_pending(CHANNEL, THREAD_TS, None, "x")
    assert store.repairs_pending() == []


def test_clear_repair_keeps_the_ended_reaction(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.bind(CHANNEL, tmp_path / "project")
    store.open_thread(CHANNEL, THREAD_TS)
    store.set_status_pending(CHANNEL, THREAD_TS, None, "x")
    store.add_request(CHANNEL, THREAD_TS, "1790549807.000001")
    store.clear_repair(CHANNEL, THREAD_TS)
    assert store.thread(CHANNEL, THREAD_TS).ended == "x"


def test_a_file_without_the_ended_key_loads_with_none(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    thread = {"directory": str(tmp_path), "session_id": SESSION, "status": None}
    channel = {"directory": str(tmp_path), "notice_pending": False, "threads": {THREAD_TS: thread}}
    path.write_text(json.dumps({"version": 2, "channels": {CHANNEL: channel}}))
    assert StateStore(path).thread(CHANNEL, THREAD_TS).ended is None


def test_threads_lists_every_thread_of_every_channel(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.bind(CHANNEL, tmp_path / "project")
    store.bind(OTHER_CHANNEL, tmp_path / "other")
    first = store.open_thread(CHANNEL, THREAD_TS)
    second = store.open_thread(OTHER_CHANNEL, OTHER_THREAD_TS)
    assert store.threads() == [
        (CHANNEL, THREAD_TS, first),
        (OTHER_CHANNEL, OTHER_THREAD_TS, second),
    ]


def test_the_observer_hears_what_the_session_index_shows_and_nothing_else(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    heard: list[str] = []
    store.bind(CHANNEL, tmp_path / "project")
    store.on_sessions_change = lambda: heard.append("changed")

    store.open_thread(CHANNEL, THREAD_TS)
    store.set_session(CHANNEL, THREAD_TS, SESSION)
    store.set_status_pending(CHANNEL, THREAD_TS, "hourglass_flowing_sand")
    store.set_status_pending(CHANNEL, THREAD_TS, None, "white_check_mark")
    assert len(heard) == 4

    # What no row of the index shows: a reply's bookkeeping, a request, bypass, effort, a rebind.
    store.replace_open_reply(CHANNEL, THREAD_TS, None, "1790549807.000001")
    store.add_request(CHANNEL, THREAD_TS, "1790549807.000002")
    store.set_bypass(CHANNEL, THREAD_TS, True)
    store.set_effort(CHANNEL, THREAD_TS, "low")
    store.bind(CHANNEL, tmp_path / "elsewhere")
    assert len(heard) == 4

    store.remove_thread(CHANNEL, THREAD_TS)
    assert len(heard) == 5


def test_the_observer_hears_a_prune_that_removed_something(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.bind(CHANNEL, tmp_path / "project")
    store.open_thread(CHANNEL, THREAD_TS, session_id=SESSION)
    heard: list[str] = []
    store.on_sessions_change = lambda: heard.append("changed")

    assert store.prune(lambda _directory: {SESSION}, now=float(THREAD_TS)) == 0
    assert heard == []
    assert store.prune(lambda _directory: set(), now=float(THREAD_TS)) == 1
    assert heard == ["changed"]
