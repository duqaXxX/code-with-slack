"""The coverage map in `docs/features.md`: which probe claims each feature has, and what is left
to check by hand. The probe prints the second at the end of a run; `tests/test_features.py`
keeps the first in step with `probe.claims.CLAIMS`."""

import re
from dataclasses import dataclass
from pathlib import Path

FEATURES = Path(__file__).resolve().parents[1] / "docs" / "features.md"
COLUMNS = ("Feature", "Test suite", "Probe", "By hand")


@dataclass(frozen=True)
class Feature:
    name: str
    tests: tuple[str, ...]  # test modules, as `test_sessions`
    claims: tuple[str, ...]  # probe claim ids, as `P1`
    by_hand: str  # empty when nothing is left to check by hand


def features(path: Path = FEATURES) -> list[Feature]:
    """The table's rows. Raises ValueError when the table is missing, its header changed, or a
    row does not have one cell per column: a broken row is an error, never a row left out of the
    checks and the checklist."""
    rows = [line for line in path.read_text().splitlines() if line.startswith("|")]
    cells = [[c.strip() for c in row.strip("|").split("|")] for row in rows]
    if not cells or tuple(cells[0]) != COLUMNS:
        raise ValueError(f"{path.name}: no table with the columns {COLUMNS}")
    for row in cells[2:]:
        if len(row) != len(COLUMNS):
            # A `|` inside a cell splits it too: write it another way.
            raise ValueError(f"{path.name}: {len(row)} cells, not {len(COLUMNS)}, in {row[0]!r}")
    out = []
    for name, tests, claims, by_hand in cells[2:]:
        out.append(
            Feature(
                name,
                tuple(re.findall(r"`(test_\w+)`", tests)),
                tuple(re.findall(r"\bP\d+\b", claims)),
                "" if by_hand == "none" else by_hand,
            )
        )
    return out


def by_hand(path: Path = FEATURES) -> str:
    """The checks no test and no probe claim makes, as the probe prints them."""
    left = [f for f in features(path) if f.by_hand]
    if not left:
        return ""
    lines = [
        "Checked by nothing automatic (docs/features.md), when the feature or Slack changes:",
        "",
    ]
    lines += [f"  [ ] {f.name}\n      how: {f.by_hand}" for f in left]
    return "\n".join(lines)
