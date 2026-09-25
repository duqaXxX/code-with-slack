import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "sdk_release_report.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sdk_release_report", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


report = load().report
BASE = {"pinned": "0.2.158", "latest": "0.2.160", "latest_cli": "2.1.285", "fixture_cli": "2.1.280"}


def test_a_new_release_names_its_versions_and_asks_for_the_checks() -> None:
    out = report(**BASE, outcome="pass", last_commented="")
    assert out["title"] == "claude-agent-sdk 0.2.160: test code-with-slack against it"
    body = out["body"]
    for value in ("0.2.158", "0.2.160", "2.1.285", "2.1.280", "pass"):
        assert value in body
    # The bundled CLI changed: the message shapes the fixtures record may have changed with it.
    assert "re-record" in body


def test_each_new_version_is_announced_once() -> None:
    assert "0.2.160" in report(**BASE, outcome="pass", last_commented="0.2.159")["comment"]
    assert report(**BASE, outcome="pass", last_commented="0.2.160")["comment"] is None


def test_a_failing_suite_is_announced_even_for_a_version_already_named() -> None:
    comment = report(**BASE, outcome="fail", last_commented="0.2.160")["comment"]
    assert comment is not None and "fail" in comment


def test_the_same_cli_asks_for_no_new_recording() -> None:
    out = report(**{**BASE, "latest_cli": "2.1.280"}, outcome="pass", last_commented="")
    assert "re-record" not in out["body"]


def test_a_version_already_pinned_needs_no_issue() -> None:
    assert report(**{**BASE, "latest": "0.2.158"}, outcome="pass", last_commented="") is None


@pytest.mark.parametrize("version", ["0.2.160; rm -rf /", "", "latest", "1..2"])
def test_a_version_that_is_not_a_version_is_refused(version: str) -> None:
    with pytest.raises(ValueError):
        report(**{**BASE, "latest": version}, outcome="pass", last_commented="")


def test_a_failed_install_is_announced_too() -> None:
    comment = report(**BASE, outcome="install failed", last_commented="0.2.160")["comment"]
    assert comment is not None and "install failed" in comment
