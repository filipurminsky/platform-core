"""
Cross-service integration test: audio AI pipeline.

Verifies the message contract and Redis state machine across all four pipeline
stages (audio-api → stt-worker → llm-worker → tts-worker) using shared fake
infrastructure. No Kafka broker, S3, Redis, or LLM gateway required.

Run via:
  make integration-test
  # or:
  cd services/audio-api && uv run --frozen pytest ../../tests/integration/ -q

Coverage:
  - Full happy-path walk: ingest → stt → llm → tts with stub backends
  - Redis state machine: queued → transcribing → summarizing → synthesizing → done
  - S3 key conventions: audio/ transcripts/ summaries/ speech/ all present at end
  - created_at propagated forward through all four hops
  - Pipeline failure gating: stt DLQ → llm and tts never receive a message
"""

import json
from unittest.mock import MagicMock

from conftest import load_service
from fastapi.testclient import TestClient

# Load each service's main.py with an isolated app.* package.
# Order matters: later loads clear app.* from sys.modules, so load audio-api
# last since TestClient needs its lifespan to run under the test context manager.
stt = load_service("stt-worker")
llm = load_service("llm-worker")
tts = load_service("tts-worker")
audio_api = load_service("audio-api")


# ─── Shared fake infrastructure ──────────────────────────────────────────────


