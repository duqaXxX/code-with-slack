"""Does code-with-slack still work on this claude-agent-sdk release?

    uv run python -m probe            run if the bundled Claude Code is not certified yet
    uv run python -m probe --force    run anyway
    uv run python -m probe --latest   run on the newest release on PyPI, in a temporary worktree

It uses the owner's Claude Code login and real tokens (Haiku), which is why it is not part of
pytest. Exit status: 0 certified, 1 a claim is BROKEN, 2 not every gesture claim could be proven.
"""

import argparse
import asyncio
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from datetime import date
from importlib.metadata import version
from pathlib import Path

from claude_agent_sdk._cli_version import __cli_version__

from probe.claims import CLAIMS, broken, can_certify, certificate, checklist, evaluate, report
from probe.scenes import run_scenes

REPO = Path(__file__).resolve().parents[1]
CERTIFIED = Path(__file__).resolve().parent / "certified-versions.json"
PYPI = "https://pypi.org/pypi/claude-agent-sdk/json"


def log(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def certified(path: Path) -> dict[str, dict[str, object]]:
    return json.loads(path.read_text()) if path.exists() else {}


def latest_release() -> str:
    with urllib.request.urlopen(PYPI, timeout=30) as response:
        return str(json.load(response)["info"]["version"])


def on_latest(force: bool) -> int:
    """Run the probe on the newest SDK in a detached worktree of HEAD, the way the release watch
    moves the pin; the certificate is written to this checkout, the pin here is left alone."""
    latest, pinned = latest_release(), version("claude-agent-sdk")
    if latest == pinned:
        log(f"claude-agent-sdk {pinned} is the newest release")
        return run(CERTIFIED, force)
    log(f"claude-agent-sdk {latest} is out (pinned {pinned}); probing it in a temporary worktree")
    with tempfile.TemporaryDirectory(prefix="cws-probe-worktree-") as parent:
        tree = Path(parent) / "tree"
        subprocess.run(["git", "worktree", "add", "--detach", str(tree)], cwd=REPO, check=True)
        try:
            pyproject = tree / "pyproject.toml"
            pyproject.write_text(
                re.sub(
                    r"claude-agent-sdk==[0-9.]+",
                    f"claude-agent-sdk=={latest}",
                    pyproject.read_text(),
                )
            )
            subprocess.run(["uv", "lock", "--quiet"], cwd=tree, check=True)
            subprocess.run(["uv", "sync", "--quiet"], cwd=tree, check=True)
            command = ["uv", "run", "python", "-m", "probe", "--certificate", str(CERTIFIED)]
            return subprocess.run(command + (["--force"] if force else []), cwd=tree).returncode
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(tree)], cwd=REPO, check=True
            )


def run(path: Path, force: bool) -> int:
    sdk = version("claude-agent-sdk")
    known = certified(path)
    if __cli_version__ in known and not force:
        log(f"Claude Code {__cli_version__} (claude-agent-sdk {sdk}) is already certified")
        return 0
    seen = asyncio.run(run_scenes(log))
    results = [evaluate(claim, seen.get(claim.id)) for claim in CLAIMS]
    print(report(results, __cli_version__, sdk))
    if hand := checklist(results):
        print("\n" + hand)
    if broken(results):
        print("\nNot certified: a claim is BROKEN. Do not pin this release.")
        return 1
    if not can_certify(results):
        print("\nNot certified: a gesture claim could not be proven.")
        return 2
    known[__cli_version__] = certificate(results, sdk, date.today().isoformat())
    path.write_text(json.dumps(dict(sorted(known.items())), indent=2) + "\n")
    print(f"\nCertified: Claude Code {__cli_version__} added to {path.name}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m probe", description=__doc__.split("\n")[0])
    parser.add_argument("--force", action="store_true", help="run even if already certified")
    parser.add_argument("--latest", action="store_true", help="probe the newest release on PyPI")
    parser.add_argument("--certificate", type=Path, default=CERTIFIED, help=argparse.SUPPRESS)
    args = parser.parse_args()
    return on_latest(args.force) if args.latest else run(args.certificate, args.force)


if __name__ == "__main__":
    sys.exit(main())
