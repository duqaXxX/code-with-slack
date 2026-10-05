"""`!open`'s files: which ones a thread's folder offers, which of them changed in the session, and
which may be shared. Git is the real one on scratch repositories, and the trust lookups the real
ones over a scratch `~/.claude.json`; nothing is mocked below the daemon's own helpers.

Block shapes follow the Block Kit reference (select menu element, option object, read
2026-10-05): an option's `text` holds 75 characters and its `value` 150, a select's `options` 100.
"""

import asyncio
import hashlib
import os
import stat
import subprocess
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from code_with_slack import openfile, texts
from code_with_slack.openfile import (
    Listings,
    NotAFile,
    TooLarge,
    changed_in,
    changed_since,
    matches_blocks,
    newest_first,
    option,
    picker_blocks,
    project_files,
    rank,
    read_openable,
    regular_files,
    repositories_of,
    start_commit,
    usable,
    walk_files,
)
from code_with_slack.trust import Repository, locate, trusted_repository
from tests.fakes import any_repository
from tests.git_layouts import commit_at, committed, git, git_at, git_init, trust


def repository(path: Path) -> Repository:
    found = locate(path)
    assert found is not None
    return found


def write(root: Path, name: str, text: str = "x\n") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def commit_all(root: Path, message: str = "x") -> None:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)


@pytest.fixture
def app(tmp_path: Path) -> Path:
    return committed(tmp_path / "app").resolve()


# --- ranking and options ---


def test_basename_matches_come_first_then_the_shorter_path() -> None:
    paths = [
        "setup/readme.md",
        "src/code_with_slack/setup.py",
        "tests/test_setup.py",
        "docs/setup.md",
        "SETUP",
    ]
    assert rank(paths, "setup") == [
        "SETUP",
        "docs/setup.md",
        "tests/test_setup.py",
        "src/code_with_slack/setup.py",
        "setup/readme.md",
    ]


def test_ranking_ignores_case_and_leaves_out_what_does_not_contain_the_words() -> None:
    assert rank(["Docs/Setup.md", "src/app.py"], "sETUP.m") == ["Docs/Setup.md"]


def test_an_option_shows_the_path_and_carries_it() -> None:
    assert option("src/app.py") == {
        "text": {"type": "plain_text", "text": "src/app.py"},
        "value": "src/app.py",
    }


def test_a_long_path_is_shortened_from_the_left_and_keeps_its_value() -> None:
    path = "a/" * 40 + "tail.py"  # 87 characters
    shown = option(path)
    assert shown is not None
    assert len(shown["text"]["text"]) == 75
    assert shown["text"]["text"] == "…" + path[-74:]
    assert shown["value"] == path


def test_a_path_whose_value_cannot_fit_is_left_out() -> None:
    assert option("d" * 150) is not None
    assert option("d" * 151) is None


# --- what may be shared ---


def test_a_regular_file_inside_the_folder_is_read(app: Path) -> None:
    write(app, "docs/a b.md", "# a\n")
    assert read_openable(app, "docs/a b.md") == b"# a\n"
    assert read_openable(app, "./docs/../docs/a b.md") == b"# a\n"


@pytest.mark.parametrize("relative", ["", "..", "../outside.txt", "missing.txt", "docs", "a\0b"])
def test_what_is_not_a_file_inside_the_folder_is_refused(
    app: Path, tmp_path: Path, relative: str
) -> None:
    write(app, "docs/a.md")
    write(tmp_path, "outside.txt")
    with pytest.raises(NotAFile):
        read_openable(app, relative)


def test_an_absolute_path_is_refused_even_when_it_is_a_file(app: Path, tmp_path: Path) -> None:
    outside = write(tmp_path, "outside.txt")
    with pytest.raises(NotAFile):
        read_openable(app, str(outside))


def test_a_symlink_that_leaves_the_folder_is_refused_after_it_is_resolved(
    app: Path, tmp_path: Path
) -> None:
    outside = write(tmp_path, "outside/secret.txt")
    (app / "link.txt").symlink_to(outside)
    (app / "dir-link").symlink_to(outside.parent)
    inside = write(app, "real.txt", "inside\n")
    (app / "alias.txt").symlink_to(inside)
    for relative in ("link.txt", "dir-link/secret.txt"):
        with pytest.raises(NotAFile):
            read_openable(app, relative)
    # A link that stays inside the folder is a file.
    assert read_openable(app, "alias.txt") == b"inside\n"


