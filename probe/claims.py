"""What the probe claims about a claude-agent-sdk release, and how an observation becomes an
outcome.

The rules follow seedeep's upgrade guard: presence is conclusive, absence is not. A gesture claim
is one the probe causes itself, so once its event happened, the behaviour's absence is proof of a
break (BROKEN); if the event itself did not happen, nothing was learned (UNPROVEN). A model claim
needs Claude to act, so its failure proves nothing and is never reported as broken.
"""

import re
from dataclasses import dataclass
from typing import Literal

Kind = Literal["gesture", "model"]
Outcome = Literal["HOLDS", "BROKEN", "UNPROVEN", "RETIRED"]


@dataclass(frozen=True)
class Claim:
    id: str
    kind: Kind
    text: str
    # The symbol that depends on the behaviour: where to look when it breaks.
    guards: str
    # What to do in Slack to check it by hand when the probe could not prove it.
    how: str
    # The measurement, with date and version, that shows Claude Code no longer offers the event.
    retired: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"P[1-9]\d*", self.id):
            raise ValueError(f"a claim id reads P and a number, not {self.id!r}")
        if self.kind not in ("gesture", "model"):
            raise ValueError(f"claim {self.id}: kind {self.kind!r} is neither gesture nor model")


@dataclass(frozen=True)
class Observation:
    """`caused`: the event happened. `holds`: the behaviour the claim names was there."""

    caused: bool
    holds: bool
    detail: str = ""

    def __post_init__(self) -> None:
        if self.holds and not self.caused:
            raise ValueError("a behaviour cannot hold when its event never happened")


@dataclass(frozen=True)
class Result:
    claim: Claim
    outcome: Outcome
    detail: str

    def __post_init__(self) -> None:
        # The rule the whole probe rests on, kept by the type rather than by `evaluate` alone.
        if self.outcome == "BROKEN" and self.claim.kind != "gesture":
            raise ValueError(f"claim {self.claim.id} is a model claim: it is never BROKEN")


CLAIMS = [
    Claim(
        "P1",
        "gesture",
        "a session starts and its init message names the CLI version",
        "code_with_slack.sessions.ChannelSession._dispatch",
        "send any message in a bound channel; `!status` shows the Claude Code version",
    ),
    Claim(
        "P2",
        "gesture",
        "`!status` reads the server info and the context usage",
        "code_with_slack.sessions.ChannelSession.status",
        "send `!status`; it lists the model and a `Context:` line",
    ),
    Claim(
        "P3",
        "gesture",
        "a text prompt gets a result whose session id is stored",
        "code_with_slack.sessions.ChannelSession._finish",
        "send a message; the reply closes with its footer",
    ),
    Claim(
        "P4",
        "gesture",
        "an image prompt reaches Claude, which names the colour of the pixel",
        "code_with_slack.attachments.prompt_for",
        "attach a screenshot and ask what it shows",
    ),
    Claim(
        "P5",
        "model",
        "Claude reads an attached text file and quotes it",
        "code_with_slack.attachments.prompt_for",
        "attach a small text file and ask what it says",
    ),
    Claim(
        "P6",
        "gesture",
        "the session is listed for `!resume`",
        "code_with_slack.sessions.directory_sessions",
        "send `!resume`; the session is in the list",
    ),
    Claim(
        "P7",
        "gesture",
        "a resumed session remembers what was said before it",
        "code_with_slack.sessions.SessionManager.resume",
        "press Resume on an older session and ask what you were talking about",
    ),
    Claim(
        "P8",
        "gesture",
        "`!stop` interrupts a running turn and its reply closes",
        "code_with_slack.sessions.ChannelSession.stop",
        "ask for a long answer and send `!stop` while it writes",
    ),
    Claim(
        "P9",
        "model",
        "with `!bypass on`, a Bash call runs without asking",
        "code_with_slack.sessions.ChannelSession.set_bypass",
        "send `!bypass on`, ask Claude to run a command: no Approve button; then `!bypass off`",
    ),
    Claim(
        "P10",
        "model",
        "a Bash call shows its tool line in the reply",
        "code_with_slack.render.renderer.TurnRenderer",
        "ask Claude to run `echo hello` with Bash",
    ),
    Claim(
        "P11",
        "model",
        "a Bash call asks for approval, and an approved call runs",
        "code_with_slack.approvals.Approvals",
        "with bypass off, ask Claude to run a command; press Approve",
    ),
    Claim(
        "P12",
        "model",
        "`!stop` ends a background command",
        "code_with_slack.sessions.ChannelSession.stop",
        "ask Claude to run `tail -f` on a file in the background, then send `!stop`",
    ),
    Claim(
        "P13",
        "model",
        "a Write and an Edit show the terminal's preview (undocumented tool_use_result)",
        "code_with_slack.render.previews.preview",
        "ask Claude to create a file and then edit it; each call shows its lines under it",
    ),
    Claim(
        "P14",
        "model",
        "the Stop hook's `cwd` follows a `cd`, so the footer shows that folder's branch",
        "code_with_slack.sessions.ChannelSession._on_stop",
        "bind a folder holding a repo one level down, ask Claude to `cd` into it; the footer "
        "shows the repo's branch",
    ),
]


if len({c.id for c in CLAIMS}) != len(CLAIMS):
    raise ValueError("two claims share an id")


def evaluate(claim: Claim, seen: Observation | None) -> Result:
    if claim.retired is not None:
        return Result(claim, "RETIRED", claim.retired)
    if seen is None or not seen.caused:
        return Result(claim, "UNPROVEN", seen.detail if seen else "not attempted")
    if seen.holds:
        return Result(claim, "HOLDS", seen.detail)
    return Result(claim, "BROKEN" if claim.kind == "gesture" else "UNPROVEN", seen.detail)


def can_certify(results: list[Result]) -> bool:
    """Every gesture claim holds or is retired: a run that proved nothing certifies nothing."""
    return all(r.outcome in ("HOLDS", "RETIRED") for r in results if r.claim.kind == "gesture")


def broken(results: list[Result]) -> bool:
    return any(r.outcome == "BROKEN" for r in results)


def report(results: list[Result], cli_version: str, sdk_version: str) -> str:
    lines = [f"claude-agent-sdk {sdk_version}, bundled Claude Code {cli_version}", ""]
    for r in results:
        detail = f"  ({r.detail})" if r.detail else ""
        lines.append(f"{r.outcome:<8} {r.claim.id:<4} {r.claim.kind:<7} {r.claim.text}{detail}")
    return "\n".join(lines)


def checklist(results: list[Result]) -> str:
    """What to test by hand: the claims the run could not prove, and where a break would be."""
    open_ = [r for r in results if r.outcome in ("UNPROVEN", "BROKEN")]
    if not open_:
        return ""
    lines = ["Test these by hand in Slack; the probe could not prove them:", ""]
    for r in open_:
        lines += [
            f"  [ ] {r.claim.id} {r.claim.text}",
            f"      how: {r.claim.how}",
            f"      if it fails, look at {r.claim.guards}",
        ]
    return "\n".join(lines)


def certificate(results: list[Result], cli_version: str, date: str) -> dict[str, object]:
    """The entry `certified-versions.json` keeps for a release: ids and versions, no content."""
    return {
        "cli": cli_version,
        "date": date,
        "holds": [r.claim.id for r in results if r.outcome == "HOLDS"],
        "open": [r.claim.id for r in results if r.outcome == "UNPROVEN"],
        "retired": [r.claim.id for r in results if r.outcome == "RETIRED"],
    }
