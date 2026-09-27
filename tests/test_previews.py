from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage

from code_with_slack.render.previews import Preview, folded, preview
from tests.fakes import sdk_messages

CWD = "/home/dev/project"
RED, GREEN = "\U0001f7e5", "\U0001f7e9"


def results(name: str) -> list[tuple[str, Any, bool]]:
    """Each tool call of a recording: its tool, its `tool_use_result`, and whether it failed."""
    tools: dict[str, str] = {}
    out: list[tuple[str, Any, bool]] = []
    for message in sdk_messages(name):
        if isinstance(message, AssistantMessage):
            tools |= {b.id: b.name for b in message.content if isinstance(b, ToolUseBlock)}
        if isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    out.append(
                        (tools[block.tool_use_id], message.tool_use_result, bool(block.is_error))
                    )
    return out


def test_a_new_file_shows_its_first_ten_lines_as_the_terminal() -> None:
    name, result, _ = results("edit-write")[0]
    assert name == "Write"
    shown = preview(name, result, CWD)
    assert shown == Preview(
        "Write(new.txt)",
        "Wrote 15 lines to new.txt",
        "\n".join(f"{i:>2} {i}" for i in range(1, 11)) + "\n… +5 lines",
    )


def test_an_edit_shows_its_diff_numbered_as_the_terminal() -> None:
    edits = [(r, failed) for name, r, failed in results("edit-write") if name == "Edit"]
    result = next(r for r, failed in edits if not failed)
    shown = preview("Edit", result, CWD)
    assert shown is not None
    assert (shown.title, shown.summary) == ("Update(notes.txt)", "Added 1 line, removed 1 line")
    assert shown.body.splitlines() == [
        "    1 alpha",
        f"-{RED} 2 beta",
        f"+{GREEN} 2 gamma",
        "    3 delta",
    ]


def test_a_write_over_a_file_shows_the_whole_diff() -> None:
    name, result, _ = [r for r in results("edit-write") if r[0] == "Write"][1]
    shown = preview(name, result, CWD)
    assert shown is not None
    assert (shown.title, shown.summary) == ("Write(notes.txt)", "Added 2 lines, removed 3 lines")
    assert shown.body.splitlines() == [
        f"-{RED} 1 alpha",
        f"-{RED} 2 gamma",
        f"-{RED} 3 delta",
        f"+{GREEN} 1 one",
        f"+{GREEN} 2 two",
    ]


def test_hunks_are_separated_as_the_terminal_separates_them() -> None:
    patch = [
        {"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1, "lines": ["-a", "+b"]},
        {"oldStart": 40, "oldLines": 1, "newStart": 40, "newLines": 1, "lines": [" c"]},
    ]
    shown = preview("Edit", {"filePath": "/elsewhere/f.txt", "structuredPatch": patch}, CWD)
    assert shown is not None
    assert shown.title == "Update(/elsewhere/f.txt)"  # outside the folder: the full path
    assert shown.body.splitlines() == [f"-{RED}  1 a", f"+{GREEN}  1 b", "...", "    40 c"]


@pytest.mark.parametrize(
    ("name", "result"),
    [
        ("Read", {"filePath": "/home/dev/project/a", "structuredPatch": []}),
        ("Edit", None),
        ("Edit", {"filePath": "/home/dev/project/a"}),
        ("Edit", {"filePath": "/home/dev/project/a", "structuredPatch": [{"lines": ["-a"]}]}),
        ("Write", {"filePath": "/home/dev/project/a", "type": "create", "content": 3}),
        ("Edit", {"filePath": 7, "structuredPatch": []}),
    ],
)
def test_any_other_tool_or_shape_falls_back_to_the_generic_line(name: str, result: Any) -> None:
    assert preview(name, result, CWD) is None


def test_folded_calls_read_as_the_terminal_for_bash_and_read_only() -> None:
    assert folded("Bash", 1) == "Ran 1 shell command"
    assert folded("Bash", 3) == "Ran 3 shell commands"
    assert folded("Read", 2) == "Read 2 files"
    assert folded("WebFetch", 1) == "WebFetch"
    assert folded("WebFetch", 2) == "WebFetch \u00d72"


def test_a_long_diff_stops_at_twenty_lines() -> None:
    patch = [
        {
            "oldStart": 1,
            "oldLines": 30,
            "newStart": 1,
            "newLines": 0,
            "lines": [f"-line {i}" for i in range(1, 31)],
        }
    ]
    shown = preview("Edit", {"filePath": "/home/dev/project/f", "structuredPatch": patch}, CWD)
    assert shown is not None
    lines = shown.body.splitlines()
    assert len(lines) == 21 and lines[19] == f"-{RED} 20 line 20" and lines[20] == "… +10 lines"
    assert shown.summary == "Removed 30 lines"  # the sentence still counts the whole change
