"""`!bind` alone: the folders under the allowed root where a session can start, a Bind button each.

Claude Code has no folder picker: the terminal starts in the folder it is launched from. The list
holds the allowed root and the folders two levels below it that Claude Code trusts
(`code_with_slack.trust`), since a session starts nowhere else. It never descends into a git
repository: its subfolders belong to that one project.
"""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from code_with_slack import texts
from code_with_slack.render.escape import shown_as_written

logger = logging.getLogger(__name__)
FOLDER_ROWS = 20
FOLDER_DEPTH = 2
BIND_ACTION = "folder_bind"
# Each trust check may run git twice: a batch at a time, so a root with many untrusted folders
# lists in a few rounds, and the check stops soon after the rows are filled.
TRUST_BATCH = 8


def _is_folder(path: Path) -> bool:
    # A symlink could lead outside the root; hidden folders are tooling (.git, .venv, .claude).
    try:
        return path.is_dir() and not path.is_symlink() and not path.name.startswith(".")
    except OSError:
        return False


def _descends(folder: Path) -> bool:
    # `.git` is a directory in a repository and a file in a worktree. A folder that cannot be
    # searched raises here (Python 3.12 ignores only a missing path): it has nothing to list.
    try:
        return not (folder / ".git").exists()
    except OSError:
        return False


def _children(folder: Path) -> list[Path]:
    try:
        entries = sorted(folder.iterdir())
    except OSError as exc:
        # A subfolder the daemon may not open (macOS privacy, permissions) lists nothing.
        logger.debug("skipped an unreadable folder: %s", type(exc).__name__)
        return []
    return [p for p in entries if _is_folder(p)]


def _candidates(root: Path) -> list[Path]:
    """The root, then its folders, then theirs: the higher levels come first."""
    # An unreadable root raises: an empty list would send the owner to trust folders instead.
    with os.scandir(root):
        pass
    levels = [[root]]
    for _ in range(FOLDER_DEPTH):
        levels.append([c for f in levels[-1] if _descends(f) for c in _children(f)])
    return [folder for level in levels for folder in level]


async def bindable_folders(root: Path, trusted: Callable[[Path], Awaitable[bool]]) -> list[Path]:
    """Trusted folders under `root`, the higher levels first, at most FOLDER_ROWS + 1: one past
    the rows shown is enough to say that more exist, and stops the trust checks there."""
    candidates = await asyncio.to_thread(_candidates, root)
    found: list[Path] = []
    for start in range(0, len(candidates), TRUST_BATCH):
        batch = candidates[start : start + TRUST_BATCH]
        verdicts = await asyncio.gather(*(trusted(folder) for folder in batch))
        found += [folder for folder, ok in zip(batch, verdicts, strict=True) if ok]
        if len(found) > FOLDER_ROWS:
            return found[: FOLDER_ROWS + 1]
    return found


def _row(root: Path, index: int, folder: Path, current: bool) -> dict[str, Any]:
    relative = folder.relative_to(root).as_posix()
    block: dict[str, Any] = {
        "type": "section",
        # An index, not the path: a block_id is capped at 255 characters.
        "block_id": f"folder-{index}",
        "text": {
            "type": "mrkdwn",
            "text": f"`{shown_as_written(relative)}`" + (texts.BIND_CURRENT if current else ""),
        },
    }
    if not current:
        block["accessory"] = {
            "type": "button",
            "action_id": BIND_ACTION,
            "value": relative,
            "text": {"type": "plain_text", "text": texts.BIND_BUTTON},
        }
    return block


def bind_blocks(root: Path, folders: list[Path], current: Path | None) -> list[dict[str, Any]]:
    """The list: the first FOLDER_ROWS folders in path order, the channel's own marked."""
    shown = sorted(folders[:FOLDER_ROWS])
    if not folders:
        return [_section(texts.BIND_EMPTY.format(root=shown_as_written(str(root))))]
    return [
        _section(texts.BIND_LIST.format(root=shown_as_written(str(root)))),
        *(_row(root, i, f, f == current) for i, f in enumerate(shown)),
        *(
            [_section(texts.BIND_MORE.format(rows=FOLDER_ROWS))]
            if len(folders) > FOLDER_ROWS
            else []
        ),
    ]


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}
