import pytest

from tests.fakes import FakeSlack
from tests.test_sessions import harness_for  # noqa: F401  (a fixture the stream tests share)


@pytest.fixture
def slack() -> FakeSlack:
    return FakeSlack()
