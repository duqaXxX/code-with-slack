import re

import pytest

from code_with_slack import texts
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
        ("!help Comp", Help("comp")),
        ("!bind ~/code/app", Bind("~/code/app")),
        ("!bind  /a b", Bind("/a b")),
        ("!bind", Bind("")),  # alone: the folders a session can start in
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


def test_help_filters_by_name_or_description() -> None:
    commands = [
        {"name": "compact", "description": "Free up context", "argumentHint": ""},
        {"name": "model", "description": "Set the model", "argumentHint": "[model]"},
    ]
    text = help_text(commands, "CONTEXT")
    assert "`!compact`" in text and "`!model" not in text
    assert "`!status`" not in text  # daemon words are filtered too
    assert "`!stop`" in help_text(commands, "stop")


def test_help_says_when_nothing_matches() -> None:
    text = help_text([{"name": "compact", "description": "x"}], "zzz")
    assert texts.HELP_NO_MATCH.format(query="zzz") in text


def own_words() -> list[str]:
    """Every word the daemon answers itself: each is a class of the `Word` union with a WORD."""
    from typing import get_args

    from code_with_slack.commands import Word

    words = [cls for cls in get_args(Word) if cls is not Invalid]
    # A word class without WORD would slip past the guide and help check below.
    assert all(hasattr(cls, "WORD") for cls in words)
    # ...and a WORD the parser does not answer would document a word that reaches Claude instead.
    assert all(not isinstance(parse_bang(f"!{cls.WORD}"), Passthrough) for cls in words)
    return [cls.WORD for cls in words]


def test_the_guide_and_the_help_explain_every_word_of_the_daemon() -> None:
    # A word added without its line in the guide and in !help fails here, so neither goes stale.
    words = own_words()
    assert {"help", "guide", "bind", "bypass", "status", "stop", "resume"} <= set(words)
    help_lines = "\n".join(texts.HELP_WORDS)
    for word in words:
        assert f"`!{word}" in texts.GUIDE, word
        assert f"`!{word}" in help_lines, word


def test_guide_parses() -> None:
    from code_with_slack.commands import Guide

    assert parse_bang("!guide") == Guide()
    assert parse_bang("!GUIDE") == Guide()


def test_the_help_titles_are_bold_in_a_markdown_block() -> None:
    # `!help` goes out as a markdown block, where bold is **text** and *text* is italic
    # (markdown block reference, read 2026-09-25).
    text = help_text([], "")
    assert text.startswith("**code-with-slack**") and "**Claude Code**" in text


# /doctor's description in the bundled CLI 2.1.280, read 2026-09-25: the 100-character cut falls
# inside its code span.
DOCTOR = (
    "Health-check the user's Claude Code setup and fix issues: diagnose installation health "
    "— what the `claude doctor` terminal diagnostics cover — from local data"
)


def test_a_description_cannot_leave_formatting_open() -> None:
    commands = [{"name": "doctor", "description": DOCTOR}, {"name": "model", "description": "*x_"}]
    lines = help_text(commands).splitlines()
    doctor = next(line for line in lines if line.startswith("`!doctor`"))
    unescaped = re.findall(r"(?<!\\)[`*_]", doctor.removeprefix("`!doctor`"))
    assert unescaped == []  # shown as the terminal's menu shows it: plain text
    assert next(line for line in lines if line.startswith("`!model`")) == r"`!model` \*x\_"


def test_the_filter_reads_the_description_as_written() -> None:
    commands = [{"name": "remote", "description": "run a_b"}]
    assert "`!remote`" in help_text(commands, "a_b")


def test_a_hint_with_a_backtick_cannot_break_the_line() -> None:
    # A code span cannot hold a backtick: such a usage is shown escaped, as plain text.
    text = help_text([{"name": "x", "argumentHint": "[`file`]", "description": "d"}])
    assert r"!x \[\`file\`\] d" in text.splitlines()
