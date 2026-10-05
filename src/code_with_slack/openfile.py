"""`!open`: a file of the session's folder, shared into its thread so that Slack's own file viewer
shows it (Markdown rendered, the thread open beside it).

The files come from git alone: the tracked ones and the untracked ones that are not ignored,
under the folder. Git runs only where the footer's git runs, on a repository the owner trusted
(`code_with_slack.trust.trusted_repository`), through the footer's `run_git`.

Every git command here is plumbing or `status` under `--no-optional-locks`, because `git diff`
refreshes and rewrites the index under `index.lock` (see `footer._changes`), and a lock left by
a killed command would stop the owner's own `git add` and commit. Measured on git 2.54.0
(2026-10-05) on a scratch repository, with the index's inode, mtime and sha256 compared before
and after, in four states of the work tree (clean; a tracked file touched and left unchanged,
which is a stat-dirty file; a tracked file edited; an untracked file):

- never written, in any state: `rev-parse --verify HEAD`, `hash-object -t tree /dev/null`,
  `ls-files --cached --others --exclude-standard`, `diff-tree -r`, `diff-files`, `diff-index`
  and `--no-optional-locks status`;
- written for the stat-dirty file: `status` without `--no-optional-locks`, `diff --name-only`
  and `--no-optional-locks diff --name-only`;
- reported as changed when only touched: `diff-files` and `diff-index` (the footer's commands);
  not reported: `status`, which compares the content of a file whose stat differs.

So the committed changes come from `diff-tree` and the rest from `status`, which also lists the
untracked files. `tests/test_openfile.py` repeats the measurement on every run.
"""

import asyncio
import logging
import os
import posixpath
import re
import stat
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from code_with_slack import texts
from code_with_slack.footer import GIT_TIMEOUT, run_git
from code_with_slack.render.escape import shown_as_written
from code_with_slack.render.sinks import context_block, describe
from code_with_slack.trust import Repository

logger = logging.getLogger(__name__)

# The picker's two menus and the message of several matches; a choice in either select is the
# same act, so the three are answered by one handler.
OPEN_CHANGED_ACTION = "open_changed"
OPEN_SEARCH_ACTION = "open_search"
OPEN_MATCH_ACTION = "open_match"
OPEN_ACTIONS = (OPEN_CHANGED_ACTION, OPEN_SEARCH_ACTION, OPEN_MATCH_ACTION)
# The picker's actions block carries its thread: a `block_suggestion` payload documents no thread
# field, and the options it asks for are the folder's of that thread.
BLOCK_PREFIX = "open-"
THREAD_TS = re.compile(r"^\d+\.\d+$")
# Slack's documented limit for a snippet: `snippet_too_large` on `files.getUploadURLExternal`
# (docs.slack.dev/reference/methods/files.getUploadURLExternal, read 2026-10-05).
SNIPPET_LIMIT = 1 << 20
# Block Kit's limits (select menu element and option object, read 2026-10-05).
OPTION_ROWS = 100
TEXT_LIMIT = 75
VALUE_LIMIT = 150
# Slack wants the options of a typed query within 3 seconds.
SUGGEST_TIMEOUT = 2.5
EMPTY_TREE_COMMAND = ("hash-object", "-t", "tree", "/dev/null")


class NotAFile(Exception):
    """The path is not a regular file inside the folder, once its links are resolved."""


class TooLarge(Exception):
    """The file is over SNIPPET_LIMIT."""


def _real(folder: Path) -> Path:
    return Path(os.path.realpath(folder))


def _locate(real: Path, relative: str) -> tuple[Path, os.stat_result]:
    """The regular file `relative` names under `real` (a resolved folder), with its stat. Raises
    `NotAFile` for anything else: a path that leaves the folder by `..`, an absolute path or a
    link, a directory, a missing file. `relative` is untrusted: an option's value comes back from
    Slack."""
    if not relative or "\0" in relative:
        raise NotAFile
    try:
        target = Path(os.path.realpath(real / relative))
        if not target.is_relative_to(real):
            raise NotAFile
        found = target.stat()
    except (OSError, ValueError) as exc:
        raise NotAFile from exc
    if not stat.S_ISREG(found.st_mode):
        raise NotAFile
    return target, found


def title_of(relative: str) -> str:
    """The path as the shared file is titled: `./docs/../docs/a.md` as `docs/a.md`."""
    return posixpath.normpath(relative)


