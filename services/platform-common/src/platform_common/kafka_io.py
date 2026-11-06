"""Kafka wiring: client construction, W3C trace-context header propagation,
delivery-confirmed produce, and consumer-lag polling.
"""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING

from confluent_kafka import Consumer, KafkaException, Producer
from opentelemetry.propagators.textmap import Getter, Setter

from platform_common.observability import log

if TYPE_CHECKING:
    from prometheus_client import Gauge

    from platform_common.settings import WorkerSettings


class KafkaHeaderGetter(Getter):
    """Read W3C trace headers from a confluent-kafka header list."""

    def get(self, carrier, key):
        values = []
        for header_key, header_value in carrier or []:
            if header_key.lower() == key.lower() and header_value is not None:
                values.append(
                    header_value.decode() if isinstance(header_value, bytes) else str(header_value)
                )
        return values or None

    def keys(self, carrier):
        return [k for k, _v in carrier or []]


class KafkaHeaderSetter(Setter):
    """Write W3C trace headers into a confluent-kafka header list."""

    def set(self, carrier, key, value):
        carrier.append((key, value.encode()))


kafka_header_getter = KafkaHeaderGetter()
kafka_header_setter = KafkaHeaderSetter()


def client_config(settings: WorkerSettings, client_name: str, extra: dict | None = None) -> dict:
    cfg = {
        "bootstrap.servers": settings.bootstrap_servers,
        "client.id": f"{client_name}-{socket.gethostname()}",
    }
    if settings.sasl_username:
        cfg.update(
            {
                "security.protocol": settings.security_protocol,
                "sasl.mechanism": "SCRAM-SHA-512",
                "sasl.username": settings.sasl_username,
                "sasl.password": settings.sasl_password.get_secret_value(),
            }
        )
        if settings.ssl_ca_location:
            cfg["ssl.ca.location"] = settings.ssl_ca_location
    if extra:
        cfg.update(extra)
    return cfg


def make_consumer(settings: WorkerSettings, client_name: str, topics: list[str]) -> Consumer:
    consumer = Consumer(
        client_config(
            settings,
            client_name,
            {
                "group.id": settings.consumer_group,
                "auto.offset.reset": "earliest",
                "enable.auto.commit": False,  # manual commit only after output/DLQ produced
                # Must cover the worst-case per-message time (validated in settings).
                "max.poll.interval.ms": settings.max_poll_interval_ms,
            },
        )
    )
    consumer.subscribe(topics)
    return consumer


def make_producer(settings: WorkerSettings, client_name: str) -> Producer:
    # enable.idempotence makes internal producer retries safe — no duplicate or
    # reordered writes on the output hop (librdkafka then forces acks=all).
    return Producer(client_config(settings, client_name, {"enable.idempotence": True}))


def produce_confirmed(
    producer: Producer,
    topic: str,
    *,
    value: bytes,
    key: bytes | None = None,
    headers: list[tuple[str, bytes]] | None = None,
    timeout: float = 10.0,
) -> None:
    """Produce one message and block until the broker confirms delivery.

    ``flush()`` alone is not enough: it only returns the number of messages still
    unconfirmed, and per-message failures surface exclusively via the delivery
    callback. Raising here keeps the consume loop from committing the input
    offset for an output event that was never written — the caller's retry/DLQ
    path (or, for a DLQ publish, a crash-and-redeliver) engages instead.
    """
    errors: list[Exception] = []

    def _on_delivery(err, _msg):
        if err is not None:
            errors.append(KafkaException(err))

    producer.produce(topic, value=value, key=key, headers=headers, on_delivery=_on_delivery)
    remaining = producer.flush(timeout=timeout)
    if errors:
        raise errors[0]
    if remaining:
        raise KafkaException(
            f"{remaining} message(s) still unconfirmed after {timeout}s flush to {topic!r}"
        )


def update_lag(consumer: Consumer, gauge: Gauge) -> None:
    """Poll watermark offsets and set ``gauge{partition}`` for each assigned partition."""
    try:
        # Drop series for partitions we no longer own so a rebalance doesn't pin a
        # stale lag value under an old partition label.
        gauge.clear()
        for tp in consumer.assignment():
            low, high = consumer.get_watermark_offsets(tp, timeout=1.0, cached=True)
            committed = consumer.committed([tp], timeout=1.0)
            committed_offset = (
                committed[0].offset if committed and committed[0].offset >= 0 else low
            )
            gauge.labels(partition=str(tp.partition)).set(max(0, high - committed_offset))
    except Exception as exc:
        log.warning("lag_poll_failed", error=str(exc))
