"""Claude Code's folder trust, read before a session starts in a bound directory.

An SDK session never shows the trust dialog and counts as trusted, so a repository's own hooks,
`env` block and allow rules apply at once (permissions reference, "What runs before you trust a
folder", read 2026-09-25). The daemon therefore starts Claude Code only where the owner has
already trusted the folder in the terminal, by the rules Claude Code documents:

- in a git repository, the trust is keyed on the repository root (the main checkout's root for
  a worktree) and a trusted parent does not cover it;
- outside a repository, a trusted folder covers its subdirectories.

The record is `projects["<path>"].hasTrustDialogAccepted` in `~/.claude.json`.

The daemon's own git (the footer, `!status`, `!open`) has a second way in, beside a trusted
repository key: a repository inside the folder the session started in (`trusted_repository`).

Which repository a folder belongs to is read from the filesystem and never asked of git there:
git would answer from the folder's own `.git` file, `commondir` and `core.worktree`, which
whoever supplied the folder wrote. The layout read here is the one gitrepository-layout(5) and
git-worktree(1) document, and the bare-repository test follows `is_git_directory` in git's
setup.c (git 2.54.0, read 2026-10-04).
"""

import asyncio
import errno
import functools
import json
import logging
import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# git's own limit for a `.git` file (setup.c, read_gitfile_gently).
GITFILE_BYTES = 1 << 20


class Unkeyed(Exception):
    """git would find a repository at the folder that no path the owner trusted can stand for:
    a bare layout, or metadata that cannot be read."""


@dataclass(frozen=True)
class Repository:
    """A folder's repository, as the filesystem shows it."""

    root: Path  # the folder that holds the `.git` entry
    key: Path  # the path whose trust covers it: the main checkout for a registered worktree
    git_dir: Path | None  # None when the `.git` entry names none
    inside_git_dir: bool  # the folder is in the root's own `.git` directory, and has no work tree


def _mode(path: Path) -> int | None:
    """`path`'s own type, a symlink not followed; None when nothing is there."""
    try:
        return os.lstat(path).st_mode
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return None
        raise Unkeyed from exc


