"""The text of the SDK release watch's issue, from the versions and the test outcome.

code-with-slack pins `claude-agent-sdk` and runs the Claude Code CLI the SDK bundles, whose
message shapes the fixtures record. A new SDK release is read against the project here: the
workflow installs it, runs the suite, and this names what is left to check by hand.

    PINNED=0.2.158 LATEST=0.2.160 LATEST_CLI=2.1.285 FIXTURE_CLI=2.1.280 OUTCOME=pass \
    LAST_COMMENTED=0.2.159 uv run python .github/scripts/sdk_release_report.py

prints `{"title": ..., "body": ..., "comment": ... or null}` as JSON, or `null` when the latest
release is the pinned one and there is nothing to report.
"""

import json
import os
import re

VERSION = re.compile(r"\d+(\.\d+)+")


def _checked(version: str) -> str:
    # Versions reach an issue title and a shell command: anything else is refused.
    if not VERSION.fullmatch(version):
        raise ValueError(f"not a version: {version!r}")
    return version


def report(
    *,
    pinned: str,
    latest: str,
    latest_cli: str,
    fixture_cli: str,
    outcome: str,
    last_commented: str,
) -> dict[str, str | None] | None:
    """The issue for `latest`, or None when it is the pinned version. A comment (a notification)
    comes once per new version, and at every run whose suite did not pass."""
    pinned, latest = _checked(pinned), _checked(latest)
    latest_cli, fixture_cli = _checked(latest_cli), _checked(fixture_cli)
    if latest == pinned:
        return None
    cli_changed = latest_cli != fixture_cli
    steps = [
        "- [ ] Merge the Dependabot pull request that moves the pin, or move it by hand.",
        "- [ ] `uv run pytest -q`, `uv run mypy src`, `uv run ruff check .` pass on the new pin.",
    ]
    if cli_changed:
        steps.append(
            f"- [ ] The bundled CLI moved from {fixture_cli} to {latest_cli}: re-record the SDK "
            "streams the fixtures hold and compare their shapes."
        )
    steps.append(
        "- [ ] Live, in a test workspace: a prompt with tools, an approval, `!resume`, "
        "an attached image and file."
    )
    body = "\n".join(
        [
            f"`claude-agent-sdk` {latest} is on PyPI; code-with-slack pins {pinned}.",
            "",
            "| | Version |",
            "|---|---|",
            f"| Pinned SDK | {pinned} |",
            f"| Latest SDK | {latest} |",
            f"| CLI bundled with it | {latest_cli} |",
            f"| CLI the fixtures record | {fixture_cli} |",
            f"| Test suite on {latest} | {outcome} |",
            "",
            *steps,
            "",
            "This body is rewritten by the SDK release watch workflow. Close the issue once the "
            "release is checked; a closed issue with this title is not opened again.",
        ]
    )
    comment: str | None = None
    if outcome != "pass":
        comment = f"The test suite on claude-agent-sdk {latest}: {outcome}."
    elif latest != last_commented:
        comment = f"claude-agent-sdk {latest} is out (bundled CLI {latest_cli}); tests: {outcome}."
    return {
        "title": f"claude-agent-sdk {latest}: test code-with-slack against it",
        "body": body,
        "comment": comment,
    }


if __name__ == "__main__":
    print(
        json.dumps(
            report(
                pinned=os.environ["PINNED"],
                latest=os.environ["LATEST"],
                latest_cli=os.environ["LATEST_CLI"],
                fixture_cli=os.environ["FIXTURE_CLI"],
                outcome=os.environ.get("OUTCOME", "skipped"),
                last_commented=os.environ.get("LAST_COMMENTED", ""),
            )
        )
    )
