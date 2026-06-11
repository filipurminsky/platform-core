"""
Tests for llm-worker/main.py.

Strategy:
  - conftest.py sets OTEL_SDK_DISABLED=true before any import, so tracing is a no-op.
  - S3, Redis, Kafka Producer, and the httpx gateway call are all mocked via
    monkeypatch / simple fakes — no network required.
  - Three scenarios:
      1. Happy path: transcript read → gateway called → S3 written → audio.summaries produced.
      2. Gateway error: after MAX_RETRIES → DLQ produced + Redis marked failed.
      3. Defensive parse fallback: assistant returns unparseable text → summary = full text,
         action_items = [].
"""

import json  # noqa: I001
from unittest.mock import MagicMock

import main  # conftest sets OTEL_SDK_DISABLED before this


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

FAKE_JOB_ID = "test-job-123"
FAKE_TRANSCRIPT = "Alice: Let's discuss Q3.\nBob: We need to ship feature X by Friday."
FAKE_CREATED_AT = "2026-01-01T00:00:00+00:00"

INBOUND_EVENT = {
    "job_id": FAKE_JOB_ID,
    "transcript_key": f"transcripts/{FAKE_JOB_ID}",
    "language": "en",
    "duration_s": 10.0,
    "created_at": FAKE_CREATED_AT,
}


