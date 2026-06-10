"""Unit tests for audio-api.

All external dependencies (S3, Redis, Kafka) are replaced by lightweight fakes
injected via monkeypatch so the tests run completely offline.
"""

import io
import json

import main
import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fakes for external dependencies
# ---------------------------------------------------------------------------


class FakeS3Client:
    """In-memory stand-in for boto3 S3 client."""

    def __init__(self):
        self._objects: dict[str, bytes] = {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self._objects[Key] = Body

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self._objects[Key])}


class FakeRedisClient:
    """In-memory stand-in for the redis client."""

    def __init__(self):
        self._store: dict[str, str] = {}

    def setex(self, key, ttl, value):
        self._store[key] = value

    def get(self, key):
        return self._store.get(key)

    def ping(self):
        return True


class FakeProducer:
    """Records produced messages without a broker."""

    def __init__(self):
        self.produced: list[dict] = []

    def produce(self, topic, value=None, key=None, headers=None, on_delivery=None):
        self.produced.append({"topic": topic, "value": value, "key": key, "headers": headers})

    def poll(self, timeout=0):
        return 0

    def flush(self, timeout=10):
        return 0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fakes(monkeypatch):
    """Inject fake clients into the module globals and return them."""
    fake_s3 = FakeS3Client()
    fake_redis = FakeRedisClient()
    fake_producer = FakeProducer()
    monkeypatch.setattr(main, "s3_client", fake_s3)
    monkeypatch.setattr(main, "redis_client", fake_redis)
    monkeypatch.setattr(main, "kafka_producer", fake_producer)
    return fake_s3, fake_redis, fake_producer


@pytest.fixture()
def client(fakes):
    with TestClient(main.app) as c:
        yield c


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_upload_happy_path_returns_job_id(client, fakes):
    fake_s3, fake_redis, fake_producer = fakes
    audio_data = b"RIFF" + b"\x00" * 40  # fake WAV bytes

    resp = client.post(
        "/v1/audio/jobs",
        files={"file": ("test.wav", audio_data, "audio/wav")},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "job_id" in body
    job_id = body["job_id"]

    # S3 write
    assert f"audio/{job_id}" in fake_s3._objects
    assert fake_s3._objects[f"audio/{job_id}"] == audio_data

    # Redis state
    raw = fake_redis.get(f"job:{job_id}")
    assert raw is not None
    state = json.loads(raw)
    assert state["status"] == "queued"
    assert state["stage"] == "audio-api"
    assert state["keys"]["audio"] == f"audio/{job_id}"

    # Kafka event
    assert len(fake_producer.produced) == 1
    msg = fake_producer.produced[0]
    assert msg["topic"] == main.KAFKA_TOPIC_JOBS
    assert msg["key"] == job_id.encode()
    event = json.loads(msg["value"])
    assert event["job_id"] == job_id
    assert event["audio_key"] == f"audio/{job_id}"
    assert event["content_type"] == "audio/wav"
    assert event["bytes"] == len(audio_data)


# ---------------------------------------------------------------------------
# Size limit
# ---------------------------------------------------------------------------


def test_oversized_upload_rejected_413(client, fakes):
    oversized = b"\x00" * (main.MAX_UPLOAD_BYTES + 1)
    resp = client.post(
        "/v1/audio/jobs",
        files={"file": ("big.wav", oversized, "audio/wav")},
    )
    assert resp.status_code == 413
    _, fake_redis, fake_producer = fakes
    assert len(fake_producer.produced) == 0


# ---------------------------------------------------------------------------
# Content-type gating
# ---------------------------------------------------------------------------


def test_disallowed_content_type_rejected(client, fakes):
    resp = client.post(
        "/v1/audio/jobs",
        files={"file": ("doc.pdf", b"%PDF", "application/pdf")},
    )
    assert resp.status_code == 415
    _, _, fake_producer = fakes
    assert len(fake_producer.produced) == 0


def test_allowed_mp3_content_type(client, fakes):
    audio_data = b"\xff\xfb" + b"\x00" * 10  # fake MP3 frame
    resp = client.post(
        "/v1/audio/jobs",
        files={"file": ("track.mp3", audio_data, "audio/mpeg")},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Status endpoint
# ---------------------------------------------------------------------------


def test_status_returns_stored_state(client, fakes):
    # First create a job.
    audio_data = b"RIFF" + b"\x00" * 40
    create_resp = client.post(
        "/v1/audio/jobs",
        files={"file": ("test.wav", audio_data, "audio/wav")},
    )
    assert create_resp.status_code == 200
    job_id = create_resp.json()["job_id"]

    # Then fetch its status.
    status_resp = client.get(f"/jobs/{job_id}")
    assert status_resp.status_code == 200
    state = status_resp.json()
    assert state["status"] == "queued"
    assert state["stage"] == "audio-api"


def test_status_returns_404_for_unknown_job(client, fakes):
    resp = client.get("/jobs/nonexistent_job_id_12345")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Auth (when API_TOKEN is set)
# ---------------------------------------------------------------------------


def test_auth_rejects_missing_token(client, fakes, monkeypatch):
    monkeypatch.setattr(main, "API_TOKEN", "secret-token")
    resp = client.post(
        "/v1/audio/jobs",
        files={"file": ("test.wav", b"RIFF", "audio/wav")},
    )
    assert resp.status_code == 401


def test_auth_accepts_valid_token(client, fakes, monkeypatch):
    monkeypatch.setattr(main, "API_TOKEN", "secret-token")
    resp = client.post(
        "/v1/audio/jobs",
        files={"file": ("test.wav", b"RIFF" + b"\x00" * 40, "audio/wav")},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert resp.status_code == 200


def test_auth_rejects_wrong_token(client, fakes, monkeypatch):
    monkeypatch.setattr(main, "API_TOKEN", "secret-token")
    resp = client.post(
        "/v1/audio/jobs",
        files={"file": ("test.wav", b"RIFF", "audio/wav")},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_readyz_ok(client, fakes):
    resp = client.get("/readyz")
    assert resp.status_code == 200


def test_metrics_endpoint(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert b"audio_uploads_total" in resp.content
