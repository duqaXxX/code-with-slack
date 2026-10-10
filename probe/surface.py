"""The SDK surface map in `docs/sdk-surface.md`: every type, field, value, method and option of
`claude-agent-sdk` the daemon depends on, and where each is known from. `tests/test_sdk_surface.py`
keeps it in step with the source and the installed package; the probe checks it against the
release it runs on and against the published reference, before it spends a token."""

import dataclasses
import importlib
import re
import typing
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

SURFACE = Path(__file__).resolve().parents[1] / "docs" / "sdk-surface.md"
REFERENCE = "https://code.claude.com/docs/en/agent-sdk/python.md"
COLUMNS = ("Owner", "Member", "Kind", "Used in", "Source", "Checked by")
# A `type` or `function` row's owner is the module it is imported from; every other row's owner
# is a type of the SDK, or a dotted path below one for what a `dict[str, Any]` of the SDK holds.
KINDS = ("type", "function", "field", "method", "option", "key", "value")
# `reference`: the published reference names it. `package`: the installed package defines it and
# the reference does not. `measured`: neither does; it was read off a real stream.
SOURCES = ("reference", "package", "measured")
PUBLIC = ("claude_agent_sdk", "claude_agent_sdk.types")


@dataclass(frozen=True)
class Row:
    owner: str
    member: str
    kind: str
    used_in: tuple[str, ...]  # `sessions.py: ThreadSession._dispatch`
    source: str
    claims: tuple[str, ...]  # probe claim ids, as `P1`

    @property
    def name(self) -> str:
        return (
            f"{self.owner}.{self.member}" if self.kind not in ("type", "function") else self.member
        )

    @property
    def root(self) -> str:
        """The SDK type a row hangs from: `StreamEvent` for `StreamEvent.event.delta`."""
        return re.split(r"[.\[]", self.owner)[0]


def surface(path: Path = SURFACE) -> list[Row]:
    """The table's rows. Raises ValueError when the table is missing, its header changed, or a
    row is malformed: a broken row is an error, never a row left out of the checks."""
    cells: list[list[str]] = []
    for line in path.read_text().splitlines():
        row = [c.strip() for c in line.strip("|").split("|")] if line.startswith("|") else None
        if cells and row is None:
            break  # the table ended: the file may hold others after it
        if row is not None and (cells or tuple(row) == COLUMNS):
            cells.append(row)
    if not cells:
        raise ValueError(f"{path.name}: no table with the columns {COLUMNS}")
    rows = []
    for row in cells[2:]:
        if len(row) != len(COLUMNS):
            raise ValueError(f"{path.name}: {len(row)} cells, not {len(COLUMNS)}, in {row[:2]}")
        owner, member, kind, used_in, source, checked = row
        if kind not in KINDS or source not in SOURCES:
            raise ValueError(f"{path.name}: kind {kind!r} or source {source!r} in {row[:2]}")
        rows.append(
            Row(
                owner.strip("`"),
                member.strip("`"),
                kind,
                tuple(place.strip() for place in used_in.split(";") if place.strip()),
                source,
                tuple(re.findall(r"\bP\d+\b", checked)),
            )
        )
    if len({(r.owner, r.member, r.kind) for r in rows}) != len(rows):
        raise ValueError(f"{path.name}: a row is listed twice")
    return rows


def _sdk_type(name: str) -> Any:
    for module in PUBLIC:
        found = getattr(importlib.import_module(module), name, None)
        if found is not None:
            return found
    return None


def _literals(annotation: Any) -> set[object]:
    """Every value a `Literal` anywhere inside `annotation` allows; empty when it has none."""
    if typing.get_origin(annotation) is Literal:
        return set(typing.get_args(annotation))
    return {value for inner in typing.get_args(annotation) for value in _literals(inner)}


def _members(owner: Any) -> dict[str, Any]:
    """The fields of a dataclass or a TypedDict of the SDK, or the parameters of one of its
    functions, with their annotations: unresolved ones when a hint does not resolve, since a
    name that cannot be resolved is still a member."""
    try:
        return typing.get_type_hints(owner)
    except Exception:
        if dataclasses.is_dataclass(owner):
            return {f.name: f.type for f in dataclasses.fields(owner)}
        found: dict[str, Any] = {}
        for base in reversed(getattr(owner, "__mro__", (owner,))):
            found.update(getattr(base, "__annotations__", None) or {})
        return found


def in_package(row: Row) -> bool | None:
    """Whether the installed `claude-agent-sdk` still defines what `row` names. False also when
    the type it hangs from is not in the package: gone in this release, or never a type of the
    SDK (a tool's input). None when the type is there and cannot say: a key inside a
    `dict[str, Any]`, a value of a plain `str` field."""
    if row.kind in ("type", "function"):
        try:
            return hasattr(importlib.import_module(row.owner), row.member)
        except Exception:  # a module of a new release that fails to import is not there either
            return False
    owner = _sdk_type(row.root)
    if owner is None:
        return False
    if row.kind == "method":
        return row.owner == row.root and callable(getattr(owner, row.member, None))
    if row.kind in ("field", "option", "key"):
        if row.owner != row.root:
            return None  # below a field: inside a dict the package does not describe
        members = _members(owner)
        if row.member in members:
            return True
        # A union of TypedDicts (`HookInput`) has no members of its own to deny it with.
        return False if members else None
    # A value: of a `Literal` alias (`PermissionMode`), or of a field (`SystemMessage.subtype`).
    path = row.owner.split(".")
    if len(path) > 2:
        return None
    allowed = _literals(owner if len(path) == 1 else _members(owner).get(path[1]))
    return row.member.strip('"') in allowed if allowed else None


