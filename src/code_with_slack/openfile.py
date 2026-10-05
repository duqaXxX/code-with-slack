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
(2026-10-05, and 2026-10-06 for the reflog commands) on a scratch repository, with the index's
inode, mtime and sha256 compared before and after, in four states of the work tree (clean; a
tracked file touched and left unchanged, which is a stat-dirty file; a tracked file edited; an
untracked file):

- never written, in any state: `rev-parse --verify HEAD@{<time>}` and `HEAD@{<n>}`, `rev-list
  -g --count` (also with `--until`), `hash-object -t tree /dev/null`, `ls-files --cached
  --others --exclude-standard`, `diff-tree -r`, `diff-files`, `diff-index` and
  `--no-optional-locks status`;
- written for the stat-dirty file: `status` without `--no-optional-locks`, `diff --name-only`
  and `--no-optional-locks diff --name-only`;
- reported as changed when only touched: `diff-files` and `diff-index` (the footer's commands);
  not reported: `status`, which compares the content of a file whose stat differs.

So the committed changes come from `diff-tree` and the rest from `status`, which also lists the
untracked files. `tests/test_openfile.py` repeats the measurement on every run.
"""

import asyncio
import hashlib
import json
import logging
import os
import posixpath
import re
import stat
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from itertools import chain
from pathlib import Path
from typing import Any

from code_with_slack import texts
from code_with_slack.folders import folders_within
from code_with_slack.footer import GIT_TIMEOUT, run_git
from code_with_slack.render.escape import shown_as_written
from code_with_slack.render.sinks import context_block, describe, plain_text_object
from code_with_slack.trust import Repository

logger = logging.getLogger(__name__)

# The button that opens the picker's modal (in the `!open` message and in the one of several
# matches), the modal's callback id, and the ids of its two input blocks. Slack keeps the state
# of an input block whose ids do not change across `views.update` (reference, read 2026-10-05
# and 2026-10-06), so the search field keeps its ids, to keep what was typed, and the rows' block
# id, which is CHOICE_BLOCK and a mark of the rows it holds, changes with them, to drop a row
# chosen among others.
OPEN_BUTTON_ACTION = "open_choose"
OPEN_FORM = "open_form"
QUERY_BLOCK = "open_query_block"
QUERY_ACTION = "open_query"
CHOICE_BLOCK = "open_choice_block"
CHOICE_ACTION = "open_choice"
# Slack's documented limit for a snippet: `snippet_too_large` on `files.getUploadURLExternal`
# (docs.slack.dev/reference/methods/files.getUploadURLExternal, read 2026-10-05).
SNIPPET_LIMIT = 1 << 20
# Block Kit's limits, read 2026-10-05: a radio button group holds 10 options (radio button group
# element); an option's `text` and its `description` 75 characters, its `value` 150 (option
# object); a modal's `private_metadata` 3000 characters, its title, submit and close text 24, and
# it holds 100 blocks (modal views).
ROW_LIMIT = 10
TEXT_LIMIT = 75
DESCRIPTION_LIMIT = 75
VALUE_LIMIT = 150
# The words a search carries: typed in the field, or in a button's value from `!open <words>`.
QUERY_LIMIT = 200
# A click waits until this long after it arrived for the rows before the modal opens without
# them: the click's `trigger_id` lives 3 seconds (views.open reference, read 2026-10-05), which
# the channel check, this wait and `views.open` share, and the rows are put in by an update when
# they come later.
OPEN_WAIT = 1.0
# A folder's listing is kept for LISTING_TTL (LISTING_PARTIAL_TTL when it is not complete), so
# the keystrokes of one search do not walk the folder again, and a listing stops at
# LISTING_BUDGET with what it found; LISTING_KEPT folders are kept at most. MODALS_KEPT modals
# are tracked at most (`ModalUpdates`).
LISTING_BUDGET = 2.0
LISTING_TTL = 30.0
LISTING_PARTIAL_TTL = 3.0
LISTING_KEPT = 16
MODALS_KEPT = 16
# The repositories found in a folder are kept for a shorter time than its files: one made in the
# session (by Claude Code, in a subfolder) should show within moments. At most GIT_CHAINS
# repositories are asked at once, the rest wait their turn.
REPOSITORIES_TTL = 5.0
GIT_CHAINS = 4

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
    looked = await asyncio.gather(*(lookup(candidate, folder) for candidate in below))
    found = [usable(repository) for repository in looked]
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
    gate = asyncio.Semaphore(GIT_CHAINS)

    async def listed(repository: Repository) -> tuple[list[str], bool]:
        try:
            # Waiting for a turn is part of the budget: it is the listing's own.
            async with asyncio.timeout(budget), gate:
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


@dataclass(frozen=True)
class Listing:
    """A folder's files for the search. `complete` says nothing was left out: no walk or git
    listing ran out of time or failed. `fresh` says it was made for the request that got it, not
    kept from an earlier one."""

    files: list[str]
    complete: bool
    fresh: bool = True


@dataclass(frozen=True)
class Found:
    """What a search for `words` found: the files that exist, at most the limit asked, ranked;
    `count` is how many there are (the matches by name once the check stopped at the limit);
    `complete` is False when the listing it came from was cut or failed."""

    paths: list[str]
    count: int
    complete: bool = True


class _Kept[T]:
    """What is made for a folder, kept for as long as its maker says, at most `limit` folders (the
    oldest go first) with the expired ones removed on the next request. Requests for a folder
    whose value is being made wait for that one."""

    def __init__(self, limit: int, clock: Callable[[], float]) -> None:
        self._limit = limit
        self._clock = clock
        self._kept: dict[Path, tuple[float, T]] = {}  # until, value
        self._running: dict[Path, asyncio.Task[T]] = {}

    async def get(
        self,
        folder: Path,
        make: Callable[[], Awaitable[tuple[T, float | None]]],
        *,
        again: bool = False,
    ) -> tuple[T, bool]:
        """The value, and whether it was made for this request (not kept). `make` answers the
        value and how long to keep it, None for not at all. `again` skips what is kept."""
        now = self._clock()
        for stale in [path for path, (until, _) in self._kept.items() if until <= now]:
            del self._kept[stale]
        if not again and (kept := self._kept.get(folder)) is not None:
            return kept[1], False
        task = self._running.get(folder)
        if task is None:
            task = self._running[folder] = asyncio.create_task(self._make(folder, make))
            task.add_done_callback(lambda done: self._running.pop(folder, None))
        # Shielded: a request that gives up (Slack's 3 seconds) leaves the work to the others.
        return await asyncio.shield(task), True

    async def _make(self, folder: Path, make: Callable[[], Awaitable[tuple[T, float | None]]]) -> T:
        started = self._clock()
        value, keep = await make()
        if keep is not None:
            self._kept.pop(folder, None)
            self._kept[folder] = (started + keep, value)
            while len(self._kept) > self._limit:
                del self._kept[next(iter(self._kept))]
        return value


class Listings:
    """The files of a session's folder for the search, and the repositories found in it, kept in
    memory: Slack asks again on every keystroke, and a folder is not walked each time. A listing
    is kept `ttl` seconds when it is complete and `partial_ttl` when it ran out of time or git
    failed for part of it, so a folder too large for the budget is not walked on each keystroke
    but is not taken for complete either. The repositories are kept `repositories_ttl` seconds.
    At most `limit` folders of each are kept (the oldest go first), and the expired ones are
    removed on the next request. Requests for a folder that is being listed wait for that
    listing. A kept listing is never the reason for "no match": `search` makes it again first."""

    def __init__(
        self,
        lookup: RepositoryLookup,
        *,
        ttl: float = LISTING_TTL,
        partial_ttl: float = LISTING_PARTIAL_TTL,
        repositories_ttl: float = REPOSITORIES_TTL,
        budget: float = LISTING_BUDGET,
        limit: int = LISTING_KEPT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lookup = lookup
        self._ttl = ttl
        self._partial_ttl = partial_ttl
        self._repositories_ttl = repositories_ttl
        self._budget = budget
        self._clock = clock
        self._listed = _Kept[Listing](limit, clock)
        self._found = _Kept[list[Repository]](limit, clock)

    async def repositories(self, folder: Path, *, again: bool = False) -> list[Repository]:
        """The repositories the daemon's git may run in for a session started in `folder`
        (`repositories_of`). Raises what the lookup raises."""

        async def find() -> tuple[list[Repository], float | None]:
            return await repositories_of(folder, self._lookup), self._repositories_ttl

        return (await self._found.get(folder, find, again=again))[0]

    async def of(self, folder: Path, *, again: bool = False) -> Listing:
        """The folder's files as paths from it. Never raises: a listing that fails is empty,
        incomplete and not kept. `again` makes it anew whatever is kept."""
        listing, fresh = await self._listed.get(
            folder, lambda: self._list(folder, again), again=again
        )
        return listing if fresh else replace(listing, fresh=False)

    async def _list(self, folder: Path, again: bool) -> tuple[Listing, float | None]:
        try:
            repositories = await self.repositories(folder, again=again)
            files, complete = await folder_files(folder, repositories, self._budget, self._clock)
        except Exception as exc:  # a search that finds nothing beats a modal that never loads
            logger.warning("could not list a folder's files: %s", describe(exc))
            return Listing([], False), None
        return Listing(files, complete), self._ttl if complete else self._partial_ttl

    async def search(self, folder: Path, words: str, *, limit: int | None = None) -> Found:
        """The files of the folder whose path contains `words`, in `rank`'s order, that exist
        (`regular_files`), at most `limit` of them checked. Past the limit `count` is the number
        of matches by name. When a kept listing finds none, the folder is listed again and
        searched anew: a file made since the listing is not told to be missing."""
        listing = await self.of(folder)
        found = await self._matching(folder, listing, words, limit)
        if not found.paths and not listing.fresh:
            found = await self._matching(folder, await self.of(folder, again=True), words, limit)
        return found

    @staticmethod
    async def _matching(folder: Path, listing: Listing, words: str, limit: int | None) -> Found:
        ranked = rank(listing.files, words)
        paths = await asyncio.to_thread(regular_files, folder, ranked, limit)
        count = len(ranked) if limit is not None and len(paths) >= limit else len(paths)
        return Found(paths, count, listing.complete)


EPOCH_SECONDS = re.compile(r"^(\d+)(?:\.\d+)?$")


async def start_commit(repository: Repository, thread_ts: str) -> str | None:
    """Where the repository's HEAD was when the thread started, which `thread_ts` (the Slack ts
    of its root, epoch seconds) says: `HEAD@{<seconds> +0000}`, read from HEAD's own reflog (a
    linked worktree has its own). A commit, or the empty tree when the repository was made after
    the thread began (below), so that every file counts. None when git has no answer: no reflog
    (a repository with `core.logAllRefUpdates` off, or a first commit not made yet), or a
    `thread_ts` that is no time.

    Measured on git 2.54.0 (2026-10-05, 2026-10-06). The bare number is read as a time
    (`HEAD@{1790000150}`) and so is the internal format with its zone, which is the one used: it
    leaves no room for git's approximate dates. A time before the oldest entry gives that entry's
    old commit (a log that `git reflog expire` shortened) or, when its old value is null, its new
    one: the first commit, or a clone's tip, which would leave that commit's own files out of
    what changed. The two are told apart by commands that read the log and write nothing:

    - `rev-list -g --until=<seconds> +0000 --count HEAD` is 0 when every entry is after the
      thread's start (it counts the entries up to that time, as `HEAD@{<time>}` does);
    - `rev-list -g --count HEAD` is the number of entries, N, and `rev-parse --verify --quiet
      HEAD@{N}` is the oldest entry's old value, which fails when it is null (the repository was
      made, or cloned, since); a shortened log answers;
    - `hash-object -t tree /dev/null` is the empty tree of the repository's hash algorithm.

    A log that git cannot be asked this about keeps the answer `HEAD@{<time>}` gave."""
    seconds = EPOCH_SECONDS.match(thread_ts)
    if seconds is None:
        return None
    when = f"{seconds[1]} +0000"
    found = await run_git(repository, "rev-parse", "--verify", "--quiet", f"HEAD@{{{when}}}")
    start = (found or "").strip() or None
    if start is None:
        return None
    until = await run_git(repository, "rev-list", "-g", f"--until={when}", "--count", "HEAD")
    if until is None or until.strip() != "0":
        return start
    entries = await run_git(repository, "rev-list", "-g", "--count", "HEAD")
    if entries is None or not entries.strip().isdecimal():
        return start
    if await run_git(repository, "rev-parse", "--verify", "--quiet", f"HEAD@{{{entries.strip()}}}"):
        return start
    emptied = await run_git(repository, "hash-object", "-t", "tree", "/dev/null")
    return (emptied or "").strip() or start


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

    gate = asyncio.Semaphore(GIT_CHAINS)

    async def changed(repository: Repository) -> list[str]:
        # The turn is waited for outside the time limit: that is git's own.
        async with gate:
            try:
                async with asyncio.timeout(GIT_TIMEOUT):
                    start = await start_commit(repository, thread_ts)
                    return await changed_since(repository, folder, start) or []
            except TimeoutError:
                return []

    return list(
        dict.fromkeys(chain.from_iterable(await asyncio.gather(*map(changed, repositories))))
    )


def _middle(text: str, room: int) -> str:
    """`text` cut in its middle with `…` to `room` characters, its end kept longer than its
    start so an extension stays."""
    if len(text) <= room:
        return text
    end = (room - 1) // 2 + (room - 1) % 2
    return f"{text[: room - 1 - end]}…{text[len(text) - end :]}"


def fits(path: str) -> bool:
    """Whether a path can be an option's value; the file stays reachable by `!open <path>`."""
    return len(path) <= VALUE_LIMIT


