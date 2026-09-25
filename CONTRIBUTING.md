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
