from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from triage.gateway import GatewayClient
from triage.store import Store
from triage.testing.fake_gateway import FakeGateway


@pytest.fixture
def fake() -> FakeGateway:
    gateway = FakeGateway()
    gateway.add_account("work", "robin@example.org", "Robin Tester")
    return gateway


@pytest.fixture
def client(fake: FakeGateway) -> Iterator[GatewayClient]:
    with GatewayClient("http://gateway.test", transport=fake.transport()) as gateway_client:
        yield gateway_client


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    cache = Store(tmp_path / "cache")
    yield cache
    cache.close()
