"""`docs/sdk-surface.md` against the source and the installed `claude-agent-sdk`: the map of what
the daemon depends on is only worth reading while both agree with it."""

import ast
import importlib
import re
from pathlib import Path

import pytest

from probe.claims import CLAIMS
from probe.surface import (
    SURFACE,
    Check,
    Row,
    check,
    in_package,
    in_reference,
    report,
    set_aside,
    surface,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "code_with_slack"
ROWS = surface()


def imported() -> set[tuple[str, str]]:
    """Every (module, name) the source imports from the SDK."""
    found = set()
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "claude_agent_sdk"
            ):
                found |= {(node.module or "", alias.name) for alias in node.names}
            elif isinstance(node, ast.Import):
                assert not any(a.name.startswith("claude_agent_sdk") for a in node.names), path
    return found


def test_every_sdk_name_the_source_imports_has_a_row_and_no_row_is_left_over() -> None:
    listed = {(r.owner, r.member) for r in ROWS if r.kind in ("type", "function")}
    assert listed == imported()


@pytest.mark.parametrize("row", ROWS, ids=lambda r: f"{r.kind}:{r.name}"[:70])
def test_a_row_s_source_agrees_with_the_installed_package(row: Row) -> None:
    defined = in_package(row)
    if row.source == "package":
        assert defined is True, "a `package` row names something the package defines"
    elif row.source == "reference":
        assert defined is not False, "the reference names it and the package no longer has it"
    else:
        assert defined is not True, "the package defines it: the row is `package`, or `reference`"


def body(file: str, symbol: str) -> str:
    """The source of `symbol` in `file`: a function, a class, a method (`Class.method`) or a
    module-level name, found at any depth since handlers are defined inside `build_app`."""
    text = (SRC / file).read_text()  # a missing file is an error here
    scope: ast.AST = ast.parse(text)
    for part in symbol.split("."):
        found = None
        for node in ast.walk(scope):
            if node is scope:
                continue
            named = isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            if named and node.name == part:
                found = node
            elif isinstance(node, ast.Assign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t, ast.Name) and t.id == part for t in targets):
                    found = node
            if found is not None:
                break
        assert found is not None, f"{file} has no {symbol}"
        scope = found
    return ast.get_source_segment(text, scope) or ""


@pytest.mark.parametrize("row", ROWS, ids=lambda r: f"{r.kind}:{r.name}"[:70])
def test_a_row_is_used_inside_a_symbol_it_names(row: Row) -> None:
    # Not "somewhere in the source": `type`, `name` and `text` are everywhere. The member has to
    # appear in the body of a symbol the row itself points at, and every such symbol has to exist.
    member = re.escape(row.member.strip('"'))
    assert row.used_in, "a row says where it is used"
    sources = []
    for place in row.used_in:
        file, _, symbols = place.partition(":")
        sources += [body(file.strip(), symbol.strip()) for symbol in symbols.split(",")]
    assert any(re.search(rf"(?<![\w-]){member}(?![\w-])", source) for source in sources)


def test_every_claim_a_row_names_exists() -> None:
    known = {claim.id for claim in CLAIMS}
    assert {claim for row in ROWS for claim in row.claims} <= known


def test_the_types_set_aside_are_exported_and_not_imported() -> None:
    types = importlib.import_module("claude_agent_sdk.types")
    names = set_aside()
    assert names and all(isinstance(getattr(types, name, None), type) for name in names)
    assert not {name for _, name in imported()} & set(names)
    assert check(ROWS, None, names).unlisted == ()  # on the pinned release, nothing is new


def test_the_pinned_release_defines_everything_the_map_lists() -> None:
    assert check(ROWS, None, set_aside()).missing == ()


PAGE = """
## Choosing between `query()` and `ClaudeSDKClient`

Mentions updated_input and summary, which proves nothing.

### `ClaudeSDKClient`

#### Methods

| `interrupt()` | Stops the turn |

### `TaskNotificationMessage`

    status: TaskNotificationStatus  # "completed" | "failed" | "stopped"
    summary: str

#### Return type: `SDKSessionInfo`

| `session_id` | `str` |

## Other
"""


def row(owner: str, member: str, kind: str, source: str = "reference") -> Row:
    return Row(owner, member, kind, ("sessions.py: x",), source, ())