class FakeS3:
    """In-memory S3 stub shared across all pipeline stages."""

    def __init__(self):
        self._store: dict[str, bytes] = {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self._store[Key] = Body if isinstance(Body, bytes) else Body

    def get_object(self, Bucket, Key):
        data = self._store[Key]
        body = MagicMock()
        body.read.return_value = data
        return {"Body": body}


class FakeRedis:
    """In-memory Redis stub that supports both setex (audio-api) and set (workers)."""

    def __init__(self):
        self._store: dict[str, str] = {}

    def setex(self, key, ttl, value):
        self._store[key] = value

    def set(self, key, value, ex=None):
        self._store[key] = value

    def get(self, key):
        return self._store.get(key)

    def ping(self):
        return True


class FakeProducer:
    """Routes produced messages into an in-process topic bus."""

    def __init__(self, bus: dict):
        self._bus = bus

    def produce(self, topic, value=None, key=None, headers=None, on_delivery=None):
        self._bus.setdefault(topic, []).append({"value": value, "key": key, "headers": headers})

    def poll(self, timeout=0):
        return 0

    def flush(self, timeout=10):
        return 0


def _stub_llm_http():
    """Fake httpx client that returns a well-formed LLM chat completion."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "SUMMARY:\nIntegration test summary.\n\nACTION_ITEMS:\n- Verify pipeline",
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }
    client = MagicMock()
    client.post.return_value = resp
    return client


# ─── Happy path ───────────────────────────────────────────────────────────────


def test_full_pipeline_happy_path(monkeypatch):
    """Full walk: audio-api ingest → stt → llm → tts using stub backends."""
    s3 = FakeS3()
    redis = FakeRedis()
    bus: dict[str, list] = {}
    producer = FakeProducer(bus)

    monkeypatch.setattr(stt, "STT_BACKEND", "stub")
    monkeypatch.setattr(tts, "TTS_BACKEND", "stub")

    # ── Stage 1: audio-api ingest ─────────────────────────────────────────────
    monkeypatch.setattr(audio_api, "s3_client", s3)
    monkeypatch.setattr(audio_api, "redis_client", redis)
    monkeypatch.setattr(audio_api, "kafka_producer", producer)

    with TestClient(audio_api.app) as client:
        resp = client.post(
            "/v1/audio/jobs",
            files={"file": ("test.wav", b"RIFF" + b"\x00" * 40, "audio/wav")},
        )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    initial_state = json.loads(redis.get(f"job:{job_id}"))
    assert initial_state["status"] == "queued"
    assert f"audio/{job_id}" in s3._store

    # ── Stage 2: stt-worker ───────────────────────────────────────────────────
    assert audio_api.KAFKA_TOPIC_JOBS in bus, "audio-api must produce to audio.jobs"
    stt_input = bus[audio_api.KAFKA_TOPIC_JOBS][0]["value"]

    stt.process_message(stt_input, producer, s3, redis)

    assert f"transcripts/{job_id}" in s3._store, "stt must write transcript key to S3"
    assert stt.TOPIC_OUT in bus, "stt must produce to audio.transcripts"

    # ── Stage 3: llm-worker ───────────────────────────────────────────────────
    llm_input = bus[stt.TOPIC_OUT][0]["value"]

    llm.process_message(llm_input, producer, s3, redis, _stub_llm_http())

    assert f"summaries/{job_id}" in s3._store, "llm must write summary key to S3"
    assert llm.TOPIC_OUT in bus, "llm must produce to audio.summaries"

    # ── Stage 4: tts-worker ───────────────────────────────────────────────────
    tts_input = bus[llm.TOPIC_OUT][0]["value"]

    tts.process_message(tts_input, producer, s3, redis)

    assert f"speech/{job_id}" in s3._store, "tts must write speech WAV to S3"
    assert tts.TOPIC_OUT in bus, "tts must produce to audio.results"

    # ── Final state ───────────────────────────────────────────────────────────
    final_state = json.loads(redis.get(f"job:{job_id}"))
    assert final_state["status"] == "done", f"expected done, got: {final_state['status']}"
    assert final_state["stage"] == "tts"
    assert "speech" in final_state.get("keys", {}), "keys.speech must be set"
    assert "transcript" in final_state.get("keys", {}), "keys.transcript must be set"

    result_event = json.loads(bus[tts.TOPIC_OUT][0]["value"])
    assert result_event["status"] == "done"
    assert result_event["job_id"] == job_id


def test_created_at_propagated_through_all_hops(monkeypatch):
    """created_at from ingest must appear unchanged in the terminal tts result."""
    s3 = FakeS3()
    redis = FakeRedis()
    bus: dict[str, list] = {}
    producer = FakeProducer(bus)

    monkeypatch.setattr(stt, "STT_BACKEND", "stub")
    monkeypatch.setattr(tts, "TTS_BACKEND", "stub")
    monkeypatch.setattr(audio_api, "s3_client", s3)
    monkeypatch.setattr(audio_api, "redis_client", redis)
    monkeypatch.setattr(audio_api, "kafka_producer", producer)

    with TestClient(audio_api.app) as client:
        resp = client.post(
            "/v1/audio/jobs",
            files={"file": ("test.wav", b"RIFF" + b"\x00" * 40, "audio/wav")},
        )
    assert resp.status_code == 200

    ingest_event = json.loads(bus[audio_api.KAFKA_TOPIC_JOBS][0]["value"])
    original_created_at = ingest_event["created_at"]

    stt.process_message(bus[audio_api.KAFKA_TOPIC_JOBS][0]["value"], producer, s3, redis)
    llm.process_message(bus[stt.TOPIC_OUT][0]["value"], producer, s3, redis, _stub_llm_http())
    tts.process_message(bus[llm.TOPIC_OUT][0]["value"], producer, s3, redis)

    result = json.loads(bus[tts.TOPIC_OUT][0]["value"])
    assert result["created_at"] == original_created_at, (
        f"created_at must be carried forward unmodified: "
        f"expected {original_created_at!r}, got {result.get('created_at')!r}"
    )


def test_stt_failure_dlqs_and_gates_downstream(monkeypatch):
    """stt S3 failure → message goes to DLQ; llm and tts must not receive anything."""
    s3 = FakeS3()
    redis = FakeRedis()
    bus: dict[str, list] = {}
    producer = FakeProducer(bus)

    monkeypatch.setattr(stt, "STT_BACKEND", "stub")
    monkeypatch.setattr(stt, "MAX_RETRIES", 1)
    if hasattr(stt, "time"):
        monkeypatch.setattr(stt.time, "sleep", lambda *_: None)

    monkeypatch.setattr(audio_api, "s3_client", s3)
    monkeypatch.setattr(audio_api, "redis_client", redis)
    monkeypatch.setattr(audio_api, "kafka_producer", producer)

    with TestClient(audio_api.app) as client:
        resp = client.post(
            "/v1/audio/jobs",
            files={"file": ("test.wav", b"RIFF" + b"\x00" * 40, "audio/wav")},
        )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    # S3 is empty for stt (audio-api wrote to its own FakeS3 instance which we
    # swapped out — stt gets a broken S3 that always raises).
    broken_s3 = MagicMock()
    broken_s3.get_object.side_effect = Exception("S3 unavailable")

    stt.process_message(bus[audio_api.KAFKA_TOPIC_JOBS][0]["value"], producer, broken_s3, redis)

    # stt must DLQ the message and NOT produce to audio.transcripts
    assert stt.TOPIC_DLQ in bus, "stt failure must produce to DLQ"
    assert len(bus.get(stt.TOPIC_OUT, [])) == 0, (
        "stt must not produce to audio.transcripts when processing fails"
    )

    # Redis must be marked failed
    final_state = json.loads(redis.get(f"job:{job_id}"))
    assert final_state["status"] == "failed"
    assert final_state["error"]["stage"] == "stt"
    assert final_state["error"]["dlq_topic"] == stt.TOPIC_DLQ

    # Downstream topics must be empty — llm and tts never ran
    assert len(bus.get(llm.TOPIC_OUT, [])) == 0, "llm must not run when stt fails"
    assert len(bus.get(tts.TOPIC_OUT, [])) == 0, "tts must not run when stt fails"


def test_s3_keys_follow_convention(monkeypatch):
    """S3 key naming convention from audio-pipeline-contract.md must be honoured."""
    s3 = FakeS3()
    redis = FakeRedis()
    bus: dict[str, list] = {}
    producer = FakeProducer(bus)

    monkeypatch.setattr(stt, "STT_BACKEND", "stub")
    monkeypatch.setattr(tts, "TTS_BACKEND", "stub")
    monkeypatch.setattr(audio_api, "s3_client", s3)
    monkeypatch.setattr(audio_api, "redis_client", redis)
    monkeypatch.setattr(audio_api, "kafka_producer", producer)

    with TestClient(audio_api.app) as client:
        resp = client.post(
            "/v1/audio/jobs",
            files={"file": ("test.wav", b"RIFF" + b"\x00" * 40, "audio/wav")},
        )
    job_id = resp.json()["job_id"]

    stt.process_message(bus[audio_api.KAFKA_TOPIC_JOBS][0]["value"], producer, s3, redis)
    llm.process_message(bus[stt.TOPIC_OUT][0]["value"], producer, s3, redis, _stub_llm_http())
    tts.process_message(bus[llm.TOPIC_OUT][0]["value"], producer, s3, redis)

    # Verify exact key prefixes per contract §3
    assert f"audio/{job_id}" in s3._store, "audio key must be audio/<job_id>"
    assert f"transcripts/{job_id}" in s3._store, "transcript key must be transcripts/<job_id>"
    assert f"summaries/{job_id}" in s3._store, "summary key must be summaries/<job_id>"
    assert f"speech/{job_id}" in s3._store, "speech key must be speech/<job_id>"