def option(path: str) -> dict[str, Any] | None:
    """A row for a path: the file's name as plain text, so that nothing in it is read as markup
    or as an emoji (shortened in its middle past TEXT_LIMIT), its folder below it (shortened
    from the left past DESCRIPTION_LIMIT, left out for a file at the root of the session's
    folder), the path itself as the value. None when the value cannot fit."""
    if not fits(path):
        return None
    folder, _, name = path.rpartition("/")
    row: dict[str, Any] = {
        "text": plain_text_object(_middle(name, TEXT_LIMIT), emoji=False),
        "value": path,
    }
    if folder:
        shown = (
            folder if len(folder) <= DESCRIPTION_LIMIT else "…" + folder[-(DESCRIPTION_LIMIT - 1) :]
        )
        row["description"] = plain_text_object(shown)
    return row


def options(paths: Iterable[str]) -> list[dict[str, Any]]:
    """The first ROW_LIMIT paths that make a row."""
    found: list[dict[str, Any]] = []
    for path in paths:
        if len(found) >= ROW_LIMIT:
            break
        if (shown := option(path)) is not None:
            found.append(shown)
    return found


@dataclass(frozen=True)
class Target:
    """The thread a modal belongs to, carried by the modal itself (`private_metadata`). It comes
    back from Slack and is untrusted: the handlers resolve it to the folder of that thread's own
    session and refuse anything else."""

    channel: str
    thread_ts: str

    def dump(self) -> str:
        return json.dumps({"c": self.channel, "t": self.thread_ts}, separators=(",", ":"))

    @classmethod
    def load(cls, text: object) -> "Target":
        """Raises `ValueError` for anything `dump` did not write."""
        if not isinstance(text, str):
            raise ValueError("no metadata")
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        channel, thread_ts = data.get("c"), data.get("t")
        if not isinstance(channel, str) or not isinstance(thread_ts, str):
            raise ValueError("no thread")
        return cls(channel, thread_ts)


