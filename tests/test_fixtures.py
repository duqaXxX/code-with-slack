import base64
import json
from typing import Any

from tests.fakes import FIXTURES


def context_ids(payload: dict[str, Any]) -> dict[str, str]:
    """The ids packed in a recorded `event_context`: `4-`, then unpadded base64 of a JSON object
    with the team, the app and the channel (shape recorded 2026-09-23)."""
    blob = payload["event_context"].partition("-")[2]
    packed = json.loads(base64.b64decode(blob + "=" * (-len(blob) % 4)))
    return {key: packed[key] for key in ("tid", "aid", "cid")}


def test_an_event_context_names_the_ids_the_payload_shows() -> None:
    # A scrub that replaces ids field by field does not see inside the base64.
    checked = 0
    for path in sorted((FIXTURES / "slack").glob("*.json")):
        payload = json.loads(path.read_text())
        if "event_context" not in payload:
            continue
        assert context_ids(payload) == {
            "tid": payload["team_id"],
            "aid": payload["api_app_id"],
            "cid": payload["event"]["channel"],
        }, path.name
        checked += 1
    assert checked