def test_the_reference_names_a_member_only_in_the_section_of_its_type() -> None:
    assert in_reference(row("ClaudeSDKClient", "interrupt", "method"), PAGE)
    assert in_reference(row("TaskNotificationMessage", "summary", "field"), PAGE)
    assert in_reference(row("TaskNotificationMessage.status", '"failed"', "value"), PAGE)
    assert in_reference(row("SDKSessionInfo", "session_id", "field"), PAGE)
    assert in_reference(row("claude_agent_sdk.types", "TaskNotificationMessage", "type"), PAGE)
    # a word elsewhere on the page, a value the section does not list, a key below a field
    assert not in_reference(row("ClaudeSDKClient", "summary", "method"), PAGE)
    assert not in_reference(row("TaskNotificationMessage.status", '"killed"', "value"), PAGE)
    assert not in_reference(row("TaskNotificationMessage.usage", "status", "key"), PAGE)
    assert not in_reference(row("claude_agent_sdk.types", "TaskUpdatedMessage", "type"), PAGE)


def test_a_symbol_gone_from_the_package_breaks_and_one_gone_from_the_reference_does_not() -> None:
    gone = row("ClaudeSDKClient", "no_such_method", "method")
    unnamed = row("TaskNotificationMessage", "task_id", "field")  # in the package, not in PAGE
    result = check([gone, unnamed], PAGE, [])
    assert result.missing == (gone,) and result.broken
    assert unnamed in result.undocumented
    assert not check([unnamed], PAGE, []).broken
    text = report(result)
    assert "BROKEN" in text and "ClaudeSDKClient.no_such_method" in text
    assert "UNPROVEN" in text and "TaskNotificationMessage.task_id" in text


def test_a_member_whose_type_left_the_package_is_missing_too() -> None:
    # `ModelUsage` has no `type` row of its own (the source never imports it): were it renamed,
    # only its fields' rows could say so.
    orphan = row("NoSuchUsage", "inputTokens", "field")
    assert in_package(orphan) is False
    assert check([orphan], None, []).missing == (orphan,)
    below = row("NoSuchMessage.data", "key", "key", source="measured")
    assert check([below], None, []).missing == ()  # a measured row never claimed the package had it


def test_a_reference_that_cannot_be_read_is_said_and_proves_nothing() -> None:
    result = check([row("TaskNotificationMessage", "summary", "field")], None, [])
    assert isinstance(result, Check) and not result.reference_read
    assert result.undocumented == () and not result.broken
    assert "could not be read" in report(result)


def test_rows_in_no_reference_and_under_no_claim_are_listed_by_hand() -> None:
    measured = row("StreamEvent.event", "type", "key", source="measured")
    claimed = Row(
        "SystemMessage.data", "claude_code_version", "key", ("sessions.py: x",), "measured", ("P1",)
    )
    result = check([measured, claimed], PAGE, [])
    assert result.by_hand == (measured,)
    # A run counts them, since they are the same on every release; the list is on request.
    assert "1 rows are in no reference" in report(result)
    assert "StreamEvent.event.type" not in report(result)
    assert "StreamEvent.event.type (measured)" in report(result, full=True)


def test_a_changed_header_or_a_bad_cell_is_an_error_not_a_shorter_map(tmp_path: Path) -> None:
    table = tmp_path / "sdk-surface.md"
    table.write_text("| Owner | Member |\n|---|---|\n| `A` | `b` |\n")
    with pytest.raises(ValueError):
        surface(table)
    header = (
        "| Owner | Member | Kind | Used in | Source | Checked by |\n|---|---|---|---|---|---|\n"
    )
    table.write_text(header + "| `A` | `b` | field | sessions.py: x | guessed | none |\n")
    with pytest.raises(ValueError):
        surface(table)
    table.write_text(header + "| `A` | `b` | field | sessions.py: x | measured | P1 |\n" * 2)
    with pytest.raises(ValueError):
        surface(table)
    table.write_text(
        "| Column | Meaning |\n|---|---|\n| x | y |\n\n"
        + header
        + "| `A` | `b` | field | a.py: x; b.py: y | measured | P1 |\n"
    )
    assert surface(table) == [Row("A", "b", "field", ("a.py: x", "b.py: y"), "measured", ("P1",))]
    assert SURFACE.name == "sdk-surface.md"
