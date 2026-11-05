"""Unit tests for llm-gateway.

Covered: the sliding-window rate limiters (incl. the real Redis Lua script via
fakeredis), caller identity (bearer keys, proxy-aware client IP / XFF spoofing),
config validation, and the proxy path (auth, rate limiting, body cap, header
stripping, upstream error mapping) with the upstream httpx client replaced by a
fake — no vLLM or Redis needed.
"""

import asyncio
import ipaddress
import time

import httpx
import main
import pytest
from app import identity
from app.config import Settings
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from pydantic import ValidationError
from starlette.requests import Request

# --- rate limiter ---------------------------------------------------------


def test_rate_limiter_allows_up_to_limit_then_blocks():
    rl = main.SlidingWindowRateLimiter(limit=2)

    async def run():
        return [await rl.is_allowed("1.2.3.4") for _ in range(3)]

    a1, a2, a3 = asyncio.run(run())
    assert a1[:2] == (True, 1)
    assert a2[:2] == (True, 0)
    assert a3[:2] == (False, 0)
    # reset_at is reported consistently on every call, including the rejection.
    assert a1[2] == a2[2] == a3[2]


def test_rate_limiter_is_per_client():
    rl = main.SlidingWindowRateLimiter(limit=1)

    async def run():
        a = await rl.is_allowed("ip-a")
        b = await rl.is_allowed("ip-b")
        return a, b

    a, b = asyncio.run(run())
    assert a[0] is True
    assert b[0] is True


def test_reset_at_is_about_one_window_ahead():
    rl = main.SlidingWindowRateLimiter(limit=5)
    before = time.time()
    _allowed, _remaining, reset_at = asyncio.run(rl.is_allowed("ip"))
    assert before + rl.WINDOW - 1 <= reset_at <= time.time() + rl.WINDOW


class _FakeAsyncRedis:
    """Async-redis stand-in. `script_ok` toggles eval success vs. failure."""

    def __init__(self, allowed=True, remaining=3, reset_at=1060, raise_on_eval=False):
        self._allowed = allowed
        self._remaining = remaining
        self._reset_at = reset_at
        self._raise = raise_on_eval
        self.calls = 0

    async def eval(self, *_args):
        self.calls += 1
        if self._raise:
            raise ConnectionError("redis down")
        return [1 if self._allowed else 0, self._remaining, self._reset_at]


def test_redis_limiter_returns_script_result_in_one_round_trip():
    fake = _FakeAsyncRedis(allowed=True, remaining=7, reset_at=1234)
    rl = main.RedisSlidingWindowRateLimiter(60, fake)

    assert asyncio.run(rl.is_allowed("1.2.3.4")) == (True, 7, 1234)
    assert fake.calls == 1


def test_redis_limiter_fails_open_when_redis_unavailable():
    rl = main.RedisSlidingWindowRateLimiter(60, _FakeAsyncRedis(raise_on_eval=True))

    # Fail open: a Redis outage must not reject traffic (returns full budget).
    allowed, remaining, reset_at = asyncio.run(rl.is_allowed("1.2.3.4"))
    assert (allowed, remaining) == (True, 60)
    assert reset_at > time.time()


def test_redis_lua_script_enforces_limit_and_reports_reset_at():
    """Run the real Lua script (fakeredis + lupa) — not just the Python wrapper."""
    import fakeredis

    rl = main.RedisSlidingWindowRateLimiter(2, fakeredis.FakeAsyncRedis(decode_responses=True))

    async def run():
        return [await rl.is_allowed("key:abc") for _ in range(3)] + [
            await rl.is_allowed("key:other")
        ]

    before = time.time()
    a1, a2, a3, other = asyncio.run(run())
    assert a1[:2] == (True, 1)
    assert a2[:2] == (True, 0)
    assert a3[:2] == (False, 0)
    assert other[:2] == (True, 1)
    # reset_at = oldest entry + window, identical across the three calls.
    assert a1[2] == a2[2] == a3[2]
    assert int(before) + rl.WINDOW <= a3[2] <= time.time() + rl.WINDOW


# --- identity: bearer parsing, key matching, client IP ---------------------


def test_bearer_token_parsing():
    assert identity.bearer_token("Bearer abc") == "abc"
    assert identity.bearer_token("bearer abc") == "abc"
    assert identity.bearer_token("Basic abc") is None
    assert identity.bearer_token("Bearer ") is None
    assert identity.bearer_token(None) is None


