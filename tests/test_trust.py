import asyncio
import itertools
import json
import logging
import os
import shutil
import subprocess
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path

import pytest

from code_with_slack.trust import workspace_trusted
from tests.git_layouts import (
    add_worktree,
    bare_layout,
    committed,
    git,
    git_finds_a_repository,
    git_init,
    git_takes_it_for,
    trust,
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


async def test_a_repository_is_trusted_at_its_root(tmp_path: Path, home: Path) -> None:
    repo = git_init(tmp_path / "code" / "app")
    (repo / "src").mkdir()
    trust(home, repo)
    assert await workspace_trusted(repo, home)
    assert await workspace_trusted(repo / "src", home)


async def test_a_trusted_parent_does_not_cover_a_repository_inside_it(
    tmp_path: Path, home: Path
) -> None:
    # "the trust covers any subdirectory ... apart from a git repository nested inside it,
    # such as a clone" (permissions reference, read 2026-09-25)
    repo = git_init(tmp_path / "code" / "clone")
    trust(home, tmp_path / "code")
    assert not await workspace_trusted(repo, home)


async def test_a_folder_outside_git_is_covered_by_a_trusted_parent(
    tmp_path: Path, home: Path
) -> None:
    folder = tmp_path / "notes" / "day"
    folder.mkdir(parents=True)
    trust(home, tmp_path / "notes")
    assert await workspace_trusted(folder, home)


async def test_a_refused_or_unknown_folder_is_not_trusted(tmp_path: Path, home: Path) -> None:
    repo = git_init(tmp_path / "app")
    assert not await workspace_trusted(repo, home)  # no record at all
    trust(home, repo, accepted=False)
    assert not await workspace_trusted(repo, home)
    (home / ".claude.json").write_text("{not json")
    assert not await workspace_trusted(repo, home)


async def test_a_worktree_follows_its_main_checkout(tmp_path: Path, home: Path) -> None:
    repo = git_init(tmp_path / "app")
    worktree = add_worktree(repo, tmp_path / "wt")
    trust(home, repo)
    assert await workspace_trusted(worktree, home)


async def test_the_answer_needs_no_git(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The check reads the filesystem: a Mac without the command line tools answers the same.
    repo = git_init(tmp_path / "notes" / "clone")
    trust(home, tmp_path / "notes")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))  # no git anywhere
    assert await workspace_trusted(tmp_path / "notes", home)
    assert not await workspace_trusted(repo, home)


