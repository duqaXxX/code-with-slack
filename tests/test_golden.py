import asyncio
import json
import subprocess
from pathlib import Path

from tests.fakes import FIXTURES
from tests.golden import GOLDEN, ROOT, generate


def tree(directory: Path) -> dict[str, str]:
    files = {
        str(p.relative_to(directory)): p.read_text(encoding="utf-8")
        for p in sorted(directory.rglob("*.json"))
    }
    if shallow():
        # The manifest names the last commit that touched src/awaydesk, which a shallow clone
        # (CI's) cannot say: there the commit is left out and everything else is compared.
        manifest = json.loads(files["manifest.json"])
        del manifest["python_commit"]
        files["manifest.json"] = json.dumps(manifest, sort_keys=True)
    return files


def shallow() -> bool:
    out = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"], cwd=ROOT, capture_output=True, text=True
    )
    return out.stdout.strip() != "false"


async def test_the_committed_goldens_are_what_the_code_produces_today(tmp_path: Path) -> None:
    await asyncio.to_thread(generate, tmp_path)
    assert tree(GOLDEN) == tree(tmp_path)


async def test_two_runs_write_the_same_bytes(tmp_path: Path) -> None:
    await asyncio.to_thread(generate, tmp_path / "first")
    await asyncio.to_thread(generate, tmp_path / "second")
    assert tree(tmp_path / "first") == tree(tmp_path / "second")


def test_every_recorded_stream_has_a_golden_at_both_seams() -> None:
    names = sorted(p.stem for p in (FIXTURES / "sdk").glob("*.jsonl"))
    assert names
    for seam in ("reply", "slack"):
        assert sorted(p.stem for p in (GOLDEN / seam).glob("*.json")) == names, seam
    assert json.loads((GOLDEN / "manifest.json").read_text())["recordings"] == names


def test_a_golden_records_the_fields_a_default_would_hide() -> None:
    reply = json.loads((GOLDEN / "reply" / "tools.json").read_text())
    [first, *_] = [c for c in reply["whole"]["calls"] if c["call"] == "task"]
    assert set(first["update"]) == {
        "id",
        "title",
        "status",
        "details",
        "output",
        "name",
        "task",
        "calls",
        "preview",
        "folded",
    }
    assert first["update"]["preview"] is None and first["update"]["details"] is None
