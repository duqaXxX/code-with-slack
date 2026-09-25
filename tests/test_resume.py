import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SDKSessionInfo

from code_with_slack import resume, texts
from code_with_slack.resume import RESUME_ROWS, by_last_activity, matching, resume_blocks
from code_with_slack.sessions import directory_sessions

NOW = datetime(2026, 9, 25, 12, 0).astimezone()


def info(sid: str, summary: str, hours_ago: float, **fields: Any) -> SDKSessionInfo:
    """Session metadata as `list_sessions` returns it (SDK 0.2.158 SDKSessionInfo)."""
    modified = NOW - timedelta(hours=hours_ago)
    return SDKSessionInfo(
        session_id=sid, summary=summary, last_modified=int(modified.timestamp() * 1000), **fields
    )


def rows(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b for b in blocks if b.get("block_id", "").startswith("session-")]


def test_each_session_is_a_row_with_the_picker_s_columns_and_a_button() -> None:
    sessions = [
        info("68da9311-0000-4000-8000-000000000001", "Fix footer effort", 2, git_branch="main",
             file_size=412_000),
        info("68da9311-0000-4000-8000-000000000002", "Add trust gate", 26,
             git_branch="security-fixes", file_size=1_100_000),
    ]  # fmt: skip
    blocks = resume_blocks(Path("/srv/dev/app"), sessions, None, NOW)
    assert "/srv/dev/app" in blocks[0]["text"]["text"]
    first, second = rows(blocks)
    assert first["text"]["text"] == "Fix footer effort · 2 hours ago · main · 402.3KB"
    assert second["text"]["text"] == "Add trust gate · 1 day ago · security-fixes · 1.0MB"
    button = first["accessory"]
    assert button["action_id"] == "session_resume"
    assert button["value"] == "68da9311-0000-4000-8000-000000000001"
    assert button["text"]["text"] == texts.RESUME_BUTTON


def test_the_current_session_is_marked_and_has_no_button() -> None:
    sessions = [info("68da9311-0000-4000-8000-000000000001", "Now", 0.1)]
    (row,) = rows(resume_blocks(Path("/srv/dev/app"), sessions, sessions[0].session_id, NOW))
    assert "current" in row["text"]["text"] and "accessory" not in row


def test_the_branch_reads_as_the_terminal_shows_it() -> None:
    # The terminal's picker shows HEAD for a folder outside git, and the list does too.
    sessions = [
        info("68da9311-0000-4000-8000-000000000001", "Notes", 50, git_branch="HEAD",
             file_size=976_000),
    ]  # fmt: skip
    (row,) = rows(resume_blocks(Path("/srv/dev/notes"), sessions, None, NOW))
    assert row["text"]["text"] == "Notes · 2 days ago · HEAD · 953.1KB"


def test_only_the_newest_sessions_are_listed() -> None:
    sessions = [info(f"68da9311-0000-4000-8000-{i:012d}", f"s{i}", i) for i in range(25)]
    blocks = resume_blocks(Path("/srv/dev/app"), sessions, None, NOW)
    listed = rows(blocks)
    assert len(listed) == RESUME_ROWS == 20  # the maintainer, 2026-09-25: ten were too few
    assert listed[0]["accessory"]["value"].endswith("000000000000")
    assert blocks[-1]["text"]["text"] == texts.RESUME_MORE.format(rows=RESUME_ROWS)
    # An untitled session has no name to type and its id is not shown: only the terminal helps.
    assert "claude --resume" in texts.RESUME_MORE and "<title>" in texts.RESUME_MORE


def test_no_more_line_when_every_session_fits() -> None:
    sessions = [info(f"68da9311-0000-4000-8000-{i:012d}", f"s{i}", i) for i in range(RESUME_ROWS)]
    blocks = resume_blocks(Path("/srv/dev/app"), sessions, None, NOW)
    assert len(rows(blocks)) == RESUME_ROWS and "accessory" in blocks[-1]


def test_a_title_is_shown_as_written() -> None:
    sessions = [info("68da9311-0000-4000-8000-000000000001", "see <http://x|ok> ```", 1)]
    (row,) = rows(resume_blocks(Path("/srv/dev/app"), sessions, None, NOW))
    assert "&lt;http://x|ok&gt;" in row["text"]["text"] and "```" not in row["text"]["text"]


def test_no_session_yet_says_so() -> None:
    blocks = resume_blocks(Path("/srv/dev/app"), [], None, NOW)
    assert blocks[0]["text"]["text"] == texts.RESUME_EMPTY.format(directory="/srv/dev/app")
    assert rows(blocks) == []