def _field(values: object, block: str, action: str) -> dict[str, Any]:
    """The state of one element in a view's `state.values` (block id, then action id), `{}` for
    any shape Slack does not send."""
    found = values.get(block) if isinstance(values, dict) else None
    element = found.get(action) if isinstance(found, dict) else None
    return element if isinstance(element, dict) else {}


def typed_in(values: object) -> str:
    """The text in the search field, from a view's `state.values`; `""` when there is none."""
    value = _field(values, QUERY_BLOCK, QUERY_ACTION).get("value")
    return value if isinstance(value, str) else ""


def choice_block(view: object) -> dict[str, Any] | None:
    """The block of rows in a view's own `blocks`; None when the view shows none."""
    blocks = view.get("blocks") if isinstance(view, dict) else None
    for block in blocks if isinstance(blocks, list) else []:
        if isinstance(block, dict) and str(block.get("block_id")).startswith(f"{CHOICE_BLOCK}:"):
            return block
    return None


def chosen_in(view: object) -> str | None:
    """The value of the row chosen in a view as Slack sends it back (`blocks` and
    `state.values`), None when none is. Only a row of the block the view shows counts: the state
    of a block with an id that is gone, or a value that is not among its options, is a choice
    made among other rows."""
    block = choice_block(view)
    if not isinstance(view, dict) or block is None:
        return None
    state = view.get("state")
    values = state.get("values") if isinstance(state, dict) else None
    chosen = _field(values, block["block_id"], CHOICE_ACTION).get("selected_option")
    value = chosen.get("value") if isinstance(chosen, dict) else None
    element = block.get("element")
    shown = element.get("options") if isinstance(element, dict) else None
    offered = (
        {o.get("value") for o in shown if isinstance(o, dict)} if isinstance(shown, list) else set()
    )
    return value if isinstance(value, str) and value in offered else None


