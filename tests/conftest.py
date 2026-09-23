import pytest

from tests.fakes import FakeSlack


@pytest.fixture
def slack() -> FakeSlack:
    return FakeSlack()
