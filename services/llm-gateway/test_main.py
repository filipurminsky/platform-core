"""Unit tests for llm-gateway.

Two concerns are covered: the sliding-window rate limiter in isolation, and the
proxy path (forwarding, rate-limit rejection, upstream timeout/error mapping)
with the upstream httpx client replaced by a fake — no vLLM needed.
"""

import asyncio

import httpx
import main
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

# --- rate limiter ---------------------------------------------------------


def test_rate_limiter_allows_up_to_limit_then_blocks():
    rl = main.SlidingWindowRateLimiter(limit=2)

    async def run():
        return [await rl.is_allowed("1.2.3.4") for _ in range(3)]

    a1, a2, a3 = asyncio.run(run())
    assert a1 == (True, 1)
    assert a2 == (True, 0)
    assert a3 == (False, 0)


def test_rate_limiter_is_per_client():
    rl = main.SlidingWindowRateLimiter(limit=1)

    async def run():
        a = await rl.is_allowed("ip-a")
        b = await rl.is_allowed("ip-b")
        return a, b

    a, b = asyncio.run(run())
    assert a[0] is True
    assert b[0] is True


def test_reset_at_is_in_the_future():
    rl = main.SlidingWindowRateLimiter(limit=5)

    async def run():
        await rl.is_allowed("ip")
        return await rl.reset_at("ip")

    assert asyncio.run(run()) > 0


class _FakeAsyncRedis:
    """Async-redis stand-in. `script_ok` toggles eval success vs. failure."""

    def __init__(self, allowed=True, remaining=3, raise_on_eval=False):
        self._allowed = allowed
        self._remaining = remaining
        self._raise = raise_on_eval

    async def eval(self, *_args):
        if self._raise:
            raise ConnectionError("redis down")
        return [1 if self._allowed else 0, self._remaining]

    async def zrange(self, *_args, **_kwargs):
        if self._raise:
            raise ConnectionError("redis down")
        return [("m", 1000.0)]


def test_redis_limiter_returns_script_result():
    rl = main.RedisSlidingWindowRateLimiter(60, _FakeAsyncRedis(allowed=True, remaining=7))

    async def run():
        return await rl.is_allowed("1.2.3.4")

    assert asyncio.run(run()) == (True, 7)


def test_redis_limiter_fails_open_when_redis_unavailable():
    rl = main.RedisSlidingWindowRateLimiter(60, _FakeAsyncRedis(raise_on_eval=True))

    async def run():
        return await rl.is_allowed("1.2.3.4")

    # Fail open: a Redis outage must not reject traffic (returns full budget).
    assert asyncio.run(run()) == (True, 60)


# --- proxy ----------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, content=b'{"ok":true}', headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {"content-type": "application/json"}
        self.closed = False

    async def aiter_bytes(self, chunk_size=None):
        yield self.content

    async def aclose(self):
        self.closed = True


class FakeClient:
    """Stands in for the shared httpx.AsyncClient."""

    def __init__(self, on_request):
        self._on_request = on_request

    def build_request(self, method, url, headers=None, content=None):
        return (method, url)

    async def send(self, request, stream=False):
        method, url = request
        return self._on_request(method, url)

    async def aclose(self):
        pass


@pytest.fixture
def client(monkeypatch):
    # Reset the limiter so a generous default doesn't carry across tests.
    monkeypatch.setattr(main, "limiter", main.SlidingWindowRateLimiter(main.RATE_LIMIT_RPM))
    with TestClient(main.app) as c:
        yield c


def test_proxy_forwards_and_adds_rate_limit_headers(client):
    upstream = FakeResponse(200, b'{"id":"x"}')
    main.http_client = FakeClient(lambda m, u: upstream)
    resp = client.post("/v1/chat/completions", json={"prompt": "hi"})
    assert resp.status_code == 200
    assert resp.content == b'{"id":"x"}'
    assert resp.headers["X-RateLimit-Limit"] == str(main.RATE_LIMIT_RPM)
    assert "X-RateLimit-Remaining" in resp.headers
    assert upstream.closed is True


def test_client_ip_ignores_spoofable_forwarding_headers():
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/models",
            "headers": [(b"x-real-ip", b"203.0.113.99")],
            "client": ("10.0.0.42", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )
    assert main._client_ip(request) == "10.0.0.42"


def test_metric_path_does_not_include_user_controlled_segments():
    assert main._normalize_path("/v1/arbitrary/model/name") == "/v1/*"


def test_proxy_rejects_when_rate_limited(client, monkeypatch):
    monkeypatch.setattr(main, "limiter", main.SlidingWindowRateLimiter(1))
    main.http_client = FakeClient(lambda m, u: FakeResponse(200))
    assert client.get("/v1/models").status_code == 200
    second = client.get("/v1/models")
    assert second.status_code == 429
    assert second.headers["X-RateLimit-Remaining"] == "0"


def test_proxy_maps_upstream_timeout_to_504(client):
    def boom(_m, _u):
        raise httpx.TimeoutException("slow")

    main.http_client = FakeClient(boom)
    assert client.post("/v1/chat/completions", json={}).status_code == 504


def test_proxy_maps_upstream_error_to_502(client):
    def boom(_m, _u):
        raise httpx.ConnectError("refused")

    main.http_client = FakeClient(boom)
    assert client.post("/v1/chat/completions", json={}).status_code == 502
