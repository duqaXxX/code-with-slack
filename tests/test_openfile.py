"""`!open`'s files: which ones a thread's folder offers, which of them changed in the session, and
which may be shared. Git is the real one on scratch repositories; nothing is mocked below the
daemon's own helpers.

Block shapes follow the Block Kit reference (select menu element, option object, read
2026-10-05): an option's `text` holds 75 characters and its `value` 150, a select's `options` 100.
"""

import hashlib
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from code_with_slack.openfile import (
    NotAFile,
    Starts,
    TooLarge,
    changed_since,
    newest_first,
    openable,
    option,
    picker_blocks,
    project_files,
    rank,
    regular_files,
    start_commit,
    usable,
)
from code_with_slack.trust import Repository, locate
from tests.fakes import any_repository
from tests.git_layouts import committed, git, git_init

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


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


def test_a_regular_file_inside_the_folder_is_openable(app: Path) -> None:
    write(app, "docs/a b.md")
    assert openable(app, "docs/a b.md") == app / "docs" / "a b.md"
    assert openable(app, "./docs/../docs/a b.md") == app / "docs" / "a b.md"


@pytest.mark.parametrize("relative", ["", "..", "../outside.txt", "missing.txt", "docs", "a\0b"])
def test_what_is_not_a_file_inside_the_folder_is_refused(
    app: Path, tmp_path: Path, relative: str
) -> None:
    write(app, "docs/a.md")
    write(tmp_path, "outside.txt")
    with pytest.raises(NotAFile):
        openable(app, relative)


def test_an_absolute_path_is_refused_even_when_it_is_a_file(app: Path, tmp_path: Path) -> None:
    outside = write(tmp_path, "outside.txt")
    with pytest.raises(NotAFile):
        openable(app, str(outside))


def test_a_symlink_that_leaves_the_folder_is_refused_after_it_is_resolved(
    app: Path, tmp_path: Path
) -> None:
    outside = write(tmp_path, "outside/secret.txt")
    (app / "link.txt").symlink_to(outside)
    (app / "dir-link").symlink_to(outside.parent)
    inside = write(app, "real.txt")
    (app / "alias.txt").symlink_to(inside)
    for relative in ("link.txt", "dir-link/secret.txt"):
        with pytest.raises(NotAFile):
            openable(app, relative)
    assert openable(app, "alias.txt") == inside  # a link that stays inside the folder is a file


def test_a_file_over_one_megabyte_is_too_large(app: Path) -> None:
    (app / "big.bin").write_bytes(b"x" * (1 << 20))
    (app / "bigger.bin").write_bytes(b"x" * ((1 << 20) + 1))
    assert openable(app, "big.bin") == app / "big.bin"
    with pytest.raises(TooLarge):
        openable(app, "bigger.bin")


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


async def test_the_start_commit_is_head(app: Path) -> None:
    assert await start_commit(repository(app)) == git(app, "rev-parse", "HEAD")


async def test_a_repository_without_a_commit_starts_from_the_empty_tree(tmp_path: Path) -> None:
    fresh = git_init(tmp_path / "fresh")
    assert git(fresh, "hash-object", "-t", "tree", "/dev/null") == EMPTY_TREE
    start = await start_commit(repository(fresh))
    assert start == EMPTY_TREE
    write(fresh, "first.py")
    commit_all(fresh)
    # Committed since the start, though the repository had no commit then.
    assert await changed_since(repository(fresh), fresh, start) == ["first.py"]


async def test_the_changed_files_are_those_added_or_modified_since_the_start(app: Path) -> None:
    write(app, "kept.py")
    write(app, "gone.py")
    write(app, "reverted.py", "one\n")
    commit_all(app)
    start = await start_commit(repository(app))
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
    start = await start_commit(repository(app))
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
    start = await start_commit(repository(app))
    write(app, "sub/after.py")
    commit_all(app)
    settled(app)
    change(app)
    before = index_state(app)
    found = repository(app)
    await start_commit(found)
    await project_files(found, app)
    await project_files(found, app / "sub")
    await changed_since(found, app, start)
    await changed_since(found, app / "sub", start)
    assert index_state(app) == before
    assert not (app / ".git" / "index.lock").exists()


async def test_a_file_that_was_only_touched_is_not_reported_as_changed(app: Path) -> None:
    settled(app)
    touched(app)
    found = await changed_since(repository(app), app, await start_commit(repository(app)))
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


# --- the start commit per thread ---


async def test_a_threads_start_is_set_once_and_kept(app: Path) -> None:
    starts = Starts(any_repository)
    first = git(app, "rev-parse", "HEAD")
    assert starts.of("C1", "1.0") is None
    await starts.seen("C1", "1.0", app)
    write(app, "later.py")
    commit_all(app)
    await starts.seen("C1", "1.0", app)  # the thread's process closed and came back
    assert starts.of("C1", "1.0") == first
    # Another thread of the same folder starts from where the repository is now.
    await starts.seen("C1", "2.0", app)
    assert starts.of("C1", "2.0") == git(app, "rev-parse", "HEAD") != first


async def test_a_folder_with_no_repository_records_no_start(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    starts = Starts(any_repository)
    await starts.seen("C1", "1.0", plain)
    assert starts.of("C1", "1.0") is None


async def test_a_lookup_that_fails_records_nothing_and_never_raises(app: Path) -> None:
    async def broken(directory: Path) -> Repository | None:
        raise OSError

    starts = Starts(broken)
    await starts.seen("C1", "1.0", app)
    assert starts.of("C1", "1.0") is None


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


def test_the_changed_menu_holds_a_hundred_and_says_how_many_there_are() -> None:
    changed_files = [f"f{i:03}.py" for i in range(137)]
    changed, _ = selects(picker_blocks("1790000000.000001", changed_files, 137))
    assert len(changed["options"]) == 100  # type: ignore[arg-type]
    assert changed["placeholder"]["text"] == "Changed in this session (137)"  # type: ignore[index]


def test_a_changed_menu_of_paths_that_cannot_fit_is_left_out() -> None:
    (only,) = selects(picker_blocks("1790000000.000001", ["d" * 151], 1))
    assert only["type"] == "external_select"
