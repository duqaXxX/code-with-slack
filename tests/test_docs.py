"""Deterministic checks over every markdown file in the repository.

Reads whole files, so it can skip headings, fenced blocks and backtick spans. The changelog is
exempt from the prose checks: it quotes history by design.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = sorted(
    p
    for p in ROOT.rglob("*.md")
    if not any(part.startswith(".") for part in p.relative_to(ROOT).parts)
)
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
LINE_POINTER = re.compile(r"\b[\w./-]+\.(?:py|md|sh|ya?ml|json|toml):\d+")
REPO_PATH = re.compile(r"`((?:src|tests|docs|\.github)/[\w./-]+)`")
BACKTICKS = re.compile(r"`[^`]*`")


def prose_lines(text: str) -> list[tuple[int, str]]:
    """Lines outside fenced blocks and headings, with backtick spans removed."""
    out: list[tuple[int, str]] = []
    fenced = False
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced or line.startswith("#"):
            continue
        out.append((number, BACKTICKS.sub("", line)))
    return out


def slug(heading: str) -> str:
    text = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return re.sub(r"\s+", "-", text)


def anchors(path: Path) -> set[str]:
    return {slug(m.group(1)) for m in re.finditer(r"^#+\s+(.*)$", path.read_text(), re.M)}


def doc_id(path: Path) -> str:
    return str(path.relative_to(ROOT))


@pytest.mark.parametrize("doc", DOCS, ids=doc_id)
def test_relative_links_resolve(doc: Path) -> None:
    for target in LINK.findall(doc.read_text()):
        if re.match(r"^[a-z]+:", target):
            continue
        file_part, _, anchor = target.partition("#")
        dest = (doc.parent / file_part).resolve() if file_part else doc
        assert dest.exists(), f"{doc.name}: broken link {target}"
        if anchor and dest.suffix == ".md":
            assert anchor in anchors(dest), f"{doc.name}: missing anchor {target}"


@pytest.mark.parametrize("doc", DOCS, ids=doc_id)
def test_repo_paths_exist(doc: Path) -> None:
    for path in REPO_PATH.findall(doc.read_text()):
        assert (ROOT / path.rstrip("/")).exists(), f"{doc.name}: {path} does not exist"


@pytest.mark.parametrize("doc", DOCS, ids=doc_id)
def test_no_line_number_pointers(doc: Path) -> None:
    hits = [n for n, line in prose_lines(doc.read_text()) if LINE_POINTER.search(line)]
    assert not hits, f"{doc.name}: line-number pointer on lines {hits}; name the symbol"


@pytest.mark.parametrize("doc", [d for d in DOCS if d.name != "CHANGELOG.md"], ids=doc_id)
def test_no_em_dash_in_prose(doc: Path) -> None:
    hits = [n for n, line in prose_lines(doc.read_text()) if "—" in line]
    assert not hits, f"{doc.name}: em dash in prose on lines {hits}"


def test_no_orphan_doc() -> None:
    linked: set[Path] = set()
    for doc in DOCS:
        for target in LINK.findall(doc.read_text()):
            file_part = target.partition("#")[0]
            if file_part and not re.match(r"^[a-z]+:", file_part):
                linked.add((doc.parent / file_part).resolve())
    orphans = [d.name for d in (ROOT / "docs").glob("*.md") if d.resolve() not in linked]
    assert not orphans, f"docs nobody links to: {orphans}"
