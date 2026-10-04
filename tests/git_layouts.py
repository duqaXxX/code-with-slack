"""Git folders built for the trust and footer tests: the layouts an owner makes, and the ones
an archive can carry (a `git clone` delivers no `.git` entry). Measured on git 2.54.0,
2026-10-04."""

import json
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