def openable(folder: Path, relative: str) -> Path:
    """The file to share for `relative` under `folder`. Raises `NotAFile` or `TooLarge`.
    Blocking: run it in a thread."""
    target, found = _locate(_real(folder), relative)
    if found.st_size > SNIPPET_LIMIT:
        raise TooLarge
    return target


def regular_files(folder: Path, relatives: Iterable[str], limit: int | None = None) -> list[str]:
    """The paths of `relatives` that are regular files inside `folder`, in order, at most
    `limit`. Blocking: run it in a thread."""
    real = _real(folder)
    found: list[str] = []
    for relative in relatives:
        if limit is not None and len(found) >= limit:
            break
        try:
            _locate(real, relative)
        except NotAFile:
            continue
        found.append(relative)
    return found


def newest_first(folder: Path, relatives: Iterable[str]) -> list[str]:
    """The paths of `relatives` that are regular files inside `folder`, each once, the most
    recently modified first (ties by path). Blocking: run it in a thread."""
    real = _real(folder)
    dated: list[tuple[int, str]] = []
    for relative in dict.fromkeys(relatives):
        try:
            _, found = _locate(real, relative)
        except NotAFile:
            continue
        dated.append((found.st_mtime_ns, relative))
    dated.sort(key=lambda item: (-item[0], item[1]))
    return [relative for _, relative in dated]


def rank(paths: Iterable[str], words: str) -> list[str]:
    """The paths that contain `words`, ignoring case: those whose file name does first, then the
    shorter paths, then by path."""
    wanted = words.lower()
    found = [path for path in paths if wanted in path.lower()]
    return sorted(
        found,
        key=lambda path: (wanted not in path.rsplit("/", 1)[-1].lower(), len(path), path),
    )


def usable(repository: Repository | None) -> Repository | None:
    """`repository` when it has a work tree git can list, else None."""
    if repository is None or repository.git_dir is None or repository.inside_git_dir:
        return None
    return repository


def _prefix(repository: Repository, folder: Path) -> str | None:
    """The folder's path from the repository's root, `""` for the root itself; None when the
    folder is not under it."""
    try:
        relative = _real(folder).relative_to(repository.root)
    except ValueError:
        return None
    return relative.as_posix() if relative.parts else ""


def _under(output: str, prefix: str) -> list[str]:
    """The paths of a NUL-separated git output that lie under `prefix`, relative to it, each
    once."""
    start = f"{prefix}/" if prefix else ""
    names = (name[len(start) :] for name in output.split("\0") if name.startswith(start))
    return list(dict.fromkeys(name for name in names if name))


def _scoped(prefix: str) -> list[str]:
    return ["--", prefix] if prefix else []


async def project_files(repository: Repository, folder: Path) -> list[str] | None:
    """The tracked files and the untracked ones that are not ignored, under `folder`, as paths
    from it; None when git fails. Not checked for existence: a tracked file deleted from the work
    tree is listed (`regular_files` drops it)."""
    prefix = await asyncio.to_thread(_prefix, repository, folder)
    if prefix is None:
        return None
    out = await run_git(
        repository,
        # Literal: the folder's name is not a glob.
        "--literal-pathspecs",
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        *_scoped(prefix),
    )
    return None if out is None else _under(out, prefix)


async def start_commit(repository: Repository) -> str | None:
    """What a thread's changes are counted from: `HEAD`, or the empty tree in a repository with
    no commit yet, as the footer compares what is staged there; None when git fails."""
    head = await run_git(repository, "rev-parse", "--verify", "--quiet", "HEAD")
    if head is None:
        head = await run_git(repository, *EMPTY_TREE_COMMAND)
    return (head or "").strip() or None


async def changed_since(
    repository: Repository, folder: Path, start: str | None
) -> list[str] | None:
    """The files under `folder` added or modified since `start`, committed or not, and the
    untracked ones that are not ignored, as paths from it. Not checked for existence: a deleted
    file may be named (`newest_first` drops it). With no `start`, only what is not committed.
    None when git fails."""
    prefix = await asyncio.to_thread(_prefix, repository, folder)
    if prefix is None:
        return None
    scope = _scoped(prefix)
    status = await run_git(
        repository,
        "--literal-pathspecs",
        "--no-optional-locks",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=all",
        "--no-renames",
        *scope,
    )
    if status is None:
        return None
    # `XY path`: two status letters and a space come before the path.
    names = [entry[3:] for entry in status.split("\0") if len(entry) > 3]
    if start is not None:
        committed = await run_git(
            repository,
            "--literal-pathspecs",
            "diff-tree",
            "-r",
            "-z",
            "--name-only",
            "--no-renames",
            "--diff-filter=AMT",
            start,
            "HEAD",
            *scope,
        )
        # A start that no longer resolves (rewritten history): the uncommitted files alone.
        names += (committed or "").split("\0")
    return _under("\0".join(names), prefix)


