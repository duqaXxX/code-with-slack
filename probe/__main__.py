"""Does code-with-slack still work on this claude-agent-sdk release?

    uv run python -m probe            run if the pinned claude-agent-sdk is not certified yet
    uv run python -m probe --force    run anyway
    uv run python -m probe --latest   run on the newest release on PyPI, in a temporary worktree

It uses the owner's Claude Code login and real tokens (Haiku), which is why it is not part of
pytest. Exit status: 0 certified, 3 a claim is BROKEN, 2 not every gesture claim could be proven;
1 is Python's own, for a crash.
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
from probe.features import by_hand
from probe.scenes import run_scenes
from probe.surface import check as check_surface
from probe.surface import read_reference, set_aside, surface
from probe.surface import report as surface_report

REPO = Path(__file__).resolve().parents[1]
CERTIFIED = Path(__file__).resolve().parent / "certified-versions.json"
PYPI = "https://pypi.org/pypi/claude-agent-sdk/json"


def log(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


CERTIFICATE_KEYS = {"cli", "date", "holds", "open", "retired"}


def certified(path: Path) -> dict[str, dict[str, object]]:
    """The certificates, by SDK version. Raises ValueError on a file of another shape, which a
    hand edit or an interrupted write could leave: a certificate is never trusted on its looks."""
    if not path.exists():
        return {}
    known = json.loads(path.read_text())
    if not isinstance(known, dict) or not all(
        isinstance(entry, dict) and set(entry) == CERTIFICATE_KEYS for entry in known.values()
    ):
        raise ValueError(f"{path.name} is not a map of SDK versions to {sorted(CERTIFICATE_KEYS)}")
    return known


def pinned_to(pyproject: str, release: str) -> str:
    """`pyproject.toml` with the SDK pinned to `release`. Raises ValueError unless exactly one pin
    was replaced: a pin written another way would leave the old SDK installed, and the probe
    would certify a release it never ran."""
    text, replaced = re.subn(
        r"claude-agent-sdk==[0-9.]+", f"claude-agent-sdk=={release}", pyproject
    )
    if replaced != 1:
        raise ValueError(f"expected one claude-agent-sdk==<version> pin, found {replaced}")
    return text


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
            pyproject.write_text(pinned_to(pyproject.read_text(), latest))
            subprocess.run(["uv", "lock", "--quiet"], cwd=tree, check=True)
            subprocess.run(["uv", "sync", "--quiet"], cwd=tree, check=True)
            command = ["uv", "run", "python", "-m", "probe", "--certificate", str(CERTIFIED)]
            return subprocess.run(command + (["--force"] if force else []), cwd=tree).returncode
        finally:
            # Not checked: a failed removal must not hide why the run above failed.
            removed = subprocess.run(["git", "worktree", "remove", "--force", str(tree)], cwd=REPO)
            if removed.returncode:
                log(f"could not remove the worktree {tree}: run `git worktree prune`")


def run(path: Path, force: bool) -> int:
    sdk = version("claude-agent-sdk")
    known = certified(path)
    # Keyed by the SDK release: a new one can bundle a certified CLI and still change the Python
    # side (its message parser, ClaudeSDKClient).
    if sdk in known and not force:
        log(f"claude-agent-sdk {sdk} (Claude Code {__cli_version__}) is already certified")
        return 0
    # Read before the scenes: a broken map stops the run before it spends tokens, and cannot
    # come between a finished run and its verdict.
    left = by_hand()
    # The SDK surface map, on the release this run is on: a symbol the package no longer defines
    # needs no scene to be known, so it stops the run here.
    mapped = check_surface(surface(), read_reference(), set_aside())
    print(surface_report(mapped) + "\n")
    if mapped.broken:
        print("Not certified: the package no longer defines a symbol the daemon uses.")
        return 3
    seen = asyncio.run(run_scenes(log))
    results = [evaluate(claim, seen.get(claim.id)) for claim in CLAIMS]
    print(report(results, __cli_version__, sdk))
    if hand := checklist(results):
        print("\n" + hand)
    # What no claim covers at all, from the coverage map: printed on every run, certified or not.
    if left:
        print("\n" + left)
    if broken(results):
        print("\nNot certified: a claim is BROKEN. Do not pin this release.")
        return 3
    if not can_certify(results):
        print("\nNot certified: a gesture claim could not be proven.")
        return 2
    known[sdk] = certificate(results, __cli_version__, date.today().isoformat())
    path.write_text(json.dumps(dict(sorted(known.items())), indent=2) + "\n")
    print(f"\nCertified: claude-agent-sdk {sdk} added to {path.name}.")
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
