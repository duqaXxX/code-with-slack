"""The anti-leak gate is the one check whose failure is irreversible, so it is tested both ways:
it fires on real leaks and stays quiet on shapes that only look like them.

Every sample is built from fragments: written whole, it would be a leak itself and the
maintainer's own commit hook would block this file.
"""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "scan-sensitive-diff.sh"


def scan(body: str) -> int:
    diff = f"--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n{body}\n"
    result = subprocess.run(["bash", str(SCRIPT)], input=diff, text=True, capture_output=True)
    return result.returncode


def test_ordinary_diff_passes() -> None:
    assert scan("+total = len(calls)") == 0


def test_a_python_decorator_is_not_an_email_address() -> None:
    assert scan("+" + "@pytest.mark.parametrize") == 0


def test_real_home_path_is_blocked() -> None:
    assert scan('+p = "/Us' + 'ers/carol/Documents/notes"') == 1
    assert scan('+p = "/ho' + 'me/carol/notes"') == 1


def test_neutral_home_placeholder_is_allowed() -> None:
    assert scan('+p = "/ho' + 'me/dev/project"') == 0


def test_personal_email_is_blocked_noreply_is_not() -> None:
    assert scan("+# contact carol" + "@" + "gmail.com") == 1
    assert scan('+author = "1234+carol' + "@" + 'users.noreply.github.com"') == 0


def test_slack_tokens_are_blocked() -> None:
    assert scan('+token = "xox' + 'b-1234567890-abcdefghij"') == 1
    assert scan('+token = "xap' + 'p-1-A0B1C2D3E4-5678"') == 1
    assert scan("+url = 'https://hooks.sla" + "ck.com/services/T0/B0/xyz'") == 1


def test_synthetic_slack_ids_are_allowed() -> None:
    assert scan('+OWNER = "U000ALICE"; TEAM = "T000TEAM"') == 0


def test_provider_secrets_are_blocked() -> None:
    assert scan('+k = "sk-' + "ant-api03-" + "A" * 40 + '"') == 1
    assert scan('+k = "gh' + "p_" + "a" * 30 + '"') == 1
    assert scan("+-----BEG" + "IN RSA PRIVATE KEY-----") == 1


def test_tracker_reference_is_blocked() -> None:
    assert scan("+see https://linear" + ".app/team/issue/X-1") == 1


def test_removed_lines_are_ignored() -> None:
    assert scan('-p = "/Us' + 'ers/carol/x"') == 0