class Starts:
    """The commit each thread's repository was on when this run first saw the thread, in memory
    only: `!open` lists the changes made since. Set once, so a thread whose Claude Code process
    closed after an idle hour and came back is still counted from its first sight."""

    def __init__(self, lookup: Callable[[Path], Awaitable[Repository | None]]) -> None:
        self._lookup = lookup
        self._commits: dict[tuple[str, str], str] = {}

    def of(self, channel: str, thread_ts: str) -> str | None:
        return self._commits.get((channel, thread_ts))

    async def seen(self, channel: str, thread_ts: str, directory: Path) -> None:
        """Note the start of a thread not seen yet. Never raises, and notes nothing where git
        runs on no repository or fails: the next sight tries again."""
        key = (channel, thread_ts)
        if key in self._commits:
            return
        try:
            async with asyncio.timeout(GIT_TIMEOUT):
                found = usable(await self._lookup(directory))
                commit = await start_commit(found) if found is not None else None
        except Exception as exc:  # a lookup that fails must not stop the message it came with
            logger.warning("could not read a thread's start commit: %s", describe(exc))
            return
        if commit is not None:
            self._commits.setdefault(key, commit)


def option(path: str) -> dict[str, Any] | None:
    """A menu option for a path: its text shortened from the left with `…` past TEXT_LIMIT, the
    path itself as the value. None when the value cannot fit (the file stays reachable by
    `!open <path>`)."""
    if len(path) > VALUE_LIMIT:
        return None
    text = path if len(path) <= TEXT_LIMIT else "…" + path[-(TEXT_LIMIT - 1) :]
    return {"text": _plain(text), "value": path}


def options(paths: Iterable[str]) -> list[dict[str, Any]]:
    """The first OPTION_ROWS paths that make an option."""
    found: list[dict[str, Any]] = []
    for path in paths:
        if len(found) >= OPTION_ROWS:
            break
        if (shown := option(path)) is not None:
            found.append(shown)
    return found


def _plain(text: str) -> dict[str, str]:
    return {"type": "plain_text", "text": text}


def _title() -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": texts.OPEN_TITLE}}


def picker_blocks(thread_ts: str, changed: list[str], count: int) -> list[dict[str, Any]]:
    """The picker: a menu of the files changed in the session (`count` of them, `changed` the
    ones to offer; left out when none can be) and a search over the folder's files."""
    elements: list[dict[str, Any]] = []
    if shown := options(changed):
        elements.append(
            {
                "type": "static_select",
                "action_id": OPEN_CHANGED_ACTION,
                "placeholder": _plain(texts.OPEN_CHANGED.format(count=count)),
                "options": shown,
            }
        )
    elements.append(
        {
            "type": "external_select",
            "action_id": OPEN_SEARCH_ACTION,
            "placeholder": _plain(texts.OPEN_SEARCH),
        }
    )
    return [
        _title(),
        {"type": "actions", "block_id": f"{BLOCK_PREFIX}{thread_ts}", "elements": elements},
        context_block(texts.OPEN_BY_NAME),
    ]


def no_source_blocks() -> list[dict[str, Any]]:
    """The picker of a folder git cannot list: the way to name a file, and no menu."""
    return [_title(), context_block(texts.OPEN_NO_GIT)]


def matches_blocks(words: str, found: list[str]) -> list[dict[str, Any]]:
    """Several files match `words`: one menu of them, and a line when more than a menu holds."""
    shown = options(found)
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": texts.OPEN_MATCHES.format(count=len(found), words=shown_as_written(words)),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "static_select",
                    "action_id": OPEN_MATCH_ACTION,
                    "placeholder": _plain(texts.OPEN_MATCH_PLACEHOLDER),
                    "options": shown,
                }
            ],
        },
    ]
    if len(found) > len(shown):
        blocks.append(
            context_block(texts.OPEN_MATCHES_CAPPED.format(shown=len(shown), count=len(found)))
        )
    return blocks


def thread_of(block_id: str) -> str | None:
    """The thread a picker's block id names, or None for any other block."""
    thread_ts = block_id.removeprefix(BLOCK_PREFIX)
    return thread_ts if block_id.startswith(BLOCK_PREFIX) and THREAD_TS.match(thread_ts) else None
