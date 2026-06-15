"""Kafka wiring: client construction + W3C trace-context header propagation."""

import socket

from confluent_kafka import Consumer, KafkaException, Producer
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
        "client.id": f"llm-worker-{socket.gethostname()}",
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
        if config.SSL_CA_LOCATION:
            cfg["ssl.ca.location"] = config.SSL_CA_LOCATION
    if extra:
        cfg.update(extra)
    return cfg


def make_consumer() -> Consumer:
    cfg = _kafka_config(
        {
            "group.id": config.CONSUMER_GROUP,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,  # manual commit only after output produced
            # Budget: 3 retries × LLM_TIMEOUT (120 s) + S3 I/O + backoff ≈ 400 s.
            # Default 300 s would evict the consumer mid-processing, causing a
            # rebalance loop and duplicate work.
            "max.poll.interval.ms": 480_000,
        }
    )
    consumer = Consumer(cfg)
    consumer.subscribe([config.TOPIC_IN])
    return consumer


def make_producer() -> Producer:
    # enable.idempotence makes internal producer retries safe (no duplicate or
    # reordered writes on the output hop; librdkafka then forces acks=all).
    return Producer(_kafka_config({"enable.idempotence": True}))


def produce_confirmed(
    producer: Producer,
    topic: str,
    *,
    value: bytes,
    key: bytes | None = None,
    headers: list[tuple[str, bytes]] | None = None,
    timeout: float = 5.0,
) -> None:
    """Produce one message and block until the broker confirms delivery.

    `flush()` alone is not enough: it only returns the number of messages still
    unconfirmed, and per-message failures surface exclusively via the delivery
    callback. Raising here is what keeps the consume loop from committing the
    input offset for an output event that was never actually written — the
    caller's retry/DLQ path (or, for a DLQ publish, a crash-and-redeliver)
    engages instead. At-least-once on the output hop; the consumers dedup.
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
