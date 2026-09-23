import logging
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from claude_agent_sdk import ResultMessage

from code_with_slack.footer import (
    FooterData,
    Limit,
    Usage,
    UsageCache,
    format_footer,
    git_branch,
    parse_usage,
    session_tokens,
)
from tests.fakes import sdk_messages

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
