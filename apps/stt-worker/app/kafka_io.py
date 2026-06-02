"""Kafka wiring: client construction + W3C trace-context header propagation."""

import socket

from confluent_kafka import Consumer, Producer
from opentelemetry.propagators.textmap import Getter, Setter

from app import config


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
        "client.id": f"stt-worker-{socket.gethostname()}",
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
            "enable.auto.commit": False,  # manual commit only after producing output event
            "max.poll.interval.ms": 300_000,
        }
    )
    consumer = Consumer(cfg)
    consumer.subscribe([config.TOPIC_IN])
    return consumer


def make_producer() -> Producer:
    return Producer(_kafka_config())
