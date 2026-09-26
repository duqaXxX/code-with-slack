"""`!resume`: Claude Code's session picker, as a message with a Resume button per session.

The terminal's `/resume` is interactive and the SDK does not offer it (not among the session's
commands, Claude Code 2.1.280), so the daemon answers the word itself, the way the terminal
does: `/resume` alone opens the picker, `/resume <session>` resumes by id or name (commands
reference, read 2026-09-25). Each row shows what a picker row shows: the session's name or
title, the time since its last activity, its git branch and its size (sessions reference, read
2026-09-25). The rows come from the SDK's `list_sessions`, newest first.
"""

import dataclasses
import heapq
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import SDKSessionInfo

# The SDK's own rules for where a directory's transcripts live (CLAUDE_CONFIG_DIR, path
# sanitising): private helpers of claude-agent-sdk 0.2.160 (unchanged since 0.2.158), pinned, and
# covered by tests.
from claude_agent_sdk._internal.sessions import _canonicalize_path, _find_project_dir

from code_with_slack import texts
from code_with_slack.render.escape import shown_as_written
from code_with_slack.render.renderer import one_line

logger = logging.getLogger(__name__)
RESUME_ROWS = 20
RESUME_ACTION = "session_resume"
TITLE_LIMIT = 80
ID_SHOWN = 8  # the first characters of a session id the list shows, and `!resume` takes


def matching(sessions: list[SDKSessionInfo], target: str) -> list[SDKSessionInfo]:
    """The sessions `!resume <target>` names: those whose id is `target` or starts with it, when
    it is at least as long as the ID_SHOWN characters the list shows (so a short title such as
    "add" is not read as an id), else those whose title (set with /rename or generated, the
    SDK's `custom_title`) is `target`."""
    by_id = [s for s in sessions if len(target) >= ID_SHOWN and s.session_id.startswith(target)]
    return by_id or [s for s in sessions if s.custom_title == target]


TAIL_BYTES = 256 * 1024


def _last_message_ms(path: Path) -> int | None:
    """The time of the last user or assistant entry in a transcript, read from its end.

    `list_sessions` dates a session by its file's mtime, and Claude Code appends bookkeeping
    entries with no timestamp to old transcripts (artifact ledgers, seen 2026-09-25 with the
    bundled CLI 2.1.280): a session
    untouched for days then reads as minutes old. The terminal's picker shows the last activity,
    so this reads it. The transcript format is not documented; None falls back to the mtime."""
    try:
        with path.open("rb") as file:
            size = file.seek(0, 2)
            file.seek(max(0, size - TAIL_BYTES))
            tail = file.read()
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # the first line of the tail may be cut
        if isinstance(entry, dict) and entry.get("type") in ("user", "assistant"):
            stamp = entry.get("timestamp")
            if isinstance(stamp, str):
                try:
                    return int(datetime.fromisoformat(stamp).timestamp() * 1000)
                except ValueError:
                    return None
    return None


def by_last_activity(directory: Path, sessions: list[SDKSessionInfo]) -> list[SDKSessionInfo]:
    """`sessions` ordered by their last message, as the terminal's picker shows them: every
    session that can be among the newest RESUME_ROWS is dated, the rest keep their file's mtime.
    Blocking file reads, run it off the event loop."""
    folder = _find_project_dir(_canonicalize_path(str(directory)))
    if folder is None:
        # Also what a change in the SDK's private helpers would look like: say so.
        logger.warning("found no transcript folder: session dates fall back to file times")
        return sessions
    by_mtime = sorted(sessions, key=lambda s: s.last_modified, reverse=True)
    dated: list[SDKSessionInfo] = []
    newest: list[int] = []  # a min-heap of the RESUME_ROWS newest stamps dated so far
    for index, session in enumerate(by_mtime):
        # A file's mtime bounds its last message from above: once RESUME_ROWS dated sessions
        # are at or above this mtime, no file left can enter the list, so none is read.
        if len(newest) == RESUME_ROWS and newest[0] >= session.last_modified:
            rest = by_mtime[index:]
            return sorted(dated, key=lambda s: s.last_modified, reverse=True) + rest
        stamp = _last_message_ms(folder / f"{session.session_id}.jsonl") or session.last_modified
        dated.append(dataclasses.replace(session, last_modified=stamp))
        if len(newest) < RESUME_ROWS:
            heapq.heappush(newest, stamp)
        else:
            heapq.heappushpop(newest, stamp)
    return sorted(dated, key=lambda s: s.last_modified, reverse=True)


def _age(modified_ms: int, now: datetime) -> str:
    """As the terminal's picker writes it ("2 days ago", seen 2026-09-25)."""
    seconds = max(0.0, now.timestamp() - modified_ms / 1000)
    for unit, length in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= length:
            count = int(seconds // length)
            return f"{count} {unit}{'s' if count > 1 else ''} ago"
    return "just now"


def _size(size: int | None) -> str | None:
    """As the terminal's picker writes it ("953.1KB" for 976,000 bytes: 1,024 per KB)."""
    if size is None:
        return None
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / (1024 * 1024):.1f}MB"


def _row(session: SDKSessionInfo, current: bool, now: datetime) -> dict[str, Any]:
    # Shown as the terminal's picker shows it, HEAD outside a repository included.
    branch = shown_as_written(session.git_branch) if session.git_branch else None
    title = shown_as_written(one_line(session.summary, TITLE_LIMIT))
    # The terminal's picker shows no id, since picking a row resumes it; here the id's start is
    # what `!resume <id>` takes. Plain text, as the rest of the row (the maintainer, 2026-09-26).
    parts = [
        title,
        _age(session.last_modified, now),
        branch,
        _size(session.file_size),
        session.session_id[:ID_SHOWN],
    ]
    line = " · ".join(p for p in parts if p) + (texts.RESUME_CURRENT if current else "")
    block: dict[str, Any] = {
        "type": "section",
        "block_id": f"session-{session.session_id}",
        "text": {"type": "mrkdwn", "text": line},
    }
    if not current:
        block["accessory"] = {
            "type": "button",
            "action_id": RESUME_ACTION,
            "value": session.session_id,
            "text": {"type": "plain_text", "text": texts.RESUME_BUTTON},
        }
    return block


def resume_blocks(
    directory: Path, sessions: list[SDKSessionInfo], current: str | None, now: datetime
) -> list[dict[str, Any]]:
    """The picker: the newest RESUME_ROWS sessions of `directory`, the channel's own marked."""
    if not sessions:
        text = texts.RESUME_EMPTY.format(directory=directory)
        return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    header = texts.RESUME_LIST.format(directory=shown_as_written(str(directory)))
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        *(_row(s, s.session_id == current, now) for s in sessions[:RESUME_ROWS]),
    ]
    if len(sessions) > RESUME_ROWS:
        more = texts.RESUME_MORE.format(rows=RESUME_ROWS)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": more}})
    return blocks
