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

from code_with_slack.trust import trusted_repository, workspace_trusted
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
        code / "link" / "src",
        code / "notes" / "empty",
        code / "notes" / "named" / "sub",
    ):
        assert await workspace_trusted(folder, home), folder
    # git records the worktree's path in the composed form (git 2.54.0 on macOS), which names
    # the same folder only where the filesystem folds the two forms together.
    composed = code / unicodedata.normalize("NFC", "caffè") / "wt"
    if composed.exists():
        assert await workspace_trusted(composed, home)


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


@pytest.mark.parametrize("tail", [b"\r\n", b"\n\n"], ids=["crlf", "two-line-feeds"])
async def test_a_registration_git_reads_through_other_line_ends_registers_the_worktree(
    tail: bytes, app: Path, home: Path
) -> None:
    # git strips trailing whitespace from `worktrees/<id>/gitdir` (worktree.c,
    # get_linked_worktree, v2.54.0); a file ending in a CRLF is still the worktree's.
    registration = app / ".git" / "worktrees" / "wt" / "gitdir"
    registration.write_bytes(registration.read_bytes().rstrip(b"\n") + tail)
    listed = git(app, "worktree", "list", "--porcelain")
    assert "wt" in listed and "prunable" not in listed  # the control: git still has it
    trust(home, app)  # the folder `code` no longer, or it would cover the worktree as a folder
    assert await workspace_trusted(app.parent / "wt", home)


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


# --- the daemon's own git in a repository inside the session's folder ---


@pytest.fixture
def work(tmp_path: Path, home: Path) -> Path:
    """The folder a session started in: not a repository, and trusted in Claude Code."""
    folder = tmp_path / "work"
    folder.mkdir()
    trust(home, folder)
    return folder.resolve()


@pytest.mark.parametrize("depth", [1, 2, 3])
@pytest.mark.parametrize("inside", [".", "src"], ids=["at-the-root", "in-a-subfolder"])
async def test_a_repository_inside_the_session_s_folder_is_usable_without_its_own_trust(
    work: Path, home: Path, depth: int, inside: str
) -> None:
    repo = git_init(work.joinpath(*"abc"[:depth]))
    (repo / "src").mkdir()
    assert not await workspace_trusted(repo, home)  # the start gate does not take it
    found = await trusted_repository(repo / inside, work, home)
    assert found is not None and found.root == repo.resolve()


async def test_a_repository_outside_the_session_s_folder_needs_its_own_trust(
    tmp_path: Path, work: Path, home: Path
) -> None:
    # The agent may `cd` anywhere: only what lies inside the folder it started in is covered.
    outside = git_init(tmp_path / "elsewhere")
    sibling = git_init(tmp_path / "work-sibling")  # the same prefix, not inside `work`
    assert await trusted_repository(outside, work, home) is None
    assert await trusted_repository(sibling, work, home) is None
    trust(home, work, outside)
    assert await trusted_repository(outside, work, home) is not None


async def test_an_untrusted_session_folder_covers_nothing_inside_it(
    tmp_path: Path, home: Path
) -> None:
    folder = tmp_path / "work"
    repo = git_init(folder / "app")
    assert await trusted_repository(repo, folder, home) is None  # no record at all
    trust(home, folder, accepted=False)
    assert await trusted_repository(repo, folder, home) is None
    trust(home, folder)
    assert await trusted_repository(repo, folder, home) is not None  # the control


async def test_a_session_folder_that_is_a_repository_does_not_cover_itself(
    tmp_path: Path, home: Path
) -> None:
    # Claude Code keys a repository on its own root: a trusted parent does not cover it, and the
    # folder's own root is not "inside" it.
    repo = git_init(tmp_path / "code" / "app")
    trust(home, tmp_path / "code")
    assert await trusted_repository(repo, repo, home) is None
    assert await trusted_repository(repo / ".git", repo, home) is None
    trust(home, repo)
    assert await trusted_repository(repo, repo, home) is not None


async def test_a_session_folder_inside_a_repository_covers_only_what_that_repository_trusts(
    tmp_path: Path, home: Path
) -> None:
    # A session in a subfolder of a repository the owner never trusted: the folder fails the start
    # gate, so a repository inside it is not covered either.
    outer = git_init(tmp_path / "outer")
    folder = outer / "packages"
    inner = git_init(folder / "lib")
    assert await trusted_repository(inner, folder, home) is None
    trust(home, outer)
    assert await trusted_repository(inner, folder, home) is not None


