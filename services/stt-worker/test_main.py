"""
Unit tests for stt-worker.

All external I/O (S3, Redis, Kafka producer) is faked in-memory
(platform_common.testing). conftest.py disables OTel export and makes retry
back-off instant before `main` is imported.
"""

import io
import json
import subprocess
import uuid
import wave
from datetime import UTC, datetime
from unittest.mock import MagicMock

import main
import pytest
from app import backends
from platform_common.retry import PermanentError
from platform_common.testing import FakeProducer, FakeRedis, FakeS3
from prometheus_client import REGISTRY


def _job_id():
    return str(uuid.uuid4())


def _event(job_id, **overrides):
    ev = {
        "job_id": job_id,
        "audio_key": f"audio/{job_id}",
        "content_type": "audio/wav",
        "bytes": 1024,
        "created_at": datetime.now(UTC).isoformat(),
    }
    ev.update(overrides)
    return json.dumps(ev).encode()


def _wav(seconds: float, rate: int = 16_000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


@pytest.fixture(autouse=True)
def stub_backend(monkeypatch):
    monkeypatch.setattr(main, "STT_BACKEND", "stub")


# --- happy path --------------------------------------------------------------
def test_happy_path_stub():
    job_id = _job_id()
    s3 = FakeS3({f"audio/{job_id}": _wav(2.0)})
    redis, producer = FakeRedis(), FakeProducer()

    assert main.process_message(_event(job_id), producer, s3, redis) is main.Outcome.COMMIT

    (put,) = s3.puts
    assert put["Key"] == f"transcripts/{job_id}"
    assert b"stub transcript" in put["Body"]

    (out,) = producer.on(main.TOPIC_OUT)
    assert out["job_id"] == job_id
    assert out["transcript_key"] == f"transcripts/{job_id}"
    assert out["language"] == "en"
    assert out["duration_s"] == pytest.approx(2.0)
    assert "created_at" in out
    assert producer.produced[0]["key"] == job_id.encode()

    state = redis.state(job_id)
    assert state["status"] == "transcribing"
    assert state["keys"]["transcript"] == f"transcripts/{job_id}"
    assert redis.store[f"dedup:stt:{job_id}"] == "done"


def test_metrics_transcript_size_and_queue_wait():
    before_size = REGISTRY.get_sample_value("stt_transcript_bytes_count") or 0.0
    before_wait = (
        REGISTRY.get_sample_value("pipeline_queue_wait_seconds_count", {"stage": "stt"}) or 0.0
    )
    job_id = _job_id()
    main.process_message(
        _event(job_id), FakeProducer(), FakeS3({f"audio/{job_id}": b"A" * 32_000}), FakeRedis()
    )
    assert REGISTRY.get_sample_value("stt_transcript_bytes_count") == before_size + 1
    assert (
        REGISTRY.get_sample_value("pipeline_queue_wait_seconds_count", {"stage": "stt"})
        == before_wait + 1
    )


def test_dedup_second_delivery_is_skipped():
    job_id = _job_id()
    s3 = FakeS3({f"audio/{job_id}": b"x" * 3200})
    redis, producer = FakeRedis(), FakeProducer()
    main.process_message(_event(job_id), producer, s3, redis)
    main.process_message(_event(job_id), producer, s3, redis)
    assert len(s3.puts) == 1
    assert len(producer.produced) == 1


def test_claim_held_by_another_pod_retries_later():
    job_id = _job_id()
    redis = FakeRedis()
    redis.store[f"dedup:stt:{job_id}"] = "processing:other-pod:abc"
    out = main.process_message(_event(job_id), FakeProducer(), FakeS3(), redis)
    assert out is main.Outcome.RETRY_LATER


def test_created_at_propagated():
    job_id = _job_id()
    ts = "2024-01-15T10:00:00+00:00"
    producer = FakeProducer()
    main.process_message(
        _event(job_id, created_at=ts), producer, FakeS3({f"audio/{job_id}": b"x"}), FakeRedis()
    )
    assert producer.on(main.TOPIC_OUT)[0]["created_at"] == ts


# --- failure paths -------------------------------------------------------------
def test_missing_object_is_permanent_and_dlqd_without_retry():
    job_id = _job_id()
    redis, producer = FakeRedis(), FakeProducer()
    main.process_message(_event(job_id), producer, FakeS3(), redis)

    (dlq,) = producer.on(main.TOPIC_DLQ)
    assert dlq["job_id"] == job_id
    assert dlq["stage"] == "stt"
    assert dlq["attempts"] == 1
    assert dlq["error_type"] == "permanent"
    assert "NoSuchKey" in dlq["error"]
    state = redis.state(job_id)
    assert state["status"] == "failed"
    assert state["error"]["stage"] == "stt"
    assert state["error"]["dlq_topic"] == main.TOPIC_DLQ
    assert redis.store[f"dedup:stt:{job_id}"] == "done"
    assert redis.ttl[f"dedup:stt:{job_id}"] == main.config.JOB_STATE_TTL_SECONDS


def test_transient_s3_error_retries_then_dlq(monkeypatch):
    monkeypatch.setattr(main, "MAX_RETRIES", 3)
    job_id = _job_id()
    s3 = MagicMock()
    s3.get_object.side_effect = ConnectionError("S3 connection refused")
    producer = FakeProducer()
    main.process_message(_event(job_id), producer, s3, FakeRedis())

    assert s3.get_object.call_count == 3
    (dlq,) = producer.on(main.TOPIC_DLQ)
    assert dlq["attempts"] == 3
    assert dlq["error_type"] == "retries_exhausted"
    assert "S3 connection refused" in dlq["error"]
    assert "failed_at" in dlq
    assert producer.on(main.TOPIC_OUT) == []


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json{{{",
        json.dumps({"audio_key": "audio/x"}).encode(),  # missing job_id
        json.dumps({"job_id": "not-a-uuid"}).encode(),
        json.dumps({"job_id": str(uuid.uuid4()), "audio_key": "speech/oops"}).encode(),
    ],
)
def test_invalid_messages_go_to_dlq(raw):
    s3, producer = MagicMock(), FakeProducer()
    main.process_message(raw, producer, s3, FakeRedis(), key=b"orig-key")
    (dlq,) = producer.on(main.TOPIC_DLQ)
    assert dlq["error_type"] == "invalid"
    assert dlq["stage"] == "stt"
    s3.get_object.assert_not_called()
    assert producer.on(main.TOPIC_OUT) == []


# --- backends ------------------------------------------------------------------
def test_stub_duration_from_wav_header():
    _, lang, duration_s = main._transcribe_stub(_wav(3.0, rate=8000))
    assert lang == "en"
    assert duration_s == pytest.approx(3.0)


def test_stub_duration_estimate_for_compressed_audio():
    transcript, _, duration_s = main._transcribe_stub(b"x" * 64_000)
    assert duration_s == pytest.approx(2.0, abs=0.1)
    assert "estimated" in transcript


def test_stub_small_audio_floor():
    assert main._transcribe_stub(b"\x00" * 10)[2] >= 0.1


def test_settings_reject_unknown_backend(monkeypatch):
    from pydantic import ValidationError

    monkeypatch.setenv("STT_BACKEND", "nemoo")
    with pytest.raises(ValidationError):
        main.config.Settings()


def test_decode_failure_is_permanent(monkeypatch):
    monkeypatch.setattr(
        backends.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, b"", b"Invalid data found"),
    )
    with pytest.raises(PermanentError, match="Invalid data"):
        backends.decode_to_pcm16k(b"garbage")


def test_missing_ffmpeg_is_permanent():
    with pytest.raises(PermanentError, match="ffmpeg not found"):
        backends.decode_to_pcm16k(b"x", ffmpeg="/nonexistent/ffmpeg")


class _Hyp:
    def __init__(self, text):
        self.text = text


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ([_Hyp("hello")], "hello"),  # NeMo 2.x Hypothesis objects
        (["plain"], "plain"),  # list of strings
        ((["best"], [["best", "alt"]]), "best"),  # legacy RNNT (best, all) tuple
        ([], ""),
    ],
)
def test_hypothesis_text(result, expected):
    assert backends._hypothesis_text(result) == expected
