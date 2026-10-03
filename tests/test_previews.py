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


def test_a_long_diff_shows_whole() -> None:
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
    # As the terminal shows it: every line, since the diff is collapsed until the owner opens it.
    lines = shown.body.splitlines()
    assert len(lines) == 30 and lines[29] == f"-{RED} 30 line 30"
    assert shown.summary == "Removed 30 lines"


def test_a_missing_final_newline_is_not_a_line_of_the_file() -> None:
    patch = [
        {
            "oldStart": 1,
            "oldLines": 1,
            "newStart": 1,
            "newLines": 2,
            "lines": ["-a", "\\ No newline at end of file", "+a", "+b"],
        }
    ]
    shown = preview("Edit", {"filePath": "/home/dev/project/f", "structuredPatch": patch}, CWD)
    assert shown is not None
    assert shown.body.splitlines() == [f"-{RED} 1 a", f"+{GREEN} 1 a", f"+{GREEN} 2 b"]


def test_an_unknown_line_shape_falls_back_to_the_generic_line() -> None:
    patch = [{"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1, "lines": ["~a"]}]
    assert (
        preview("Edit", {"filePath": "/home/dev/project/f", "structuredPatch": patch}, CWD) is None
    )


def test_an_answered_question_shows_each_answer_as_the_terminal() -> None:
    # ask-answered.jsonl (CLI 2.1.286): a single choice answered with a label, a multi-select
    # with a label and typed text, the way `approvals.to_permission` answers.
    [(name, result, failed)] = results("ask-answered")
    assert (name, failed) == ("AskUserQuestion", False)
    assert preview(name, result, CWD) == Preview(
        "User answered Claude's questions:",
        "",
        "· Which color do you prefer? → Blue\n· Do you also like green? → Yes, Only in spring",
        plain=True,
    )


def test_a_question_nobody_answered_has_no_preview() -> None:
    # ask.jsonl: the question was refused, so its result carries no answers.
    [(name, result, failed)] = results("ask")
    assert (name, failed) == ("AskUserQuestion", True)
    assert preview(name, result, CWD) is None


@pytest.mark.parametrize(
    "result",
    [
        {"questions": [{"question": "Colour?"}]},
        {"questions": "Colour?", "answers": {"Colour?": "blue"}},
        {"questions": [{"question": "Colour?"}], "answers": ["blue"]},
        {"questions": [{"question": "Colour?"}], "answers": {"Size?": "s"}},
    ],
)
def test_an_answer_of_another_shape_has_no_preview(result: dict[str, Any]) -> None:
    assert preview("AskUserQuestion", result, CWD) is None


def test_a_question_written_on_several_lines_keeps_one_line() -> None:
    result = {
        "questions": [{"question": "Colour?\nPick one."}],
        "answers": {"Colour?\nPick one.": "blue"},
    }
    shown = preview("AskUserQuestion", result, CWD)
    assert shown is not None and shown.body == "· Colour? Pick one. → blue"
