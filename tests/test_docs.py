"""Deterministic checks over every markdown file in the repository.

Reads whole files, so it can skip headings, fenced blocks and backtick spans. The changelog is
exempt from the prose checks: it quotes history by design.
"""

import ast
import json
import re
import tomllib
from functools import cache
from pathlib import Path
from typing import get_args

import pytest

from code_with_slack import config
from code_with_slack.commands import Invalid, Word
from probe.features import features

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "code_with_slack"
README = ROOT / "README.md"
SETUP = ROOT / "docs" / "setup.md"
# A feature's row is a name to scan: what it does in full is in docs/setup.md or under Details.
# Raise this when a name cannot be said in fewer characters.
FEATURE_NAME_LIMIT = 300
# The README is the landing page: a feature's detail goes in docs/setup.md. The limit sits a
# quarter above the page's length when it was set (677 words, as `str.split` counts them). It is
# a prompt to ask where new text belongs: raise it when the page needs the words.
README_WORD_LIMIT = 846
DOCS = sorted(
    p
    for p in ROOT.rglob("*.md")
    if not any(part.startswith(".") for part in p.relative_to(ROOT).parts)
)
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
LINE_POINTER = re.compile(r"\b[\w./-]+\.(?:py|md|sh|ya?ml|json|toml):\d+")
REPO_PATH = re.compile(r"`((?:src|tests|docs|\.github)/[\w./-]+)`")
BACKTICKS = re.compile(r"`[^`]*`")
# A code span that is nothing but a dotted name: `sinks.ReplySink`, `ThreadSession.mode`.
SYMBOL = re.compile(r"`([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)`")
# `state.json` and `config.py` are files, not attributes of the modules of the same name.
FILE_SUFFIXES = {"py", "md", "json", "jsonl", "toml", "sh", "yml", "yaml", "txt", "plist", "log"}
# Dotted names that start like a module of the package and belong to Slack: `state.values` is a
# key of an interaction payload.
NOT_OURS = {"state.values"}
# A label of the decision log, which no file in this repository defines.
DECISION_LABEL = re.compile(r"\bD\d{1,2}\b")
PROSE_DOCS = [d for d in DOCS if d.name != "CHANGELOG.md"]


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


def readme_section(title: str) -> str:
    """The README's text under one `##` heading, up to the next one."""
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", README.read_text(), re.M | re.S)
    assert match, f"README.md has no '## {title}' section"
    return match.group(1)


def test_the_readme_lists_every_word_of_the_daemon_and_no_other() -> None:
    # A word added, renamed or removed without its row in the README's table fails here.
    words = sorted(cls.WORD for cls in get_args(Word) if cls is not Invalid)
    rows = sorted(set(re.findall(r"^\| `!(\w+)", readme_section("Commands"), re.M)))
    assert rows == words


def test_the_readme_states_the_version_and_the_python_the_package_declares() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    text = README.read_text()
    assert f"version {project['version']}" in text
    assert f"Python {project['requires-python'].removeprefix('>=')}" in text


@cache
def defined_names(module: Path) -> frozenset[str]:
    """Every name a module defines, binds or imports, at any depth."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(module.read_text())):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add((node.asname or node.name).split(".")[0])
    return frozenset(names)


@cache
def owners() -> dict[str, frozenset[str]]:
    """What a dotted name can start with, and the names found there: a module of the package,
    or a class, under every module that defines one of that name (two define a `Pending`)."""
    modules = {p.stem: p for p in SRC.rglob("*.py") if p.stem != "__init__"}
    found = {stem: defined_names(path) for stem, path in modules.items()}
    for path in modules.values():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ClassDef):
                found[node.name] = found.get(node.name, frozenset()) | defined_names(path)
    return found


def unresolved(span: str) -> bool:
    """True for a dotted name that starts in this package and names something it does not hold."""
    parts = span.removeprefix("code_with_slack.").removeprefix("render.").split(".")
    names = owners().get(parts[0])
    if names is None or len(parts) < 2 or parts[-1] in FILE_SUFFIXES or span in NOT_OURS:
        return False
    return not set(parts[1:]) <= names


@pytest.mark.parametrize("doc", PROSE_DOCS, ids=doc_id)
def test_every_symbol_a_doc_names_exists_in_the_source(doc: Path) -> None:
    # A function, class or constant renamed in the source and not in the doc fails here.
    missing = sorted({span for span in SYMBOL.findall(doc.read_text()) if unresolved(span)})
    assert not missing, f"{doc.name} names what the source does not define: {missing}"


@pytest.mark.parametrize("doc", PROSE_DOCS, ids=doc_id)
def test_no_decision_label_in_prose(doc: Path) -> None:
    hits = [n for n, line in prose_lines(doc.read_text()) if DECISION_LABEL.search(line)]
    assert not hits, f"{doc.name}: decision label on lines {hits}; name the behaviour"


def test_the_setup_guide_names_every_variable_scope_and_word() -> None:
    text = SETUP.read_text()
    read = re.findall(r'values(?:\.get\(|\[)"([A-Z_]+)"', (SRC / "config.py").read_text())
    scopes = json.loads((ROOT / "slack-app-manifest.json").read_text())["oauth_config"]["scopes"]
    for name in {*config.REQUIRED, *read, *scopes["bot"]}:
        assert f"`{name}`" in text, f"docs/setup.md does not name {name}"
    rows = set(re.findall(r"^\| `!(\w+)", text.partition("### Commands")[2], re.M))
    words = {cls.WORD for cls in get_args(Word) if cls is not Invalid}
    assert words <= rows, f"docs/setup.md has no row for {sorted(words - rows)}"


def test_the_features_table_can_be_scanned() -> None:
    long = {f.name[:40]: len(f.name) for f in features() if len(f.name) > FEATURE_NAME_LIMIT}
    assert not long, f"docs/features.md: feature names over {FEATURE_NAME_LIMIT} characters: {long}"


def test_the_readme_stays_a_landing_page() -> None:
    words = len(README.read_text().split())
    assert words <= README_WORD_LIMIT, (
        f"README.md has {words} words, over {README_WORD_LIMIT}: detail belongs in docs/setup.md"
    )