async def test_a_symlink_in_the_session_s_folder_to_a_repository_elsewhere_is_not_inside(
    tmp_path: Path, work: Path, home: Path
) -> None:
    elsewhere = git_init(tmp_path / "elsewhere" / "repo")
    (elsewhere / "src").mkdir()
    (work / "link").symlink_to(elsewhere)
    assert await trusted_repository(work / "link", work, home) is None
    assert await trusted_repository(work / "link" / "src", work, home) is None
    # The control: the same repository really inside the folder.
    git_init(work / "real")
    assert await trusted_repository(work / "real", work, home) is not None


async def test_a_session_folder_reached_through_a_symlink_covers_what_is_inside_it(
    tmp_path: Path, work: Path, home: Path
) -> None:
    repo = git_init(work / "app")
    (tmp_path / "via").symlink_to(work)
    found = await trusted_repository(tmp_path / "via" / "app", tmp_path / "via", home)
    assert found is not None and found.root == repo.resolve()


async def test_a_worktree_is_inside_only_when_its_main_checkout_is(
    tmp_path: Path, work: Path, home: Path
) -> None:
    # A worktree is keyed on its main checkout, wherever the worktree itself is.
    main_outside = git_init(tmp_path / "main")
    away = add_worktree(main_outside, work / "wt-of-an-outside-main")
    assert await trusted_repository(away, work, home) is None
    main_inside = git_init(work / "main")
    beside = add_worktree(main_inside, work / "wt-of-an-inside-main")
    found = await trusted_repository(beside, work, home)
    assert found is not None and found.key == main_inside.resolve()
    # The main checkout trusted by the owner covers its worktree wherever it is.
    trust(home, work, main_outside)
    assert await trusted_repository(away, work, home) is not None


def a_gitfile_naming_an_outside_git_dir(work: Path, outside: Path) -> Path:
    folder = work / "gitfile"
    folder.mkdir()
    (folder / ".git").write_text(f"gitdir: {outside}/.git\n")
    return folder


def a_gitfile_naming_an_outside_git_dir_by_a_relative_path(work: Path, outside: Path) -> Path:
    folder = work / "gitfile"
    folder.mkdir()
    (folder / ".git").write_text(f"gitdir: {os.path.relpath(outside / '.git', folder)}\n")
    return folder


def a_git_symlink_to_an_outside_git_dir(work: Path, outside: Path) -> Path:
    folder = work / "gitlink"
    folder.mkdir()
    (folder / ".git").symlink_to(outside / ".git")
    return folder


def a_worktree_of_an_outside_checkout_moved_in_by_hand(work: Path, outside: Path) -> Path:
    moved = add_worktree(outside, outside.parent / "wt").rename(work / "wt-moved")
    return moved


