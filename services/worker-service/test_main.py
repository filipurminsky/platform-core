"""Unit tests for worker-service.

Focuses on the processing contract that carries the most risk: handler
dispatch, deduplication, malformed/unknown messages, and the retry → DLQ path.
No Kafka broker is required — `process_message` takes an injected producer, so
a tiny fake captures what would have been published to the DLQ.
"""

import json
from collections import OrderedDict

import main
import pytest


class FakeProducer:
    """Records produced messages instead of talking to Kafka."""

    def __init__(self):
        self.produced = []

    def produce(self, topic, value=None, key=None, headers=None, on_delivery=None):
        self.produced.append({"topic": topic, "value": value, "key": key, "headers": headers})
        if on_delivery is not None:
            on_delivery(None, None)  # simulate broker ack (no error)

    def flush(self, timeout=None):
        return 0  # 0 messages still unconfirmed


@pytest.fixture
def producer():
    return FakeProducer()


# --- handlers -------------------------------------------------------------


@pytest.mark.parametrize(
    ("operation", "text", "expected"),
    [
        ("uppercase", "hi", "HI"),
        ("lowercase", "HI", "hi"),
        ("reverse", "abc", "cba"),
    ],
)
def test_data_transform_operations(operation, text, expected):
    job = {"type": "data-transform", "payload": {"input": text, "operation": operation}}
    assert main.handle_data_transform(job)["result"] == expected


def test_data_transform_unknown_operation_raises():
    with pytest.raises(ValueError):
        main.handle_data_transform({"payload": {"input": "x", "operation": "nope"}})


def test_ping_handler():
    assert main.handle_ping({})["pong"] is True


# --- process_message ------------------------------------------------------


def _dlq(producer):
    return [json.loads(m["value"]) for m in producer.produced if m["topic"] == main.TOPIC_DLQ]


def test_successful_job_records_id_and_no_dlq(producer):
    seen = OrderedDict()
    raw = json.dumps({"id": "job-1", "type": "ping"}).encode()
    assert main.process_message(raw, producer, seen) is main.Outcome.COMMIT
    assert "job-1" in seen
    assert producer.produced == []


def test_duplicate_id_is_skipped(producer):
    seen = OrderedDict([("job-1", True)])
    raw = json.dumps(
        {"id": "job-1", "type": "data-transform", "payload": {"operation": "bad"}}
    ).encode()
    # A bad op would normally DLQ; dedup must short-circuit before the handler.
    main.process_message(raw, producer, seen)
    assert producer.produced == []


def test_dedup_cache_is_bounded(producer, monkeypatch):
    monkeypatch.setattr(main, "DEDUP_CACHE_SIZE", 2)
    seen = OrderedDict()
    for i in range(3):
        main.process_message(json.dumps({"id": f"j{i}", "type": "ping"}).encode(), producer, seen)
    assert list(seen) == ["j1", "j2"]


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"{not valid json", "invalid JSON"),
        (json.dumps({"type": "ping"}).encode(), "invalid job"),
        (json.dumps({"id": "", "type": "ping"}).encode(), "invalid job"),
        (json.dumps(["not", "an", "object"]).encode(), "JSON object"),
        (json.dumps({"id": "job-2", "type": "does-not-exist"}).encode(), "unknown job type"),
    ],
)
def test_invalid_messages_go_to_dlq(producer, raw, reason):
    main.process_message(raw, producer, OrderedDict(), key=b"k")
    (dlq,) = _dlq(producer)
    assert reason in dlq["error"]
    assert dlq["error_type"] == "invalid"
    assert dlq["attempts"] == 0
    assert producer.produced[0]["key"] == b"k"
    assert "original_job" in dlq or "raw_b64" in dlq


def test_permanent_handler_error_goes_to_dlq_without_retry(producer):
    raw = json.dumps(
        {"id": "job-p", "type": "data-transform", "payload": {"operation": "nope"}}
    ).encode()
    main.process_message(raw, producer, OrderedDict())
    (dlq,) = _dlq(producer)
    assert dlq["attempts"] == 1
    assert dlq["error_type"] == "permanent"
    assert dlq["original_job"]["id"] == "job-p"


def test_transient_handler_failure_goes_to_dlq_after_retries(producer, monkeypatch):
    calls = []

    def always_fails(_job):
        calls.append(1)
        raise ConnectionError("downstream unavailable")

    monkeypatch.setitem(main._HANDLERS, "always-fail", always_fails)

    raw = json.dumps({"id": "job-3", "type": "always-fail"}).encode()
    main.process_message(raw, producer, OrderedDict())

    (dlq,) = _dlq(producer)
    assert producer.produced[0]["topic"] == main.TOPIC_DLQ
    assert len(calls) == main.MAX_RETRIES
    assert dlq["attempts"] == main.MAX_RETRIES
    assert dlq["error_type"] == "retries_exhausted"
    assert "downstream unavailable" in dlq["error"]
    assert dlq["original_job"]["id"] == "job-3"


def test_transient_failure_then_success(producer, monkeypatch):
    calls = []

    def flaky(_job):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("slow")
        return {"ok": True}

    monkeypatch.setitem(main._HANDLERS, "flaky", flaky)
    seen = OrderedDict()
    main.process_message(json.dumps({"id": "job-4", "type": "flaky"}).encode(), producer, seen)
    assert len(calls) == 2
    assert producer.produced == []
    assert "job-4" in seen