async def test_the_trust_record_is_read_again_only_when_it_changes(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "notes"
    folder.mkdir()
    trust(home, folder)
    reads: list[str] = []
    real = json.loads
    monkeypatch.setattr(json, "loads", lambda text: reads.append("read") or real(text))
    assert await workspace_trusted(folder, home) and await workspace_trusted(folder, home)
    assert len(reads) == 1  # a `!bind` list checks many folders against one record
    trust(home, folder, accepted=False)
    os.utime(home / ".claude.json", ns=(1, 1))  # a different mtime, as a real write gives
    assert not await workspace_trusted(folder, home)


async def test_a_batch_of_checks_parses_the_record_once(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folders = [tmp_path / f"f{i}" for i in range(8)]
    for folder in folders:
        folder.mkdir()
    trust(home, *folders)
    os.utime(home / ".claude.json", ns=(2, 2))  # a record no earlier test cached
    parses: list[str] = []
    real = json.loads

    def slow_loads(text: str) -> object:
        parses.append("parse")
        time.sleep(0.05)  # a multi-MB record: the other threads arrive meanwhile
        return real(text)

    monkeypatch.setattr(json, "loads", slow_loads)
    verdicts = await asyncio.gather(*(workspace_trusted(f, home) for f in folders))
    assert all(verdicts) and len(parses) == 1


def gitfile_absolute(folder: Path, app: Path) -> tuple[str, Path]:
    (folder / ".git").write_text(f"gitdir: {app}/.git\n")
    return "--git-common-dir", app / ".git"


def gitfile_relative(folder: Path, app: Path) -> tuple[str, Path]:
    (folder / ".git").write_text(f"gitdir: {os.path.relpath(app / '.git', folder)}\n")
    return "--git-common-dir", app / ".git"


def gitfile_to_a_worktree_s_admin_dir(folder: Path, app: Path) -> tuple[str, Path]:
    (folder / ".git").write_text(f"gitdir: {app}/.git/worktrees/wt\n")
    return "--git-common-dir", app / ".git"


def symlink_to_the_git_dir(folder: Path, app: Path) -> tuple[str, Path]:
    (folder / ".git").symlink_to(app / ".git")
    return "--git-common-dir", app / ".git"


def symlink_to_a_worktree_s_gitfile(folder: Path, app: Path) -> tuple[str, Path]:
    (folder / ".git").symlink_to(app.parent / "wt" / ".git")
    return "--git-common-dir", app / ".git"


def hard_link_to_a_worktree_s_gitfile(folder: Path, app: Path) -> tuple[str, Path]:
    os.link(app.parent / "wt" / ".git", folder / ".git")
    return "--git-common-dir", app / ".git"


def commondir_in_a_git_dir(folder: Path, app: Path) -> tuple[str, Path]:
    (folder / ".git").mkdir()
    (folder / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (folder / ".git" / "commondir").write_text(f"{app}/.git\n")
    return "--git-common-dir", app / ".git"


def commondir_in_a_bare_layout(folder: Path, app: Path) -> tuple[str, Path]:
    (folder / "HEAD").write_text("ref: refs/heads/main\n")
    (folder / "commondir").write_text(f"{app}/.git\n")
    return "--git-common-dir", app / ".git"


def bare_layout_with_core_worktree(folder: Path, app: Path) -> tuple[str, Path]:
    folder.rmdir()
    bare_layout(folder, **{"core.bare": "false", "core.worktree": str(app)})
    return "--show-toplevel", app


PLANTED = [
    gitfile_absolute,
    gitfile_relative,
    gitfile_to_a_worktree_s_admin_dir,
    symlink_to_the_git_dir,
    symlink_to_a_worktree_s_gitfile,
    hard_link_to_a_worktree_s_gitfile,
    commondir_in_a_git_dir,
    commondir_in_a_bare_layout,
    bare_layout_with_core_worktree,
]


@pytest.fixture
def app(tmp_path: Path, home: Path) -> Path:
    """A repository the owner trusted, with a registered worktree `wt` beside it, in a folder
    `code` the owner trusted too."""
    repo = committed(tmp_path / "code" / "app")
    git(repo, "worktree", "add", "-q", str(tmp_path / "code" / "wt"))
    trust(home, repo, tmp_path / "code")
    return repo.resolve()


@pytest.mark.parametrize("nested", [False, True], ids=["under-a-trusted-folder", "in-a-clone"])
@pytest.mark.parametrize("plant", PLANTED, ids=lambda plant: plant.__name__)
async def test_a_folder_that_claims_a_trusted_repository_is_not_trusted(
    plant: Callable[[Path, Path], tuple[str, Path]], nested: bool, app: Path, home: Path
) -> None:
    code = app.parent
    folder = (committed(code / "clone") / "vendor" if nested else code) / "planted"
    folder.mkdir(parents=True)
    asked, claimed = plant(folder, app)
    # The control: git itself takes the folder for the trusted repository.
    assert git_takes_it_for(folder, asked, claimed)
    assert not await workspace_trusted(folder, home)


async def test_a_folder_named_like_a_worktree_with_a_trailing_space_is_not_it(
    app: Path, home: Path
) -> None:
    twin = app.parent / "wt "
    twin.mkdir()
    (twin / ".git").write_text(f"gitdir: {app}/.git/worktrees/wt\n")
    assert await workspace_trusted(app.parent / "wt", home)
    assert not await workspace_trusted(twin, home)


@pytest.mark.parametrize("nested", [False, True], ids=["under-a-trusted-folder", "in-a-clone"])
@pytest.mark.parametrize(
    ("name", "gitfile"),
    [
        ("pointer", "gitdir: /nonexistent\n"),
        ("x not a gitdir y", "gitdir: nowhere\n"),
        ("not a git repository", "garbage\n"),
        ("empty", ""),
    ],
)
async def test_a_broken_gitfile_is_not_read_as_outside_a_repository(
    name: str, gitfile: str, nested: bool, app: Path, home: Path
) -> None:
    # git repeats the path in its errors: a folder's name must not decide what the error means.
    folder = (committed(app.parent / "clone") if nested else app.parent) / name
    folder.mkdir()
    (folder / ".git").write_text(gitfile)
    assert not await workspace_trusted(folder, home)


@pytest.mark.parametrize("entry", [".git", ".git/HEAD", ".git/config"])
async def test_a_fifo_in_the_git_metadata_does_not_hold_the_answer(
    entry: str, app: Path, home: Path
) -> None:
    # An archive can carry a FIFO; git blocks on HEAD and config (measured 2026-10-04).
    folder = app.parent / "piped"
    if entry == ".git":
        folder.mkdir()
    else:
        committed(folder)
        (folder / entry).unlink()
    os.mkfifo(folder / entry)
    assert not await asyncio.wait_for(workspace_trusted(folder, home), 2)


ENTRIES = ("HEAD", "objects", "refs", "commondir")


@pytest.mark.parametrize(
    "entries",
    [c for n in range(len(ENTRIES) + 1) for c in itertools.combinations(ENTRIES, n)],
    ids="+".join,
)
async def test_a_folder_git_takes_for_a_bare_repository_is_never_covered_by_its_parent(
    entries: tuple[str, ...], app: Path, home: Path
) -> None:
    # Pins the reading of git's own test (setup.c, is_git_directory, v2.54.0) to the git
    # installed here: wherever git finds a repository, a trusted parent must not cover it.
    folder = app.parent / "layout"
    folder.mkdir()
    for entry in entries:
        if entry == "HEAD":
            (folder / entry).write_text("ref: refs/heads/main\n")
        elif entry == "commondir":
            (folder / entry).write_text(f"{app}/.git\n")
        else:
            (folder / entry).mkdir()
    assert await workspace_trusted(folder, home) is not git_finds_a_repository(folder)


async def test_legitimate_layouts_keep_their_trust(tmp_path: Path, home: Path) -> None:
    code = tmp_path / "code"
    repo = committed(code / "app")
    (repo / "src").mkdir()
    inside = add_worktree(repo, repo / "wt-in")
    lib = committed(code / "lib")
    git(repo, "submodule", "add", "-q", str(lib), "sub")
    git(code, "init", "-q", "--bare", "store.git")
    git(code / "store.git", "fetch", "-q", str(lib), "HEAD:refs/heads/main")
    git(code / "store.git", "worktree", "add", "-q", str(code / "of-bare"), "main")
    (code / "sep").mkdir()
    git(code / "sep", "init", "-q", "--separate-git-dir", str(code / "sep-git-dir"))
    container = code / "container"
    container.mkdir()
    git(container, "clone", "-q", "--bare", str(lib), ".git")
    git(container, "worktree", "add", "-q", str(container / "main"), "HEAD")
    (container / "plain").mkdir()
    line_break = code / "wt\nnl"
    git(repo, "worktree", "add", "-q", "--detach", str(line_break))
    decomposed = code / unicodedata.normalize("NFD", "caffè")
    decomposed.mkdir()
    under_nfd = add_worktree(repo, decomposed / "wt")
    (code / "link").symlink_to(repo)
    (code / "notes" / "empty" / ".git").mkdir(parents=True)  # no git dir: git walks past it
    (code / "notes" / "named" / "sub").mkdir(parents=True)
    (code / "notes" / "named" / "HEAD").write_text("a file of that name, no repository\n")
    trust(home, repo, repo / "sub", code / "of-bare", code / "sep", container, code / "notes")
    for folder in (
        repo / "src",
        inside,
        repo / "sub",
        code / "of-bare",
        code / "sep",
        container,
        container / "plain",
        container / "main",
        repo / ".git",
        repo / ".git" / "refs",
        line_break,
        under_nfd,
        code / unicodedata.normalize("NFC", "caffè") / "wt",
        code / "link" / "src",
        code / "notes" / "empty",
        code / "notes" / "named" / "sub",
    ):
        assert await workspace_trusted(folder, home), folder


async def test_a_path_in_another_case_names_the_same_folder(tmp_path: Path, home: Path) -> None:
    repo = git_init(tmp_path / "code" / "app")
    (tmp_path / "notes" / "day").mkdir(parents=True)
    if not (tmp_path / "CODE").exists():
        pytest.skip("this filesystem tells the two cases apart")
    trust(home, repo, tmp_path / "notes")
    assert await workspace_trusted(tmp_path / "CODE" / "APP", home)
    assert await workspace_trusted(tmp_path / "NOTES" / "day", home)


async def test_a_worktree_linked_with_relative_paths_follows_its_main_checkout(
    tmp_path: Path, home: Path
) -> None:
    repo = committed(tmp_path / "app")
    worktree = tmp_path / "wt"
    try:
        git(repo, "worktree", "add", "-q", "--relative-paths", str(worktree))
    except subprocess.CalledProcessError:
        pytest.skip("git older than 2.48 has no --relative-paths")
    trust(home, repo)
    assert await workspace_trusted(worktree, home)
    git(repo, "worktree", "move", str(worktree), str(tmp_path / "moved"))
    assert await workspace_trusted(tmp_path / "moved", home)


async def test_a_worktree_renamed_by_hand_is_trusted_again_once_repaired(
    tmp_path: Path, home: Path
) -> None:
    # The main checkout still registers the old path: nothing on its side names the new one.
    repo = git_init(tmp_path / "app")
    worktree = add_worktree(repo, tmp_path / "wt")
    trust(home, repo)
    renamed = worktree.rename(tmp_path / "renamed")
    assert not await workspace_trusted(renamed, home)
    git(renamed, "worktree", "repair")
    assert await workspace_trusted(renamed, home)


async def test_known_limit_a_folder_at_the_path_of_a_deleted_worktree_passes_for_it(
    tmp_path: Path, home: Path
) -> None:
    # Trust is by path, as in Claude Code: until `git worktree prune`, the main checkout still
    # registers that path, and whatever sits there is its worktree.
    repo = git_init(tmp_path / "app")
    worktree = add_worktree(repo, tmp_path / "wt")
    trust(home, repo)
    shutil.rmtree(worktree)
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {repo}/.git/worktrees/wt\n")
    assert await workspace_trusted(worktree, home)


async def test_a_folder_s_name_never_reaches_the_log(
    app: Path, home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    folder = app.parent / "x\nERROR forged line"
    folder.mkdir()
    (folder / ".git").write_text("gitdir: /nonexistent\n")
    with caplog.at_level(logging.DEBUG):
        assert not await workspace_trusted(folder, home)
    assert "forged" not in caplog.text


def nul_in_the_gitfile(folder: Path) -> None:
    (folder / ".git").write_bytes(b"gitdir: /srv/\x00/x\n")


def gitfile_to_a_long_chain_of_symlinks(folder: Path) -> None:
    (folder / ".git").write_text(f"gitdir: {symlink_chain(folder.parent / 'chain')}\n")


def git_symlink_to_a_long_chain_of_symlinks(folder: Path) -> None:
    (folder / ".git").symlink_to(symlink_chain(folder.parent / "chain"))


def symlink_chain(holder: Path, length: int = 1200) -> Path:
    """The first of `length` symlinks, each naming the next: more than Python's `realpath`
    follows before it gives up with a RecursionError (3.12)."""
    holder.mkdir()
    (holder / "end").mkdir()
    for n in range(length):
        (holder / f"l{n}").symlink_to(holder / (f"l{n + 1}" if n + 1 < length else "end"))
    return holder / "l0"


@pytest.mark.parametrize(
    "plant",
    [
        nul_in_the_gitfile,
        gitfile_to_a_long_chain_of_symlinks,
        git_symlink_to_a_long_chain_of_symlinks,
    ],
    ids=lambda plant: plant.__name__,
)
async def test_metadata_that_names_no_path_leaves_the_folder_untrusted(
    plant: Callable[[Path], None], app: Path, home: Path
) -> None:
    # One such folder under the allowed root must not take the whole `!bind` list down with it.
    folder = app.parent / "planted"
    folder.mkdir()
    plant(folder)
    assert not await workspace_trusted(folder, home)


async def test_a_worktree_registry_planted_in_a_trusted_folder_that_is_no_repository_lends_nothing(
    tmp_path: Path, home: Path
) -> None:
    # `code` is trusted and outside git. A `.git` there that holds a worktree registry and no
    # repository does not make `code` a main checkout.
    code = tmp_path / "code"
    folder = committed(code / "clone") / "vendor"
    folder.mkdir()
    registry = code / ".git" / "worktrees" / "id"
    registry.mkdir(parents=True)
    (registry / "gitdir").write_text(f"{folder}/.git\n")
    (folder / ".git").write_text(f"gitdir: {registry}\n")
    trust(home, code)
    assert not await workspace_trusted(folder, home)


def record(home: Path, *paths: Path) -> None:
    """The record with the paths as written, a symlink in them not resolved."""
    projects = {str(p): {"hasTrustDialogAccepted": True} for p in paths}
    (home / ".claude.json").write_text(json.dumps({"projects": projects}))


async def test_a_recorded_path_that_passes_through_a_symlink_trusts_nothing(
    tmp_path: Path, home: Path
) -> None:
    real = (tmp_path / "elsewhere" / "notes").resolve()
    real.mkdir(parents=True)
    base = real.parent.parent
    (base / "link").symlink_to(real)
    (base / "parent").symlink_to(real.parent)
    record(home, base / "link")
    assert not await workspace_trusted(real, home)
    record(home, base / "parent" / "notes")
    assert not await workspace_trusted(real, home)
    record(home, real)
    assert await workspace_trusted(real, home)


async def test_the_git_dir_reached_in_another_case_is_the_repository_s_own(
    tmp_path: Path, home: Path
) -> None:
    repo = committed(tmp_path / "app")
    if not (repo / ".GIT").exists():
        pytest.skip("this filesystem tells the two cases apart")
    trust(home, repo)
    assert await workspace_trusted(repo / ".GIT" / "refs", home)


@pytest.mark.parametrize("kind", ["fifo", "symlink"])
async def test_a_registration_that_is_no_regular_file_registers_nothing(
    kind: str, app: Path, home: Path
) -> None:
    worktree = app.parent / "wt"
    registration = app / ".git" / "worktrees" / "wt" / "gitdir"
    written = registration.read_text()
    registration.unlink()
    if kind == "fifo":
        os.mkfifo(registration)
    else:
        (app.parent / "elsewhere").write_text(written)
        registration.symlink_to(app.parent / "elsewhere")
    trust(home, app)  # the folder `code` no longer, or it would cover the worktree as a folder
    assert not await asyncio.wait_for(workspace_trusted(worktree, home), 2)


async def test_stricter_than_git_where_only_the_names_match(app: Path, home: Path) -> None:
    # Two readings kept on the closed side: entries named like a git dir's that git itself
    # rejects, and a `.git` symlink that names nothing.
    lookalike = app.parent / "lookalike"
    for entry in ("objects", "refs"):
        (lookalike / entry).mkdir(parents=True)
    (lookalike / "HEAD").write_text("no ref at all\n")
    dangling = app.parent / "dangling"
    dangling.mkdir()
    (dangling / ".git").symlink_to(app.parent / "nothing-here")
    assert not git_finds_a_repository(lookalike)
    assert not await workspace_trusted(lookalike, home)
    assert not await workspace_trusted(dangling, home)
