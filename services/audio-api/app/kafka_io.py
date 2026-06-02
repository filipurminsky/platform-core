"""Kafka wiring: producer config + W3C traceparent header injection.

Mirrors the worker-service `_kafka_config` pattern so SASL/SCRAM is picked up
identically in prod and falls back to plaintext for local dev.
"""

import socket

from opentelemetry.propagators.textmap import Setter

from app import config


class KafkaHeaderSetter(Setter):
    def set(self, carrier, key, value):
        carrier.append((key, value.encode()))


kafka_header_setter = KafkaHeaderSetter()


def _kafka_config(extra: dict | None = None) -> dict:
    cfg = {
        "bootstrap.servers": config.KAFKA_BOOTSTRAP_SERVERS,
        "client.id": f"audio-api-{socket.gethostname()}",
    }
    if config.KAFKA_SASL_USERNAME:
        cfg.update(
            {
                "security.protocol": config.KAFKA_SECURITY_PROTOCOL,
                "sasl.mechanism": "SCRAM-SHA-512",
                "sasl.username": config.KAFKA_SASL_USERNAME,
                "sasl.password": config.KAFKA_SASL_PASSWORD,
            }
        )
    if extra:
        cfg.update(extra)
    return cfg