def _chat_completion_response(content: str, completion_tokens: int = 42) -> dict:
    """Build a minimal OpenAI-compatible chat completion response."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": completion_tokens,
            "total_tokens": 142,
        },
    }


def _make_s3_mock(transcript: str = FAKE_TRANSCRIPT):
    """Return a mock boto3 S3 client pre-loaded with a transcript."""
    s3 = MagicMock()
    s3.get_object.return_value = {
        "Body": MagicMock(read=MagicMock(return_value=transcript.encode()))
    }
    s3.put_object.return_value = {}
    return s3


def _make_redis_mock():
    """Return a mock redis client."""
    r = MagicMock()
    r.get.return_value = None  # no existing state
    r.set.return_value = True
    return r


def _make_producer_mock():
    """Return a mock Kafka Producer."""
    p = MagicMock()
    p.produce = MagicMock()
    # produce_confirmed treats a non-zero flush() return as "delivery unconfirmed"
    # and raises — a bare MagicMock return value is truthy, so pin it to 0.
    p.flush = MagicMock(return_value=0)
    return p


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path(monkeypatch):
    """
    Transcript is read from S3, LLM gateway is called with structured prompt,
    summary JSON is written to S3, audio.summaries is produced, Redis is updated.
    """
    # Arrange
    s3 = _make_s3_mock()
    r = _make_redis_mock()
    producer = _make_producer_mock()

    llm_text = (
        "SUMMARY:\nQ3 planning session covered feature X delivery timeline.\n\n"
        "ACTION_ITEMS:\n- Ship feature X by Friday\n- Update roadmap doc"
    )
    chat_resp = _chat_completion_response(llm_text, completion_tokens=55)

    fake_http_response = MagicMock()
    fake_http_response.raise_for_status = MagicMock()
    fake_http_response.json.return_value = chat_resp

    http_client = MagicMock()
    http_client.post.return_value = fake_http_response

    # Act
    main.process_message(
        json.dumps(INBOUND_EVENT).encode(),
        producer,
        s3,
        r,
        http_client,
    )

    # Assert: S3 read
    s3.get_object.assert_called_once_with(Bucket=main.S3_BUCKET, Key=f"transcripts/{FAKE_JOB_ID}")

    # Assert: LLM gateway called with POST /v1/chat/completions
    http_client.post.assert_called_once()
    call_kwargs = http_client.post.call_args
    assert call_kwargs[0][0] == "/v1/chat/completions"
    body = call_kwargs[1]["json"]
    assert body["model"] == main.LLM_MODEL
    assert any(m["role"] == "system" for m in body["messages"])
    assert FAKE_TRANSCRIPT in body["messages"][-1]["content"]

    # Assert: S3 write with summary JSON
    s3.put_object.assert_called_once()
    put_kwargs = s3.put_object.call_args[1]
    assert put_kwargs["Key"] == f"summaries/{FAKE_JOB_ID}"
    written = json.loads(put_kwargs["Body"].decode())
    assert written["job_id"] == FAKE_JOB_ID
    assert "Q3 planning session" in written["summary"]
    assert "Ship feature X by Friday" in written["action_items"]
    assert written["created_at"] == FAKE_CREATED_AT

    # Assert: audio.summaries produced with action_items + created_at
    producer.produce.assert_called_once()
    # First positional arg is topic; value is a keyword arg
    assert producer.produce.call_args[0][0] == main.TOPIC_OUT
    out_value = json.loads(producer.produce.call_args[1]["value"])
    assert out_value["job_id"] == FAKE_JOB_ID
    assert out_value["summary_key"] == f"summaries/{FAKE_JOB_ID}"
    assert "Ship feature X by Friday" in out_value["action_items"]
    assert out_value["created_at"] == FAKE_CREATED_AT

    # Assert: Redis updated (set called with job key)
    assert r.set.called
    last_call = r.set.call_args_list[-1]
    key_arg = last_call[0][0]
    assert key_arg == f"job:{FAKE_JOB_ID}"
    state = json.loads(last_call[0][1])
    assert "summary" in state.get("keys", {})

    # Assert: job_id deduped on second call
    r.get.return_value = "1"
    main.process_message(
        json.dumps(INBOUND_EVENT).encode(),
        producer,
        s3,
        r,
        http_client,
    )
    # produce should still be called only once
    assert http_client.post.call_count == 1


# ---------------------------------------------------------------------------
# Gateway error → DLQ
# ---------------------------------------------------------------------------


def test_metrics_summary_size_and_queue_wait(monkeypatch):
    """Happy path records the summary-size and queue-wait histograms."""
    from prometheus_client import REGISTRY

    s3 = _make_s3_mock()
    r = _make_redis_mock()
    producer = _make_producer_mock()
    chat_resp = _chat_completion_response("SUMMARY:\nok\n\nACTION_ITEMS:\n- do x", 10)
    fake_http_response = MagicMock()
    fake_http_response.raise_for_status = MagicMock()
    fake_http_response.json.return_value = chat_resp
    http_client = MagicMock()
    http_client.post.return_value = fake_http_response

    before_size = REGISTRY.get_sample_value("llm_summary_bytes_count") or 0.0
    before_wait = (
        REGISTRY.get_sample_value("pipeline_queue_wait_seconds_count", {"stage": "llm"}) or 0.0
    )

    main.process_message(json.dumps(INBOUND_EVENT).encode(), producer, s3, r, http_client)

    after_size = REGISTRY.get_sample_value("llm_summary_bytes_count") or 0.0
    after_wait = REGISTRY.get_sample_value("pipeline_queue_wait_seconds_count", {"stage": "llm"})
    assert after_size == before_size + 1
    assert after_wait == before_wait + 1


def test_gateway_error_goes_to_dlq(monkeypatch):
    """
    When the LLM gateway raises an error on every attempt, the job is dead-lettered:
    audio.llm-dlq is produced and Redis is marked failed.
    """
    s3 = _make_s3_mock()
    r = _make_redis_mock()
    producer = _make_producer_mock()

    http_client = MagicMock()
    http_client.post.side_effect = Exception("gateway timeout")

    monkeypatch.setattr(main, "MAX_RETRIES", 2)

    main.process_message(
        json.dumps(INBOUND_EVENT).encode(),
        producer,
        s3,
        r,
        http_client,
    )

    # DLQ produced
    producer.produce.assert_called_once()
    # The first positional arg is the topic
    assert producer.produce.call_args[0][0] == main.TOPIC_DLQ

    dlq_value = json.loads(producer.produce.call_args[1]["value"])
    assert dlq_value["stage"] == "llm"
    assert dlq_value["attempts"] == 2
    assert "gateway timeout" in dlq_value["error"]

    # Redis marked failed
    last_set = r.set.call_args_list[-1]
    state = json.loads(last_set[0][1])
    assert state["status"] == "failed"
    assert state["error"]["stage"] == "llm"
    assert state["error"]["dlq_topic"] == main.TOPIC_DLQ

    # gateway was called MAX_RETRIES times
    assert http_client.post.call_count == 2


# ---------------------------------------------------------------------------
# Defensive parse fallback
# ---------------------------------------------------------------------------


def test_defensive_parse_fallback(monkeypatch):
    """
    When the LLM returns unstructured text, _parse_llm_response falls back to
    using the whole text as summary with empty action_items. Worker does NOT crash.
    """
    # Direct unit test of _parse_llm_response
    raw = "I just rambled about nothing useful."
    summary, items = main._parse_llm_response(raw)
    assert summary == raw
    assert items == []


def test_defensive_parse_partial_summary(monkeypatch):
    """SUMMARY header present but no ACTION_ITEMS — items should be empty list."""
    raw = "SUMMARY:\nWe discussed the roadmap.\n"
    summary, items = main._parse_llm_response(raw)
    assert "roadmap" in summary
    assert items == []


def test_defensive_parse_full_structure():
    """Well-formed response parses correctly."""
    raw = (
        "SUMMARY:\nThe team aligned on Q3 goals.\n\n"
        "ACTION_ITEMS:\n- Finish API spec\n- Schedule design review"
    )
    summary, items = main._parse_llm_response(raw)
    assert "Q3 goals" in summary
    assert "Finish API spec" in items
    assert "Schedule design review" in items


def test_end_to_end_with_fallback_parse(monkeypatch):
    """
    Integration: gateway returns unparseable text. Worker writes summary with
    full text and empty action_items. No crash, audio.summaries still produced.
    """
    s3 = _make_s3_mock()
    r = _make_redis_mock()
    producer = _make_producer_mock()

    unparseable = "Sorry, I cannot help with that request."
    chat_resp = _chat_completion_response(unparseable, completion_tokens=10)

    fake_http_response = MagicMock()
    fake_http_response.raise_for_status = MagicMock()
    fake_http_response.json.return_value = chat_resp

    http_client = MagicMock()
    http_client.post.return_value = fake_http_response

    main.process_message(
        json.dumps(INBOUND_EVENT).encode(),
        producer,
        s3,
        r,
        http_client,
    )

    # audio.summaries produced (not DLQ)
    producer.produce.assert_called_once()
    assert producer.produce.call_args[0][0] == main.TOPIC_OUT

    # S3 written with empty action_items
    put_kwargs = s3.put_object.call_args[1]
    written = json.loads(put_kwargs["Body"].decode())
    assert written["summary"] == unparseable
    assert written["action_items"] == []


# ---------------------------------------------------------------------------
# Malformed JSON input — commit and move on (no crash, no produce)
# ---------------------------------------------------------------------------


def test_malformed_json(monkeypatch):
    s3 = _make_s3_mock()
    r = _make_redis_mock()
    producer = _make_producer_mock()
    http_client = MagicMock()

    main.process_message(b"not-json{{{", producer, s3, r, http_client)

    producer.produce.assert_not_called()
    http_client.post.assert_not_called()
