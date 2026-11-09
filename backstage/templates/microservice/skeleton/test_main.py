from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)


def test_health_and_readiness():
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_request_is_observable():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["service"] == "${{ values.name }}"
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert (
        '${{ values.name | replace("-", "_") }}_requests_total{method="GET",path="/",status="200"}'
        in metrics.text
    )
