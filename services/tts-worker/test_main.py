"""Unit tests for tts-worker.

All external services (S3, Redis, Kafka producer) are mocked. No network or
broker required — tests run fully offline.

Coverage:
  - Stub backend happy path: writes speech WAV to S3, produces audio.results
    with status=done, sets Redis done state, increments pipeline_jobs_completed_total.
  - Failure path: all retries exhausted → DLQ produced, Redis set to failed,
    pipeline_jobs_failed_total{stage="tts"} incremented.
  - Deduplication: second call with same job_id is a no-op.
  - Malformed JSON: swallowed, nothing produced.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import main
import pytest

# ---------------------------------------------------------------------------
# Fake collaborators
# ---------------------------------------------------------------------------


class FakeProducer:
    def __init__(self):
        self.produced: list[dict] = []

    def produce(self, topic, value=None, key=None, headers=None, on_delivery=None):
        self.produced.append({"topic": topic, "value": value, "key": key})
        if on_delivery is not None:
            on_delivery(None, None)  # simulate broker ack (no error)

    def flush(self, timeout=None):
        return 0  # 0 messages still unconfirmed


class FakeS3:
    """Fake S3 client that stores objects in-memory."""

    def __init__(self, initial: dict[str, bytes] | None = None):
        self._store: dict[str, bytes] = initial or {}

    def get_object(self, Bucket, Key):
        data = self._store.get(Key)
        if data is None:
            raise KeyError(f"No object at {Key}")
        # Mimic boto3 StreamingBody
        body = MagicMock()
        body.read.return_value = data
        return {"Body": body}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self._store[Key] = Body if isinstance(Body, bytes) else Body.encode()


class FakeRedis:
    """Fake Redis that records SET calls."""

    def __init__(self):
        self._store: dict[str, str] = {}
        self.calls: list[dict] = []

    def set(self, key, value, ex=None):
        self._store[key] = value
        self.calls.append({"key": key, "value": value, "ex": ex})

    def get(self, key):
        return self._store.get(key)

    def setex(self, key, ttl, value):
        self._store[key] = value
        self.calls.append({"key": key, "value": value, "ex": ttl})

    def eval(self, _script, _numkeys, key, update_json, ttl, now):
        """Replicate the job_state cjson merge: shallow-merge keys, stamp now."""
        update = json.loads(update_json)
        raw = self._store.get(key)
        cur = json.loads(raw) if raw else {}
        if isinstance(update.get("keys"), dict):
            cur["keys"] = {**cur.get("keys", {}), **update["keys"]}
            update = {k: v for k, v in update.items() if k != "keys"}
        cur.update(update)
        cur["updated_at"] = now
        value = json.dumps(cur)
        self._store[key] = value
        self.calls.append({"key": key, "value": value, "ex": int(ttl)})
        return 1


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def producer():
    return FakeProducer()


@pytest.fixture
def summary_s3(tmp_path):
    """S3 fake pre-loaded with a summary for job-1."""
    summary = json.dumps(
        {
            "executive_summary": "Meeting concluded with three action items.",
            "action_items": ["Buy milk", "File report", "Schedule follow-up"],
        }
    ).encode()
    return FakeS3({"summaries/job-1": summary})


@pytest.fixture
def redis_client():
    return FakeRedis()


@pytest.fixture
def valid_event():
    return json.dumps(
        {
            "job_id": "job-1",
            "summary_key": "summaries/job-1",
            "action_items": ["Buy milk", "File report"],
            "created_at": "2024-01-01T00:00:00+00:00",
        }
    ).encode()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_stub_happy_path_writes_speech_to_s3(producer, summary_s3, redis_client, valid_event):
    main.process_message(valid_event, producer, summary_s3, redis_client)

    # S3 write
    assert "speech/job-1" in summary_s3._store
    wav = summary_s3._store["speech/job-1"]
    # RIFF header: first 4 bytes are b"RIFF"
    assert wav[:4] == b"RIFF", "Expected a WAV RIFF header"


def test_stub_happy_path_produces_audio_results(producer, summary_s3, redis_client, valid_event):
    main.process_message(valid_event, producer, summary_s3, redis_client)

    assert len(producer.produced) == 1
    msg = producer.produced[0]
    assert msg["topic"] == main.TOPIC_OUT
    payload = json.loads(msg["value"])
    assert payload["job_id"] == "job-1"
    assert payload["speech_key"] == "speech/job-1"
    assert payload["status"] == "done"
    assert payload["created_at"] == "2024-01-01T00:00:00+00:00"


def test_stub_happy_path_sets_redis_done(producer, summary_s3, redis_client, valid_event):
    main.process_message(valid_event, producer, summary_s3, redis_client)

    # Last Redis write should be status=done
    done_calls = [c for c in redis_client.calls if c["key"] == "job:job-1"]
    assert done_calls, "Expected at least one Redis SET for job:job-1"
    last_state = json.loads(done_calls[-1]["value"])
    assert last_state["status"] == "done"
    assert last_state["stage"] == "tts"
    assert last_state["keys"]["speech"] == "speech/job-1"


def test_stub_happy_path_increments_pipeline_completed(
    producer, summary_s3, redis_client, valid_event
):
    before = main.PIPELINE_COMPLETED._value.get()
    main.process_message(valid_event, producer, summary_s3, redis_client)
    after = main.PIPELINE_COMPLETED._value.get()
    assert after == before + 1


def test_stub_happy_path_observes_e2e_duration(producer, summary_s3, redis_client, valid_event):
    # Just ensure no exception; histogram sum grows
    before = main.PIPELINE_E2E_DURATION._sum.get()
    main.process_message(valid_event, producer, summary_s3, redis_client)
    after = main.PIPELINE_E2E_DURATION._sum.get()
    assert after >= before  # created_at is in the past → positive duration


def test_stub_happy_path_observes_queue_wait(producer, summary_s3, redis_client, valid_event):
    from prometheus_client import REGISTRY

    label = {"stage": "tts"}
    before = REGISTRY.get_sample_value("pipeline_queue_wait_seconds_count", label) or 0.0
    main.process_message(valid_event, producer, summary_s3, redis_client)
    after = REGISTRY.get_sample_value("pipeline_queue_wait_seconds_count", label)
    assert after == before + 1  # queue-wait observed once per message


# ---------------------------------------------------------------------------
# Failure / DLQ path
# ---------------------------------------------------------------------------


def test_failure_sends_to_dlq(producer, redis_client, valid_event, monkeypatch):
    # S3 always raises → all retries fail
    bad_s3 = FakeS3()  # no objects loaded — get_object will raise KeyError

    monkeypatch.setattr(main.time, "sleep", lambda *_: None)

    main.process_message(valid_event, producer, bad_s3, redis_client)

    dlq_msgs = [m for m in producer.produced if m["topic"] == main.TOPIC_DLQ]
    assert len(dlq_msgs) == 1
    dlq = json.loads(dlq_msgs[0]["value"])
    assert dlq["stage"] == "tts"
    assert dlq["attempts"] == main.MAX_RETRIES
    assert "job-1" in dlq_msgs[0]["key"].decode()


def test_failure_sets_redis_failed(producer, redis_client, valid_event, monkeypatch):
    bad_s3 = FakeS3()
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)

    main.process_message(valid_event, producer, bad_s3, redis_client)

    done_calls = [c for c in redis_client.calls if c["key"] == "job:job-1"]
    last_state = json.loads(done_calls[-1]["value"])
    assert last_state["status"] == "failed"
    assert last_state["error"]["stage"] == "tts"


def test_failure_increments_pipeline_failed(producer, redis_client, valid_event, monkeypatch):
    bad_s3 = FakeS3()
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)

    before = main.PIPELINE_FAILED.labels(stage="tts")._value.get()
    main.process_message(valid_event, producer, bad_s3, redis_client)
    after = main.PIPELINE_FAILED.labels(stage="tts")._value.get()
    assert after == before + 1


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_deduplication_skips_second_call(producer, summary_s3, redis_client, valid_event):
    main.process_message(valid_event, producer, summary_s3, redis_client)
    first_count = len(producer.produced)

    # Second call with same job_id — should be skipped entirely
    main.process_message(valid_event, producer, summary_s3, redis_client)
    assert len(producer.produced) == first_count  # no new messages


def test_malformed_json_is_dropped(producer, summary_s3, redis_client):
    main.process_message(b"{not valid json", producer, summary_s3, redis_client)
    assert producer.produced == []


def test_stub_synthesizer_returns_valid_wav():
    wav = main._synthesize_stub("hello world")
    assert wav[:4] == b"RIFF"
    assert len(wav) > 44  # at minimum larger than the 44-byte WAV header


def test_e2e_duration_missing_created_at(producer, summary_s3, redis_client):
    """If created_at is absent, duration is skipped without crashing."""
    event = json.dumps(
        {
            "job_id": "job-missing-ts",
            "summary_key": "summaries/job-missing-ts",
        }
    ).encode()
    # Pre-load S3
    summary_s3._store["summaries/job-missing-ts"] = json.dumps(
        {"executive_summary": "No timestamp test."}
    ).encode()

    main.process_message(event, producer, summary_s3, redis_client)
    # Should succeed without raising
    assert any(m["topic"] == main.TOPIC_OUT for m in producer.produced)
