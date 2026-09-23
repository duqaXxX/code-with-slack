import pytest

from code_with_slack.commands import (
    Bind,
    Bypass,
    Invalid,
    Passthrough,
    Picker,
    Status,
    Stop,
    bang_command,
    parse_cc,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", Picker()),
        ("   ", Picker()),
        ("bind ~/code/app", Bind("~/code/app")),
        ("bind  /a b", Bind("/a b")),
        ("bind", Invalid()),
        ("bypass on", Bypass(True)),
        ("bypass OFF", Bypass(False)),
        ("bypass", Invalid()),
        ("bypass maybe", Invalid()),
        ("status", Status()),
        ("stop", Stop()),
        ("compact", Passthrough("compact")),
        ("model opus", Passthrough("model opus")),
        ("/compact", Passthrough("compact")),
        ("status now", Passthrough("status now")),
    ],
)
def test_parse_cc(text: str, expected: object) -> None:
    assert parse_cc(text) == expected


KNOWN = {"compact", "status", "model"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("!compact", "compact"),
        ("!model opus", "model opus"),
        ("  !status", "status"),
        ("!important: read this", None),
        ("hello", None),
        ("!", None),
        ("! compact", None),
    ],
)
def test_bang_command(text: str, expected: str | None) -> None:
    assert bang_command(text, KNOWN) == expected
