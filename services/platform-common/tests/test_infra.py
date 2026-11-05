import json
import threading
import urllib.error
import urllib.request

import pytest
from confluent_kafka import KafkaException
from platform_common.consumer_loop import Outcome, run_consumer_loop
from platform_common.health import Heartbeat, start_metrics_server
from platform_common.kafka_io import (
    client_config,
    kafka_header_getter,
    kafka_header_setter,
    produce_confirmed,
)
from platform_common.settings import AudioWorkerSettings, WorkerSettings
from pydantic import ValidationError


def test_header_roundtrip():
    carrier = []
    kafka_header_setter.set(carrier, "traceparent", "00-abc-def-01")
    assert kafka_header_getter.get(carrier, "TraceParent") == ["00-abc-def-01"]
    assert kafka_header_getter.keys(carrier) == ["traceparent"]


def test_produce_confirmed_raises_on_delivery_error(producer):
    from platform_common.testing import FakeProducer

    produce_confirmed(producer, "t", value=b"x")
    with pytest.raises(KafkaException):
        produce_confirmed(FakeProducer(fail_topics={"t"}), "t", value=b"x")


def test_client_config_sasl(monkeypatch):
    monkeypatch.setenv("KAFKA_SASL_USERNAME", "u")
    monkeypatch.setenv("KAFKA_SASL_PASSWORD", "p")
    monkeypatch.setenv("KAFKA_SSL_CA_LOCATION", "/ca.crt")
    cfg = client_config(WorkerSettings(), "svc")
    assert cfg["security.protocol"] == "SASL_SSL"
    assert cfg["sasl.password"] == "p" and cfg["ssl.ca.location"] == "/ca.crt"
    assert cfg["client.id"].startswith("svc-")


def test_settings_reject_poll_interval_shorter_than_budget(monkeypatch):
    monkeypatch.setenv("RETRY_BUDGET_SECONDS", "600")
    monkeypatch.setenv("KAFKA_MAX_POLL_INTERVAL_MS", "300000")
    with pytest.raises(ValidationError):
        WorkerSettings()
    monkeypatch.setenv("KAFKA_MAX_POLL_INTERVAL_MS", "900000")
    s = AudioWorkerSettings()
    assert s.effective_dedup_lease_seconds == 960


def test_health_server_heartbeat():
    hb = Heartbeat()
    server = start_metrics_server(0, stale_seconds=60, host="127.0.0.1", heartbeat=hb)
    port = server.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz") as r:
            assert json.loads(r.read())["status"] == "ok"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics") as r:
            assert r.status == 200
        hb._ts -= 120  # pretend the loop wedged
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz")
        assert exc.value.code == 503
    finally:
        server.shutdown()


class _Msg:
    def __init__(self, offset):
        self._offset = offset

    def error(self):
        return None

    def topic(self):
        return "t"

    def partition(self):
        return 0

    def offset(self):
        return self._offset

    def value(self):
        return b"{}"


class _Consumer:
    def __init__(self, msgs, shutdown):
        self.msgs = list(msgs)
        self.commits = []
        self.seeks = []
        self.shutdown = shutdown

    def poll(self, timeout):
        if not self.msgs:
            self.shutdown.set()
            return None
        return self.msgs.pop(0)

    def commit(self, message, asynchronous):
        self.commits.append(message.offset())

    def seek(self, tp):
        self.seeks.append(tp.offset)


def test_consumer_loop_commits_and_seeks_back():
    shutdown = threading.Event()
    consumer = _Consumer([_Msg(1), _Msg(2), None, _Msg(3)], shutdown)
    outcomes = {1: Outcome.COMMIT, 2: Outcome.RETRY_LATER, 3: None}
    run_consumer_loop(
        consumer,
        lambda m: outcomes[m.offset()],
        shutdown=shutdown,
        retry_later_pause_seconds=0,
    )
    assert consumer.commits == [1, 3]
    assert consumer.seeks == [2]


def test_consumer_loop_stops_without_commit_on_shutdown_request():
    from platform_common.retry import ShutdownRequested

    shutdown = threading.Event()
    consumer = _Consumer([_Msg(1), _Msg(2)], shutdown)

    def process(msg):
        if msg.offset() == 2:
            raise ShutdownRequested()
        return Outcome.COMMIT

    run_consumer_loop(consumer, process, shutdown=shutdown)
    assert consumer.commits == [1]
