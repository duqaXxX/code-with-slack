"""`!open`'s files: which ones a thread's folder offers, which of them changed in the session, and
which may be shared. Git is the real one on scratch repositories, and the trust lookups the real
ones over a scratch `~/.claude.json`; nothing is mocked below the daemon's own helpers.

Block shapes follow the Slack reference, read 2026-10-05 on docs.slack.dev: the radio button group
element (at most 10 options), the option object (`text` and `description` at most 75 characters,
`value` at most 150, `mrkdwn` text allowed in a radio group), the plain-text input element and the
dispatch action configuration object (`trigger_actions_on`), the modal views page (at most 100
blocks, `private_metadata` at most 3000 characters, `title`, `submit` and `close` at most 24), and
the view interaction payloads (`view.state.values`, as `tests/fixtures/slack/form-submit.json`
records it for a radio group and a plain-text input).
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
    CHOICE_ACTION,
    CHOICE_BLOCK,
    OPEN_BUTTON_ACTION,
    QUERY_ACTION,
    QUERY_BLOCK,
    Listings,
    ModalUpdates,
    NotAFile,
    Target,
    TooLarge,
    changed_in,
    changed_since,
    chosen_in,
    matches_blocks,
    modal_view,
    newest_first,
    option,
    options,
    picker_blocks,
    project_files,
    rank,
    read_openable,
    regular_files,
    repositories_of,
    start_commit,
    typed_in,
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


def test_a_row_names_the_file_with_its_folder_below_and_carries_the_path() -> None:
    assert option("src/app/main.py") == {
        "text": {"type": "plain_text", "text": "main.py", "emoji": False},
        "description": {"type": "plain_text", "text": "src/app"},
        "value": "src/app/main.py",
    }


def test_a_file_at_the_root_of_the_folder_has_no_description() -> None:
    # A text object is never empty: there is no folder to say.
    assert option("README.md") == {
        "text": {"type": "plain_text", "text": "README.md", "emoji": False},
        "value": "README.md",
    }


@pytest.mark.parametrize(
    "name",
    ["__init__.py", "*starred*.md", "~struck~.txt", "a<b>&c.txt", "x`y`.md", "_a_.py", ":tada:.md"],
)
def test_a_name_that_reads_as_formatting_is_shown_exactly(name: str) -> None:
    # Plain text: nothing in a name is markup, so nothing is escaped, bolded or reinterpreted.
    shown = option(f"pkg/{name}")
    assert shown is not None and shown["text"]["text"] == name
    assert shown["text"]["type"] == "plain_text" and shown["text"]["emoji"] is False


def test_a_long_file_name_is_shortened_in_its_middle_and_keeps_its_end() -> None:
    name = "a" * 60 + "-middle-" + "b" * 30 + ".test.py"
    shown = option(f"src/{name}")
    assert shown is not None
    text = shown["text"]["text"]
    assert len(text) == 75 and text.startswith("aaa") and text.endswith("b.test.py")
    assert "…" in text[1:-1]
    assert shown["value"] == f"src/{name}"
    exact = option("n" * 75)  # the limit itself: nothing cut
    assert exact is not None and exact["text"]["text"] == "n" * 75
    assert option("n" * 76)["text"]["text"] == f"{'n' * 37}…{'n' * 37}"  # type: ignore[index]


def test_a_long_folder_is_shortened_from_the_left() -> None:
    folder = "dir/" * 30  # 120 characters
    shown = option(f"{folder}x.py")
    assert shown is not None
    assert len(shown["description"]["text"]) == 75
    assert shown["description"]["text"] == "…" + folder.rstrip("/")[-74:]
    assert shown["value"] == f"{folder}x.py"


def test_a_path_whose_value_cannot_fit_is_left_out() -> None:
    assert option("d" * 150) is not None
    assert option("d" * 151) is None


def test_a_group_holds_ten_rows_and_skips_the_paths_that_cannot_fit() -> None:
    paths = ["z" * 151, *[f"f{i}.py" for i in range(30)]]
    assert [row["value"] for row in options(paths)] == [f"f{i}.py" for i in range(10)]


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


def empty_tree(repo: Path) -> str:
    """The empty tree of the repository's own hash algorithm, as git computes it."""
    return git(repo, "hash-object", "-t", "tree", "/dev/null")