def test_match_api_key_and_key_id():
    keys = ("k-one", "k-two")
    assert identity.match_api_key("k-two", keys) == "k-two"
    assert identity.match_api_key("k-three", keys) is None
    kid = identity.key_id("k-two")
    assert len(kid) == 16 and "k-two" not in kid
    assert kid != identity.key_id("k-one")


def _request(peer, xff=None):
    headers = [
        (b"x-forwarded-for", v.encode()) for v in ([xff] if isinstance(xff, str) else xff or [])
    ]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/models",
            "headers": headers,
            "client": (peer, 12345),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )


TRUSTED = (ipaddress.ip_network("10.0.0.0/8"),)


def test_client_ip_without_trusted_proxies_ignores_xff():
    req = _request("10.0.0.42", "203.0.113.99")
    assert identity.client_ip(req, ()) == "10.0.0.42"


def test_client_ip_untrusted_peer_cannot_spoof_xff():
    # Peer is outside the trusted CIDR → its XFF is attacker-controlled; ignore it.
    req = _request("198.51.100.7", "203.0.113.99")
    assert identity.client_ip(req, TRUSTED) == "198.51.100.7"


def test_client_ip_trusted_peer_uses_rightmost_untrusted_hop():
    # Client prepended a fake entry; the ingress appended the real address.
    req = _request("10.1.2.3", "1.1.1.1, 203.0.113.99, 10.9.9.9")
    assert identity.client_ip(req, TRUSTED) == "203.0.113.99"


def test_client_ip_trusted_peer_joins_multiple_xff_headers():
    req = _request("10.1.2.3", ["1.1.1.1", "203.0.113.5"])
    assert identity.client_ip(req, TRUSTED) == "203.0.113.5"


def test_client_ip_trusted_peer_malformed_hop_falls_back_to_peer():
    req = _request("10.1.2.3", "not-an-ip")
    assert identity.client_ip(req, TRUSTED) == "10.1.2.3"


def test_client_ip_trusted_peer_without_xff_uses_peer():
    assert identity.client_ip(_request("10.1.2.3"), TRUSTED) == "10.1.2.3"


def test_client_ip_all_hops_trusted_returns_leftmost():
    req = _request("10.1.2.3", "10.5.5.5, 10.6.6.6")
    assert identity.client_ip(req, TRUSTED) == "10.5.5.5"


def test_client_ip_ipv4_mapped_peer_matches_ipv4_cidr():
    req = _request("::ffff:10.1.2.3", "203.0.113.99")
    assert identity.client_ip(req, TRUSTED) == "203.0.113.99"


# --- config ----------------------------------------------------------------


def test_settings_prod_requires_api_keys():
    """ENVIRONMENT=prod without GATEWAY_API_KEYS must refuse to start."""
    with pytest.raises(ValidationError, match="GATEWAY_API_KEYS"):
        Settings(ENVIRONMENT="prod", GATEWAY_API_KEYS="")
    with pytest.raises(ValidationError, match="GATEWAY_API_KEYS"):
        Settings(ENVIRONMENT="production", GATEWAY_API_KEYS=" , ")


def test_settings_parses_keys_and_cidrs():
    s = Settings(
        ENVIRONMENT="prod",
        GATEWAY_API_KEYS="key-aaa, key-bbb ,",
        TRUSTED_PROXY_CIDRS="10.0.0.0/8, fd00::/8",
    )
    assert s.api_keys == ("key-aaa", "key-bbb")
    assert [str(c) for c in s.trusted_proxy_cidrs] == ["10.0.0.0/8", "fd00::/8"]
    assert "key-aaa" not in repr(s)  # SecretStr: keys never rendered


def test_settings_rejects_bad_cidr():
    with pytest.raises(ValidationError):
        Settings(TRUSTED_PROXY_CIDRS="not-a-cidr")


def test_settings_defaults():
    s = Settings()
    assert s.api_keys == ()
    assert s.trusted_proxy_cidrs == []
    assert s.upstream_unavailable_retry_after_seconds == 30
    assert s.max_request_bytes == 1024 * 1024


# --- proxy ----------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, content=b'{"ok":true}', headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = httpx.Headers(headers or {"content-type": "application/json"})
        self.closed = False

    async def aiter_bytes(self, chunk_size=None):
        yield self.content

    async def aclose(self):
        self.closed = True


