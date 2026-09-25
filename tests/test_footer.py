import logging
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage

from code_with_slack import footer
from code_with_slack.footer import (
    FooterData,
    Limit,
    Usage,
    UsageCache,
    UsageProbe,
    effort_change,
    format_footer,
    git_branch,
    parse_usage,
    session_tokens,
)
from tests.fakes import FakeClaudeClient, sdk_messages

NOW = datetime(2026, 9, 23, 21, 0, tzinfo=ZoneInfo("Europe/Berlin"))


def recorded_usage_text() -> str:
    result = [m for m in sdk_messages("usage") if isinstance(m, ResultMessage)][-1]
    assert result.result
    return result.result


def test_parses_the_recorded_usage() -> None:
    usage = parse_usage(recorded_usage_text(), NOW)
    assert usage.session is not None and 0 <= usage.session.percent <= 100
    assert usage.week is not None and 0 <= usage.week.percent <= 100
    assert usage.session.resets_at is not None and usage.session.resets_at.tzinfo is not None


def test_parses_the_measured_wording() -> None:
    text = (
        "Current session: 3% used · resets Sep 24 at 1:10am (Europe/Berlin)\n"
        "Current week (all models): 25% used · resets Sep 27 at 6pm (Europe/Berlin)\n"
        "Current week (Fable): 0% used · resets Sep 27 at 6pm (Europe/Berlin)"
    )
    usage = parse_usage(text, NOW)
    rome = ZoneInfo("Europe/Berlin")
    assert usage.session == Limit(3, datetime(2026, 9, 24, 1, 10, tzinfo=rome))
    assert usage.week == Limit(25, datetime(2026, 9, 27, 18, 0, tzinfo=rome))


def test_a_reset_in_january_seen_in_december_is_next_year() -> None:
    december = datetime(2026, 12, 31, 22, 0, tzinfo=UTC)
    usage = parse_usage("Current session: 1% used · resets Jan 1 at 12am (UTC)", december)
    assert usage.session is not None and usage.session.resets_at == datetime(
        2027, 1, 1, 0, 0, tzinfo=ZoneInfo("UTC")
    )


def test_unknown_wording_fails_soft() -> None:
    assert parse_usage("Session budget: plenty", NOW) == Usage(None, None)
    usage = parse_usage("Current session: 9% used · resets sometime (Nowhere/Zone)", NOW)
    assert usage.session == Limit(9, None)


def test_full_footer() -> None:
    usage = Usage(Limit(3, NOW + timedelta(hours=2, minutes=10)), Limit(25, None))
    data = FooterData(
        bypass=True,
        branch="main",
        model="claude-opus-5-5",
        context_percent=6.4,
        session_tokens=12_345,
        usage=usage,
    )
    assert format_footer(data, NOW) == (
        "⚡ bypass · main · claude-opus-5-5 · ctx 6% · 12.3k tok · 5h 3% ↻ 2h · 7d 25%"
    )


def test_minimal_footer_hides_what_it_does_not_know() -> None:
    data = FooterData(
        bypass=False, branch=None, model=None, context_percent=None, session_tokens=None, usage=None
    )
    assert format_footer(data, NOW) == ""


def test_session_tokens_sum_every_model() -> None:
    result = [m for m in sdk_messages("tools") if isinstance(m, ResultMessage)][-1]
    assert result.model_usage
    expected = sum(
        u["inputTokens"]
        + u["outputTokens"]
        + u["cacheReadInputTokens"]
        + u["cacheCreationInputTokens"]
        for u in result.model_usage.values()
    )
    assert session_tokens(result) == expected


async def test_cache_fetches_once_per_ttl_and_after_invalidate() -> None:
    calls = 0
    clock = [0.0]

    async def fetch() -> str:
        nonlocal calls
        calls += 1
        return "Current session: 5% used"

    cache = UsageCache(fetch, ttl=300, clock=lambda: clock[0])
    await cache.refresh_if_stale()
    await cache.refresh_if_stale()
    assert calls == 1 and cache.current is not None and cache.current.session == Limit(5, None)
    cache.invalidate()
    await cache.refresh_if_stale()
    assert calls == 2
    clock[0] = 301
    await cache.refresh_if_stale()
    assert calls == 3


async def test_a_failing_fetch_keeps_the_old_value_and_logs_no_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fetch() -> str:
        raise RuntimeError("SECRET-DETAIL")

    cache = UsageCache(fetch, ttl=0)
    cache.current = Usage(Limit(1, None), None)
    with caplog.at_level(logging.WARNING):
        await cache.refresh_if_stale()
    assert cache.current == Usage(Limit(1, None), None)
    assert "SECRET-DETAIL" not in caplog.text


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "feature-x", str(tmp_path)], check=True)
    return tmp_path


async def test_git_branch(repo: Path) -> None:
    assert await git_branch(repo) == "feature-x"
    assert await git_branch(repo / "missing") is None


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        # Measured on Claude Code 2.1.280 (2026-09-23) through the SDK.
        (
            "Set effort level to high (this session only): Comprehensive implementation",
            (True, "high"),
        ),
        ("Effort level set to auto (this session only)", (True, "auto")),
        ("Set model to `Sonnet 5` for this session only", (True, None)),
        # The form ccstatusline reads from transcripts.
        ("Set model to Opus with xhigh effort", (True, "xhigh")),
        ("Current session: 5% used", (False, None)),
    ],
)
def test_effort_change(output: str, expected: tuple[bool, str | None]) -> None:
    assert effort_change(output) == expected


def test_the_footer_shows_the_effort_after_the_model() -> None:
    data = FooterData(
        bypass=False,
        branch="main",
        model="claude-opus-5-5",
        context_percent=None,
        session_tokens=None,
        usage=None,
        effort="high",
    )
    assert format_footer(data, NOW) == "main · claude-opus-5-5 · effort high"


async def test_a_usage_probe_with_no_answer_gives_up_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(footer, "USAGE_TIMEOUT", 0.05)
    clients: list[FakeClaudeClient] = []

    def factory(options: ClaudeAgentOptions) -> FakeClaudeClient:
        clients.append(FakeClaudeClient(options))  # no scripted turn: /usage never answers
        return clients[-1]

    probe = UsageProbe(tmp_path, factory)
    with pytest.raises(TimeoutError):
        await probe()
    assert clients[0].connected is False  # the next refresh starts from a clean client


@pytest.mark.parametrize(
    ("directory", "shown"),
    [("/srv/alice/code/app", "code/app"), ("/app", "app"), ("/", None)],
)
def test_the_footer_ends_with_the_folder_s_last_two_names(directory: str, shown: str) -> None:
    # As the owner's terminal status line shows it (ccstatusline current-working-dir, 2 segments;
    # the maintainer, 2026-09-25).
    data = FooterData(
        bypass=False,
        branch="main",
        model=None,
        context_percent=None,
        session_tokens=None,
        usage=None,
        directory=Path(directory),
    )
    assert format_footer(data, NOW) == " · ".join(p for p in ("main", shown) if p)


def test_the_folder_and_the_branch_are_shown_as_written() -> None:
    data = FooterData(
        bypass=False,
        branch="fix/<a>&b",
        model=None,
        context_percent=None,
        session_tokens=None,
        usage=None,
        directory=Path("/srv/alice/R&D/<x>"),
    )
    assert format_footer(data, NOW) == "fix/&lt;a&gt;&amp;b · R&amp;D/&lt;x&gt;"