async def test_a_repository_made_after_the_thread_began_counts_every_file(dated: Path) -> None:
    # git answers the first commit itself for a time before the log began, so the first commit's
    # files would never be listed: the start is the empty tree.
    found = repository(dated)
    start = await start_commit(found, started(T0 + 10))
    assert start == empty_tree(dated) and start != commit_of(dated, 2)
    assert sorted(await changed_since(found, dated, start) or []) == ["c1.py", "c2.py", "c3.py"]
    # A thread that began once the first commit was made counts from it, as before.
    assert await start_commit(found, started(T0 + 100)) == commit_of(dated, 2)
    assert await changed_in(dated, [found], started(T0 + 10)) == ["c1.py", "c2.py", "c3.py"]


async def test_a_clone_made_after_the_thread_began_counts_every_file(
    dated: Path, tmp_path: Path
) -> None:
    # The log of a clone starts with `clone: from ...`, whose old value is null as well.
    clone = tmp_path / "clone"
    git_at(tmp_path, T0 + 500, "clone", "-q", str(dated), str(clone))
    found = repository(clone.resolve())
    assert await start_commit(found, started(T0 + 400)) == empty_tree(clone)
    assert await start_commit(found, started(T0 + 600)) == commit_of(clone, 0)


async def test_the_empty_tree_is_the_one_of_the_repository_s_hash_algorithm(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "sha256"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main", "--object-format=sha256")
    commit_at(repo, "a.py", T0 + 100)
    start = await start_commit(repository(repo.resolve()), started(T0 + 10))
    assert start == empty_tree(repo) and len(start or "") == 64


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
    # Each command ran and answered, so that an unchanged index is not a command that failed:
    # a thread that began before this repository was made runs every command of `start_commit`
    # (the time, the entries up to it, all the entries, the first one's old value, the empty tree).
    assert await start_commit(found, started(1)) == empty_tree(app)
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


async def test_the_repositories_below_a_folder_are_looked_up_together(work: Path) -> None:
    for name in ("a", "b", "c", "d"):
        git_init(work / name)
    active = peak = 0

    async def slow(directory: Path, session_folder: Path) -> Repository | None:
        nonlocal active, peak
        if directory == session_folder:
            return None
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return repository(directory)

    assert sorted(r.root for r in await repositories_of(work, slow)) == [
        work / name for name in ("a", "b", "c", "d")
    ]
    assert peak == 4  # not one after the other


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


async def files_of(listing: Listings, folder: Path) -> list[str]:
    return (await listing.of(folder)).files


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
    assert sorted(await files_of(listing, plain)) == [
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
    assert sorted(await files_of(listing, app)) == [
        ".gitignore",
        "README",
        "src/new.py",
        "src/tracked.py",
    ]
    assert sorted(await files_of(Listings(any_repository), app / "src")) == ["new.py", "tracked.py"]


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
    assert sorted(await files_of(listing, work)) == [
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

    assert await files_of(Listings(trusted), work) == ["plain.txt"]


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
    assert await files_of(listing, plain) == ["first.txt"]
    write_all(plain, "second.txt")
    now[0] = 105.0
    assert await files_of(listing, plain) == ["first.txt"]  # a keystroke later: not walked again
    now[0] = 111.0
    assert sorted(await files_of(listing, plain)) == ["first.txt", "second.txt"]


async def test_the_listings_of_two_folders_are_kept_apart(tmp_path: Path) -> None:
    write_all(tmp_path / "x", "x.txt")
    write_all(tmp_path / "y", "y.txt")
    listing = Listings(any_repository)
    assert await files_of(listing, tmp_path / "x") == ["x.txt"]
    assert await files_of(listing, tmp_path / "y") == ["y.txt"]


async def test_a_lookup_that_fails_lists_nothing_and_never_raises(tmp_path: Path) -> None:
    async def broken(directory: Path, session_folder: Path) -> Repository | None:
        raise OSError

    plain = tmp_path / "plain"
    write_all(plain, "a.txt")
    listing = Listings(broken)
    assert await files_of(listing, plain) == []


async def test_concurrent_requests_for_one_folder_share_one_listing(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    write_all(plain, "a.txt")
    lookups: list[Path] = []

    async def slow(directory: Path, session_folder: Path) -> Repository | None:
        lookups.append(directory)
        await asyncio.sleep(0.05)
        return None

    listing = Listings(slow)
    answers = await asyncio.gather(*(files_of(listing, plain) for _ in range(6)))
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
    waiting = asyncio.create_task(files_of(listing, plain))
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.02):
            await files_of(listing, plain)  # a keystroke whose time ran out
    assert await waiting == ["a.txt"]
    assert await files_of(listing, plain) == ["a.txt"]  # kept: the listing finished all the same


async def test_a_listing_that_ran_out_of_time_is_not_kept_as_complete(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    write_all(plain, "first.txt")
    now = [100.0]
    lookups: list[Path] = []

    async def counting(directory: Path, session_folder: Path) -> Repository | None:
        lookups.append(directory)
        return None

    # A budget of nothing: the walk is out of time at once and finds nothing. The repositories are
    # not kept, so that each lookup counts a listing made.
    listing = Listings(
        counting,
        ttl=30.0,
        partial_ttl=3.0,
        repositories_ttl=0.0,
        budget=0.0,
        clock=lambda: now[0],
    )
    assert await files_of(listing, plain) == []
    built = len(lookups)
    now[0] = 102.0
    await files_of(listing, plain)
    assert len(lookups) == built  # a moment later: the same answer, not asked again
    now[0] = 104.0
    await files_of(listing, plain)
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
    assert await files_of(listing, app) == []  # git did not answer in time
    monkeypatch.setenv("PATH", path)
    now[0] = 102.0
    assert await files_of(listing, app) == []  # still the short-lived answer
    now[0] = 104.0
    assert "src/found.py" in await files_of(listing, app)  # asked again, long before 30 seconds


async def test_a_listing_that_fails_is_not_kept(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    write_all(plain, "a.txt")
    failing = [True]

    async def flaky(directory: Path, session_folder: Path) -> Repository | None:
        if failing[0]:
            raise OSError
        return None

    listing = Listings(flaky)
    assert await files_of(listing, plain) == []
    failing[0] = False
    assert await files_of(listing, plain) == ["a.txt"]


async def test_the_kept_listings_are_bounded_and_the_expired_ones_go(tmp_path: Path) -> None:
    now = [100.0]
    listing = Listings(any_repository, ttl=10.0, limit=3, clock=lambda: now[0])
    folders = [tmp_path / f"f{i}" for i in range(5)]
    for folder in folders:
        write_all(folder, "a.txt")
        await files_of(listing, folder)
    assert len(listing._listed._kept) == 3
    assert set(listing._listed._kept) == set(folders[2:])  # the oldest went first
    now[0] = 111.0
    await files_of(listing, folders[0])
    assert set(listing._listed._kept) == {folders[0]}  # the others expired and were removed


async def test_a_kept_listing_that_finds_no_match_is_made_again_before_saying_so(
    tmp_path: Path,
) -> None:
    plain = tmp_path / "plain"
    write_all(plain, "first.txt")
    now = [100.0]
    listing = Listings(any_repository, ttl=30.0, clock=lambda: now[0])
    assert (await listing.search(plain, "second")).paths == []
    write_all(plain, "second.txt")
    now[0] = 105.0  # well inside the listing's life
    found = await listing.search(plain, "second")
    assert found.paths == ["second.txt"] and found.count == 1 and found.complete
    # A match in the kept listing is answered from it, as before: no second walk.
    write_all(plain, "second-more.txt")
    now[0] = 106.0
    assert (await listing.search(plain, "second")).paths == ["second.txt"]


async def test_a_listing_made_for_the_request_is_not_made_again(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    write_all(plain, "first.txt")
    walks: list[Path] = []

    async def counting(directory: Path, session_folder: Path) -> Repository | None:
        walks.append(directory)
        return None

    listing = Listings(counting, repositories_ttl=0.0)
    assert (await listing.search(plain, "nothing")).paths == []
    assert len(walks) == 1  # nothing was kept: the one listing is fresh, and it is the answer


async def test_a_search_says_whether_the_listing_it_came_from_was_cut(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    write_all(plain, "first.txt")
    cut = Listings(any_repository, budget=0.0)  # out of time at once
    found = await cut.search(plain, "first")
    assert found.paths == [] and not found.complete
    whole = await Listings(any_repository).search(plain, "first")
    assert whole.paths == ["first.txt"] and whole.complete


async def test_a_search_checks_files_only_until_the_limit_and_counts_by_name_past_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = tmp_path / "plain"
    write_all(plain, *(f"part{i:02}.csv" for i in range(40)))
    listing = Listings(any_repository)
    checked: list[str] = []
    real = openfile._locate

    def counting(folder: Path, relative: str) -> object:
        checked.append(relative)
        return real(folder, relative)

    monkeypatch.setattr(openfile, "_locate", counting)
    found = await listing.search(plain, "part", limit=10)
    assert len(found.paths) == 10 and found.count == 40 and len(checked) == 10
    checked.clear()
    # Fewer files than the limit: every match was checked, so the count is exact.
    few = await listing.search(plain, "part0", limit=20)
    assert len(few.paths) == 10 and few.count == 10 and len(checked) == 10
    checked.clear()
    assert len((await listing.search(plain, "part")).paths) == 40  # no limit: all are checked
    assert len(checked) == 40


async def test_the_repositories_of_a_folder_are_kept_for_a_short_while(
    work: Path, lookup: Callable[[Path, Path], Awaitable[Repository | None]]
) -> None:
    git_init(work / "one")
    asked: list[Path] = []
    now = [100.0]

    async def counting(directory: Path, session_folder: Path) -> Repository | None:
        asked.append(directory)
        return await lookup(directory, session_folder)

    listing = Listings(counting, repositories_ttl=5.0, clock=lambda: now[0])
    first = await listing.repositories(work)
    made = len(asked)
    assert [r.root for r in first] == [work / "one"] and made > 0
    now[0] = 104.0
    assert await listing.repositories(work) == first and len(asked) == made
    assert await listing.repositories(work, again=True) == first and len(asked) > made
    made = len(asked)
    now[0] = 110.0
    await listing.repositories(work)
    assert len(asked) > made  # past its short life


async def test_a_nested_repository_s_index_is_relative_to_the_folder_above_it(
    work: Path,
) -> None:
    nested = git_init(work / "app")
    write_all(nested, "docs/setup.md")
    commit_all(nested)
    assert await project_files(repository(nested), work) == ["app/docs/setup.md"]


# --- the changes of every repository of the folder ---


def fake_repository(root: Path) -> Repository:
    return Repository(root, root, root / ".git", False)


async def test_git_runs_in_at_most_a_few_repositories_at_once(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(openfile, "GIT_CHAINS", 3)
    active = peak = done = 0

    async def fake_start(repository: Repository, thread_ts: str) -> str | None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        return None

    async def fake_changes(repository: Repository, folder: Path, start: str | None) -> list[str]:
        nonlocal active, done
        active -= 1
        done += 1
        return [f"{repository.root.name}.py"]

    monkeypatch.setattr(openfile, "start_commit", fake_start)
    monkeypatch.setattr(openfile, "changed_since", fake_changes)
    repositories = [fake_repository(work / f"r{i:02}") for i in range(12)]
    found = await changed_in(work, repositories, started(T0))
    assert done == 12 and len(found) == 12
    assert peak == 3


async def test_the_listing_runs_git_in_at_most_a_few_repositories_at_once(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(openfile, "GIT_CHAINS", 2)
    active = peak = 0

    async def listed(repository: Repository, folder: Path) -> list[str] | None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return [f"{repository.root.name}/a.py"]

    monkeypatch.setattr(openfile, "project_files", listed)
    repositories = [fake_repository(work / f"r{i:02}") for i in range(8)]
    files, complete = await openfile.folder_files(work, repositories, budget=5.0)
    assert complete and len(files) == 8
    assert peak == 2


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
    assert await changed_in(work, [broken], started(T0 + 150)) == []
    assert await changed_in(work, [found], started(T0 + 150)) == ["one/a.py"]


async def test_no_repository_has_no_changes(work: Path) -> None:
    assert await changed_in(work, [], started(T0)) == []


# --- the picker and its modal ---

TARGET = Target("C000CHAN", "1790000000.000001")


def blocks_of(view: dict[str, object], kind: str) -> list[dict[str, object]]:
    return [b for b in view["blocks"] if b["type"] == kind]  # type: ignore[attr-defined]


def test_the_message_of_open_alone_is_a_title_one_button_and_the_way_to_open_by_name() -> None:
    section, actions, context = picker_blocks()
    assert section == {"type": "section", "text": {"type": "mrkdwn", "text": "*Open a file*"}}
    (button,) = actions["elements"]
    assert button == {
        "type": "button",
        "action_id": OPEN_BUTTON_ACTION,
        "text": {"type": "plain_text", "text": "Choose a file"},
    }
    assert context == {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "Or type `!open setup` to open a file by name."}],
    }


def test_several_matches_are_counted_and_the_button_carries_the_words() -> None:
    section, actions = matches_blocks("set<up", ["a/setup.py", "b/setup.md"])
    assert section["text"]["text"] == "*2 files match* `set&lt;up`"
    (button,) = actions["elements"]
    assert button["text"]["text"] == "Choose a file" and button["value"] == "set<up"
    # The words a button carries are bounded: its value is at most 2000 characters in Slack.
    (long,) = matches_blocks("w" * 5000, ["a.py", "b.py"])[1]["elements"]
    assert len(long["value"]) == 200


def test_one_match_is_said_in_the_singular_and_a_cut_listing_is_said_last() -> None:
    section, _ = matches_blocks("set", ["a/setup.py"])
    assert section["text"]["text"] == "*1 file matches* `set`"
    cut = matches_blocks("set", ["a/setup.py", "b/setup.md"], complete=False)
    assert [b["type"] for b in cut] == ["section", "actions", "context"]
    assert cut[2]["elements"][0]["text"] == texts.OPEN_PARTIAL
    too_long = matches_blocks("zz", ["e" * 200], complete=False)
    assert [b["type"] for b in too_long] == ["section", "context", "context"]


def test_matches_none_of_which_fits_a_row_are_said_and_get_no_button() -> None:
    blocks = matches_blocks("zz", ["d" * 151 + "/a.py", "e" * 200])
    assert [b["type"] for b in blocks] == ["section", "context"]
    assert "2 files match" in blocks[0]["text"]["text"]
    assert blocks[1]["elements"][0]["text"] == texts.OPEN_MATCHES_TOO_LONG
    # The control: one that fits is a button again.
    fits = matches_blocks("zz", ["d" * 151, "ok.py"])
    assert [b["type"] for b in fits] == ["section", "actions"]


def test_the_modal_is_a_search_field_and_a_row_for_each_changed_file() -> None:
    view = modal_view(TARGET, "", ["docs/guide.md", "notes.txt"], opening=True)
    assert view["type"] == "modal" and view["callback_id"] == "open_form"
    assert view["title"] == {"type": "plain_text", "text": "Open a file"}
    assert view["close"] == {"type": "plain_text", "text": "Close"}
    assert view["submit"] == {"type": "plain_text", "text": "Open"}
    query, choice = view["blocks"]
    assert query == {
        "type": "input",
        "block_id": QUERY_BLOCK,
        "dispatch_action": True,
        "optional": True,
        "label": {"type": "plain_text", "text": "Search any file"},
        "element": {
            "type": "plain_text_input",
            "action_id": QUERY_ACTION,
            "max_length": 200,
            "focus_on_load": True,
            "placeholder": {"type": "plain_text", "text": "Type part of a name"},
            "dispatch_action_config": {"trigger_actions_on": ["on_character_entered"]},
        },
    }
    assert choice["type"] == "input" and choice["block_id"] == choice_id(view)
    assert choice["label"]["text"] == "Changed in this session (2), newest first"
    assert choice.get("dispatch_action", False) is False  # a row chosen sends nothing
    radio = choice["element"]
    assert radio["type"] == "radio_buttons" and radio["action_id"] == CHOICE_ACTION
    assert [o["value"] for o in radio["options"]] == ["docs/guide.md", "notes.txt"]
    assert radio["options"][0] == option("docs/guide.md")


def test_the_search_field_holds_the_words_only_in_the_opening_view() -> None:
    # An update keeps what was typed through the field's ids; restating a value there could
    # put an older text back (views.update reference: "Preserving input entry").
    opening = modal_view(TARGET, "setup", ["a/setup.py"], opening=True)
    update = modal_view(TARGET, "setup", ["a/setup.py"])
    assert opening["blocks"][0]["element"]["initial_value"] == "setup"
    assert "initial_value" not in update["blocks"][0]["element"]
    assert "focus_on_load" not in update["blocks"][0]["element"]
    assert "initial_value" not in modal_view(TARGET, "", [], opening=True)["blocks"][0]["element"]


def choice_id(view: dict[str, object]) -> str:
    (block,) = [b for b in blocks_of(view, "input") if b["block_id"] != QUERY_BLOCK]
    return block["block_id"]  # type: ignore[return-value]


def test_the_search_field_keeps_its_ids_from_one_view_to_the_next() -> None:
    # Slack keeps what was typed in an input block whose ids do not change (views.update).
    def ids(view: dict[str, object]) -> tuple[str, str]:
        (field,) = [b for b in blocks_of(view, "input") if b["block_id"] == QUERY_BLOCK]
        return field["block_id"], field["element"]["action_id"]  # type: ignore[return-value,index]

    first = modal_view(TARGET, "", ["a.py"], opening=True)
    for later in (modal_view(TARGET, "a", ["a.py", "b/a.py"]), modal_view(TARGET, "zz", [])):
        assert ids(later) == ids(first) == (QUERY_BLOCK, QUERY_ACTION)


def test_the_rows_get_an_id_that_follows_them_so_a_selection_is_not_kept_across_other_rows() -> (
    None
):
    # Slack keeps the state of an input block whose ids stay, a chosen row included, even when
    # the rows it chose from are gone (views.update, "Preserving input entry"): the id follows
    # the rows, as the Home tab's controls follow their choice.
    first = modal_view(TARGET, "", ["a.py", "b.py"])
    assert choice_id(first).startswith(f"{CHOICE_BLOCK}:")
    assert choice_id(modal_view(TARGET, "a", ["a.py", "b.py"])) == choice_id(first)  # same rows
    assert choice_id(modal_view(TARGET, "", ["a.py", "c.py"])) != choice_id(first)
    assert choice_id(modal_view(TARGET, "", ["b.py", "a.py"])) != choice_id(first)
    assert len(choice_id(first)) <= 255
    (group,) = [b for b in blocks_of(first, "input") if b["block_id"] != QUERY_BLOCK]
    assert group["element"]["action_id"] == CHOICE_ACTION  # type: ignore[index]
    # With no row there is no radio group (Slack wants an option in it), and the field stays.
    assert [b["block_id"] for b in blocks_of(modal_view(TARGET, "zz", []), "input")] == [
        QUERY_BLOCK
    ]


def test_what_the_rows_are_is_said_above_them() -> None:
    def heading(words: str, paths: list[str]) -> str:
        view = modal_view(TARGET, words, paths)
        return view["blocks"][1]["label"]["text"]

    assert heading("", ["a.py"]) == "Changed in this session (1), newest first"
    assert heading("a", ["a.py", "b/a.py", "c/a.py"]) == "3 files match"
    assert heading("a", ["a.py"]) == "1 file matches"


def test_with_nothing_to_list_a_line_says_what_to_do_and_there_are_no_rows() -> None:
    nothing = modal_view(TARGET, "", [])
    assert [b["type"] for b in nothing["blocks"]] == ["input", "context"]
    assert nothing["blocks"][1]["elements"][0]["text"] == (
        "No changed files to show. Type part of a name to find a file."
    )
    none_match = modal_view(TARGET, "zz", [])
    assert none_match["blocks"][1]["elements"][0]["text"] == "0 files match"


def test_while_the_files_are_listed_the_modal_says_so() -> None:
    loading = modal_view(TARGET, "", None, opening=True)
    assert [b["type"] for b in loading["blocks"]] == ["input", "context"]
    assert loading["blocks"][1]["elements"][0]["text"] == "Looking for files…"


def test_more_than_ten_matches_list_ten_and_say_how_many_there_are() -> None:
    paths = [f"data/part{i:03}.csv" for i in range(137)]
    view = modal_view(TARGET, "part", paths)
    _, choice, capped = view["blocks"]
    assert len(choice["element"]["options"]) == 10
    assert choice["label"]["text"] == "137 files match"
    assert capped["elements"][0]["text"] == texts.OPEN_MATCHES_CAPPED.format(shown=10, count=137)


def test_the_count_past_the_rows_is_the_one_given_and_a_cut_listing_is_said() -> None:
    paths = [f"data/part{i:03}.csv" for i in range(10)]
    view = modal_view(TARGET, "part", paths, count=137)
    _, choice, capped = view["blocks"]
    assert choice["label"]["text"] == "137 files match"
    assert capped["elements"][0]["text"] == texts.OPEN_MATCHES_CAPPED.format(shown=10, count=137)
    cut = modal_view(TARGET, "part", paths[:1], complete=False)
    assert [b["type"] for b in cut["blocks"]] == ["input", "input", "context"]
    assert cut["blocks"][2]["elements"][0]["text"] == texts.OPEN_PARTIAL
    nothing = modal_view(TARGET, "zz", [], complete=False)
    assert [b["type"] for b in nothing["blocks"]] == ["input", "context", "context"]
    assert nothing["blocks"][2]["elements"][0]["text"] == texts.OPEN_PARTIAL
    assert modal_view(TARGET, "zz", []) == modal_view(TARGET, "zz", [], complete=True)


def test_matches_none_of_which_fits_a_row_get_the_line_and_no_group() -> None:
    view = modal_view(TARGET, "e", ["e" * 200])
    assert [b["type"] for b in view["blocks"]] == ["input", "context", "context"]
    assert view["blocks"][1]["elements"][0]["text"] == "1 file matches"
    assert view["blocks"][2]["elements"][0]["text"] == texts.OPEN_MATCHES_TOO_LONG


def test_a_view_stays_inside_what_slack_allows() -> None:
    paths = [
        path
        for i in range(20)
        for path in (
            f"x{i}/" + "d" * 40 + "/" + "e" * 40 + "/" + "y" * 40 + ".py",  # a folder too long
            f"x{i}/" + "y" * 80 + ".py",  # a name too long
        )
    ]
    view = modal_view(TARGET, "x", paths, opening=True)
    assert len(view["blocks"]) <= 100 and len(view["private_metadata"]) <= 3000  # type: ignore[arg-type]
    for key in ("title", "submit", "close"):
        assert len(view[key]["text"]) <= 24  # type: ignore[index]
    for row in view["blocks"][1]["element"]["options"]:  # type: ignore[index]
        assert len(row["text"]["text"]) <= 75 and len(row["value"]) <= 150
        assert len(row["description"]["text"]) <= 75


def test_the_thread_of_a_modal_comes_back_from_its_metadata() -> None:
    view = modal_view(TARGET, "", [])
    assert Target.load(view["private_metadata"]) == TARGET


@pytest.mark.parametrize(
    "text",
    [
        None,
        5,
        "",
        "not json",
        "[]",
        "null",
        '{"c": "C1"}',
        '{"c": 1, "t": "2"}',
        '{"c": "C", "t": []}',
    ],
)
def test_metadata_that_is_not_ours_is_refused(text: object) -> None:
    with pytest.raises(ValueError):
        Target.load(text)


def picked(block_id: str, value: str | None) -> dict[str, object]:
    """The state of a radio group as form-submit.json records it: `selected_option` is None when
    nothing was chosen."""
    option_ = (
        None if value is None else {"text": {"type": "plain_text", "text": value}, "value": value}
    )
    return {block_id: {CHOICE_ACTION: {"type": "radio_buttons", "selected_option": option_}}}


def view_with(blocks: list[dict[str, object]], values: dict[str, object]) -> dict[str, object]:
    return {"blocks": blocks, "state": {"values": values}}


def test_the_typed_text_is_read_from_the_views_state() -> None:
    values = {QUERY_BLOCK: {QUERY_ACTION: {"type": "plain_text_input", "value": "setup"}}}
    assert typed_in(values) == "setup"
    untouched = {QUERY_BLOCK: {QUERY_ACTION: {"type": "plain_text_input", "value": None}}}
    assert typed_in(untouched) == ""


def test_the_row_chosen_is_read_from_the_views_state() -> None:
    shown = modal_view(TARGET, "", ["docs/a.md", "b.md"])
    block = choice_id(shown)
    assert chosen_in(view_with(shown["blocks"], picked(block, "docs/a.md"))) == "docs/a.md"  # type: ignore[arg-type]
    assert chosen_in(view_with(shown["blocks"], picked(block, None))) is None  # type: ignore[arg-type]


def test_a_row_that_the_view_no_longer_shows_is_never_chosen() -> None:
    # Slack kept the state of a radio group across an update; the rows have changed since.
    older = modal_view(TARGET, "", ["old.md", "other.md"])
    newer = modal_view(TARGET, "n", ["new.md"])
    blocks = newer["blocks"]  # type: ignore[assignment]
    # The selection sits under the id the older rows had: no such block is in this view.
    assert chosen_in(view_with(blocks, picked(choice_id(older), "old.md"))) is None
    # Under the id of the rows shown, but for a row that is not among them.
    assert chosen_in(view_with(blocks, picked(choice_id(newer), "old.md"))) is None
    # Under the id of the rows shown, and one of them: the control.
    assert chosen_in(view_with(blocks, picked(choice_id(newer), "new.md"))) == "new.md"


@pytest.mark.parametrize(
    "view",
    [
        None,
        [],
        {},
        {"blocks": None, "state": None},
        {"blocks": [None, 1, {"block_id": 5}], "state": {"values": {}}},
        {"blocks": [{"block_id": f"{CHOICE_BLOCK}:x", "element": []}], "state": {"values": {}}},
    ],
)
def test_a_view_of_a_shape_slack_does_not_send_chooses_nothing(view: object) -> None:
    assert chosen_in(view) is None
    assert typed_in(None) == "" and typed_in([]) == "" and typed_in({QUERY_BLOCK: None}) == ""


# --- the updates of an open modal ---


def test_an_update_older_than_one_already_taken_is_refused() -> None:
    updates = ModalUpdates()
    assert updates.claim("V1", 5.0)
    assert not updates.claim("V1", 4.0)  # arrived late
    assert not updates.claim("V1", 5.0)  # the same event, delivered twice
    assert updates.claim("V1", 6.0)
    assert not updates.current("V1", 5.0) and updates.current("V1", 6.0)


def test_each_view_has_its_own_newest_update() -> None:
    updates = ModalUpdates()
    assert updates.claim("V1", 9.0) and updates.claim("V2", 1.0)
    assert updates.current("V1", 9.0) and updates.current("V2", 1.0)


def test_the_first_fill_of_a_view_is_older_than_any_keystroke() -> None:
    updates = ModalUpdates()
    assert updates.claim("V1", 1790000000.5)
    assert not updates.claim("V1", 0.0)


def test_only_a_few_views_are_tracked_and_the_oldest_go_first() -> None:
    updates = ModalUpdates(limit=2)
    for view_id in ("V1", "V2", "V3"):
        updates.claim(view_id, 1.0)
    assert not updates.current("V1", 1.0) and updates.current("V2", 1.0)
    # One dropped to make room is not closed: a keystroke of it may still come.
    assert updates.claim("V1", 2.0)


def test_a_view_that_was_forgotten_is_never_tracked_again() -> None:
    # The modal was submitted: an update still on its way (an event delivered after the Submit)
    # must not put the view back into the maps.
    updates = ModalUpdates()
    updates.claim("V1", 1.0)
    held = updates.lock("V1")
    updates.forget("V1")
    assert not updates.current("V1", 1.0)
    assert not updates.claim("V1", 5.0) and not updates.current("V1", 5.0)
    assert updates.lock("V1") is not held  # a lock of its own, kept nowhere
    assert "V1" not in updates._newest and "V1" not in updates._locks


def test_only_a_few_forgotten_views_are_remembered() -> None:
    updates = ModalUpdates(limit=2)
    for view_id in ("V1", "V2", "V3"):
        updates.forget(view_id)
    assert len(updates._gone) == 2
    assert updates.claim("V1", 1.0) and not updates.claim("V3", 1.0)  # the oldest is let go


def test_a_view_has_one_lock() -> None:
    updates = ModalUpdates()
    assert updates.lock("V1") is updates.lock("V1") and updates.lock("V1") is not updates.lock("V2")
