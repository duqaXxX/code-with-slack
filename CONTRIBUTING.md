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
   in [docs/CHANGELOG.md](docs/CHANGELOG.md) for a change in behaviour. `test/docs.test.ts`
   fails on a name a doc cites that the source no longer defines (`sinks.ReplySink`,
   `ThreadSession.mode`), and on a variable, a Slack scope or a word that
   [docs/setup.md](docs/setup.md) does not name.
4. A new `!word` of the daemon is a member of the `Word` union of `src/core/commands.ts` with its
   entry in `WORD`, and needs a line in `texts.GUIDE` (the `!guide` text) and in
   `texts.HELP_WORDS`: `test/core/commands.test.ts` fails until both explain it. Change the guide
   whenever a behaviour it describes changes. The word also needs a row in the Commands table of
   [README.md](README.md), which `test/docs.test.ts` checks against `WORD`.
5. The README is the landing page and has a word limit (`README_WORD_LIMIT` in
   `test/docs.test.ts`): describe a feature in [docs/setup.md](docs/setup.md), and change the
   README only when one of its lines stops being true.

## Checks

The daemon is TypeScript on Node 22.18 or later, in three layers: `src/agent/` (the Claude back
end), `src/core/` and `src/chat/` (the Slack provider), with `src/main.ts` joining them.
`test/imports.test.ts` fails when a layer imports what it must not. Run from the repository root:

```bash
npm ci
npm run check
```

`npm run check` runs Biome (`biome check .`), `tsc --noEmit` and the tests. Node runs the tests
from the sources, with the types stripped, so they need no build; they start no Claude Code process
and open no connection. One file: `node --test test/core/config.test.ts`. `npm run format` applies
Biome's fixes, and `npm run build` compiles `src/` to `dist/`.

The Python daemon (`src/awaydesk/`, `tests/`, `probe/*.py`, `pyproject.toml`, `uv.lock`) stays in
the repository beside the TypeScript one until it is removed, and CI runs both. Its checks are
below. `uv run python -m tests.golden` (`tests/golden.py`) writes `test/golden/` again from the
recordings of `tests/fixtures/sdk/`, which is what the TypeScript tests compare against, and
`tests/test_golden.py` fails when those files differ from what it produces.

```bash
uv sync
uv run pytest -q
uv run mypy src
uv run ruff check .
uv run ruff format --check .
```

Test fixtures are synthetic in content (ids such as `U000ALICE`) and recorded in shape, under
`tests/fixtures/`, which `test/support/fixtures.ts` reads. Never write the shape of an SDK message
or a Slack payload by hand.

## SDK releases

awaydesk pins `@anthropic-ai/claude-agent-sdk` to an exact version in `package.json` and runs the
Claude Code CLI that package bundles. The suite replays recorded streams, so it cannot see what a
new Claude Code CLI does differently. The **probe** can. It runs the daemon's own session code
against the real CLI the SDK bundles, with Slack faked, and uses your Claude Code login and Haiku
tokens from your plan, so it runs on your machine and is not part of `npm test`:

```
node probe/main.ts            # the pinned SDK, if it is not certified yet
node probe/main.ts --latest   # the newest release on npm, in a temporary git worktree
node probe/main.ts --force    # run even if already certified
```

`--latest` works on a worktree of `HEAD`, so commit a change to the probe before using it. Each
claim in `probe/claims.ts` ends in one of four outcomes:

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

`node probe/live-agent.ts` is a second live run, of the Claude back end alone: 23 checks through
`ClaudeBackend` and `ClaudeSession` on Haiku, in fresh temporary folders with none of your
settings loaded, one `PASS` or `FAIL` line each. It spends a few cents of tokens, is not tied to a
release, and exits 1 when a check fails.

Before its scenes, the probe checks
[docs/sdk-surface-typescript.md](docs/sdk-surface-typescript.md), the table of every type, field,
value, method and option of the SDK the source depends on, against the release it runs on. A row
the package no longer defines is BROKEN and stops the run with exit status 3 before any token is
spent. A row marked `reference` that the published
[reference](https://code.claude.com/docs/en/agent-sdk/typescript) no longer names is UNPROVEN and
is listed to be read by hand. The rows the reference never named and no claim covers are counted;
`node probe/main.ts --surface` lists them and checks the table alone, with no token spent. A
change that reads a new field, key or value of the SDK adds its row there in the same pull
request; `test/agent/claude/sdk-surface.test.ts` fails on an imported SDK name with no row.
[docs/sdk-surface.md](docs/sdk-surface.md) is the same table for the Python source.

The probe also compares the commands a session is offered on the release with the list recorded
in `tests/fixtures/sdk/server-info.json`, and prints the ones that are new or gone after the
claims. A new command is typed in Slack as `!name` from the day the release is pinned: try it in
a thread, decide what Slack shows for it, then record the list again. The comparison stops no
certificate. `node probe/main.ts --commands` runs it alone, with no token spent.

One claim sends every command that [docs/limits.md](docs/limits.md) lists as not offered to a
session. A command that no longer answers `isn't available in this environment` makes the claim
BROKEN: the limit is gone, and its row leaves the page before the release is pinned.

The **SDK release watch** workflow (`.github/workflows/sdk-release-watch.yml`) still watches the
Python package of the same name: every day it reads the latest `claude-agent-sdk` release from
PyPI, runs `uv run pytest -q` on it, and keeps one issue labelled `sdk release` with the versions,
the outcome and the checks left to do by hand. The body is rewritten on each run; a comment is
added once per new version and whenever the suite does not pass. Close the issue once the release
is checked: a closed issue with the same title is not opened again. The npm release has the same
pair of scripts, `.github/scripts/watch-sdk-release-npm.sh` and
`.github/scripts/sdk-release-report.mjs`, which the workflow does not call yet, so the
`@anthropic-ai/claude-agent-sdk` pin moves by hand: run `node probe/main.ts --latest` when npm
serves a newer version.
