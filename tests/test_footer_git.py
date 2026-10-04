"""The footer's branch and changes: git runs only on a repository the owner trusted, named to
it outright, so no folder's own config or attributes decide what runs."""

import functools
import os
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from code_with_slack import footer
from code_with_slack.footer import git_state
from code_with_slack.trust import Repository, trusted_repository
from tests.git_layouts import add_worktree, bare_layout, committed, git, trust

Lookup = Callable[[Path], Awaitable[Repository | None]]


@pytest.fixture
def marker(tmp_path: Path) -> Path:
    """The file a planted filter touches when git runs it."""
    return tmp_path / "filter-ran"


@pytest.fixture
def app(tmp_path: Path) -> Path:
    """A repository the owner trusted, in a folder `code` trusted too."""
    return committed(tmp_path / "code" / "app").resolve()


@pytest.fixture
def repository(tmp_path: Path, app: Path) -> Lookup:
    home = tmp_path / "home"
    home.mkdir()
    trust(home, app, app.parent)
    return functools.partial(trusted_repository, home=home)


def clean_filter(marker: Path) -> str:
    return f"sh -c 'touch \"{marker}\"; cat'"


def planted(path: Path, marker: Path) -> Path:
    """Someone else's repository: a clean filter in its own config, bound to every path, and a
    tracked file whose content git must read to diff it."""
    committed(path)
    git(path, "config", "filter.planted.clean", clean_filter(marker))
    (path / ".git" / "info").mkdir(exist_ok=True)
    (path / ".git" / "info" / "attributes").write_text("* filter=planted\n")
    (path / "README").write_text("two\nthree\n")
    return path


def a_repository_under_a_trusted_folder(app: Path, marker: Path) -> Path:
    return planted(app.parent / "clone", marker)


def an_embedded_bare_layout(app: Path, marker: Path) -> Path:
    seed = planted(app.parent / "seed", marker)
    layout = app / "vendor"
    (seed / ".git").rename(layout)
    (layout / "tree").mkdir()
    (layout / "tree" / "README").write_text("two\nthree\n")
    git(layout, "config", "-f", "config", "core.bare", "false")
    git(layout, "config", "-f", "config", "core.worktree", str(layout / "tree"))
    return layout


def core_worktree_naming_the_trusted_root(app: Path, marker: Path) -> Path:
    clone = planted(app.parent / "clone", marker)
    git(clone, "config", "core.worktree", str(app))
    return clone


