"""The few tools the terminal shows in words of its own, and nothing else.

Rendering stays generic: a tool not named here shows as its name, counted when it ran more
than once, and its line, with no change anywhere. For the tools below, the terminal writes a
sentence or a preview instead, and this module reproduces it from what the SDK stream carries.
The shapes read here come from `UserMessage.tool_use_result`, which the SDK types as
`dict[str, Any]` and does not document: measured on Claude Code 2.1.283 (2026-09-27,
`tests/fixtures/sdk/edit-write.jsonl`), and guarded by the release probe. Any other shape gives
None, and the generic line is shown instead. An answered question is the exception: its lines
come from the questions and answers the daemon itself sent back (`answered`), not from a result.
"""

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

from code_with_slack import texts

# How the terminal folds finished calls of these tools. Captured from the terminal on Claude Code
# 2.1.283 (2026-09-27), where Bash also does the searching (the CLI has no Grep or Glob tool):
# `echo hi` read `Ran 1 shell command`, `ls` `Listed 1 directory`, a grep `Searched for 1
# pattern`. That classification of the command is undocumented, so every Bash call reads here as
# a shell command.
WORDS = {
    "Bash": ("Ran {n} shell command", "Ran {n} shell commands"),
    "Read": ("Read {n} file", "Read {n} files"),
}
# A diff line's colour where Slack draws none (mobile); an emoji is two columns wide, so a
# context line gets two spaces and the numbers stay aligned.
MARKS = {"-": "\U0001f7e5", "+": "\U0001f7e9", " ": "  "}
# The terminal shows a new file's first lines, then how many it leaves out.
NEW_FILE_LINES = 10
# The tools `preview` reads. A reply shows a call of one only once it has ended: a stream cannot
# take back a card, and a call that ended well is its preview alone.
PREVIEWED = frozenset({"Edit", "Write"})


@dataclass(frozen=True)
class Preview:
    title: str  # `Update(notes.txt)`
    summary: str  # `Added 1 line, removed 1 line`
    body: str  # numbered lines, as the terminal prints them
    # `diff` for a diff, which shows collapsed under its call's line, in a code block Slack
    # colours as a diff (rich text preformatted element reference, 2026-09-27: `language`).
    language: Literal["", "diff"] = ""
    # The body is lines of words (a question's answers): shown small under the card, as
    # written, in no code block.
    plain: bool = False


def folded(name: str, n: int) -> str:
    """How `n` finished calls of `name` read in a folded line."""
    words = WORDS.get(name)
    if words is None:
        return name if n == 1 else f"{name} \u00d7{n}"
    return (words[0] if n == 1 else words[1]).format(n=n)


def _lines(n: int, noun: str = "line") -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _shown(path: str, cwd: str | None) -> str:
    """The path as the terminal names it: relative to the session's folder when inside it."""
    if cwd:
        try:
            return str(PurePosixPath(path).relative_to(cwd))
        except ValueError:
            pass
    return path


def _diff(patch: list[Any]) -> tuple[int, int, list[str]] | None:
    """Lines added and removed, and the hunks numbered as the terminal numbers them: a removed
    line by its old number, any other by its new one, `...` between hunks. The sign leads the
    line, where Slack desktop's diff highlighting looks for it; a coloured square follows, since
    Slack mobile does not highlight (both measured 2026-09-27): `-🟥 7 sette`, `+🟩 7 7`."""
    added = removed = 0
    rows: list[tuple[int, str]] = []
    for index, hunk in enumerate(patch):
        if not isinstance(hunk, dict):
            return None
        old, new, lines = hunk.get("oldStart"), hunk.get("newStart"), hunk.get("lines")
        if not isinstance(old, int) or not isinstance(new, int) or not isinstance(lines, list):
            return None
        if index:
            rows.append((0, "..."))
        for line in lines:
            if not isinstance(line, str) or not line:
                return None
            sign, text = line[0], line[1:]
            if sign == "-":
                rows.append((old, f"-{text}"))
                old, removed = old + 1, removed + 1
            elif sign == "+":
                rows.append((new, f"+{text}"))
                new, added = new + 1, added + 1
            elif sign == " ":
                rows.append((new, f" {text}"))
                old, new = old + 1, new + 1
            elif sign != "\\":  # `\ No newline at end of file` is about the line above: skipped
                return None
    width = len(str(max((n for n, _ in rows), default=0)))
    return (
        added,
        removed,
        [
            text if n == 0 else f"{text[0]}{MARKS[text[0]]} {n:>{width}} {text[1:]}"
            for n, text in rows
        ],
    )


def _changed(added: int, removed: int) -> str:
    parts = [f"Added {_lines(added)}"] if added else []
    if removed:
        parts.append(f"removed {_lines(removed)}" if parts else f"Removed {_lines(removed)}")
    return ", ".join(parts) or "No change"


def answered(
    questions: list[dict[str, Any]], answers: dict[str, str | list[str]]
) -> Preview | None:
    """The answers to a question as the terminal keeps them: a line per question that has one,
    `· question → answer`, a multi-select's answers joined by commas. None with no answer."""
    lines: list[str] = []
    for entry in questions:
        question = entry["question"]
        answer = answers.get(question, "")
        shown = ", ".join(answer) if isinstance(answer, list) else answer
        if shown:
            # One line each, however the question or a typed answer was written.
            lines.append(f"· {' '.join(question.split())} → {' '.join(shown.split())}")
    return Preview(texts.ANSWERED, "", "\n".join(lines), plain=True) if lines else None


def preview(name: str, result: Any, cwd: str | None) -> Preview | None:
    """The terminal's view of a finished `Edit` or `Write`, or None for any other tool or shape."""
    if not isinstance(result, dict) or not isinstance(result.get("filePath"), str):
        return None
    path = _shown(result["filePath"], cwd)
    patch = result.get("structuredPatch")
    if name == "Write" and result.get("type") == "create":
        content = result.get("content")
        if not isinstance(content, str):
            return None
        lines = content.splitlines()
        width = len(str(min(len(lines), NEW_FILE_LINES)))
        body = [f"{i:>{width}} {line}" for i, line in enumerate(lines[:NEW_FILE_LINES], 1)]
        if len(lines) > NEW_FILE_LINES:
            body.append(f"… +{_lines(len(lines) - NEW_FILE_LINES)}")
        return Preview(f"Write({path})", f"Wrote {_lines(len(lines))} to {path}", "\n".join(body))
    if name in PREVIEWED and isinstance(patch, list) and patch:
        diff = _diff(patch)
        if diff is None:
            return None
        added, removed, body = diff
        # The terminal names an edit `Update`, and a Write over an existing file keeps `Write`.
        title = f"{'Update' if name == 'Edit' else 'Write'}({path})"
        return Preview(title, _changed(added, removed), "\n".join(body), "diff")
    return None