def _regular_file(path: Path) -> bytes | None:
    """A regular file's content. Never waits on a FIFO, never follows a symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        found = os.fstat(fd)
        if not stat.S_ISREG(found.st_mode) or found.st_size > GITFILE_BYTES:
            return None
        return os.read(fd, GITFILE_BYTES)
    except OSError:
        return None
    finally:
        os.close(fd)


def _git_dir_like(folder: Path) -> bool:
    """Whether git could take `folder` for a git dir: it wants a HEAD, and `objects` and `refs`
    there or in the directory a `commondir` names. Every folder git accepts passes here; a test
    holds that against the git installed."""
    if not os.path.lexists(folder / "HEAD"):
        return False
    if os.path.lexists(folder / "commondir"):
        return True
    return os.path.lexists(folder / "objects") and os.path.lexists(folder / "refs")


def _gitfile_target(gitfile: Path) -> Path | None:
    """The git dir a `.git` file names, read as git reads it."""
    content = _regular_file(gitfile)
    if content is None or not content.startswith(b"gitdir: "):
        return None
    # git strips line ends only: a trailing space belongs to the path.
    target = os.fsdecode(content[len(b"gitdir: ") :].rstrip(b"\r\n"))
    return Path(os.path.realpath(gitfile.parent / target)) if target else None


def _main_checkout(git_dir: Path, root: Path) -> Path | None:
    """The main checkout that registers `root` as a linked worktree, None when none does.

    The link is read on the main checkout's side, in `<main>/.git/worktrees/<id>/gitdir`, which
    git writes there; what `root`'s own `.git` file says only tells where to look.
    """
    if git_dir.parent.name != "worktrees" or git_dir.parent.parent.name != ".git":
        return None
    # A main checkout is a repository by the walk's own test: a `.git` holding a worktree
    # registry and nothing else, left in a trusted folder outside git, makes it none.
    if not _git_dir_like(git_dir.parent.parent):
        return None
    content = _regular_file(git_dir / "gitdir")
    if content is None:
        return None
    # An absolute path, or one relative to this directory (`git worktree add --relative-paths`).
    registered = git_dir / os.fsdecode(content.removesuffix(b"\n"))
    # The two folders are compared as directories on disk: the same one whatever the case or
    # the Unicode form git wrote, another one for a name one space longer. Not the `.git` files:
    # a file can be hard-linked into a second folder.
    try:
        if registered.name != ".git" or not os.path.samestat(
            os.stat(registered.parent), os.stat(root)
        ):
            return None
    except OSError:
        return None
    return git_dir.parent.parent.parent


def locate(directory: Path) -> Repository | None:
    """The repository holding `directory`, None outside git. Raises `Unkeyed`.

    The walk is git's own, with one difference: a folder holding a `.git` entry is keyed on
    itself, whatever that entry claims, unless a main checkout registers it as its worktree.
    """
    try:
        return _locate(Path(os.path.realpath(directory)))
    except (OSError, ValueError, RecursionError) as exc:
        # What a `.git` file or symlink names is somebody else's text: a NUL in it is no path,
        # and `realpath` gives up on a long enough chain of symlinks (Python 3.12).
        raise Unkeyed from exc


def _locate(folder: Path) -> Repository | None:
    if not stat.S_ISDIR(os.stat(folder).st_mode):
        raise Unkeyed
    passed: list[Path] = []
    git_dir_passed: Path | None = None  # the first on the way up, where git would stop
    for root in (folder, *folder.parents):
        entry = root / ".git"
        mode = _mode(entry)
        # git walks past a `.git` directory that is no git dir.
        if mode is not None and not (stat.S_ISDIR(mode) and not _git_dir_like(entry)):
            break
        if git_dir_passed is None and _git_dir_like(root):
            git_dir_passed = root
        passed.append(root)
    else:
        if git_dir_passed is not None:
            raise Unkeyed
        return None
    if git_dir_passed is not None:
        # In the root's own `.git` directory, reached in whatever case, the repository is the
        # root's. Anywhere else it is a bare layout below the root: git stops there and reads
        # that folder's own config.
        own = os.stat(entry)
        if not (stat.S_ISDIR(mode) and any(os.path.samestat(own, os.stat(p)) for p in passed)):
            raise Unkeyed
        return Repository(root, root, git_dir_passed, True)
    if stat.S_ISDIR(mode):
        return Repository(root, root, entry, False)
    if stat.S_ISLNK(mode):
        return Repository(root, root, Path(os.path.realpath(entry)), False)
    git_dir = _gitfile_target(entry) if stat.S_ISREG(mode) else None
    main = _main_checkout(git_dir, root) if git_dir else None
    return Repository(root, main or root, git_dir, False)


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


def _trusted(path: Path, home: Path) -> bool:
    """Whether `path` is a folder the owner trusted: the same directory on disk as a recorded
    path, so a path in another case or Unicode form is that folder and `app ` is not `app`."""
    trusted = _trusted_paths(home)
    if str(path) in trusted:
        return True
    try:
        mine = os.stat(path)
    except OSError:
        return False
    for recorded in trusted:
        try:
            # A recorded path stands for the folder it spells, not for one a symlink in it
            # leads to: none of its components may be a symlink.
            if (
                os.path.samestat(mine, os.lstat(recorded))
                and os.path.realpath(recorded) == recorded
            ):
                return True
        except (OSError, ValueError):
            continue
    return False


def _inside(path: Path, folder: Path) -> bool:
    """Whether `path` lies strictly inside `folder`, both as they are on disk: a symlink in the
    folder that leads elsewhere is not inside it."""
    real = Path(os.path.realpath(folder))
    resolved = Path(os.path.realpath(path))
    return resolved != real and resolved.is_relative_to(real)


def _trusted_repository(directory: Path, session_folder: Path, home: Path) -> Repository | None:
    try:
        repository = locate(directory)
    except Unkeyed:
        return None
    if repository is None:
        return None
    if _trusted(repository.key, home):
        return repository
    # The second way in, for a repository the session's folder holds.
    if _inside(repository.key, session_folder) and _workspace_trusted(session_folder, home):
        return repository
    return None


def _workspace_trusted(directory: Path, home: Path) -> bool:
    try:
        repository = locate(directory)
    except Unkeyed:
        return False
    if repository is not None:
        return _trusted(repository.key, home)
    folder = Path(os.path.realpath(directory))
    return any(_trusted(path, home) for path in (folder, *folder.parents))


async def trusted_repository(
    directory: Path, session_folder: Path, home: Path | None = None
) -> Repository | None:
    """The repository holding `directory` when the daemon's own git may run in it; None outside
    git, where the layout has no key, and in a repository neither of these covers:

    - its key is a path the owner trusted in Claude Code (a worktree's key is its main checkout);
    - its key lies strictly inside `session_folder`, the folder the session was started in, and
      that folder passes `workspace_trusted`.

    The second is the owner's decision (2026-10-05): Slack is only an interface to Claude Code,
    which, launched in a trusted folder, works in its subfolders. It reaches no further: a
    repository outside `session_folder` needs its own trust since the agent may `cd` anywhere,
    and `workspace_trusted`, which gates a session's start and `!bind`, is not widened. Inside is
    decided on resolved paths, so a symlink in the folder that leads to a repository elsewhere,
    and a worktree whose main checkout is elsewhere, are not inside. Nothing here runs git.
    """
    return await asyncio.to_thread(
        _trusted_repository, directory, session_folder, home or Path.home()
    )


async def workspace_trusted(directory: Path, home: Path | None = None) -> bool:
    """Whether the owner trusted `directory` in Claude Code, as the terminal would decide; False
    where git would find a repository that no trusted path stands for."""
    return await asyncio.to_thread(_workspace_trusted, directory, home or Path.home())