def a_commondir_borrowing_the_trusted_repository(app: Path, marker: Path) -> Path:
    # The owner's repository has per-worktree config on; the planted git dir brings its own.
    git(app, "config", "core.repositoryformatversion", "1")
    git(app, "config", "extensions.worktreeConfig", "true")
    folder = app.parent / "clone"
    (folder / ".git").mkdir(parents=True)
    (folder / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (folder / ".git" / "commondir").write_text(f"{app}/.git\n")
    worktree_config = str(folder / ".git" / "config.worktree")
    git(
        folder.parent, "config", "-f", worktree_config, "filter.planted.clean", clean_filter(marker)
    )
    shutil.copy(app / ".git" / "index", folder / ".git" / "index")
    (folder / ".gitattributes").write_text("* filter=planted\n")
    (folder / "README").write_text("two\nthree\n")
    return folder


def a_gitfile_to_a_planted_git_dir(app: Path, marker: Path) -> Path:
    seed = planted(app.parent / "seed", marker)
    (seed / ".git").rename(app.parent / "planted-git-dir")
    git(app.parent, "config", "-f", "planted-git-dir/config", "core.worktree", str(app))
    folder = app.parent / "clone"
    folder.mkdir()
    (folder / ".git").write_text(f"gitdir: {app.parent}/planted-git-dir\n")
    return folder


def a_sibling_named_with_a_trailing_space(app: Path, marker: Path) -> Path:
    return planted(app.parent / "app ", marker)


def a_gitfile_to_a_git_dir_named_with_a_trailing_space(app: Path, marker: Path) -> Path:
    seed = planted(app.parent / "seed", marker)
    (seed / ".git").rename(app / ".git ")
    (app / "dep").mkdir()
    (app / "dep" / ".git").write_text("gitdir: ../.git \n")
    (app / "dep" / "README").write_text("two\nthree\n")
    return app / "dep"


def a_gitfile_borrowing_the_trusted_git_dir(app: Path, marker: Path) -> Path:
    # The filter is the owner's own, in the trusted repository's config: the borrowing folder's
    # attributes choose to run it on files the owner never put there.
    git(app, "config", "filter.owned.clean", clean_filter(marker))
    (app / "dep").mkdir()
    (app / "dep" / ".git").write_text("gitdir: ../.git\n")
    (app / "dep" / ".gitattributes").write_text("* filter=owned\n")
    (app / "dep" / "README").write_text("not the owner's file\n")
    return app / "dep"


PLANTED = [
    a_repository_under_a_trusted_folder,
    an_embedded_bare_layout,
    core_worktree_naming_the_trusted_root,
    a_commondir_borrowing_the_trusted_repository,
    a_gitfile_to_a_planted_git_dir,
    a_sibling_named_with_a_trailing_space,
    a_gitfile_to_a_git_dir_named_with_a_trailing_space,
    a_gitfile_borrowing_the_trusted_git_dir,
]


def filter_ran(marker: Path) -> bool:
    """Whether the planted filter ran since the last look."""
    ran = marker.exists()
    marker.unlink(missing_ok=True)
    return ran


def git_diffs_there(folder: Path) -> None:
    """What a footer that lets git find the repository runs in `folder`."""
    subprocess.run(["git", "-C", str(folder), "diff-files", "--shortstat"], capture_output=True)


@pytest.mark.parametrize("plant", PLANTED, ids=lambda plant: plant.__name__)
async def test_no_filter_runs_where_the_owner_trusted_no_repository(
    plant: Callable[[Path, Path], Path], app: Path, marker: Path, repository: Lookup
) -> None:
    folder = plant(app, marker)
    filter_ran(marker)  # building the folder ran it once
    # The control: git, left to find the repository from the folder, runs the filter.
    git_diffs_there(folder)
    assert filter_ran(marker)
    assert await git_state(folder, repository) == (None, None)
    assert not filter_ran(marker)


async def test_a_nested_repository_staged_as_a_gitlink_is_not_entered(
    app: Path, marker: Path, repository: Lookup
) -> None:
    nested = planted(app / "nested", marker)
    (nested / "README").write_text("two\n")  # the committed size: only its content tells
    git(app, "add", "nested")
    git(app, "commit", "-q", "-m", "gitlink")
    filter_ran(marker)
    # The control: a plain diff asks the nested repository whether it is dirty, under its config.
    git_diffs_there(app)
    assert filter_ran(marker)
    assert await git_state(app, repository) == ("main", (0, 0))
    assert not filter_ran(marker)


async def test_a_submodule_counts_by_its_commit_alone(
    tmp_path: Path, app: Path, repository: Lookup
) -> None:
    # git never looks inside a submodule for the footer, which is what keeps it out of a planted
    # one. Its files therefore count nothing, and a checked-out commit that differs counts one
    # line each way, also where the submodule is set to `ignore = all`.
    git(app, "submodule", "add", "-q", str(committed(tmp_path / "lib")), "sub")
    git(app, "config", "-f", ".gitmodules", "submodule.sub.ignore", "all")
    git(app, "commit", "-q", "-am", "sub")
    (app / "sub" / "new").write_text("untracked\n")
    (app / "sub" / "README").write_text("changed\n")
    assert await git_state(app, repository) == ("main", (0, 0))
    git(app / "sub", "commit", "-q", "-am", "moved")
    assert await git_state(app, repository) == ("main", (1, 1))


async def test_inside_the_trusted_repository_s_git_dir_the_branch_alone_shows(
    app: Path, repository: Lookup
) -> None:
    assert await git_state(app / ".git", repository) == ("main", None)
    assert await git_state(app / ".git" / "refs", repository) == ("main", None)


async def test_a_bare_layout_shows_nothing_and_a_trusted_bare_container_its_branch(
    tmp_path: Path, app: Path
) -> None:
    layout = bare_layout(app.parent / "layout")
    container = app.parent / "container"
    container.mkdir()
    git(container, "clone", "-q", "--bare", str(app), ".git")
    home = tmp_path / "home"
    home.mkdir()
    trust(home, app.parent, container)
    lookup = functools.partial(trusted_repository, home=home)
    assert await git_state(layout, lookup) == (None, None)
    assert await git_state(container, lookup) == ("main", None)  # no work tree to diff


async def test_a_worktree_shows_its_own_branch_and_changes(app: Path, repository: Lookup) -> None:
    worktree = add_worktree(app, app.parent / "feature-x")
    (worktree / "README").write_text("one\nmore\n")
    assert await git_state(worktree, repository) == ("feature-x", (1, 0))


async def test_inside_a_worktree_s_admin_dir_the_branch_is_that_worktree_s(
    app: Path, repository: Lookup
) -> None:
    add_worktree(app, app.parent / "feature-x")
    assert await git_state(app / ".git" / "worktrees" / "feature-x", repository) == (
        "feature-x",
        None,
    )


async def test_a_file_named_head_at_the_root_does_not_change_the_count(
    app: Path, repository: Lookup
) -> None:
    # git runs at the root: `HEAD` there must still be read as the commit, not as that file.
    (app / "src").mkdir()
    (app / "HEAD").write_text("a file\n" * 500)
    git(app, "add", "-A")
    git(app, "commit", "-q", "-m", "a file named HEAD")
    assert await git_state(app / "src", repository) == ("main", (0, 0))


async def test_a_gitfile_s_trailing_space_belongs_to_the_path(tmp_path: Path, app: Path) -> None:
    # As git reads it (setup.c, read_gitfile_gently): only the line end is dropped.
    code = app.parent
    for name, branch in (("store ", "spaced"), ("store", "decoy")):
        seed = committed(code / f"seed-{branch}")
        git(seed, "switch", "-q", "-c", branch)
        (seed / ".git").rename(code / name)
    folder = code / "sep"
    folder.mkdir()
    (folder / ".git").write_text("gitdir: ../store \n")
    home = tmp_path / "home"
    home.mkdir()
    trust(home, folder)
    lookup = functools.partial(trusted_repository, home=home)
    assert (await git_state(folder, lookup))[0] == "spaced"


async def test_git_is_given_the_repository_and_never_looks_for_one(
    tmp_path: Path, app: Path, marker: Path
) -> None:
    # A trusted folder whose `.git` git itself rejects, inside someone else's repository: git
    # left to search from there would walk up into that repository and run its filter.
    clone = planted(app.parent / "clone", marker)
    kept = clone / "kept"
    for entry in ("objects", "refs"):
        (kept / ".git" / entry).mkdir(parents=True)
    (kept / ".git" / "HEAD").write_text("no ref at all\n")
    home = tmp_path / "home"
    home.mkdir()
    trust(home, kept)
    filter_ran(marker)
    git_diffs_there(kept)
    assert filter_ran(marker)  # the control
    assert await git_state(kept, functools.partial(trusted_repository, home=home)) == (None, None)
    assert not filter_ran(marker)


async def test_every_git_call_shares_one_time_limit(
    tmp_path: Path, app: Path, repository: Lookup, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reply waits for its footer. Each call here answers within the limit, and the limit
    # still ends the second one: it is for all of them together.
    stub = tmp_path / "bin" / "git"
    stub.parent.mkdir()
    stub.write_text("#!/bin/sh\nsleep 0.5\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub.parent}:{os.environ['PATH']}")
    monkeypatch.setattr(footer, "GIT_TIMEOUT", 0.8)
    assert await git_state(app, repository) == (None, None)
