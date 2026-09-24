import pytest

from code_with_slack.commands import (
    Bind,
    Bypass,
    Help,
    Invalid,
    Passthrough,
    Status,
    Stop,
    help_text,
    parse_bang,
)
from tests.fakes import sdk_json


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("!help", Help()),
        ("!bind ~/code/app", Bind("~/code/app")),
        ("!bind  /a b", Bind("/a b")),
        ("!bind", Invalid()),
        ("!bypass on", Bypass(True)),
        ("!bypass OFF", Bypass(False)),
        ("!bypass", Invalid()),
        ("!bypass maybe", Invalid()),
        ("!status", Status()),
        ("  !stop", Stop()),
        ("!compact", Passthrough("compact")),
        ("!model opus", Passthrough("model opus")),
        ("!status now", Passthrough("status now")),
        ("!important: read this", Passthrough("important: read this")),
        ("hello", None),
        ("!", None),
        ("! compact", None),
    ],
)
def test_parse_bang(text: str, expected: object) -> None:
    assert parse_bang(text) == expected


def test_help_lists_the_daemon_words_and_every_session_command() -> None:
    commands = sdk_json("server-info")["commands"]
    text = help_text(commands)
    for word in ("!help", "!status", "!stop", "!bind", "!bypass"):
        assert f"`{word}" in text
    assert all(f"`!{c['name']}" in text for c in commands)


def test_help_before_binding_says_where_the_commands_come_from() -> None:
    text = help_text(None)
    assert "`!bind" in text and "`!compact" not in text
