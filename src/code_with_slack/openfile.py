"""`!open`: a file of the session's folder, shared into its thread so that Slack's own file viewer
shows it (Markdown rendered, the thread open beside it).

Which files the search offers is read from the folder's disk. Inside a repository the daemon's git
may run in (`code_with_slack.trust.trusted_repository`: the repository holding the session's
folder, or the ones found at most two levels below it) the files come from git, through the
footer's `run_git`: the tracked ones and the untracked ones that are not ignored. That is what the
terminal's `@` file picker does, since its setting `respectGitignore` defaults to `true` and
leaves out the files that match `.gitignore` patterns (code.claude.com/docs/en/settings-reference,
read 2026-10-05). Everywhere else the folder is walked: regular files only, no symlinked folder
entered, no `.git` entered. The changed files come from git alone, each repository counted from
where its HEAD was when the thread started: the reflog read at the thread's `thread_ts`
(`start_commit`), computed when the list is built and kept nowhere.

Every git command here is plumbing or `status` under `--no-optional-locks`, because `git diff`
refreshes and rewrites the index under `index.lock` (see `footer._changes`), and a lock left by
a killed command would stop the owner's own `git add` and commit. Measured on git 2.54.0
(2026-10-05) on a scratch repository, with the index's inode, mtime and sha256 compared before
and after, in four states of the work tree (clean; a tracked file touched and left unchanged,
which is a stat-dirty file; a tracked file edited; an untracked file):

- never written, in any state: `rev-parse --verify HEAD@{<time>}`, `ls-files --cached --others
  --exclude-standard`, `diff-tree -r`, `diff-files`, `diff-index` and `--no-optional-locks
  status`;
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
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from itertools import chain
from pathlib import Path
from typing import Any

from code_with_slack import texts
from code_with_slack.folders import folders_within
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
# Slack wants the options of a typed query within 3 seconds: a listing stops at LISTING_BUDGET
# and answers with what it found, SUGGEST_TIMEOUT backs it up. A folder's listing is kept for
# LISTING_TTL (LISTING_PARTIAL_TTL when it is not complete), so the keystrokes of one query do not
# walk the folder again; LISTING_KEPT folders are kept at most.
SUGGEST_TIMEOUT = 2.5
LISTING_BUDGET = 2.0
LISTING_TTL = 30.0
LISTING_PARTIAL_TTL = 3.0
LISTING_KEPT = 16

# Answers for a repository: the one holding a directory, usable for a session started in a folder
# (`code_with_slack.trust.trusted_repository`, whose arguments they are).
RepositoryLookup = Callable[[Path, Path], Awaitable[Repository | None]]


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


def read_openable(folder: Path, relative: str) -> bytes:
    """The content of the file `relative` names under `folder`. Raises `NotAFile` or `TooLarge`.
    Blocking: run it in a thread.

    The path is resolved and checked, then opened once, and everything after is read from that
    descriptor: `O_NOFOLLOW` refuses a file that became a link in between, `O_NONBLOCK` never
    waits on a FIFO, and the size is read from the descriptor (`fstat`) and the read stops past
    SNIPPET_LIMIT, so what was checked is what is read. Slack gets these bytes: its SDK would
    open a path again, following links and with no size limit."""
    target, _ = _locate(_real(folder), relative)
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as exc:
        raise NotAFile from exc
    try:
        found = os.fstat(fd)
        if not stat.S_ISREG(found.st_mode):
            raise NotAFile
        if found.st_size > SNIPPET_LIMIT:
            raise TooLarge
        content = b""
        while len(content) <= SNIPPET_LIMIT:
            chunk = os.read(fd, SNIPPET_LIMIT + 1 - len(content))
            if not chunk:
                break
            content += chunk
    except OSError as exc:
        raise NotAFile from exc
    finally:
        os.close(fd)
    if len(content) > SNIPPET_LIMIT:
        raise TooLarge
    return content


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


def _relation(repository: Repository, folder: Path) -> tuple[str, str] | None:
    """How the folder and the repository's root meet, as `(strip, add)`: git's paths are relative
    to the root, and a path from the folder is that path less `strip` and with `add` before it.
    A folder inside the repository strips its own path from the root (`""` at the root itself),
    a repository inside the folder adds its own path from the folder, and any other pair is None."""
    real = _real(folder)
    if real.is_relative_to(repository.root):
        relative = real.relative_to(repository.root)
        return (relative.as_posix() if relative.parts else ""), ""
    if repository.root.is_relative_to(real):
        return "", f"{repository.root.relative_to(real).as_posix()}/"
    return None


def _under(output: str, strip: str, add: str) -> list[str]:
    """The paths of a NUL-separated git output that lie under `strip`, as paths from the folder,
    each once."""
    start = f"{strip}/" if strip else ""
    names = (name[len(start) :] for name in output.split("\0") if name.startswith(start))
    return list(dict.fromkeys(f"{add}{name}" for name in names if name))


def _scoped(strip: str) -> list[str]:
    return ["--", strip] if strip else []


async def repositories_of(folder: Path, lookup: RepositoryLookup) -> list[Repository]:
    """The repositories the daemon's git may run in for a session started in `folder`: the one
    holding the folder when `lookup` finds it usable, else the usable ones found at most two
    levels below it (the depth `!bind` lists folders to), none of them inside another."""
    held = usable(await lookup(folder, folder))
    if held is not None:
        return [held]
    below = await asyncio.to_thread(_repository_folders, folder)
    found = [usable(await lookup(candidate, folder)) for candidate in below]
    return list({r.root: r for r in found if r is not None}.values())


def _repository_folders(folder: Path) -> list[Path]:
    """The folders under `folder`, as `!bind` lists them, that hold a `.git` entry."""
    try:
        below = folders_within(folder)
    except OSError:
        return []
    return [path for path in below if path != folder and os.path.lexists(path / ".git")]


def walk_files(
    root: Path, skip: frozenset[str], expired: Callable[[], bool]
) -> tuple[list[str], bool]:
    """The regular files under `root` as paths from it, the shallower first: a symlink is neither
    followed nor listed, no `.git` is entered, nor any folder in `skip` (paths). Stops when
    `expired()` is true, with what it has found; the flag says the walk reached its end.
    Blocking: run it in a thread."""
    found: list[str] = []
    queue = deque([(str(root), "")])
    while queue and not expired():
        directory, prefix = queue.popleft()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name == ".git":
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.path not in skip:
                            queue.append((entry.path, f"{prefix}{entry.name}/"))
                    elif entry.is_file(follow_symlinks=False):
                        found.append(f"{prefix}{entry.name}")
        except OSError as exc:
            # A folder the daemon may not open (macOS privacy, permissions) lists nothing.
            logger.debug("skipped an unreadable folder: %s", type(exc).__name__)
    return found, not queue


async def project_files(repository: Repository, folder: Path) -> list[str] | None:
    """The tracked files and the untracked ones that are not ignored, in the part of the
    repository that is `folder` or inside it, as paths from `folder`; None when git fails. Not
    checked for existence: a tracked file deleted from the work tree is listed (`regular_files`
    drops it)."""
    relation = await asyncio.to_thread(_relation, repository, folder)
    if relation is None:
        return None
    strip, add = relation
    out = await run_git(
        repository,
        # Literal: the folder's name is not a glob.
        "--literal-pathspecs",
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        *_scoped(strip),
    )
    return None if out is None else _under(out, strip, add)


async def folder_files(
    folder: Path,
    repositories: list[Repository],
    budget: float,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[str], bool]:
    """The files `!open` can name under `folder`, as paths from it: from git inside a repository
    of `repositories`, the folder's own disk anywhere else. Within `budget` seconds: what is
    found by then is returned, a repository whose listing is not done adding nothing. The flag
    says nothing was left out: no walk or git listing ran out of time or failed."""
    deadline = clock() + budget
    real = await asyncio.to_thread(_real, folder)

    async def listed(repository: Repository) -> tuple[list[str], bool]:
        try:
            async with asyncio.timeout(budget):
                found = await project_files(repository, folder)
        except TimeoutError:
            return [], False
        return found or [], found is not None

    if any(real.is_relative_to(r.root) for r in repositories):
        # Every file of the folder is in the repository that holds it.
        parts = await asyncio.gather(*map(listed, repositories))
        walked: list[str] = []
        walk_done = True
    else:
        skip = frozenset(str(r.root) for r in repositories)
        (walked, walk_done), *parts = await asyncio.gather(
            asyncio.to_thread(walk_files, real, skip, lambda: clock() >= deadline),
            *map(listed, repositories),
        )
    files = list(dict.fromkeys(chain(walked, *(found for found, _ in parts))))
    return files, walk_done and all(done for _, done in parts)


class Listings:
    """The files of a session's folder for the search, kept in memory: Slack asks again on every
    keystroke, and a folder is not walked each time. A listing is kept `ttl` seconds when it is
    complete and `partial_ttl` when it ran out of time or git failed for part of it, so a
    folder too large for the budget is not walked on each keystroke but is not taken for
    complete either. At most `limit` folders are kept (the oldest go first), and the expired
    ones are removed on the next request. Requests for a folder that is being listed wait for
    that listing."""

    def __init__(
        self,
        lookup: RepositoryLookup,
        *,
        ttl: float = LISTING_TTL,
        partial_ttl: float = LISTING_PARTIAL_TTL,
        budget: float = LISTING_BUDGET,
        limit: int = LISTING_KEPT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lookup = lookup
        self._ttl = ttl
        self._partial_ttl = partial_ttl
        self._budget = budget
        self._limit = limit
        self._clock = clock
        self._kept: dict[Path, tuple[float, list[str]]] = {}  # until, files
        self._running: dict[Path, asyncio.Task[list[str]]] = {}

    async def of(self, folder: Path) -> list[str]:
        """The folder's files as paths from it. Never raises: a listing that fails is empty and
        not kept."""
        now = self._clock()
        for stale in [path for path, (until, _) in self._kept.items() if until <= now]:
            del self._kept[stale]
        if (kept := self._kept.get(folder)) is not None:
            return kept[1]
        task = self._running.get(folder)
        if task is None:
            task = self._running[folder] = asyncio.create_task(self._list(folder))
            task.add_done_callback(lambda done: self._running.pop(folder, None))
        # Shielded: a request that gives up (Slack's 3 seconds) leaves the listing to the others.
        return await asyncio.shield(task)

    async def _list(self, folder: Path) -> list[str]:
        started = self._clock()
        try:
            files, complete = await folder_files(
                folder, await repositories_of(folder, self._lookup), self._budget, self._clock
            )
        except Exception as exc:  # a search that finds nothing beats a menu that never loads
            logger.warning("could not list a folder's files: %s", describe(exc))
            return []
        self._kept[folder] = (started + (self._ttl if complete else self._partial_ttl), files)
        while len(self._kept) > self._limit:
            del self._kept[next(iter(self._kept))]
        return files


EPOCH_SECONDS = re.compile(r"^(\d+)(?:\.\d+)?$")


async def start_commit(repository: Repository, thread_ts: str) -> str | None:
    """Where the repository's HEAD was when the thread started, which `thread_ts` (the Slack ts
    of its root, epoch seconds) says: `HEAD@{<seconds> +0000}`, read from HEAD's own reflog (a
    linked worktree has its own). None when git has no answer: no reflog (a repository with
    `core.logAllRefUpdates` off, or a first commit not made yet), or a `thread_ts` that is no
    time. A reflog that does not go back that far gives its oldest entry, git's own answer (it
    warns on stderr, which `run_git` discards).

    Measured on git 2.54.0 (2026-10-05). The bare number is read as a time (`HEAD@{1790000150}`)
    and so is the internal format with its zone, which is the one used: it leaves no room for
    git's approximate dates. A time before the oldest entry gives that entry's old commit (a log
    that `git reflog expire` shortened) or, when the log starts with the first commit, that
    commit; no reflog at all and a repository with no commit are exit status 1."""
    seconds = EPOCH_SECONDS.match(thread_ts)
    if seconds is None:
        return None
    found = await run_git(
        repository, "rev-parse", "--verify", "--quiet", f"HEAD@{{{seconds[1]} +0000}}"
    )
    return (found or "").strip() or None


async def changed_since(
    repository: Repository, folder: Path, start: str | None
) -> list[str] | None:
    """The files in the part of the repository that is `folder` or inside it, added or modified
    since `start`, committed or not, and the untracked ones that are not ignored, as paths from
    `folder`. Not checked for existence: a deleted file may be named (`newest_first` drops it).
    With no `start`, only what is not committed. None when git fails."""
    relation = await asyncio.to_thread(_relation, repository, folder)
    if relation is None:
        return None
    strip, add = relation
    scope = _scoped(strip)
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
    return _under("\0".join(names), strip, add)


async def changed_in(folder: Path, repositories: list[Repository], thread_ts: str) -> list[str]:
    """The changed files of every repository of `repositories` (see `changed_since`), each from
    where its own HEAD was when the thread `thread_ts` started (`start_commit`), as paths from
    `folder`, each once. A repository whose git fails or runs over `GIT_TIMEOUT` adds none."""

    async def changed(repository: Repository) -> list[str]:
        try:
            async with asyncio.timeout(GIT_TIMEOUT):
                start = await start_commit(repository, thread_ts)
                return await changed_since(repository, folder, start) or []
        except TimeoutError:
            return []

    return list(
        dict.fromkeys(chain.from_iterable(await asyncio.gather(*map(changed, repositories))))
    )


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


def matches_blocks(words: str, found: list[str]) -> list[dict[str, Any]]:
    """Several files match `words`: one menu of them, and a line when more than a menu holds, or
    when none can be an option (the line alone)."""
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
    if not shown:
        # Every path is too long for an option: a menu with no options would not open.
        return [blocks[0], context_block(texts.OPEN_MATCHES_TOO_LONG)]
    if len(found) > len(shown):
        blocks.append(
            context_block(texts.OPEN_MATCHES_CAPPED.format(shown=len(shown), count=len(found)))
        )
    return blocks


def thread_of(block_id: str) -> str | None:
    """The thread a picker's block id names, or None for any other block."""
    thread_ts = block_id.removeprefix(BLOCK_PREFIX)
    return thread_ts if block_id.startswith(BLOCK_PREFIX) and THREAD_TS.match(thread_ts) else None