def _section(page: str, name: str) -> str | None:
    """The part of the reference under the heading that names `name`, to the next heading of
    the same or a higher level."""
    # The heading that is the name alone, or that ends in it after a colon (`Return type:`):
    # another one may only mention it.
    found = re.search(
        rf"^(#{{2,4}}) (?:[^`\n]*: )?`{re.escape(name)}(\(\))?`[ \t]*$", page, re.MULTILINE
    )
    if found is None:
        return None
    rest = page[found.end() :]
    end = re.search(rf"^#{{2,{len(found.group(1))}}} ", rest, re.MULTILINE)
    return rest[: end.start()] if end else rest


def in_reference(row: Row, page: str) -> bool:
    """Whether the published reference names what `row` names: a heading for a type or a
    function, and for anything else the member inside the section of the type it hangs from."""
    if row.kind in ("type", "function"):
        return _section(page, row.member) is not None
    # Below a field the reference describes no keys, and a word match there would prove nothing:
    # only a quoted value of a field is looked for one level down.
    depth = len(re.split(r"[.\[]", row.owner))
    if depth > (2 if row.kind == "value" else 1):
        return False
    section = _section(page, row.root)
    if section is None:
        return False
    return re.search(rf"(?<![\w]){re.escape(row.member)}(?![\w])", section) is not None


def read_reference(url: str = REFERENCE) -> str | None:
    """The reference as markdown, or None when it cannot be read: the probe then says so and
    leaves every `reference` row unproven."""
    # The site answers 403 to urllib's default agent (measured 2026-10-07): the probe names
    # itself.
    request = urllib.request.Request(url, headers={"User-Agent": "awaydesk-probe"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return str(response.read().decode())
    except Exception:
        return None


SET_ASIDE = "## Types the daemon does not read"


def set_aside(path: Path = SURFACE) -> list[str]:
    """The types named under SET_ASIDE: exported by the package, looked at, and left unread on
    purpose, so that only a type nobody has looked at is reported as new."""
    text = path.read_text()
    if SET_ASIDE not in text:
        raise ValueError(f"{path.name}: no section {SET_ASIDE!r}")
    section = text.split(SET_ASIDE, 1)[1].split("\n## ", 1)[0]
    return re.findall(r"^- `(\w+)`", section, re.MULTILINE)


def unlisted_types(rows: list[Row], known: list[str]) -> list[str]:
    """Message, block and event types the installed package exports that the map neither lists
    nor sets aside: what a new release added that the daemon has never looked at."""
    listed = {r.member for r in rows if r.kind == "type"} | set(known)
    types = importlib.import_module("claude_agent_sdk.types")
    names = [
        name
        for name in dir(types)
        if re.search(r"(Message|Block|Event)$", name) and isinstance(getattr(types, name), type)
    ]
    return sorted(set(names) - listed)


@dataclass(frozen=True)
class Check:
    missing: tuple[Row, ...]  # the map says the package defines it, and the package does not
    undocumented: tuple[Row, ...]  # a `reference` row the reference no longer names
    reference_read: bool
    by_hand: tuple[Row, ...]  # known from no reference and covered by no claim
    unlisted: tuple[str, ...]

    @property
    def broken(self) -> bool:
        return bool(self.missing)


def check(rows: list[Row], page: str | None, known: list[str]) -> Check:
    return Check(
        missing=tuple(r for r in rows if r.source != "measured" and in_package(r) is False),
        undocumented=tuple(
            r
            for r in rows
            if page is not None and r.source == "reference" and not in_reference(r, page)
        ),
        reference_read=page is not None,
        by_hand=tuple(r for r in rows if r.source != "reference" and not r.claims),
        unlisted=tuple(unlisted_types(rows, known)),
    )


def report(result: Check, *, full: bool = False) -> str:
    """What the probe prints about the map, before its scenes. The rows no reference names and
    no claim covers are the same from one release to the next, so a run counts them and only
    `full` lists them: they are where to look when a release misbehaves, not a list to tick."""
    lines = [f"SDK surface ({SURFACE.name}):"]
    if result.missing:
        lines.append("  BROKEN, no longer in the package:")
        lines += [
            f"    {r.name} ({r.kind}), used in {'; '.join(r.used_in)}" for r in result.missing
        ]
    else:
        lines.append("  every listed type, field, method and option is in the package")
    if not result.reference_read:
        lines.append(f"  UNPROVEN, the reference could not be read: {REFERENCE}")
    elif result.undocumented:
        lines.append("  UNPROVEN, listed as `reference` and not found in it, read the page:")
        lines += [f"    {r.name} ({r.kind})" for r in result.undocumented]
    else:
        lines.append("  every `reference` row is still named by the reference")
    if result.unlisted:
        lines.append("  in the package and not in the map: " + ", ".join(result.unlisted))
    if result.by_hand and full:
        lines.append("  in no reference and under no claim:")
        lines += [f"    {r.name} ({r.source})" for r in result.by_hand]
    elif result.by_hand:
        lines.append(
            f"  {len(result.by_hand)} rows are in no reference and under no claim"
            " (`python -m probe --surface` lists them)"
        )
    return "\n".join(lines)
