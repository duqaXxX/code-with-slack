# Contributing

## Reporting a problem

Open an issue with the **Bug report** or **Feature request** form; blank issues are turned off.
The bug form asks for the awaydesk and Claude Code versions, what you did, what you expected
and what happened. Leave out tokens, real Slack ids, paths from your machine and excerpts of real
sessions. A vulnerability goes to the **Security** tab, never to an issue (see
[SECURITY.md](SECURITY.md)).

## Sending a change

1. Fork, branch, and keep one subject per pull request.
2. Run the checks below; CI runs the same ones.
3. Update the doc a change makes false in the same commit, and add a line under `## Unreleased`
   in [docs/CHANGELOG.md](docs/CHANGELOG.md) for a change in behaviour. `tests/test_docs.py`
   fails on a name a doc cites that the source no longer defines (`sinks.ReplySink`,
   `ThreadSession.mode`), and on a variable, a Slack scope or a word that
   [docs/setup.md](docs/setup.md) does not name.
4. A new `!word` of the daemon is a class in the `Word` union of `awaydesk.commands` with
   its `WORD`, and needs a line in `texts.GUIDE` (the `!guide` text) and in `texts.HELP_WORDS`:
   `tests/test_commands.py` fails until both explain it. Change the guide whenever a behaviour
   it describes changes. The word also needs a row in the Commands table of
   [README.md](README.md), which `tests/test_docs.py` checks against the same union.
5. The README is the landing page and has a word limit (`README_WORD_LIMIT` in
   `tests/test_docs.py`): describe a feature in [docs/setup.md](docs/setup.md), and change the
   README only when one of its lines stops being true.

## Checks

```bash
uv sync
uv run pytest -q
uv run mypy src
uv run ruff check .
uv run ruff format --check .
```

A TypeScript daemon is being written beside the Python one, under `src/agent/`, `src/core/` and
`src/chat/`, with its tests under `test/`. It needs Node 22.18 or later, and its checks are Biome,
`tsc --noEmit` and the tests, which Node runs from the sources:

```bash
npm ci
npm run check
```

`test/golden/` holds what the Python code does with each recording of `tests/fixtures/sdk/`, at
the two places where the TypeScript tests compare against it. `uv run python -m tests.golden`
writes those files again, and `tests/test_golden.py` fails when they differ from what the code
produces.

Test fixtures are synthetic in content (ids such as `U000ALICE`) and recorded in shape. Never
write the shape of an SDK message or a Slack payload by hand.

## SDK releases

awaydesk pins `claude-agent-sdk` and runs the Claude Code CLI it bundles. Every day the
**SDK release watch** workflow (`.github/workflows/sdk-release-watch.yml`) reads the latest release
from PyPI, runs the test suite on it, and keeps one issue labelled `sdk release` with the versions,
the outcome and the checks left to do by hand. The body is rewritten on each run; a comment is
added once per new version and whenever the suite does not pass. Close the issue once the release
is checked: a closed issue with the same title is not opened again. Dependabot's weekly pull
request still moves the pin.

The suite replays recorded streams, so it cannot see what a new Claude Code CLI does differently.
The **probe** can. It runs the daemon's own session code against the real CLI the SDK bundles,
with Slack faked, and uses your Claude Code login and Haiku tokens from your plan, so it runs on
your machine and is not part of pytest:

```
uv run python -m probe            # the pinned SDK, if it is not certified yet
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
BROKEN: Claude may simply have chosen otherwise. The release is certified, and its version
added to `probe/certified-versions.json`, only when every gesture claim holds or is retired. What
the run could not prove is printed as a checklist of things to try in Slack, followed by the By
hand column of [docs/features.md](docs/features.md), which maps every feature to its tests, its
probe claims and what nothing checks automatically; a new feature or claim adds its row there. A
failure of the machine rather than the SDK (a turn past its time limit, the network) leaves its
claims UNPROVEN. The design follows the upgrade guard of
[seedeep](https://github.com/duqaXxX/seedeep).

Before its scenes, the probe checks [docs/sdk-surface.md](docs/sdk-surface.md), the table of every
type, field, value, method and option of the SDK the source depends on, against the release it
runs on. A row the package no longer defines is BROKEN and stops the run with exit status 3 before
any token is spent. A row marked `reference` that the published
[reference](https://code.claude.com/docs/en/agent-sdk/python) no longer names is UNPROVEN and is
listed to be read by hand. The rows the reference never named and no claim covers are counted;
`uv run python -m probe --surface` lists them and checks the table alone, with no token spent. A
change that reads a new field, key or value of the SDK adds its row there in the same pull
request; `tests/test_sdk_surface.py` fails on an imported SDK name with no row.

The probe also compares the commands a session is offered on the release with the list recorded
in `tests/fixtures/sdk/server-info.json`, and prints the ones that are new or gone after the
claims. A new command is typed in Slack as `!name` from the day the release is pinned: try it in
a thread, decide what Slack shows for it, then record the list again. The comparison stops no
certificate. `uv run python -m probe --commands` runs it alone, with no token spent.

One claim sends every command that [docs/limits.md](docs/limits.md) lists as not offered to a
session. A command that no longer answers `isn't available in this environment` makes the claim
BROKEN: the limit is gone, and its row leaves the page before the release is pinned.