class FakeClient:
    """Stands in for the shared httpx.AsyncClient; records what was forwarded."""

    def __init__(self, on_request):
        self._on_request = on_request
        self.sent: list[dict] = []

    def build_request(self, method, url, headers=None, content=None):
        self.sent.append({"method": method, "url": url, "headers": headers, "content": content})
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
    monkeypatch.setattr(main, "API_KEYS", ())
    monkeypatch.setattr(main, "TRUSTED_PROXY_CIDRS", ())
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setattr(main, "API_KEYS", ("key-alpha", "key-beta"))


def test_proxy_forwards_and_adds_rate_limit_headers(client):
    upstream = FakeResponse(200, b'{"id":"x"}')
    main.http_client = FakeClient(lambda m, u: upstream)
    resp = client.post("/v1/chat/completions", json={"prompt": "hi"})
    assert resp.status_code == 200
    assert resp.content == b'{"id":"x"}'
    assert resp.headers["X-RateLimit-Limit"] == str(main.RATE_LIMIT_RPM)
    assert "X-RateLimit-Remaining" in resp.headers
    assert int(resp.headers["X-RateLimit-Reset"]) > time.time()
    assert upstream.closed is True


def test_proxy_strips_hop_by_hop_and_credentials(client, auth_on):
    fake = FakeClient(lambda m, u: FakeResponse(200))
    main.http_client = fake
    resp = client.post(
        "/v1/chat/completions",
        content=b"{}",
        headers={
            "Authorization": "Bearer key-alpha",
            "Cookie": "session=s3cr3t",
            "Connection": "keep-alive, X-Custom-Hop",
            "X-Custom-Hop": "1",
            "Keep-Alive": "timeout=5",
            "Proxy-Authorization": "Basic zzz",
            "TE": "trailers",
            "Upgrade": "h2c",
            "Content-Type": "application/json",
            "X-Request-Id": "abc",
        },
    )
    assert resp.status_code == 200
    forwarded = {k.lower() for k in fake.sent[0]["headers"]}
    for h in (
        "authorization",
        "cookie",
        "connection",
        "x-custom-hop",
        "keep-alive",
        "proxy-authorization",
        "te",
        "upgrade",
        "host",
        "content-length",
    ):
        assert h not in forwarded, h
    assert "content-type" in forwarded
    assert "x-request-id" in forwarded


def test_proxy_strips_hop_by_hop_from_upstream_response(client):
    upstream = FakeResponse(
        200,
        headers={
            "content-type": "application/json",
            "connection": "close",
            "keep-alive": "timeout=5",
            "x-upstream": "yes",
        },
    )
    main.http_client = FakeClient(lambda m, u: upstream)
    resp = client.get("/v1/models")
    assert resp.headers["x-upstream"] == "yes"
    assert "keep-alive" not in resp.headers


def test_metric_path_does_not_include_user_controlled_segments():
    assert main._normalize_path("/v1/arbitrary/model/name") == "/v1/*"


def test_proxy_rejects_when_rate_limited_with_retry_after(client, monkeypatch):
    monkeypatch.setattr(main, "limiter", main.SlidingWindowRateLimiter(1))
    main.http_client = FakeClient(lambda m, u: FakeResponse(200))
    assert client.get("/v1/models").status_code == 200
    second = client.get("/v1/models")
    assert second.status_code == 429
    assert second.json() == {"error": "rate limit exceeded"}
    assert second.headers["X-RateLimit-Remaining"] == "0"
    assert 1 <= int(second.headers["Retry-After"]) <= main.SlidingWindowRateLimiter.WINDOW


