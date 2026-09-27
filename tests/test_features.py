from pathlib import Path

import pytest

from probe.claims import CLAIMS
from probe.features import FEATURES, Feature, by_hand, features

TESTS = Path(__file__).resolve().parent


def test_every_probe_claim_is_mapped_to_a_feature_and_nothing_else_is() -> None:
    mapped = [claim for f in features() for claim in f.claims]
    assert sorted(mapped) == sorted(c.id for c in CLAIMS)


@pytest.mark.parametrize("feature", features(), ids=lambda f: f.name[:40])
def test_every_row_names_test_modules_that_exist(feature: Feature) -> None:
    assert feature.tests, "a feature no test covers still names where its checks live"
    for module in feature.tests:
        assert (TESTS / f"{module}.py").is_file(), module


def test_the_checklist_lists_only_what_is_left_by_hand(tmp_path: Path) -> None:
    table = tmp_path / "features.md"
    table.write_text(
        "| Feature | Test suite | Probe | By hand |\n|---|---|---|---|\n"
        "| A | `test_a` | P1 | none |\n| B | `test_b` | none | Look at it |\n"
    )
    assert [f.claims for f in features(table)] == [("P1",), ()]
    text = by_hand(table)
    assert "B" in text and "Look at it" in text and "  [ ] A" not in text


def test_a_changed_header_is_an_error_not_an_empty_map(tmp_path: Path) -> None:
    table = tmp_path / "features.md"
    table.write_text("| Feature | Tests |\n|---|---|\n| A | x |\n")
    with pytest.raises(ValueError):
        features(table)
    assert FEATURES.name == "features.md"


@pytest.mark.parametrize(
    "row",
    [
        "| A | `test_a` | none |",
        "| A | `x | y` | none | none |",
        "| A | `test_a` | P1 | none | extra |",
    ],
)
def test_a_row_with_the_wrong_number_of_cells_is_an_error(tmp_path: Path, row: str) -> None:
    # A row dropped in silence would leave both the checks and the checklist.
    table = tmp_path / "features.md"
    table.write_text("| Feature | Test suite | Probe | By hand |\n|---|---|---|---|\n" + row + "\n")
    with pytest.raises(ValueError):
        features(table)