def _choose_button(words: str) -> dict[str, Any]:
    button: dict[str, Any] = {
        "type": "button",
        "action_id": OPEN_BUTTON_ACTION,
        "text": plain_text_object(texts.OPEN_BUTTON),
    }
    if words:
        button["value"] = words[:QUERY_LIMIT]
    return button


def picker_blocks() -> list[dict[str, Any]]:
    """What `!open` alone posts: the button that opens the modal, and the way to open by name."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": texts.OPEN_TITLE}},
        {"type": "actions", "elements": [_choose_button("")]},
        context_block(texts.OPEN_BY_NAME),
    ]


def matches_blocks(words: str, found: list[str], *, complete: bool = True) -> list[dict[str, Any]]:
    """Files match `words`: how many, and the button that opens the modal with `words` in its
    field; or, when no path is short enough to be a row, the line that says so. With `complete`
    False the line that says the folder could not be listed in full comes last."""
    heading = {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (texts.OPEN_MATCHES_ONE if len(found) == 1 else texts.OPEN_MATCHES).format(
                count=len(found), words=shown_as_written(words)
            ),
        },
    }
    blocks: list[dict[str, Any]] = [heading]
    if any(fits(path) for path in found):
        blocks.append({"type": "actions", "elements": [_choose_button(words)]})
    else:
        blocks.append(context_block(texts.OPEN_MATCHES_TOO_LONG))
    return blocks if complete else [*blocks, context_block(texts.OPEN_PARTIAL)]


def _mark(rows: list[dict[str, Any]]) -> str:
    """What the block of these rows is told apart by: the same rows, the same mark."""
    return hashlib.sha256("\0".join(row["value"] for row in rows).encode()).hexdigest()[:8]


def modal_view(
    target: Target,
    words: str,
    paths: list[str] | None,
    *,
    count: int | None = None,
    complete: bool = True,
    opening: bool = False,
) -> dict[str, Any]:
    """The picker's modal for `target`: the search field holding `words`, and one row for each of
    the first ROW_LIMIT of `paths`, which are the session's changed files (newest first) while
    `words` is empty and the files matching it otherwise; None while they are still being
    listed. `count` is how many files there are when `paths` holds only some of them (it is the
    length of `paths` when None), and `complete` False adds the line that says the folder could
    not be listed in full. `opening` is the view `views.open` takes: it alone sets the field's
    initial value and focus, since an update keeps what was typed through the field's ids and
    must not restate it."""
    field: dict[str, Any] = {
        "type": "plain_text_input",
        "action_id": QUERY_ACTION,
        "max_length": QUERY_LIMIT,
        "placeholder": plain_text_object(texts.OPEN_QUERY_HINT),
        "dispatch_action_config": {"trigger_actions_on": ["on_character_entered"]},
    }
    if opening:
        field["focus_on_load"] = True
        if words:
            field["initial_value"] = words
    blocks: list[dict[str, Any]] = [
        {
            "type": "input",
            "block_id": QUERY_BLOCK,
            "dispatch_action": True,
            "optional": True,
            "label": plain_text_object(texts.OPEN_QUERY_LABEL),
            "element": field,
        }
    ]
    if paths is None:
        blocks.append(context_block(texts.OPEN_LOADING))
    else:
        rows = options(paths)
        total = len(paths) if count is None else count
        if words:
            heading = (texts.OPEN_ROWS_MATCH_ONE if total == 1 else texts.OPEN_ROWS_MATCH).format(
                count=total
            )
        elif total:
            heading = texts.OPEN_ROWS_CHANGED.format(count=total)
        else:
            heading = texts.OPEN_TYPE_A_NAME
        if rows:
            blocks.append(
                {
                    "type": "input",
                    "block_id": f"{CHOICE_BLOCK}:{_mark(rows)}",
                    "optional": True,
                    "label": plain_text_object(heading),
                    "element": {
                        "type": "radio_buttons",
                        "action_id": CHOICE_ACTION,
                        "options": rows,
                    },
                }
            )
        else:
            blocks.append(context_block(heading))
        if total > len(rows):
            blocks.append(
                context_block(
                    texts.OPEN_MATCHES_CAPPED.format(shown=len(rows), count=total)
                    if rows
                    else texts.OPEN_MATCHES_TOO_LONG
                )
            )
        if not complete:
            blocks.append(context_block(texts.OPEN_PARTIAL))
    return {
        "type": "modal",
        "callback_id": OPEN_FORM,
        "private_metadata": target.dump(),
        "title": plain_text_object(texts.OPEN_MODAL_TITLE),
        "submit": plain_text_object(texts.OPEN_MODAL_SUBMIT),
        "close": plain_text_object(texts.OPEN_MODAL_CLOSE),
        "blocks": blocks,
    }


class ModalUpdates:
    """Which update of an open modal may still be written, so that an older one never overwrites
    a newer one. Each keystroke reaches the daemon as its own `block_actions` event, handled in
    its own task, and answers can finish in any order. The daemon is the only writer of its
    modals, so an update carries no `hash` (optional in `views.update`, reference read
    2026-10-06): what orders the updates is a `key` (the event's `action_ts`; 0 for the first
    fill), and:

    - `claim` refuses a key that is not newer than one already taken, and any key of a view that
      was forgotten;
    - `lock` lets one update of a view run at a time, and `current` says, once its turn has
      come and again before it writes, whether it is still the newest;
    - `forget` drops a view whose modal was submitted, for good: nothing of it is tracked again.

    At most `limit` modals are tracked, the oldest dropped first (it is tracked again by its next
    update), and at most `limit` forgotten ones are remembered."""

    def __init__(self, limit: int = MODALS_KEPT) -> None:
        self._limit = limit
        self._newest: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._gone: dict[str, None] = {}

    def claim(self, view_id: str, key: float) -> bool:
        """Takes `key` as the newest of the view; False when one as new was taken already."""
        if view_id in self._gone or key <= self._newest.get(view_id, float("-inf")):
            return False
        self._newest.pop(view_id, None)
        self._newest[view_id] = key
        while len(self._newest) > self._limit:
            self._drop(next(iter(self._newest)))
        return True

    def current(self, view_id: str, key: float) -> bool:
        return self._newest.get(view_id) == key

    def lock(self, view_id: str) -> asyncio.Lock:
        if view_id in self._gone:
            return asyncio.Lock()
        return self._locks.setdefault(view_id, asyncio.Lock())

    def forget(self, view_id: str) -> None:
        self._drop(view_id)
        self._gone[view_id] = None
        while len(self._gone) > self._limit:
            del self._gone[next(iter(self._gone))]

    def _drop(self, view_id: str) -> None:
        self._newest.pop(view_id, None)
        self._locks.pop(view_id, None)
