# Contributing

## Reporting a problem

Open an issue with what you did, what you expected and what happened. Leave out tokens, real
Slack ids, and paths from your machine.

## Sending a change

1. Fork, branch, and keep one subject per pull request.
2. Run the checks below; CI runs the same ones.
3. Update the doc a change makes false in the same commit, and add a line under `## Unreleased`
   in [docs/CHANGELOG.md](docs/CHANGELOG.md) for a change in behaviour.

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
