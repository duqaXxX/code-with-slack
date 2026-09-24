import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from code_with_slack.lock import AlreadyRunning, single_instance


def test_a_second_holder_is_refused(tmp_path: Path) -> None:
    with single_instance(tmp_path), pytest.raises(AlreadyRunning), single_instance(tmp_path):
        pass


def test_the_lock_is_released_on_exit(tmp_path: Path) -> None:
    with single_instance(tmp_path):
        pass
    with single_instance(tmp_path):
        pass


def test_another_process_is_refused(tmp_path: Path) -> None:
    holder = textwrap.dedent(f"""
        import sys, time
        from pathlib import Path
        from code_with_slack.lock import single_instance
        with single_instance(Path({str(tmp_path)!r})):
            print("held", flush=True)
            time.sleep(30)
    """)
    proc = subprocess.Popen([sys.executable, "-c", holder], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
        with pytest.raises(AlreadyRunning), single_instance(tmp_path):
            pass
    finally:
        proc.kill()
        proc.wait()


def test_the_lock_creates_no_file(tmp_path: Path) -> None:
    with single_instance(tmp_path):
        assert list(tmp_path.iterdir()) == []
