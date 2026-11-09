"""Subprocess running the actual STT handler, stage processor and consumer loop.

Test-only barriers make crashes deterministic without adding fault switches to
production code. Telemetry keys record observed offsets and handler executions.
"""

import os
import sys
import threading
from dataclasses import replace

sys.path.insert(0, "/worker")

import main as stt  # noqa: E402
from confluent_kafka import Consumer  # noqa: E402
from platform_common.consumer_loop import (  # noqa: E402
    install_signal_handlers,
    run_consumer_loop,
)
from platform_common.job_state import make_redis_client  # noqa: E402
from platform_common.kafka_io import make_producer  # noqa: E402
from platform_common.retry import TransientError  # noqa: E402
from platform_common.stage import process_stage_message  # noqa: E402
from platform_common.storage import make_s3_client  # noqa: E402


def main():
    mode = os.environ.get("FAULT_MODE", "none")
    prefix = os.environ["TEST_PREFIX"]
    redis = make_redis_client(os.environ["REDIS_URL"], os.environ["REDIS_PASSWORD"])
    s3 = make_s3_client(region="us-east-1", endpoint_url=os.environ["S3_ENDPOINT_URL"])
    producer = make_producer(stt.settings, "recovery-test")
    consumer = Consumer(
        {
            "bootstrap.servers": os.environ["KAFKA_BOOTSTRAP_SERVERS"],
            "group.id": os.environ["KAFKA_CONSUMER_GROUP"],
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
            "session.timeout.ms": 6000,
            "heartbeat.interval.ms": 1000,
        }
    )
    consumer.subscribe([stt.TOPIC_IN])
    # Short leases keep this fixture fast; production keeps its configured lease.
    cfg = replace(stt.stage_config(), dedup_lease_seconds=5)
    cfg = replace(cfg, policy=replace(cfg.policy, base_delay=30, max_delay=30))
    handler = stt.make_handler(s3)

    def barrier(name):
        redis.set(f"{prefix}:barrier", name, ex=180)
        if threading.Event().wait(120):
            return
        raise RuntimeError("test controller did not interrupt the worker within 120s")

    def handle(event):
        redis.incr(f"{prefix}:handler_calls")
        if mode == "sigterm_retry":
            redis.set(f"{prefix}:barrier", mode, ex=180)
            raise TransientError("injected dependency timeout", retry_after=30)
        result = handler(event)
        if mode == "after_write":
            barrier(mode)
        return result

    def process(msg):
        redis.rpush(f"{prefix}:deliveries", str(msg.offset()))
        result = process_stage_message(
            stt.SPEC,
            cfg,
            raw_value=msg.value(),
            producer=producer,
            redis_client=redis,
            handler=handle,
            headers=msg.headers(),
            key=msg.key(),
        )
        if mode == "before_commit":
            barrier(mode)
        return result

    install_signal_handlers()
    try:
        run_consumer_loop(consumer, process, retry_later_pause_seconds=0.2)
    finally:
        consumer.close()
        producer.flush(5)


if __name__ == "__main__":
    main()
