import importlib
import json
from pathlib import Path

import pytest

from probe.__main__ import certified, pinned_to
from probe.claims import (
    CLAIMS,
    Claim,
    Observation,
    Result,
    broken,
    can_certify,
    certificate,
    checklist,
    evaluate,
)

GESTURE = Claim("P90", "gesture", "a gesture", "code_with_slack.sessions.SessionManager", "do it")
MODEL = Claim("P91", "model", "a model act", "code_with_slack.approvals.Approvals", "ask for it")


@pytest.mark.parametrize(
    ("claim", "seen", "outcome"),
    [
        (GESTURE, Observation(caused=True, holds=True), "HOLDS"),
        (GESTURE, Observation(caused=True, holds=False), "BROKEN"),
        (GESTURE, Observation(caused=False, holds=False), "UNPROVEN"),
        (GESTURE, None, "UNPROVEN"),
        (MODEL, Observation(caused=True, holds=True), "HOLDS"),
        # Claude decides a model claim: its absence proves nothing, so it is never broken.
        (MODEL, Observation(caused=True, holds=False), "UNPROVEN"),
        (MODEL, Observation(caused=False, holds=False), "UNPROVEN"),
    ],
)
def test_an_observation_becomes_an_outcome(
    claim: Claim, seen: Observation | None, outcome: str
) -> None:
    assert evaluate(claim, seen).outcome == outcome


def test_a_retired_claim_stays_retired_whatever_the_run_saw() -> None:
    retired = Claim("P92", "gesture", "gone", "code_with_slack.sessions", "n/a", retired="measured")
    assert evaluate(retired, Observation(caused=True, holds=False)).outcome == "RETIRED"


def test_a_release_is_certified_only_when_every_gesture_claim_holds() -> None:
    holds = evaluate(GESTURE, Observation(caused=True, holds=True))
    unproven = evaluate(GESTURE, None)
    model_unproven = evaluate(MODEL, None)
    assert can_certify([holds, model_unproven])
    # A run that caused nothing breaks nothing, and certifies nothing either.
    assert not can_certify([unproven, model_unproven])
    assert not broken([unproven]) and broken([evaluate(GESTURE, Observation(True, False))])


def test_the_checklist_lists_what_the_run_could_not_prove() -> None:
    results = [evaluate(GESTURE, Observation(True, True)), evaluate(MODEL, None)]
    text = checklist(results)
    assert "P91 a model act" in text and "ask for it" in text and "Approvals" in text
    assert "a gesture" not in text
    assert checklist(results[:1]) == ""


def test_the_certificate_holds_ids_and_versions_only() -> None:
    results = [evaluate(GESTURE, Observation(True, True, detail="x")), evaluate(MODEL, None)]
    assert certificate(results, "2.1.283", "2026-09-27") == {
        "cli": "2.1.283",
        "date": "2026-09-27",
        "holds": ["P90"],
        "open": ["P91"],
        "retired": [],
    }


@pytest.mark.parametrize("claim", CLAIMS, ids=[c.id for c in CLAIMS])
def test_every_claim_names_a_symbol_that_exists(claim: Claim) -> None:
    # A renamed symbol would send whoever reads a broken claim to nothing.
    parts = claim.guards.split(".")
    for split in range(len(parts), 0, -1):
        try:
            target: object = importlib.import_module(".".join(parts[:split]))
        except ModuleNotFoundError:
            continue
        for name in parts[split:]:
            target = getattr(target, name)
        return
    pytest.fail(f"{claim.guards} names no module")


def test_claim_ids_are_unique() -> None:
    assert len({c.id for c in CLAIMS}) == len(CLAIMS)


def test_every_claim_belongs_to_exactly_one_scene() -> None:
    from probe.scenes import SCENES

    owned = [claim for claims in SCENES.values() for claim in claims]
    assert sorted(owned) == sorted(c.id for c in CLAIMS)


def test_a_model_claim_cannot_be_made_broken_by_hand() -> None:
    with pytest.raises(ValueError):
        Result(MODEL, "BROKEN", "")


def test_a_behaviour_cannot_hold_without_its_event() -> None:
    with pytest.raises(ValueError):
        Observation(caused=False, holds=True)


@pytest.mark.parametrize("id", ["p1", "P0", "1", "P1 "])
def test_a_claim_id_is_p_and_a_number(id: str) -> None:
    with pytest.raises(ValueError):
        Claim(id, "gesture", "t", "code_with_slack.sessions", "how")


def test_the_pin_moves_only_when_there_is_exactly_one() -> None:
    assert pinned_to('deps = ["claude-agent-sdk==0.2.160"]', "0.2.161") == (
        'deps = ["claude-agent-sdk==0.2.161"]'
    )
    # A pin written another way would leave the old SDK installed and certify it as the new one.
    for other in ('["claude-agent-sdk>=0.2.160"]', '["claude-agent-sdk[x]==0.2.160"]', ""):
        with pytest.raises(ValueError):
            pinned_to(other, "0.2.161")


def test_a_certificate_file_of_another_shape_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "certified-versions.json"
    assert certified(path) == {}
    entry = {"cli": "2.1.283", "date": "2026-09-27", "holds": [], "open": [], "retired": []}
    path.write_text(json.dumps({"0.2.160": entry}))
    assert certified(path) == {"0.2.160": entry}
    path.write_text(json.dumps({"0.2.160": {"cli": "2.1.283"}}))
    with pytest.raises(ValueError):
        certified(path)