def test_a_path_swapped_for_a_link_after_it_was_resolved_is_not_followed(
    app: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The check and the read are one open: a file that becomes a link between resolving its path
    # and opening it is refused, not read from wherever the link leads.
    outside = write(tmp_path, "outside/secret.txt", "OUTSIDE\n")
    inside = write(app, "notes.md", "inside\n")
    resolved = openfile._locate(Path(os.path.realpath(app)), "notes.md")
    inside.unlink()
    inside.symlink_to(outside)
    monkeypatch.setattr(openfile, "_locate", lambda real, relative: resolved)
    with pytest.raises(NotAFile):
        read_openable(app, "notes.md")


def test_a_fifo_is_refused_and_never_waited_on(app: Path) -> None:
    os.mkfifo(app / "pipe")
    with pytest.raises(NotAFile):
        read_openable(app, "pipe")


def test_a_file_over_one_megabyte_is_too_large(app: Path) -> None:
    (app / "big.bin").write_bytes(b"x" * (1 << 20))
    (app / "bigger.bin").write_bytes(b"x" * ((1 << 20) + 1))
    assert len(read_openable(app, "big.bin")) == 1 << 20
    with pytest.raises(TooLarge):
        read_openable(app, "bigger.bin")


def test_a_file_that_grows_while_it_is_read_never_gives_more_than_the_limit(
    app: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `fstat` said it fits; by the time it is read it holds more.
    (app / "growing.log").write_bytes(b"x" * 10)
    real = os.fstat

    def small(fd: int) -> os.stat_result:
        found = real(fd)
        if stat.S_ISREG(found.st_mode):
            (app / "growing.log").write_bytes(b"x" * ((1 << 20) * 3))
        return found

    monkeypatch.setattr(os, "fstat", small)
    with pytest.raises(TooLarge):
        read_openable(app, "growing.log")


def test_regular_files_keeps_what_exists_in_order_and_stops_at_the_limit(app: Path) -> None:
    for name in ("a.py", "b.py", "c.py"):
        write(app, name)
    names = ["a.py", "gone.py", "docs", "b.py", "c.py"]
    assert regular_files(app, names) == ["a.py", "b.py", "c.py"]
    assert regular_files(app, names, limit=2) == ["a.py", "b.py"]


# --- the file index ---


async def test_the_index_is_the_tracked_files_and_the_untracked_ones_not_ignored(
    app: Path,
) -> None:
    write(app, ".gitignore", "*.log\nbuild/\n")
    write(app, "src/tracked.py")
    commit_all(app)
    write(app, "src/new.py")
    write(app, "debug.log")
    write(app, "build/out.js")
    assert sorted(await project_files(repository(app), app) or []) == [
        ".gitignore",
        "README",
        "src/new.py",
        "src/tracked.py",
    ]


async def test_the_index_of_a_subfolder_is_relative_to_it_and_stops_at_its_edge(
    app: Path,
) -> None:
    # A folder named with glob characters: a pathspec must take it literally.
    folder = app / "we*ird [x]"
    write(app, "we*ird [x]/in.py")
    write(app, "we*ird [x]/deep/er.py")
    write(app, "weeird x/outside.py")
    write(app, "other.py")
    commit_all(app)
    assert sorted(await project_files(repository(app), folder) or []) == ["deep/er.py", "in.py"]


async def test_the_index_follows_a_folder_reached_through_a_symlink(
    app: Path, tmp_path: Path
) -> None:
    write(app, "sub/in.py")
    commit_all(app)
    (tmp_path / "via").symlink_to(app / "sub")
    assert await project_files(repository(app), tmp_path / "via") == ["in.py"]


async def test_a_folder_without_a_repository_has_no_source(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert usable(await any_repository(plain)) is None


def test_a_git_dir_with_no_work_tree_is_no_source(app: Path) -> None:
    inside = repository(app / ".git")
    assert inside.inside_git_dir
    assert usable(inside) is None
    assert usable(repository(app)) is not None


# --- the start commit and the changed files ---


# The reflog's times are the committer date: a repository built with fixed ones answers the same
# whatever day the tests run.
T0 = 1_790_000_000


def started(when: int) -> str:
    """A thread's `thread_ts` for a thread that started at the epoch second `when`."""
    return f"{when}.000100"


@pytest.fixture
def dated(tmp_path: Path) -> Path:
    """A repository whose HEAD moved at T0+100, T0+200 and T0+300."""
    repo = git_init(tmp_path / "dated").resolve()
    for step in (1, 2, 3):
        commit_at(repo, f"c{step}.py", T0 + step * 100)
    return repo


def commit_of(repo: Path, back: int) -> str:
    return git(repo, "rev-parse", f"HEAD~{back}")


async def test_the_start_is_where_head_was_when_the_thread_started(dated: Path) -> None:
    found = repository(dated)
    assert await start_commit(found, started(T0 + 250)) == commit_of(dated, 1)
    assert await start_commit(found, started(T0 + 200)) == commit_of(dated, 1)
    assert await start_commit(found, started(T0 + 199)) == commit_of(dated, 2)
    # A thread started after the last move: HEAD itself.
    assert await start_commit(found, started(T0 + 300)) == commit_of(dated, 0)
    assert await start_commit(found, started(T0 + 9999)) == commit_of(dated, 0)


async def test_a_reflog_that_does_not_go_back_that_far_gives_its_oldest_entry(dated: Path) -> None:
    # What `git reflog expire` leaves: the first entry gone. git answers the oldest it has (and
    # warns on stderr, which the daemon never reads).
    log = dated / ".git" / "logs" / "HEAD"
    log.write_text("".join(log.read_text().splitlines(keepends=True)[1:]))
    assert await start_commit(repository(dated), started(T0 + 10)) == commit_of(dated, 2)


async def test_a_thread_that_started_before_the_first_commit_counts_from_it(dated: Path) -> None:
    # A repository made after the thread began: git has no earlier entry to give.
    assert await start_commit(repository(dated), started(T0 + 10)) == commit_of(dated, 2)


async def test_without_a_reflog_there_is_no_start(tmp_path: Path) -> None:
    repo = git_init(tmp_path / "unlogged").resolve()
    git(repo, "config", "core.logAllRefUpdates", "false")
    commit_at(repo, "a.py", T0 + 100)
    assert not (repo / ".git" / "logs").exists()
    assert await start_commit(repository(repo), started(T0 + 150)) is None
    # The list is then what is uncommitted or untracked now.
    write(repo, "b.py")
    commit_at(repo, "c.py", T0 + 200)
    write(repo, "d.py")
    found = await changed_in(repo, [repository(repo)], started(T0 + 150))
    assert found == ["d.py"]


async def test_a_repository_with_no_commit_has_no_start_and_lists_what_is_staged(
    tmp_path: Path,
) -> None:
    fresh = git_init(tmp_path / "fresh").resolve()
    write(fresh, "staged.py")
    git(fresh, "add", "staged.py")
    write(fresh, "untracked.py")
    assert await start_commit(repository(fresh), started(T0)) is None
    found = await changed_in(fresh, [repository(fresh)], started(T0))
    assert sorted(found) == ["staged.py", "untracked.py"]


async def test_a_linked_worktree_has_its_own_start(tmp_path: Path) -> None:
    main = git_init(tmp_path / "main").resolve()
    commit_at(main, "a.py", T0 + 100)
    second = commit_at(main, "b.py", T0 + 200)
    git_at(main, T0 + 250, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feature")
    worktree = (tmp_path / "wt").resolve()
    third = commit_at(worktree, "c.py", T0 + 400)
    found = repository(worktree)
    assert found.key == main and found.git_dir != main / ".git"
    assert await start_commit(found, started(T0 + 300)) == second
    assert await start_commit(found, started(T0 + 450)) == third
    # The main checkout's own log is another one: it never saw the worktree's commit.
    assert await start_commit(repository(main), started(T0 + 450)) == second
    assert await changed_since(found, worktree, second) == ["c.py"]


async def test_a_thread_that_started_before_a_branch_switch_counts_from_where_it_began(
    tmp_path: Path,
) -> None:
    repo = git_init(tmp_path / "switching").resolve()
    commit_at(repo, "base.py", T0 + 100)
    main_tip = commit_at(repo, "on_main.py", T0 + 200)
    git_at(repo, T0 + 300, "switch", "-q", "-c", "feature")
    commit_at(repo, "on_feature.py", T0 + 400)
    # Begun on main, now on feature: what feature holds beyond main's tip is what changed.
    start = await start_commit(repository(repo), started(T0 + 250))
    assert start == main_tip
    assert await changed_since(repository(repo), repo, start) == ["on_feature.py"]
    # Begun on feature, now back on main: the files of feature are not there to be opened.
    git_at(repo, T0 + 500, "switch", "-q", "main")
    begun_on_feature = await start_commit(repository(repo), started(T0 + 450))
    assert begun_on_feature == git(repo, "rev-parse", "feature")
    assert await changed_since(repository(repo), repo, begun_on_feature) == []


@pytest.mark.parametrize(
    "ts", ["", "abc", "1790000000.x", "-5", "1790000000 +0000", "@{1}", "../x"]
)
async def test_a_thread_ts_that_is_no_epoch_second_gives_no_start(dated: Path, ts: str) -> None:
    assert await start_commit(repository(dated), ts) is None


async def test_a_start_that_git_cannot_read_is_none(dated: Path) -> None:
    found = repository(dated)
    broken = Repository(found.root, found.key, dated / "no-such-git-dir", False)
    assert await start_commit(broken, started(T0 + 250)) is None


async def test_the_changed_files_are_those_added_or_modified_since_the_start(app: Path) -> None:
    write(app, "kept.py")
    write(app, "gone.py")
    write(app, "reverted.py", "one\n")
    commit_all(app)
    start = git(app, "rev-parse", "HEAD")
    # Committed since the start.
    write(app, "committed_new.py")
    write(app, "kept.py", "changed\n")
    write(app, "reverted.py", "two\n")
    commit_all(app)
    write(app, "reverted.py", "one\n")
    commit_all(app)
    # Not committed: staged, unstaged and untracked; a deletion; an ignored file.
    write(app, "staged.py")
    git(app, "add", "staged.py")
    write(app, "README", "edited\n")
    write(app, "untracked.py")
    (app / "gone.py").unlink()
    write(app, ".gitignore", "ignored.py\n")
    write(app, "ignored.py")
    found = await changed_since(repository(app), app, start)
    assert found is not None
    # `reverted.py` was committed changed and committed back: no net change since the start.
    assert sorted(found) == [
        ".gitignore",
        "README",
        "committed_new.py",
        "gone.py",  # named, though gone: dropped when the list is made, below
        "kept.py",
        "staged.py",
        "untracked.py",
    ]
    assert sorted(newest_first(app, found)) == [
        ".gitignore",
        "README",
        "committed_new.py",
        "kept.py",
        "staged.py",
        "untracked.py",
    ]


async def test_without_a_start_commit_only_the_uncommitted_files_count(app: Path) -> None:
    write(app, "untracked.py")
    write(app, "README", "edited\n")
    found = await changed_since(repository(app), app, None)
    assert found is not None and sorted(found) == ["README", "untracked.py"]


async def test_the_changed_files_of_a_subfolder_are_relative_to_it(app: Path) -> None:
    write(app, "sub/a.py")
    commit_all(app)
    start = git(app, "rev-parse", "HEAD")
    write(app, "sub/b.py")
    write(app, "elsewhere.py")
    commit_all(app)
    write(app, "sub/c.py")
    write(app, "elsewhere2.py")
    found = await changed_since(repository(app), app / "sub", start)
    assert found is not None and sorted(found) == ["b.py", "c.py"]


def test_the_changed_files_come_newest_first_and_only_the_existing_ones(app: Path) -> None:
    for age, name in enumerate(["old.py", "new.py", "middle.py"]):
        path = write(app, name)
        stamp = 1_780_000_000 + [0, 200, 100][age]
        os.utime(path, (stamp, stamp))
    (app / "dir").mkdir()
    assert newest_first(app, ["old.py", "dir", "new.py", "missing.py", "middle.py", "new.py"]) == [
        "new.py",
        "middle.py",
        "old.py",
    ]


def test_a_changed_file_that_leaves_the_folder_through_a_link_is_not_listed(
    app: Path, tmp_path: Path
) -> None:
    (app / "leak.txt").symlink_to(write(tmp_path, "outside.txt"))
    assert newest_first(app, ["leak.txt"]) == []


# --- git never writes the index here (measured on git 2.54.0, 2026-10-05) ---


def index_state(repo: Path) -> tuple[int, int, str]:
    index = repo / ".git" / "index"
    stat = os.stat(index)
    return stat.st_ino, stat.st_mtime_ns, hashlib.sha256(index.read_bytes()).hexdigest()


def touched(repo: Path) -> None:
    """A tracked file whose stat differs from the index and whose content does not."""
    future = time.time() + 1000
    os.utime(repo / "README", (future, future))


def edited(repo: Path) -> None:
    write(repo, "README", "edited\n")


def untracked(repo: Path) -> None:
    write(repo, "new.py")


def settled(repo: Path) -> None:
    """Files older than the index, so that no entry is racily clean and hides a change."""
    past = time.time() - 100
    for path in repo.rglob("*"):
        if path.is_file() and ".git" not in path.relative_to(repo).parts:
            os.utime(path, (past, past))
    git(repo, "update-index", "--refresh")


@pytest.mark.parametrize("change", [lambda r: None, touched, edited, untracked])
async def test_no_command_of_open_writes_the_index_or_leaves_a_lock(
    app: Path, change: Callable[[Path], None]
) -> None:
    write(app, "sub/more.py")
    commit_all(app)
    start = git(app, "rev-parse", "HEAD")
    write(app, "sub/after.py")
    commit_all(app)
    settled(app)
    change(app)
    before = index_state(app)
    found = repository(app)
    # Each command ran and answered, so that an unchanged index is not a command that failed.
    assert await start_commit(found, started(T0)) is not None
    assert await start_commit(found, started(int(time.time()) + 1)) == git(app, "rev-parse", "HEAD")
    await project_files(found, app)
    await project_files(found, app / "sub")
    await changed_since(found, app, start)
    await changed_since(found, app / "sub", start)
    assert index_state(app) == before
    assert not (app / ".git" / "index.lock").exists()


async def test_a_file_that_was_only_touched_is_not_reported_as_changed(app: Path) -> None:
    settled(app)
    touched(app)
    found = await changed_since(repository(app), app, git(app, "rev-parse", "HEAD"))
    assert found == []
    # The commands the footer uses report it, which is why they are not used here.
    assert git(app, "diff-files", "--name-only") == "README"


def test_the_measurement_would_notice_a_write(app: Path) -> None:
    # A control: `git status` without `--no-optional-locks` refreshes the index of a touched
    # file, which is what the assertions above are made to catch.
    settled(app)
    touched(app)
    before = index_state(app)
    subprocess.run(["git", "status", "--porcelain"], cwd=app, check=True, capture_output=True)
    assert index_state(app) != before


# --- the repositories of a session's folder ---


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def work(tmp_path: Path, home: Path) -> Path:
    """A session's folder: not a repository, trusted in Claude Code, repositories inside it."""
    folder = tmp_path / "work"
    folder.mkdir()
    trust(home, folder)
    return folder.resolve()


@pytest.fixture
def lookup(home: Path) -> Callable[[Path, Path], Awaitable[Repository | None]]:
    async def trusted(directory: Path, session_folder: Path) -> Repository | None:
        return await trusted_repository(directory, session_folder, home)

    return trusted


async def roots(
    folder: Path, lookup: Callable[[Path, Path], Awaitable[Repository | None]]
) -> list[Path]:
    return sorted(r.root for r in await repositories_of(folder, lookup))


async def test_a_folder_inside_a_repository_has_that_repository(
    app: Path, home: Path, lookup: Callable[[Path, Path], Awaitable[Repository | None]]
) -> None:
    trust(home, app)
    (app / "src").mkdir()
    assert await roots(app, lookup) == [app]
    assert await roots(app / "src", lookup) == [app]


async def test_a_plain_folder_has_the_usable_repositories_two_levels_below_it(
    work: Path, lookup: Callable[[Path, Path], Awaitable[Repository | None]]
) -> None:
    one = git_init(work / "one")
    two = git_init(work / "group" / "two")
    git_init(work / "group" / "deeper" / "three")  # three levels down: not found
    assert await roots(work, lookup) == sorted([one, two])


async def test_a_repository_that_is_not_usable_is_left_out(
    tmp_path: Path, work: Path, lookup: Callable[[Path, Path], Awaitable[Repository | None]]
) -> None:
    git_init(work / "a")
    (work / "broken").mkdir()
    (work / "broken" / ".git").write_text("not a gitfile\n")  # a `.git` that names no git dir
    (work / "link").symlink_to(git_init(tmp_path / "elsewhere"))
    assert await roots(work, lookup) == [work / "a"]


async def test_the_search_never_goes_into_a_repository_for_more(
    work: Path, lookup: Callable[[Path, Path], Awaitable[Repository | None]]
) -> None:
    outer = git_init(work / "outer")
    git_init(outer / "vendored")
    assert await roots(work, lookup) == [outer]


async def test_hidden_folders_are_not_searched_for_a_repository(
    work: Path, lookup: Callable[[Path, Path], Awaitable[Repository | None]]
) -> None:
    git_init(work / ".hidden")
    assert await roots(work, lookup) == []


async def test_a_folder_in_an_untrusted_repository_has_nothing_below_it(
    tmp_path: Path, home: Path, lookup: Callable[[Path, Path], Awaitable[Repository | None]]
) -> None:
    outer = git_init(tmp_path / "outer")
    folder = outer / "packages"
    inner = git_init(folder / "lib")
    assert await roots(folder, lookup) == []
    trust(home, inner)  # a repository the owner trusted on its own stays usable
    assert await roots(folder, lookup) == [inner.resolve()]
    trust(home, outer)
    assert await roots(folder, lookup) == [outer.resolve()]


# --- what the folder offers to search ---


def write_all(root: Path, *names: str) -> None:
    for name in names:
        write(root, name)


async def test_a_plain_folder_is_walked_for_regular_files_only(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    write_all(plain, "a.txt", "docs/b.md", "docs/deep/c.md", ".hidden/d.txt")
    write(tmp_path / "outside", "secret.txt")
    (plain / "dir-link").symlink_to(tmp_path / "outside")
    (plain / "file-link").symlink_to(plain / "a.txt")
    write_all(plain, ".git/config", "sub/.git/config")
    (plain / "empty-dir").mkdir()
    listing = Listings(any_repository)
    assert sorted(await listing.of(plain)) == [
        ".hidden/d.txt",
        "a.txt",
        "docs/b.md",
        "docs/deep/c.md",
    ]


async def test_a_folder_inside_a_repository_lists_what_git_does_not_ignore(app: Path) -> None:
    write(app, ".gitignore", ".venv/\n*.log\n")
    write_all(app, "src/tracked.py")
    commit_all(app)
    write_all(app, "src/new.py", ".venv/lib/pkg.py", "debug.log")
    listing = Listings(any_repository)
    assert sorted(await listing.of(app)) == [
        ".gitignore",
        "README",
        "src/new.py",
        "src/tracked.py",
    ]
    assert sorted(await Listings(any_repository).of(app / "src")) == ["new.py", "tracked.py"]


async def test_the_files_of_nested_repositories_come_from_git_and_the_rest_from_disk(
    work: Path, lookup: Callable[[Path, Path], Awaitable[Repository | None]]
) -> None:
    nested = git_init(work / "code-with-slack-workspace")
    write(nested, ".gitignore", ".venv/\n")
    write_all(nested, "docs/setup.md", ".venv/lib/pkg.py")
    commit_all(nested)
    write_all(nested, "docs/new.md", ".venv/lib/more.py")
    deep = git_init(work / "a" / "b" / "c")  # three levels down: walked like any folder
    write_all(deep, "x.txt", ".venv/y.txt")
    write_all(work, "notes.md", "a/readme.md")
    listing = Listings(lookup)
    assert sorted(await listing.of(work)) == [
        "a/b/c/.venv/y.txt",
        "a/b/c/x.txt",
        "a/readme.md",
        "code-with-slack-workspace/.gitignore",
        "code-with-slack-workspace/docs/new.md",
        "code-with-slack-workspace/docs/setup.md",
        "notes.md",
    ]


async def test_the_files_of_a_repository_that_is_not_usable_are_walked(
    tmp_path: Path, work: Path, home: Path
) -> None:
    # A symlinked folder is not entered, so a repository behind a link is not listed at all.
    elsewhere = committed(tmp_path / "elsewhere")
    (work / "link").symlink_to(elsewhere)
    write(work, "plain.txt")

    async def trusted(directory: Path, session_folder: Path) -> Repository | None:
        return await trusted_repository(directory, session_folder, home)

    assert await Listings(trusted).of(work) == ["plain.txt"]


def test_a_walk_that_runs_out_of_time_returns_what_it_found_and_says_so(tmp_path: Path) -> None:
    write_all(tmp_path, "top.txt", "one/mid.txt", "one/two/low.txt")
    visits = iter([False, True])  # the root is read, then the time is up
    assert walk_files(tmp_path, frozenset(), lambda: next(visits, True)) == (["top.txt"], False)
    done, complete = walk_files(tmp_path, frozenset(), lambda: False)
    assert sorted(done) == ["one/mid.txt", "one/two/low.txt", "top.txt"] and complete


async def test_a_listing_is_kept_for_a_short_time(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    write_all(plain, "first.txt")
    now = [100.0]
    listing = Listings(any_repository, ttl=10.0, clock=lambda: now[0])
    assert await listing.of(plain) == ["first.txt"]
    write_all(plain, "second.txt")
    now[0] = 105.0
    assert await listing.of(plain) == ["first.txt"]  # a keystroke later: not walked again
    now[0] = 111.0
    assert sorted(await listing.of(plain)) == ["first.txt", "second.txt"]


async def test_the_listings_of_two_folders_are_kept_apart(tmp_path: Path) -> None:
    write_all(tmp_path / "x", "x.txt")
    write_all(tmp_path / "y", "y.txt")
    listing = Listings(any_repository)
    assert await listing.of(tmp_path / "x") == ["x.txt"]
    assert await listing.of(tmp_path / "y") == ["y.txt"]


async def test_a_lookup_that_fails_lists_nothing_and_never_raises(tmp_path: Path) -> None:
    async def broken(directory: Path, session_folder: Path) -> Repository | None:
        raise OSError

    plain = tmp_path / "plain"
    write_all(plain, "a.txt")
    listing = Listings(broken)
    assert await listing.of(plain) == []


async def test_concurrent_requests_for_one_folder_share_one_listing(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    write_all(plain, "a.txt")
    lookups: list[Path] = []

    async def slow(directory: Path, session_folder: Path) -> Repository | None:
        lookups.append(directory)
        await asyncio.sleep(0.05)
        return None

    listing = Listings(slow)
    answers = await asyncio.gather(*(listing.of(plain) for _ in range(6)))
    assert answers == [["a.txt"]] * 6
    assert len(lookups) == 1  # one listing, not six


async def test_a_request_that_gives_up_does_not_stop_the_listing_the_others_wait_for(
    tmp_path: Path,
) -> None:
    plain = tmp_path / "plain"
    write_all(plain, "a.txt")

    async def slow(directory: Path, session_folder: Path) -> Repository | None:
        await asyncio.sleep(0.1)
        return None

    listing = Listings(slow)
    waiting = asyncio.create_task(listing.of(plain))
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.02):
            await listing.of(plain)  # a keystroke whose time ran out
    assert await waiting == ["a.txt"]
    assert await listing.of(plain) == ["a.txt"]  # kept: the listing finished all the same


async def test_a_listing_that_ran_out_of_time_is_not_kept_as_complete(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    write_all(plain, "first.txt")
    now = [100.0]
    lookups: list[Path] = []

    async def counting(directory: Path, session_folder: Path) -> Repository | None:
        lookups.append(directory)
        return None

    # A budget of nothing: the walk is out of time at once and finds nothing.
    listing = Listings(counting, ttl=30.0, partial_ttl=3.0, budget=0.0, clock=lambda: now[0])
    assert await listing.of(plain) == []
    built = len(lookups)
    now[0] = 102.0
    await listing.of(plain)
    assert len(lookups) == built  # a moment later: the same answer, not asked again
    now[0] = 104.0
    await listing.of(plain)
    assert len(lookups) > built  # past its short life: built again, not kept for 30 seconds


async def test_a_listing_whose_git_part_timed_out_is_not_kept_as_complete(
    app: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(app, "src/found.py")
    commit_all(app)
    now = [100.0]
    listing = Listings(any_repository, ttl=30.0, partial_ttl=3.0, budget=0.2, clock=lambda: now[0])
    stub = tmp_path / "bin" / "git"
    stub.parent.mkdir()
    stub.write_text("#!/bin/sh\nsleep 1\n")
    stub.chmod(0o755)
    path = os.environ["PATH"]
    monkeypatch.setenv("PATH", f"{stub.parent}:{path}")
    assert await listing.of(app) == []  # git did not answer in time
    monkeypatch.setenv("PATH", path)
    now[0] = 102.0
    assert await listing.of(app) == []  # still the short-lived answer
    now[0] = 104.0
    assert "src/found.py" in await listing.of(app)  # asked again, long before 30 seconds


async def test_a_listing_that_fails_is_not_kept(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    write_all(plain, "a.txt")
    failing = [True]

    async def flaky(directory: Path, session_folder: Path) -> Repository | None:
        if failing[0]:
            raise OSError
        return None

    listing = Listings(flaky)
    assert await listing.of(plain) == []
    failing[0] = False
    assert await listing.of(plain) == ["a.txt"]


async def test_the_kept_listings_are_bounded_and_the_expired_ones_go(tmp_path: Path) -> None:
    now = [100.0]
    listing = Listings(any_repository, ttl=10.0, limit=3, clock=lambda: now[0])
    folders = [tmp_path / f"f{i}" for i in range(5)]
    for folder in folders:
        write_all(folder, "a.txt")
        await listing.of(folder)
    assert len(listing._kept) == 3
    assert set(listing._kept) == set(folders[2:])  # the oldest went first
    now[0] = 111.0
    await listing.of(folders[0])
    assert set(listing._kept) == {folders[0]}  # the others expired and were removed


async def test_a_nested_repository_s_index_is_relative_to_the_folder_above_it(
    work: Path,
) -> None:
    nested = git_init(work / "app")
    write_all(nested, "docs/setup.md")
    commit_all(nested)
    assert await project_files(repository(nested), work) == ["app/docs/setup.md"]


# --- the changes of every repository of the folder ---


def dated_repo(path: Path) -> Path:
    """A repository with one commit at T0+100."""
    repo = git_init(path).resolve()
    commit_at(repo, "base.py", T0 + 100)
    return repo


async def test_the_changes_of_nested_repositories_are_relative_to_the_folder(work: Path) -> None:
    one = dated_repo(work / "one")
    two = dated_repo(work / "group" / "two")
    write(one, "a.py")
    commit_at(two, "b.py", T0 + 400)
    write(two, "c.py")
    found = await changed_in(work, [repository(one), repository(two)], started(T0 + 300))
    assert sorted(found) == ["group/two/b.py", "group/two/c.py", "one/a.py"]


async def test_each_repository_is_counted_from_where_its_own_head_was(work: Path) -> None:
    one = dated_repo(work / "one")
    two = dated_repo(work / "two")
    commit_at(one, "before.py", T0 + 200)  # before the thread began
    commit_at(two, "also_before.py", T0 + 250)
    commit_at(one, "during_one.py", T0 + 400)
    commit_at(two, "during_two.py", T0 + 500)
    found = await changed_in(work, [repository(one), repository(two)], started(T0 + 300))
    assert sorted(found) == ["one/during_one.py", "two/during_two.py"]


async def test_the_start_comes_from_the_log_and_survives_a_new_process(work: Path) -> None:
    # Nothing is kept in memory: a daemon restarted after every change counts the same.
    one = dated_repo(work / "one")
    commit_at(one, "during.py", T0 + 400)
    first = await changed_in(work, [repository(one)], started(T0 + 300))
    second = await changed_in(work, [repository(one)], started(T0 + 300))
    assert first == second == ["one/during.py"]


async def test_a_repository_whose_git_fails_adds_no_changes(work: Path) -> None:
    one = dated_repo(work / "one")
    write(one, "a.py")
    found = repository(one)
    broken = Repository(found.root, found.key, work / "no-such-git-dir", False)
    assert await changed_in(work, [broken], started(T0)) == []
    assert await changed_in(work, [found], started(T0)) == ["one/a.py"]


async def test_no_repository_has_no_changes(work: Path) -> None:
    assert await changed_in(work, [], started(T0)) == []


# --- the picker ---


def selects(blocks: list[dict[str, object]]) -> list[dict[str, object]]:
    (actions,) = (b for b in blocks if b["type"] == "actions")
    return list(actions["elements"])  # type: ignore[call-overload]


def test_the_picker_offers_the_changed_files_and_a_search() -> None:
    blocks = picker_blocks("1790000000.000001", ["a.py", "docs/b.md"], 2)
    section, actions, context = blocks
    assert section == {"type": "section", "text": {"type": "mrkdwn", "text": "*Open a file*"}}
    assert actions["block_id"] == "open-1790000000.000001"
    changed, search = selects(blocks)
    assert changed["type"] == "static_select"
    assert changed["placeholder"] == {"type": "plain_text", "text": "Changed in this session (2)"}
    assert [o["value"] for o in changed["options"]] == ["a.py", "docs/b.md"]  # type: ignore[index]
    assert search["type"] == "external_select"
    assert search["placeholder"] == {"type": "plain_text", "text": "Search any file…"}
    assert context == {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "Or type `!open setup` to open a file by name."}],
    }


def test_without_changed_files_the_picker_has_the_search_alone() -> None:
    (search,) = selects(picker_blocks("1790000000.000001", [], 0))
    assert search["type"] == "external_select"


def test_matches_none_of_which_fits_a_menu_are_said_not_posted_as_an_empty_select() -> None:
    blocks = matches_blocks("zz", ["d" * 151 + "/a.py", "e" * 200])
    assert [b["type"] for b in blocks] == ["section", "context"]
    assert "2 files match" in blocks[0]["text"]["text"]
    assert blocks[1]["elements"][0]["text"] == texts.OPEN_MATCHES_TOO_LONG
    # The control: one that fits is a menu again, and the others are counted.
    fits = matches_blocks("zz", ["d" * 151, "ok.py"])
    assert [b["type"] for b in fits] == ["section", "actions", "context"]


def test_the_changed_menu_holds_a_hundred_and_says_how_many_there_are() -> None:
    changed_files = [f"f{i:03}.py" for i in range(137)]
    changed, _ = selects(picker_blocks("1790000000.000001", changed_files, 137))
    assert len(changed["options"]) == 100  # type: ignore[arg-type]
    assert changed["placeholder"]["text"] == "Changed in this session (137)"  # type: ignore[index]


def test_a_changed_menu_of_paths_that_cannot_fit_is_left_out() -> None:
    (only,) = selects(picker_blocks("1790000000.000001", ["d" * 151], 1))
    assert only["type"] == "external_select"
