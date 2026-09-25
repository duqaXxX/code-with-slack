import json
import subprocess
from pathlib import Path

import pytest

from code_with_slack.trust import workspace_trusted


def trust(home: Path, *paths: Path, accepted: bool = True) -> None:
    """Write Claude Code's own record of trusted folders (permissions reference, 2026-09-25:
    `projects["<path>"].hasTrustDialogAccepted` in `~/.claude.json`)."""
    record = {"projects": {str(p.resolve()): {"hasTrustDialogAccepted": accepted} for p in paths}}
    (home / ".claude.json").write_text(json.dumps(record))


def git_init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


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


def add_worktree(repo: Path, path: Path) -> Path:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git(
        "-c",
        "user.name=a",
        "-c",
        "user.email=a@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    git("worktree", "add", "-q", str(path))
    return path


async def test_a_worktree_follows_its_main_checkout(tmp_path: Path, home: Path) -> None:
    repo = git_init(tmp_path / "app")
    worktree = add_worktree(repo, tmp_path / "wt")
    trust(home, repo)
    assert await workspace_trusted(worktree, home)


async def test_no_git_to_run_means_not_trusted(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "notes"
    folder.mkdir()
    trust(home, tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))  # no git anywhere
    assert not await workspace_trusted(folder, home)


async def test_a_failing_git_is_not_read_as_outside_a_repository(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A Mac without the command line tools has a git stub that fails; a trusted parent must not
    # then cover the folder as if it were outside any repository.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "git"
    stub.write_text("#!/bin/sh\necho 'xcrun: error: invalid active developer path' >&2\nexit 1\n")
    stub.chmod(0o755)
    folder = tmp_path / "notes"
    folder.mkdir()
    trust(home, tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:/bin")
    assert not await workspace_trusted(folder, home)