def test_proxy_honours_xff_only_from_trusted_proxy(client, monkeypatch):
    """Two users behind the ingress get separate buckets; a direct caller can't spoof."""
    monkeypatch.setattr(main, "limiter", main.SlidingWindowRateLimiter(1))
    # TestClient's peer address is "testclient" (not an IP) → never trusted.
    main.http_client = FakeClient(lambda m, u: FakeResponse(200))
    monkeypatch.setattr(main, "TRUSTED_PROXY_CIDRS", TRUSTED)
    assert client.get("/v1/models", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    # Spoofing a different XFF does not buy a fresh bucket.
    assert client.get("/v1/models", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 429


def test_proxy_requires_auth_when_keys_configured(client, auth_on):
    main.http_client = FakeClient(lambda m, u: FakeResponse(200))
    missing = client.get("/v1/models")
    assert missing.status_code == 401
    assert missing.json() == {"error": "missing bearer token"}
    assert missing.headers["WWW-Authenticate"] == "Bearer"
    wrong = client.get("/v1/models", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    assert wrong.json() == {"error": "invalid api key"}
    ok = client.get("/v1/models", headers={"Authorization": "Bearer key-beta"})
    assert ok.status_code == 200


def test_auth_failure_does_not_reach_upstream_or_log_key(client, auth_on, capsys):
    fake = FakeClient(lambda m, u: FakeResponse(200))
    main.http_client = fake
    client.get("/v1/models", headers={"Authorization": "Bearer super-secret-guess"})
    client.get("/v1/models", headers={"Authorization": "Bearer key-alpha"})
    assert len(fake.sent) == 1
    out = capsys.readouterr().out
    assert "super-secret-guess" not in out
    assert "key-alpha" not in out
    assert identity.key_id("key-alpha") in out


def test_probes_and_metrics_stay_unauthenticated(client, auth_on):
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    assert client.get("/metrics").status_code == 200


def test_rate_limit_is_per_api_key_when_auth_on(client, auth_on, monkeypatch):
    monkeypatch.setattr(main, "limiter", main.SlidingWindowRateLimiter(1))
    main.http_client = FakeClient(lambda m, u: FakeResponse(200))
    a = {"Authorization": "Bearer key-alpha"}
    b = {"Authorization": "Bearer key-beta"}
    assert client.get("/v1/models", headers=a).status_code == 200
    assert client.get("/v1/models", headers=a).status_code == 429
    # Same IP, different key → independent bucket.
    assert client.get("/v1/models", headers=b).status_code == 200


def test_proxy_rejects_oversized_content_length(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_REQUEST_BYTES", 10)
    fake = FakeClient(lambda m, u: FakeResponse(200))
    main.http_client = fake
    resp = client.post("/v1/chat/completions", content=b"x" * 11)
    assert resp.status_code == 413
    assert fake.sent == []


def test_proxy_enforces_body_cap_without_content_length(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_REQUEST_BYTES", 10)
    fake = FakeClient(lambda m, u: FakeResponse(200))
    main.http_client = fake

    def chunks():  # generator body → chunked transfer, no Content-Length
        yield b"x" * 6
        yield b"x" * 6

    resp = client.post("/v1/chat/completions", content=chunks())
    assert resp.status_code == 413
    assert fake.sent == []


def test_proxy_accepts_body_at_cap(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_REQUEST_BYTES", 10)
    fake = FakeClient(lambda m, u: FakeResponse(200))
    main.http_client = fake
    assert client.post("/v1/chat/completions", content=b"x" * 10).status_code == 200
    assert fake.sent[0]["content"] == b"x" * 10


def _upstream_errors() -> float:
    return REGISTRY.get_sample_value("gateway_upstream_errors_total") or 0.0


def test_proxy_maps_upstream_timeout_to_504(client):
    def boom(_m, _u):
        raise httpx.ReadTimeout("slow")

    main.http_client = FakeClient(boom)
    before = _upstream_errors()
    resp = client.post("/v1/chat/completions", json={})
    assert resp.status_code == 504
    assert "Retry-After" not in resp.headers
    assert _upstream_errors() == before + 1


@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ConnectTimeout("syn")])
def test_proxy_maps_connect_failure_to_503_with_retry_after(client, exc):
    """vLLM scaled to zero: 503 + Retry-After, and still counted for KEDA activation."""

    def boom(_m, _u):
        raise exc

    main.http_client = FakeClient(boom)
    before = _upstream_errors()
    resp = client.post("/v1/chat/completions", json={})
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == str(main.UPSTREAM_UNAVAILABLE_RETRY_AFTER)
    assert _upstream_errors() == before + 1
    assert (
        REGISTRY.get_sample_value(
            "gateway_requests_total", {"method": "POST", "path": "/v1/*", "status": "503"}
        )
        or 0
    ) >= 1


def test_proxy_maps_other_upstream_error_to_502(client):
    def boom(_m, _u):
        raise httpx.RemoteProtocolError("garbage")

    main.http_client = FakeClient(boom)
    before = _upstream_errors()
    assert client.post("/v1/chat/completions", json={}).status_code == 502
    assert _upstream_errors() == before + 1
