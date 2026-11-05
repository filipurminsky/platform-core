"""Unit tests for tts-worker.

All external services (S3, Redis, Kafka producer) are in-memory fakes
(platform_common.testing). conftest.py disables OTel export and makes retry
back-off instant before `main` is imported.
"""

from __future__ import annotations

import json
import sys
import types
import uuid
from unittest.mock import MagicMock

import main
import pytest
from app import backends
from platform_common.testing import FakeProducer, FakeRedis, FakeS3
from prometheus_client import REGISTRY

JOB = str(uuid.UUID(int=1))


@pytest.fixture
def producer():
    return FakeProducer()


@pytest.fixture
def redis_client():
    return FakeRedis()


@pytest.fixture
def summary_s3():
    summary = json.dumps(
        {
            "executive_summary": "Meeting concluded with three action items.",
            "action_items": ["Buy milk", "File report", "Schedule follow-up"],
        }
    ).encode()
    return FakeS3({f"summaries/{JOB}": summary})


@pytest.fixture
def valid_event():
    return json.dumps(
        {
            "job_id": JOB,
            "summary_key": f"summaries/{JOB}",
            "action_items": ["Buy milk", "File report"],
            "created_at": "2024-01-01T00:00:00+00:00",
        }
    ).encode()


# --- happy path ----------------------------------------------------------------
def test_stub_happy_path_writes_speech_to_s3(producer, summary_s3, redis_client, valid_event):
    main.process_message(valid_event, producer, summary_s3, redis_client)
    assert summary_s3.objects[f"speech/{JOB}"][:4] == b"RIFF"


def test_stub_happy_path_produces_audio_results(producer, summary_s3, redis_client, valid_event):
    main.process_message(valid_event, producer, summary_s3, redis_client)
    (payload,) = producer.on(main.TOPIC_OUT)
    assert payload == {
        "job_id": JOB,
        "speech_key": f"speech/{JOB}",
        "status": "done",
        "created_at": "2024-01-01T00:00:00+00:00",
    }


def test_stub_happy_path_sets_redis_done(producer, summary_s3, redis_client, valid_event):
    main.process_message(valid_event, producer, summary_s3, redis_client)
    state = redis_client.state(JOB)
    assert state["status"] == "done"
    assert state["stage"] == "tts"
    assert state["keys"]["speech"] == f"speech/{JOB}"
    assert redis_client.store[f"dedup:tts:{JOB}"] == "done"


def test_stub_happy_path_increments_pipeline_metrics(
    producer, summary_s3, redis_client, valid_event
):
    before_done = REGISTRY.get_sample_value("pipeline_jobs_completed_total") or 0.0
    before_e2e = REGISTRY.get_sample_value("pipeline_end_to_end_duration_seconds_count") or 0.0
    before_wait = (
        REGISTRY.get_sample_value("pipeline_queue_wait_seconds_count", {"stage": "tts"}) or 0.0
    )
    main.process_message(valid_event, producer, summary_s3, redis_client)
    assert REGISTRY.get_sample_value("pipeline_jobs_completed_total") == before_done + 1
    assert REGISTRY.get_sample_value("pipeline_end_to_end_duration_seconds_count") == before_e2e + 1
    assert (
        REGISTRY.get_sample_value("pipeline_queue_wait_seconds_count", {"stage": "tts"})
        == before_wait + 1
    )


def test_summary_text_preferred(producer, redis_client, valid_event, monkeypatch):
    spoken = []
    monkeypatch.setattr(main, "synthesize", lambda t: spoken.append(t) or b"RIFF....")
    s3 = FakeS3({f"summaries/{JOB}": json.dumps({"summary": "Hello team."}).encode()})
    main.process_message(valid_event, producer, s3, redis_client)
    assert spoken == ["Hello team."]