def test_a_session_is_found_by_id_or_by_its_name() -> None:
    named = info("68da9311-0000-4000-8000-000000000001", "trust", 1, custom_title="trust")
    other = info("68da9311-0000-4000-8000-000000000002", "Other", 2)
    twin = info("68da9311-0000-4000-8000-000000000003", "twin", 3, custom_title="twin")
    twin2 = info("68da9311-0000-4000-8000-000000000004", "twin", 4, custom_title="twin")
    sessions = [named, other, twin, twin2]
    assert matching(sessions, other.session_id) == [other]
    assert matching(sessions, "trust") == [named]
    assert matching(sessions, "Other") == []  # a summary with no title (a first prompt) is no name
    assert matching(sessions, "twin") == [twin, twin2]


def write_transcript(config: Path, directory: Path, sid: str, lines: list[dict[str, Any]]) -> Path:
    """A session file where Claude Code keeps it (the SDK's own path rules, CLAUDE_CONFIG_DIR)."""
    from claude_agent_sdk._internal.sessions import _canonicalize_path, _get_project_dir

    folder = _get_project_dir(_canonicalize_path(str(directory)))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def message(kind: str, when: str, sid: str, text: str) -> dict[str, Any]:
    # The keys a transcript entry carries (read from real transcripts, CLI 2.1.280).
    return {
        "type": kind, "timestamp": when, "sessionId": sid, "uuid": f"{kind}-{when}",
        "parentUuid": None, "message": {"role": kind, "content": text},
    }  # fmt: skip


def ledger(sid: str) -> dict[str, Any]:
    # Claude Code appends these after the last message when a session's artifacts change,
    # with no timestamp (seen 2026-09-25): they move the file's mtime, not its activity.
    return {"type": "artifact-autoreact-ledger", "v": 1, "sessionId": sid, "artifacts": {}}


def test_a_session_s_time_is_its_last_message_not_its_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, project = tmp_path / "config", tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    old, new = "68da9311-0000-4000-8000-00000000000a", "68da9311-0000-4000-8000-00000000000b"
    stale = write_transcript(config, project, old, [
        message("user", "2026-09-22T15:01:10.504Z", old, "Livesqlbench"),
        message("assistant", "2026-09-22T15:40:44.477Z", old, "done"),
        ledger(old), ledger(old),
    ])  # fmt: skip
    fresh = write_transcript(config, project, new, [
        message("user", "2026-09-24T18:08:03.063Z", new, "Replies"),
        message("assistant", "2026-09-25T09:58:00.438Z", new, "ok"),
    ])  # fmt: skip
    os.utime(fresh, (1_790_000_000, 1_790_000_000))  # the older file on disk
    os.utime(stale, (1_790_100_000, 1_790_100_000))  # touched later by its ledger

    listed = by_last_activity(project, directory_sessions(project))
    assert [s.session_id for s in listed] == [new, old]  # ordered by activity, as the picker
    stamp = datetime(2026, 9, 22, 15, 40, 44, 477000, tzinfo=UTC).timestamp()
    assert listed[1].last_modified == int(stamp * 1000)


def test_dating_stops_once_the_rest_cannot_enter_the_list(monkeypatch: pytest.MonkeyPatch) -> None:
    # A file's mtime bounds its last message from above: past the list's rows, older files
    # cannot overtake them, so their transcripts are not read.
    sessions = [
        info(f"68da9311-0000-4000-8000-{i:012d}", f"s{i}", i) for i in range(RESUME_ROWS + 10)
    ]
    reads: list[Path] = []

    def last_message(path: Path) -> int | None:
        reads.append(path)
        return None  # no stamp: the mtime stands

    monkeypatch.setattr(resume, "_last_message_ms", last_message)
    monkeypatch.setattr(resume, "_find_project_dir", lambda _: Path("/sessions"))
    assert by_last_activity(Path("/srv/dev/app"), sessions) == sessions
    assert len(reads) == RESUME_ROWS


def test_the_branch_and_the_folder_are_shown_as_written() -> None:
    # git accepts `<`, `>` and `&` in a branch name; unescaped, `<!here>` would notify the channel.
    sessions = [info("68da9311-0000-4000-8000-000000000001", "t", 1, git_branch="fix/<!here>")]
    blocks = resume_blocks(Path("/srv/R&D"), sessions, None, NOW)
    assert "R&amp;D" in blocks[0]["text"]["text"]
    assert "fix/&lt;!here&gt;" in rows(blocks)[0]["text"]["text"]


def test_dates_falling_back_to_file_times_are_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A change in the SDK's private helpers must not bring the wrong dates back unnoticed.
    monkeypatch.setattr(resume, "_find_project_dir", lambda _: None)
    sessions = [info("68da9311-0000-4000-8000-000000000001", "s", 1)]
    with caplog.at_level("WARNING"):
        assert by_last_activity(Path("/srv/dev/app"), sessions) == sessions
    assert "file times" in caplog.text
