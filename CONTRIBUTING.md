# Contributing

## Reporting a problem

Open an issue with the **Bug report** or **Feature request** form; blank issues are turned off.
The bug form asks for the code-with-slack and Claude Code versions, what you did, what you expected
and what happened. Leave out tokens, real Slack ids, paths from your machine and excerpts of real
sessions. A vulnerability goes to the **Security** tab, never to an issue (see
[SECURITY.md](SECURITY.md)).

## Sending a change

1. Fork, branch, and keep one subject per pull request.
2. Run the checks below; CI runs the same ones.
3. Update the doc a change makes false in the same commit, and add a line under `## Unreleased`
   in [docs/CHANGELOG.md](docs/CHANGELOG.md) for a change in behaviour.
4. A new `!word` of the daemon is a class in the `Word` union of `code_with_slack.commands` with
   its `WORD`, and needs a line in `texts.GUIDE` (the `!guide` text) and in `texts.HELP_WORDS`:
   `tests/test_commands.py` fails until both explain it. Change the guide whenever a behaviour
   it describes changes.

## Checks

```bash
uv sync
uv run pytest -q
uv run mypy src
uv run ruff check .
uv run ruff format --check .
```

Test fixtures are synthetic in content (ids such as `U000ALICE`) and recorded in shape. Never
write the shape of an SDK message or a Slack payload by hand.

## SDK releases

code-with-slack pins `claude-agent-sdk` and runs the Claude Code CLI it bundles. Every day the
**SDK release watch** workflow (`.github/workflows/sdk-release-watch.yml`) reads the latest release
from PyPI, runs the test suite on it, and keeps one issue labelled `sdk release` with the versions,
the outcome and the checks left to do by hand. The body is rewritten on each run; a comment is
added once per new version and whenever the suite does not pass. Close the issue once the release
is checked: a closed issue with the same title is not opened again. Dependabot's weekly pull
request still moves the pin.

The suite replays recorded streams, so it cannot see what a new Claude Code CLI does differently.
The **probe** can. It runs the daemon's own session code against the real CLI the SDK bundles,
with Slack faked, and uses your Claude Code login and a few cents of Haiku tokens, so it runs on
your machine and is not part of pytest:

```
uv run python -m probe            # the pinned SDK, if its CLI is not certified yet
uv run python -m probe --latest   # the newest release on PyPI, in a temporary git worktree
uv run python -m probe --force    # run even if already certified
```

`--latest` works on a worktree of `HEAD`, so commit a change to the probe before using it. Each
claim in `probe/claims.py` ends in one of four outcomes:

| Outcome | Meaning |
|---|---|
| HOLDS | the probe caused the event and the behaviour was there |
| BROKEN | the probe caused the event itself and the behaviour was missing: do not pin the release |
| UNPROVEN | the event did not happen, so nothing was learned |
| RETIRED | Claude Code no longer offers the event |

A **gesture** claim is one the probe causes itself (a prompt, `!stop`, a resume). A **model**
claim needs Claude to act (call Bash, read a file), so when it fails it is UNPROVEN, never
BROKEN: Claude may simply have chosen otherwise. The release is certified, and its CLI version
added to `probe/certified-versions.json`, only when every gesture claim holds. What the run could
not prove is printed as a checklist of things to try in Slack. The design follows the upgrade
guard of [seedeep](https://github.com/duqaXxX/seedeep): presence is conclusive, absence is not.
