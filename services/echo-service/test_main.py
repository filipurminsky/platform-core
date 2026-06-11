"""Unit tests for echo-service.

Exercises the probes, the metrics endpoint, and the echo handler (including
JSON-body reflection) via Starlette's TestClient — no network or cluster needed.
"""

import main
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

client = TestClient(main.app)


def test_healthz_always_ok():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_always_ready():
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_metrics_exposes_prometheus_counter():
    # The middleware records every request, so the counter is registered.
    client.get("/healthz")
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert b"echo_requests_total" in resp.content


def test_echo_reflects_request_metadata():
    resp = client.get("/?foo=bar")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "echo-service"
    assert body["request"]["method"] == "GET"
    assert body["request"]["path"] == "/"
    assert body["request"]["query"] == "foo=bar"


def test_echo_reflects_json_body():
    resp = client.request("GET", "/", json={"hello": "world"})
    assert resp.status_code == 200
    assert resp.json()["request"]["body"] == {"hello": "world"}


def test_echo_ignores_non_json_body():
    resp = client.request("GET", "/", content=b"not json", headers={"content-type": "text/plain"})
    assert resp.status_code == 200
    assert resp.json()["request"]["body"] is None


def test_middleware_records_unhandled_500():
    @main.app.get("/test/crash")
    async def crash():
        raise RuntimeError("boom")

    before = (
        REGISTRY.get_sample_value(
            "echo_requests_total", {"method": "GET", "path": "/test/crash", "status": "500"}
        )
        or 0.0
    )
    with TestClient(main.app, raise_server_exceptions=False) as error_client:
        response = error_client.get("/test/crash")
    after = REGISTRY.get_sample_value(
        "echo_requests_total", {"method": "GET", "path": "/test/crash", "status": "500"}
    )
    assert response.status_code == 500
    assert after == before + 1
