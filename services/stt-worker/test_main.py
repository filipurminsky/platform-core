"""
Unit tests for stt-worker.

All external I/O (S3, Redis, Kafka producer) is mocked.
Tests run fully offline — no Kafka broker, S3, Redis, or OTel collector needed.

conftest.py sets OTEL_SDK_DISABLED=true before main is imported.
"""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

# conftest.py sets OTEL_SDK_DISABLED=true before this import
import main
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_event(job_id="test-job-123", audio_key=None, bytes_=1024):
    return {
        "job_id": job_id,
        "audio_key": audio_key or f"audio/{job_id}",
        "content_type": "audio/wav",
        "bytes": bytes_,
        "created_at": datetime.now(UTC).isoformat(),
    }


def _make_mock_s3(audio_bytes=b"x" * 3200):
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=audio_bytes))}
    s3.put_object.return_value = {}
    return s3


def _make_mock_redis():
    redis = MagicMock()
    redis.get.return_value = None  # no prior state
    redis.set.return_value = True
    return redis


def _make_mock_producer():
    producer = MagicMock()
    producer.produce.return_value = None
    producer.flush.return_value = 0
    return producer


# ---------------------------------------------------------------------------
# Happy-path test: stub backend end-to-end
# ---------------------------------------------------------------------------
def test_happy_path_stub(monkeypatch):
    """
    Stub backend:
      - S3 get succeeds (returns fake audio bytes)
      - transcribe (stub) returns placeholder transcript + language + duration_s
      - S3 put is called with transcript text
      - Redis is updated (transcribing, then keys.transcript set)
      - Producer produces to audio.transcripts with correct fields
      - Producer does NOT produce to DLQ
    """
    monkeypatch.setattr(main, "STT_BACKEND", "stub")

    event = _make_event(job_id="happy-job-1")
    raw_value = json.dumps(event).encode()

    s3 = _make_mock_s3(audio_bytes=b"A" * 32_000)  # ~1 second of audio
    redis = _make_mock_redis()
    producer = _make_mock_producer()
    seen_ids: set = set()

    main.process_message(raw_value, producer, s3, redis, seen_ids)

    # S3 get called with the audio key
    s3.get_object.assert_called_once_with(Bucket=main.S3_BUCKET, Key="audio/happy-job-1")

    # S3 put called with transcripts/<job_id>
    s3.put_object.assert_called_once()
    put_kwargs = s3.put_object.call_args[1]
    assert put_kwargs["Key"] == "transcripts/happy-job-1"
    assert b"stub transcript" in put_kwargs["Body"]

    # Redis updated at least once
    assert redis.set.call_count >= 1

    # Producer produced to audio.transcripts (not DLQ)
    assert producer.produce.call_count == 1
    produce_call = producer.produce.call_args
    assert produce_call[0][0] == main.TOPIC_OUT

    # Check event payload
    produced_value = json.loads(produce_call[1]["value"])
    assert produced_value["job_id"] == "happy-job-1"
    assert produced_value["transcript_key"] == "transcripts/happy-job-1"
    assert produced_value["language"] == "en"
    assert produced_value["duration_s"] > 0
    assert "created_at" in produced_value  # carried forward


def test_metrics_transcript_size_and_queue_wait(monkeypatch):
    """Happy path records the transcript-size and queue-wait histograms."""
    from prometheus_client import REGISTRY

    monkeypatch.setattr(main, "STT_BACKEND", "stub")

    before_size = REGISTRY.get_sample_value("stt_transcript_bytes_count") or 0.0
    before_wait = (
        REGISTRY.get_sample_value("pipeline_queue_wait_seconds_count", {"stage": "stt"}) or 0.0
    )

    event = _make_event(job_id="metrics-job-1")
    main.process_message(
        json.dumps(event).encode(),
        _make_mock_producer(),
        _make_mock_s3(audio_bytes=b"A" * 32_000),
        _make_mock_redis(),
        set(),
    )

    after_size = REGISTRY.get_sample_value("stt_transcript_bytes_count") or 0.0
    after_wait = REGISTRY.get_sample_value("pipeline_queue_wait_seconds_count", {"stage": "stt"})
    assert after_size == before_size + 1
    assert after_wait == before_wait + 1


def test_happy_path_dedup(monkeypatch):
    """Processing the same job_id twice should skip the second invocation."""
    monkeypatch.setattr(main, "STT_BACKEND", "stub")

    event = _make_event(job_id="dedup-job")
    raw_value = json.dumps(event).encode()

    s3 = _make_mock_s3()
    redis = _make_mock_redis()
    producer = _make_mock_producer()
    seen_ids: set = set()

    main.process_message(raw_value, producer, s3, redis, seen_ids)
    # Second call — same job_id, already in seen_ids
    main.process_message(raw_value, producer, s3, redis, seen_ids)

    # S3 and producer should only have been called once
    assert s3.get_object.call_count == 1
    assert producer.produce.call_count == 1


