"""Kafka wiring: client construction, W3C trace-context header propagation,
and consumer-lag polling.
"""

import socket

from confluent_kafka import Consumer, Producer
from opentelemetry.propagators.textmap import Getter, Setter

from app import config
from app.metrics import CONSUMER_LAG
from app.observability import log


class KafkaHeaderGetter(Getter):
    def get(self, carrier, key):
        values = []
        for header_key, header_value in carrier or []:
            if header_key.lower() == key.lower() and header_value is not None:
                values.append(
                    header_value.decode() if isinstance(header_value, bytes) else str(header_value)
                )
        return values or None

    def keys(self, carrier):
        return [key for key, _value in carrier or []]


class KafkaHeaderSetter(Setter):
    def set(self, carrier, key, value):
        carrier.append((key, value.encode()))


kafka_header_getter = KafkaHeaderGetter()
kafka_header_setter = KafkaHeaderSetter()


def _kafka_config(extra: dict | None = None) -> dict:
    cfg = {
        "bootstrap.servers": config.BOOTSTRAP_SERVERS,
        "client.id": f"worker-service-{socket.gethostname()}",
    }
    if config.SASL_USERNAME:
        cfg.update(
            {
                "security.protocol": config.SECURITY_PROTOCOL,
                "sasl.mechanism": "SCRAM-SHA-512",
                "sasl.username": config.SASL_USERNAME,
                "sasl.password": config.SASL_PASSWORD,
            }
        )
    if extra:
        cfg.update(extra)
    return cfg


def make_consumer() -> Consumer:
    cfg = _kafka_config(
        {
            "group.id": config.CONSUMER_GROUP,
            "auto.offset.reset": "latest",
            "enable.auto.commit": False,  # manual commit only after processing
            "max.poll.interval.ms": 300_000,
        }
    )
    consumer = Consumer(cfg)
    consumer.subscribe([config.TOPIC_JOBS])
    return consumer


def make_producer() -> Producer:
    return Producer(_kafka_config())


def update_lag(consumer: Consumer) -> None:
    """Poll watermark offsets and update the lag gauge for each assigned partition."""
    try:
        for tp in consumer.assignment():
            low, high = consumer.get_watermark_offsets(tp, timeout=1.0, cached=True)
            committed = consumer.committed([tp], timeout=1.0)
            committed_offset = (
                committed[0].offset if committed and committed[0].offset >= 0 else low
            )
            lag = max(0, high - committed_offset)
            CONSUMER_LAG.labels(partition=str(tp.partition)).set(lag)
    except Exception as exc:
        log.warning("lag_poll_failed", error=str(exc))
