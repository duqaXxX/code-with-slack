"""The commands Claude Code offers a session started through the SDK, on the release this run is
on, against what the repository recorded and what `docs/limits.md` says.

Two questions a new release raises about commands:

- which commands are new, or gone: a new one is typed in Slack as `!name` from the day the SDK is
  pinned, so somebody has to see what it does there; `changes` compares the list a session is
  offered with `tests/fixtures/sdk/server-info.json`, the list recorded when the fixtures were;
- whether a command the limits page calls not offered still is: `not_offered` reads the page's
  table, and the scene `commands not offered` sends each one (claim P23).

The list is asked of a session with no settings loaded, as the fixture was recorded: the owner's
own skills and plugins add commands of theirs, which are no business of a release. Asking starts
Claude Code and sends no prompt, so it spends no tokens.
"""

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

ROOT = Path(__file__).resolve().parents[1]
LIMITS = ROOT / "docs" / "limits.md"
RECORDED = ROOT / "tests" / "fixtures" / "sdk" / "server-info.json"
SDK_SECTION = "Limits of the Claude Agent SDK"
# Claude Code's whole answer to a command it does not offer a session (measured 2026-10-09,
# Claude Code 2.1.292, on 22 commands).
UNAVAILABLE = "isn't available in this environment"


@dataclass(frozen=True)
class Changes:
    new: tuple[str, ...]  # offered now, absent from the recording
    gone: tuple[str, ...]  # recorded, no longer offered


def not_offered(path: Path = LIMITS) -> list[str]:
    """The commands `docs/limits.md` names in the table of the SDK's limits, without the slash.
    Raises ValueError when the section or its table is missing: a page that lists nothing would
    make the claim hold on nothing."""
    text = path.read_text()
    if f"## {SDK_SECTION}\n" not in text:
        raise ValueError(f"{path.name}: no section {SDK_SECTION!r}")
    section = text.split(f"## {SDK_SECTION}\n", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("|")][2:]
    names = [
        name for row in rows for name in re.findall(r"`/([a-z-]+)`", row.strip("|").split("|")[1])
    ]
    if not names:
        raise ValueError(f"{path.name}: the table under {SDK_SECTION!r} names no command")
    return names


def recorded(path: Path = RECORDED) -> set[str]:
    return {str(command["name"]) for command in json.loads(path.read_text())["commands"]}


def changes(offered: set[str], known: set[str]) -> Changes:
    return Changes(tuple(sorted(offered - known)), tuple(sorted(known - offered)))


async def offered_now() -> set[str]:
    """The commands a session with no settings is offered on the installed release."""
    with tempfile.TemporaryDirectory(prefix="cws-probe-commands-") as cwd:
        async with ClaudeSDKClient(ClaudeAgentOptions(cwd=cwd, setting_sources=[])) as client:
            info = await client.get_server_info() or {}
    return {str(command["name"]) for command in info.get("commands") or []}


def report(found: Changes, cli: str) -> str:
    """What changed in the list of commands, and what each change asks for."""
    if not found.new and not found.gone:
        return f"Commands: the same as recorded (Claude Code {cli})."
    lines = [f"Commands: the list differs from the recording (Claude Code {cli})."]
    if found.new:
        lines += [
            "",
            "  New, typed in Slack as `!name` once this release is pinned. Try each in a thread",
            "  and decide what Slack shows for it:",
            *(f"  [ ] /{name}" for name in found.new),
        ]
    if found.gone:
        lines += [
            "",
            "  Gone. Look for each in the docs and in `docs/limits.md`:",
            *(f"  [ ] /{name}" for name in found.gone),
        ]
    lines += ["", f"  Then record {RECORDED.relative_to(ROOT)} again."]
    return "\n".join(lines)
