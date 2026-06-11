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


def test_successful_job_records_id_and_no_dlq(producer):
    seen = OrderedDict()
    raw = json.dumps({"id": "job-1", "type": "ping"}).encode()
    main.process_message(raw, producer, seen)
    assert "job-1" in seen
    assert producer.produced == []


def test_duplicate_id_is_skipped(producer):
    seen = OrderedDict([("job-1", True)])
    raw = json.dumps(
        {"id": "job-1", "type": "data-transform", "payload": {"operation": "bad"}}
    ).encode()
    # A bad op would normally retry+DLQ; dedup must short-circuit before the handler.
    main.process_message(raw, producer, seen)
    assert producer.produced == []


def test_malformed_json_is_dropped_not_dlqd(producer):
    main.process_message(b"{not valid json", producer, OrderedDict())
    assert producer.produced == []


def test_unknown_job_type_is_dropped_not_dlqd(producer):
    raw = json.dumps({"id": "job-2", "type": "does-not-exist"}).encode()
    main.process_message(raw, producer, OrderedDict())
    assert producer.produced == []


def test_handler_failure_goes_to_dlq_after_retries(producer, monkeypatch):
    # Don't actually sleep through the exponential back-off.
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)

    def always_fails(_job):
        raise RuntimeError("boom")

    monkeypatch.setitem(main._HANDLERS, "always-fail", always_fails)

    raw = json.dumps({"id": "job-3", "type": "always-fail"}).encode()
    main.process_message(raw, producer, OrderedDict())

    assert len(producer.produced) == 1
    dlq = json.loads(producer.produced[0]["value"])
    assert producer.produced[0]["topic"] == main.TOPIC_DLQ
    assert dlq["attempts"] == main.MAX_RETRIES
    assert "boom" in dlq["error"]
    assert dlq["original_job"]["id"] == "job-3"
