import re
from typing import Any

import pytest

from code_with_slack import texts
from code_with_slack.commands import (
    Bind,
    Bypass,
    Help,
    Invalid,
    Open,
    Passthrough,
    Status,
    Stop,
    help_text,
    parse_bang,
    refused_in_thread,
    unformatted,
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
        ("!open", Open()),  # alone: the picker
        ("!OPEN  setup ", Open("setup")),
        ("!open docs/my file.md", Open("docs/my file.md")),
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
        ("!compact\nnotes", Passthrough("compact notes")),  # the word ends at a line break too
        ("!stop\n", Stop()),
        ("!model  opus ", Passthrough("model opus")),
    ],
)
def test_parse_bang(text: str, expected: object) -> None:
    assert parse_bang(text) == expected


def rich_text(*parts: dict[str, Any]) -> list[dict[str, Any]]:
    """A composer message's blocks, as Slack stores them. Read back on 2026-10-07: a section
    with a `text` element in inline code, and a `rich_text_preformatted` part. The `bold` style
    and the `rich_text_quote` and `rich_text_list` part types are named by the Block Kit
    reference and were not read from a real message."""
    return [{"type": "rich_text", "block_id": "Rd58H", "elements": list(parts)}]


def part(*leaves: dict[str, Any], kind: str = "rich_text_section") -> dict[str, Any]:
    return {"type": kind, "elements": list(leaves)}


def leaf(text: str, **style: bool) -> dict[str, Any]:
    return {"type": "text", "text": text, **({"style": style} if style else {})}


CODE_BLOCK = {**part(leaf("!goal tick"), kind="rich_text_preformatted"), "border": 0}


@pytest.mark.parametrize(
    ("text", "blocks", "expected"),
    [
        # The two shapes read back from Slack: inline code, and a code block.
        ("`!goal tick`", rich_text(part(leaf("!goal tick", code=True))), "!goal tick"),
        ("```!goal tick```\n", rich_text(CODE_BLOCK), "!goal tick"),
        # Only the first run is read in the blocks: what follows it comes from the text, with
        # its own formatting and its links as the owner sent them.
        (
            "`!goal` what is it?",
            rich_text(part(leaf("!goal", code=True), leaf(" what is it?"))),
            "!goal what is it?",
        ),
        (
            "*!goal* run `make test` see https://example.com/1",
            rich_text(part(leaf("!goal", bold=True), leaf(" run "), leaf("make test", code=True))),
            "!goal run `make test` see https://example.com/1",
        ),
        (
            "`!compact`\n```notes```",
            rich_text(
                part(leaf("!compact", code=True)),
                part(leaf("notes"), kind="rich_text_preformatted"),
            ),
            "!compact\n```notes```",
        ),
        # Anything before the `!` keeps the message a prompt, formatted or not.
        ("`\\!stop`", rich_text(part(leaf("\\!stop", code=True))), ""),
        ("say `!stop`", rich_text(part(leaf("say "), leaf("!stop", code=True))), ""),
        ("!stop", rich_text(part(leaf("!stop"))), ""),  # no marks: `parse_bang` reads the text
        # A quote and a list are not read through.
        ("> !stop", rich_text(part(leaf("!stop"), kind="rich_text_quote")), ""),
        ("• !stop", rich_text(part(part(leaf("!stop")), kind="rich_text_list")), ""),
        # A text that does not read as marks, the run, the marks again: left alone.
        ("`!stop`", rich_text(part(leaf("!status", code=True))), ""),
        ("`!stop", rich_text(part(leaf("!stop", code=True))), ""),
        ("x`!stop`", rich_text(part(leaf("!stop", code=True))), ""),
        # No composer block: nothing tells a mark from a character the owner typed.
        ("`!stop`", [{"type": "section", "text": {"type": "mrkdwn", "text": "`!stop`"}}], ""),
        ("`!stop`", [], ""),
        ("`!stop`", None, ""),
    ],
)
def test_unformatted(text: str, blocks: object, expected: str) -> None:
    assert unformatted(text, blocks) == expected


def test_help_lists_the_daemon_words_and_every_session_command() -> None:
    commands = sdk_json("server-info")["commands"]
    text = help_text(commands)
    for word in ("!help", "!status", "!stop", "!bind", "!bypass", "!open"):
        assert f"`{word}" in text
    assert all(f"`!{c['name']}" in text for c in commands if c["name"] != "clear")


def test_help_says_how_a_command_is_told_from_a_text() -> None:
    commands = sdk_json("server-info")["commands"]
    for listed in (None, commands):  # in the channel and inside a session's thread
        assert help_text(listed).split("\n")[1] == texts.HELP_RULE
        assert texts.HELP_RULE not in help_text(listed, "status")  # a search lists matches only
    assert "`\\!goal`" in texts.HELP_RULE


def test_help_leaves_out_clear_which_a_thread_refuses() -> None:
    # Issue #76: a session's commands are listed inside its thread only, where `!clear` is
    # refused. The recorded list (SDK 0.2.163) does carry it.
    commands = sdk_json("server-info")["commands"]
    assert any(c["name"] == "clear" for c in commands)
    assert "`!clear" not in help_text(commands)
    assert "`!compact" in help_text(commands)


def test_a_thread_refuses_clear_and_its_aliases_before_the_session_lists_them() -> None:
    # A session rebuilt after a restart has listed no command until it connects.
    assert {"clear", "reset", "new"} <= refused_in_thread([])
    assert {"clear", "reset", "new"} <= refused_in_thread(None)


def test_a_thread_refuses_an_alias_of_clear_the_session_adds() -> None:
    commands = [
        {"name": "clear", "description": "d", "aliases": ["reset", "new", "wipe"]},
        {"name": "rename", "description": "d", "aliases": ["name"]},
    ]
    refused = refused_in_thread(commands)
    assert "wipe" in refused
    assert "name" not in refused and "rename" not in refused


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
    assert {"help", "guide", "bind", "bypass", "status", "stop", "resume", "open"} <= set(words)
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
