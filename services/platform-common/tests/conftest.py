"""Shared fakes. OTEL export disabled before anything configures tracing."""

import os

os.environ.setdefault("OTEL_SDK_DISABLED", "true")

import pytest  # noqa: E402
from platform_common.testing import FakeProducer, FakeRedis  # noqa: E402


@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def producer():
    return FakeProducer()


@pytest.fixture
def no_sleep(monkeypatch):
    slept: list[float] = []
    import platform_common.retry as retry_mod

    monkeypatch.setattr(retry_mod.time, "sleep", lambda s: slept.append(s))
    return slept
