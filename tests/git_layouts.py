"""Git folders built for the trust and footer tests: the layouts an owner makes, and the ones
an archive can carry (a `git clone` delivers no `.git` entry). Measured on git 2.54.0,
2026-10-04."""

import json
import os
import subprocess
from pathlib import Path


def trust(home: Path, *paths: Path, accepted: bool = True) -> None:
    """Write Claude Code's own record of trusted folders (permissions reference, 2026-09-25:
    `projects["<path>"].hasTrustDialogAccepted` in `~/.claude.json`)."""
    record = {"projects": {str(p.resolve()): {"hasTrustDialogAccepted": accepted} for p in paths}}
    (home / ".claude.json").write_text(json.dumps(record))


def git(cwd: Path, *args: str) -> str:
    identity = ["-c", "user.name=alice", "-c", "user.email=alice@example.com"]
    done = subprocess.run(
        ["git", *identity, "-c", "protocol.file.allow=always", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def git_at(cwd: Path, when: int, *args: str) -> None:
    """`git` run with the epoch second `when` as its committer and author date: the time the
    reflog records for what it does."""
    env = {**os.environ, "GIT_COMMITTER_DATE": f"{when} +0000", "GIT_AUTHOR_DATE": f"{when} +0000"}
    identity = ["-c", "user.name=alice", "-c", "user.email=alice@example.com"]
    subprocess.run(["git", *identity, *args], cwd=cwd, check=True, env=env, capture_output=True)


def commit_at(root: Path, name: str, when: int) -> str:
    """Commit a new file at the epoch second `when`; the commit's id."""
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_text("x\n")
    git_at(root, when, "add", "-A")
    git_at(root, when, "commit", "-q", "-m", name)
    return git(root, "rev-parse", "HEAD")


def git_init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    return path


def committed(path: Path) -> Path:
    """A repository on `main` with one tracked file, `README`."""
    git_init(path)
    (path / "README").write_text("one\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "x")
    return path


def add_worktree(repo: Path, path: Path) -> Path:
    git(repo, "commit", "-q", "--allow-empty", "-m", "x")
    git(repo, "worktree", "add", "-q", str(path))
    return path


def bare_layout(path: Path, **config: str) -> Path:
    """A folder that is itself a git dir: HEAD, objects, refs and a config of its own."""
    seed = committed(path.parent / f"{path.name}-seed")
    (seed / ".git").rename(path)
    for key, value in config.items():
        git(path, "config", "-f", "config", key, value)
    return path


def git_takes_it_for(folder: Path, asked: str, claimed: Path) -> bool:
    """Whether git, run in `folder`, answers `rev-parse <asked>` with `claimed`."""
    return Path(git(folder, "rev-parse", "--path-format=absolute", asked)).samefile(claimed)


def git_finds_a_repository(folder: Path) -> bool:
    found = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=folder, capture_output=True)
    return found.returncode == 0