def test_happy_path_created_at_propagated(monkeypatch):
    """created_at from the inbound event must appear in the produced event."""
    monkeypatch.setattr(main, "STT_BACKEND", "stub")

    original_ts = "2024-01-15T10:00:00+00:00"
    event = _make_event(job_id="ts-job")
    event["created_at"] = original_ts
    raw_value = json.dumps(event).encode()

    s3 = _make_mock_s3()
    redis = _make_mock_redis()
    producer = _make_mock_producer()

    main.process_message(raw_value, producer, s3, redis, set())

    produced_value = json.loads(producer.produce.call_args[1]["value"])
    assert produced_value["created_at"] == original_ts


# ---------------------------------------------------------------------------
# Failure-path test: S3 read error → retries → DLQ
# ---------------------------------------------------------------------------
def test_s3_error_goes_to_dlq(monkeypatch):
    """
    When S3 get_object consistently raises, after MAX_RETRIES the message
    is produced to the DLQ and Redis is set to failed.
    Producer is NOT called with TOPIC_OUT.
    """
    monkeypatch.setattr(main, "STT_BACKEND", "stub")
    monkeypatch.setattr(main, "MAX_RETRIES", 3)

    event = _make_event(job_id="fail-job-s3")
    raw_value = json.dumps(event).encode()

    s3 = MagicMock()
    s3.get_object.side_effect = Exception("S3 connection refused")

    redis = _make_mock_redis()
    producer = _make_mock_producer()
    seen_ids: set = set()

    main.process_message(raw_value, producer, s3, redis, seen_ids)

    # S3 get called MAX_RETRIES times
    assert s3.get_object.call_count == main.MAX_RETRIES

    # Produced once — to DLQ, not to TOPIC_OUT
    assert producer.produce.call_count == 1
    dlq_call = producer.produce.call_args
    assert dlq_call[0][0] == main.TOPIC_DLQ

    dlq_value = json.loads(dlq_call[1]["value"])
    assert dlq_value["job_id"] == "fail-job-s3"
    assert dlq_value["stage"] == "stt"
    assert "S3 connection refused" in dlq_value["error"]
    assert dlq_value["attempts"] == 3
    assert "failed_at" in dlq_value

    # Redis updated to failed
    # Find the set() call that contains "failed" status
    set_calls = redis.set.call_args_list
    final_state = json.loads(set_calls[-1][0][1])
    assert final_state["status"] == "failed"
    assert final_state["error"]["stage"] == "stt"
    assert final_state["error"]["dlq_topic"] == main.TOPIC_DLQ


def test_s3_put_error_goes_to_dlq(monkeypatch):
    """
    S3 get succeeds but put_object (writing transcript) fails — should DLQ.
    """
    monkeypatch.setattr(main, "STT_BACKEND", "stub")
    monkeypatch.setattr(main, "MAX_RETRIES", 2)

    event = _make_event(job_id="fail-put-job")
    raw_value = json.dumps(event).encode()

    s3 = _make_mock_s3()
    s3.put_object.side_effect = Exception("S3 put failed")

    redis = _make_mock_redis()
    producer = _make_mock_producer()

    main.process_message(raw_value, producer, s3, redis, set())

    assert producer.produce.call_count == 1
    dlq_call = producer.produce.call_args
    assert dlq_call[0][0] == main.TOPIC_DLQ


def test_bad_json_is_skipped():
    """Malformed JSON should be logged and skipped — no produce, no Redis update."""
    s3 = MagicMock()
    redis = MagicMock()
    producer = _make_mock_producer()

    main.process_message(b"not-json{{{", producer, s3, redis, set())

    producer.produce.assert_not_called()
    redis.set.assert_not_called()


# ---------------------------------------------------------------------------
# Stub backend unit test
# ---------------------------------------------------------------------------
def test_stub_transcribe_returns_duration():
    """Stub transcription duration_s should scale with byte count."""
    transcript, lang, duration_s = main._transcribe_stub(b"x" * 64_000)
    assert lang == "en"
    assert duration_s == pytest.approx(2.0, abs=0.1)
    assert "stub transcript" in transcript


def test_stub_transcribe_small_audio():
    """Very small audio should return at least 0.1s (minimum floor)."""
    _, _, duration_s = main._transcribe_stub(b"\x00" * 10)
    assert duration_s >= 0.1