def a_git_dir_whose_commondir_names_an_outside_repository(work: Path, outside: Path) -> Path:
    folder = work / "commondir"
    (folder / ".git").mkdir(parents=True)
    (folder / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (folder / ".git" / "commondir").write_text(f"{outside}/.git\n")
    return folder


def a_git_dir_whose_commondir_is_a_symlink_to_an_outside_name(work: Path, outside: Path) -> Path:
    folder = work / "commondir-link"
    (folder / ".git").mkdir(parents=True)
    (folder / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (folder.parent / "names-outside").write_text(f"{outside}/.git\n")
    (folder / ".git" / "commondir").symlink_to(folder.parent / "names-outside")
    return folder


def a_commondir_naming_an_outside_repository_by_a_link_and_a_line_end(
    work: Path, outside: Path, name: str, line_end: bytes
) -> Path:
    # git strips every trailing CR and LF from `commondir` (setup.c, get_common_dir_noenv,
    # v2.54.0), so `link` is the name whatever follows it; a name that kept its `\r` would be a
    # path that does not exist, which resolves to a place inside the folder.
    folder = work / name
    (folder / ".git").mkdir(parents=True)
    (folder / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (folder / ".git" / "link").symlink_to(outside / ".git")
    (folder / ".git" / "commondir").write_bytes(b"link" + line_end)
    return folder


def a_git_dir_whose_commondir_ends_in_crlf(work: Path, outside: Path) -> Path:
    return a_commondir_naming_an_outside_repository_by_a_link_and_a_line_end(
        work, outside, "commondir-crlf", b"\r\n"
    )


def a_git_dir_whose_commondir_ends_in_two_line_feeds(work: Path, outside: Path) -> Path:
    return a_commondir_naming_an_outside_repository_by_a_link_and_a_line_end(
        work, outside, "commondir-lflf", b"\n\n"
    )


LEADING_OUTSIDE = [
    a_gitfile_naming_an_outside_git_dir,
    a_gitfile_naming_an_outside_git_dir_by_a_relative_path,
    a_git_symlink_to_an_outside_git_dir,
    a_worktree_of_an_outside_checkout_moved_in_by_hand,
    a_git_dir_whose_commondir_names_an_outside_repository,
    a_git_dir_whose_commondir_is_a_symlink_to_an_outside_name,
    a_git_dir_whose_commondir_ends_in_crlf,
    a_git_dir_whose_commondir_ends_in_two_line_feeds,
]


@pytest.mark.parametrize("layout", LEADING_OUTSIDE, ids=lambda layout: layout.__name__)
async def test_a_layout_inside_the_folder_that_leads_git_outside_it_is_not_covered(
    tmp_path: Path, work: Path, home: Path, layout: Callable[[Path, Path], Path]
) -> None:
    # The folder's trust covers a repository inside it, and git must read that repository's own
    # config: a `.git` that points elsewhere makes git read someone else's.
    outside = committed(tmp_path / "outside")
    assert await trusted_repository(outside, work, home) is None  # the control: nothing trusted
    folder = layout(work, outside)
    assert await trusted_repository(folder, work, home) is None
    # The owner's own trust of the folder itself is the first way in, and stays as it was.
    trust(home, work, folder)
    assert await trusted_repository(folder, work, home) is not None


@pytest.mark.parametrize(
    "layout",
    [a_git_dir_whose_commondir_ends_in_crlf, a_git_dir_whose_commondir_ends_in_two_line_feeds],
    ids=lambda layout: layout.__name__,
)
def test_git_reads_the_common_dir_through_those_line_ends(
    tmp_path: Path, work: Path, layout: Callable[[Path, Path], Path]
) -> None:
    # The control of the layouts above: git itself ends up in the outside repository.
    outside = committed(tmp_path / "outside").resolve()
    folder = layout(work, outside)
    assert git_takes_it_for(folder, "--git-common-dir", outside / ".git")


async def test_a_commondir_git_cannot_read_covers_nothing(
    tmp_path: Path, work: Path, home: Path
) -> None:
    # An empty `commondir` is a fatal error in git ("failed to read"): not a layout to guess at.
    folder = work / "commondir-empty"
    (folder / ".git").mkdir(parents=True)
    (folder / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (folder / ".git" / "commondir").write_bytes(b"")
    assert not git_finds_a_repository(folder)
    assert await trusted_repository(folder, work, home) is None


async def test_a_git_dir_outside_the_folder_that_the_owner_trusted_is_not_a_second_way_in(
    tmp_path: Path, work: Path, home: Path
) -> None:
    outside = committed(tmp_path / "outside")
    trust(home, work, outside)
    folder = a_gitfile_naming_an_outside_git_dir(work, outside)
    # The folder is no repository the owner trusted, and its git dir is not the folder's.
    assert await trusted_repository(folder, work, home) is None
    assert await trusted_repository(outside, work, home) is not None


async def test_layouts_that_stay_inside_the_folder_are_covered(work: Path, home: Path) -> None:
    gitfile = git_init(work / "elsewhere-inside" / "store")
    (work / "viafile").mkdir()
    (work / "viafile" / ".git").write_text(f"gitdir: {gitfile / '.git'}\n")
    (work / "vialink").mkdir()
    (work / "vialink" / ".git").symlink_to(gitfile / ".git")
    for folder in (work / "viafile", work / "vialink"):
        assert await trusted_repository(folder, work, home) is not None
    # A worktree whose main checkout is inside: its git dir and its common dir are too.
    main = committed(work / "main")
    beside = add_worktree(main, work / "wt")
    found = await trusted_repository(beside, work, home)
    assert found is not None and found.key == main.resolve()


async def test_the_start_gate_still_refuses_a_repository_the_owner_did_not_trust(
    work: Path, home: Path
) -> None:
    repo = git_init(work / "app")
    assert await trusted_repository(repo, work, home) is not None
    assert not await workspace_trusted(
        repo, home
    )  # `!bind` and a session's start stay as they were
    assert await workspace_trusted(work, home)


async def test_a_layout_with_no_key_is_not_covered_inside_the_session_s_folder(
    work: Path, home: Path
) -> None:
    layout = bare_layout(work / "vendor")
    assert await trusted_repository(layout, work, home) is None
    assert await trusted_repository(layout / "refs", work, home) is None


async def test_the_inside_check_needs_no_git(
    tmp_path: Path, work: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = git_init(work / "app")
    outside = git_init(tmp_path / "elsewhere")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))  # no git anywhere
    assert await trusted_repository(repo, work, home) is not None
    assert await trusted_repository(outside, work, home) is None