# --- failure / DLQ ---------------------------------------------------------------
def test_missing_summary_is_permanent(producer, redis_client, valid_event):
    before = REGISTRY.get_sample_value("pipeline_jobs_failed_total", {"stage": "tts"}) or 0.0
    main.process_message(valid_event, producer, FakeS3(), redis_client)

    (dlq,) = producer.on(main.TOPIC_DLQ)
    assert dlq["stage"] == "tts"
    assert dlq["attempts"] == 1
    assert dlq["error_type"] == "permanent"
    assert producer.produced[0]["key"] == JOB.encode()
    state = redis_client.state(JOB)
    assert state["status"] == "failed"
    assert state["error"]["stage"] == "tts"
    assert REGISTRY.get_sample_value("pipeline_jobs_failed_total", {"stage": "tts"}) == before + 1


def test_corrupt_summary_json_is_permanent(producer, redis_client, valid_event):
    s3 = FakeS3({f"summaries/{JOB}": b"{not json"})
    main.process_message(valid_event, producer, s3, redis_client)
    (dlq,) = producer.on(main.TOPIC_DLQ)
    assert dlq["error_type"] == "permanent"


def test_transient_failure_retries_then_dlq(producer, redis_client, valid_event):
    s3 = MagicMock()
    s3.get_object.side_effect = TimeoutError("s3 read timeout")
    main.process_message(valid_event, producer, s3, redis_client)
    (dlq,) = producer.on(main.TOPIC_DLQ)
    assert s3.get_object.call_count == main.MAX_RETRIES
    assert dlq["attempts"] == main.MAX_RETRIES
    assert dlq["error_type"] == "retries_exhausted"


# --- edge cases ------------------------------------------------------------------
def test_deduplication_skips_second_call(producer, summary_s3, redis_client, valid_event):
    main.process_message(valid_event, producer, summary_s3, redis_client)
    main.process_message(valid_event, producer, summary_s3, redis_client)
    assert len(producer.produced) == 1


@pytest.mark.parametrize(
    "raw",
    [
        b"{not valid json",
        json.dumps({"summary_key": "summaries/x"}).encode(),
        json.dumps({"job_id": JOB, "action_items": "not-a-list"}).encode(),
    ],
)
def test_malformed_messages_are_dead_lettered(producer, summary_s3, redis_client, raw):
    main.process_message(raw, producer, summary_s3, redis_client)
    (dlq,) = producer.on(main.TOPIC_DLQ)
    assert dlq["error_type"] == "invalid"
    assert producer.on(main.TOPIC_OUT) == []


def test_e2e_duration_missing_created_at(producer, summary_s3, redis_client):
    event = json.dumps({"job_id": JOB, "summary_key": f"summaries/{JOB}"}).encode()
    main.process_message(event, producer, summary_s3, redis_client)
    assert len(producer.on(main.TOPIC_OUT)) == 1


def test_stub_synthesizer_returns_valid_wav():
    wav = main._synthesize_stub("hello world")
    assert wav[:4] == b"RIFF"
    assert len(wav) > 44


def test_settings_reject_unknown_backend(monkeypatch):
    from pydantic import ValidationError

    monkeypatch.setenv("TTS_BACKEND", "kokor0")
    with pytest.raises(ValidationError):
        main.config.Settings()


def test_kokoro_pipeline_is_cached(monkeypatch):
    np = pytest.importorskip("numpy")
    built = []

    class FakePipeline:
        def __init__(self, lang_code):
            built.append(lang_code)

        def __call__(self, text, voice):
            yield text, "", np.zeros(240, dtype="float32")

    monkeypatch.setitem(sys.modules, "kokoro", types.SimpleNamespace(KPipeline=FakePipeline))
    monkeypatch.setattr(backends, "_kokoro_pipelines", {})
    for _ in range(3):
        assert backends._synthesize_kokoro("hi")[:4] == b"RIFF"
    assert built == ["a"]


def test_kokoro_missing_is_permanent(monkeypatch):
    from platform_common.retry import PermanentError

    monkeypatch.setitem(sys.modules, "kokoro", None)
    monkeypatch.setattr(backends, "_kokoro_pipelines", {})
    with pytest.raises(PermanentError):
        backends._synthesize_kokoro("hi")
