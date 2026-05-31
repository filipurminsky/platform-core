"""Unit tests for echo-service.

Exercises the probes, the metrics endpoint, and the echo handler (including
JSON-body reflection) via Starlette's TestClient — no network or cluster needed.
"""

from fastapi.testclient import TestClient

import main

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
