"""Claude Code's folder trust, read before a session starts in a bound directory.

An SDK session never shows the trust dialog and counts as trusted, so a repository's own hooks,
`env` block and allow rules apply at once (permissions reference, "What runs before you trust a
folder", read 2026-09-25). The daemon therefore starts Claude Code only where the owner has
already trusted the folder in the terminal, by the rules Claude Code documents:

- in a git repository, the trust is keyed on the repository root (the main checkout's root for
  a worktree) and a trusted parent does not cover it;
- outside a repository, a trusted folder covers its subdirectories.

The record is `projects["<path>"].hasTrustDialogAccepted` in `~/.claude.json`.
"""

import asyncio
import functools
import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class GitUnavailable(Exception):
    """git could not answer, so whether the folder is in a repository is unknown."""


async def _git(directory: Path, *args: str) -> str | None:
    """git's answer, or None when `directory` is outside any repository (exit 128, measured
    2026-09-25). Any other failure raises: it must not pass for "outside a repository"."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(directory),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise GitUnavailable(type(exc).__name__) from exc
    out, err = await proc.communicate()
    if proc.returncode == 0 and out.strip():
        return out.decode(errors="replace").strip()
    if proc.returncode == 128 and b"not a git repository" in err:
        return None
    raise GitUnavailable(f"exit {proc.returncode}")


async def repository_root(directory: Path) -> Path | None:
    """The root Claude Code keys a repository's trust on, or None outside git."""
    common = await _git(directory, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common is None:
        return None
    # git prints resolved paths, the form Claude Code records.
    if Path(common).name == ".git":
        return Path(common).parent
    top = await _git(directory, "rev-parse", "--show-toplevel")
    return Path(top) if top else None


def _trusted_paths(home: Path) -> frozenset[str]:
    record = home / ".claude.json"
    try:
        stat = record.stat()
    except OSError as exc:
        logger.warning("could not read Claude Code's trusted folders: %s", type(exc).__name__)
        return frozenset()
    # The checks of a `!bind` list run in parallel threads: one parses, the others wait for it.
    with _READING:
        return _read_trusted(record, stat.st_mtime_ns, stat.st_size)


_READING = threading.Lock()


# A `!bind` list checks many folders in a row: the record, several MB, is parsed again only once
# it has changed on disk.
@functools.lru_cache(maxsize=1)
def _read_trusted(record_path: Path, mtime_ns: int, size: int) -> frozenset[str]:
    try:
        record: Any = json.loads(record_path.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("could not read Claude Code's trusted folders: %s", type(exc).__name__)
        return frozenset()
    projects = record.get("projects") if isinstance(record, dict) else None
    if not isinstance(projects, dict):
        return frozenset()
    return frozenset(
        path
        for path, state in projects.items()
        if isinstance(state, dict) and state.get("hasTrustDialogAccepted") is True
    )


def _covered(directory: Path, root: Path | None, home: Path) -> bool:
    trusted = _trusted_paths(home)
    if root is not None:
        return str(root) in trusted
    folder = directory.resolve()
    return any(str(p) in trusted for p in (folder, *folder.parents))


async def workspace_trusted(directory: Path, home: Path | None = None) -> bool:
    """Whether the owner trusted `directory` in Claude Code, as the terminal would decide; False
    when git cannot tell whether it is in a repository."""
    try:
        root = await repository_root(directory)
    except GitUnavailable as exc:
        logger.warning("could not ask git about %s: %s", directory, exc)
        return False
    return await asyncio.to_thread(_covered, directory, root, home or Path.home())
