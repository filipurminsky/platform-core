"""Real single-broker Kafka + authenticated Redis + SeaweedFS stage acceptance.

The actual STT stub handler and shared processing loop run in child processes.
This suite does not claim GPU inference, full audio pipeline, Kubernetes, or
multi-broker failure coverage.
"""

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import boto3
import pytest
import redis
from botocore.config import Config
from confluent_kafka import Consumer, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic


def eventually(check, description, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.1)
    pytest.fail(f"Timed out after {timeout}s: {description}")


@pytest.fixture
def infra():
    prefix = "acceptance-" + uuid.uuid4().hex[:12]
    topics = [f"{prefix}.{suffix}" for suffix in ("input", "output", "dlq")]
    bootstrap = os.environ["KAFKA_BOOTSTRAP_SERVERS"]
    admin = AdminClient({"bootstrap.servers": bootstrap})
    for future in admin.create_topics([NewTopic(t, 1, 1) for t in topics]).values():
        future.result(timeout=30)
    state = redis.from_url(
        os.environ["REDIS_URL"],
        password=os.environ["REDIS_PASSWORD"],
        decode_responses=True,
        socket_timeout=3,
    )
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT_URL"],
        region_name="us-east-1",
        config=Config(connect_timeout=3, read_timeout=3, retries={"max_attempts": 2}),
    )
    s3.create_bucket(Bucket=prefix)
    producer = Producer({"bootstrap.servers": bootstrap, "enable.idempotence": True})
    observer = Consumer(
        {"bootstrap.servers": bootstrap, "group.id": prefix, "enable.auto.commit": False}
    )
    observer.assign([TopicPartition(topics[1], 0, 0), TopicPartition(topics[2], 0, 0)])
    children = []
    logfiles = []

    def start(mode="none"):
        env = {
            **os.environ,
            "KAFKA_CONSUMER_GROUP": prefix,
            "KAFKA_TOPIC_IN": topics[0],
            "KAFKA_TOPIC_OUT": topics[1],
            "KAFKA_DLQ_TOPIC": topics[2],
            "S3_BUCKET": prefix,
            "TEST_PREFIX": prefix,
            "FAULT_MODE": mode,
        }
        log = Path("/evidence") / f"{prefix}-{len(children)}-{mode}.log"
        logfiles.append(log.open("w"))
        child = subprocess.Popen(
            [sys.executable, "/tests/worker.py"],
            env=env,
            stdout=logfiles[-1],
            stderr=subprocess.STDOUT,
        )
        children.append(child)
        return child

    def send(job_id, value):
        failures = []
        producer.produce(
            topics[0],
            key=job_id.encode(),
            value=json.dumps(value).encode(),
            on_delivery=lambda error, _msg: failures.append(error) if error else None,
        )
        assert producer.flush(15) == 0
        assert not failures

    def committed():
        return observer.committed([TopicPartition(topics[0], 0)], timeout=5)[0].offset

    def receive():
        msg = observer.poll(0.2)
        if msg is not None:
            assert not msg.error(), msg.error()
        return msg

    yield {
        "prefix": prefix,
        "topics": topics,
        "redis": state,
        "s3": s3,
        "start": start,
        "send": send,
        "committed": committed,
        "receive": receive,
    }
    for child in children:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
    for log in logfiles:
        log.close()
    observer.close()
    state.close()
    s3.close()
    # The unique compose project owns every topic, bucket and key; runner removes
    # its containers and anonymous volumes even when this fixture fails.


@pytest.mark.parametrize("fault", ["after_write", "before_commit", "sigterm_retry"])
def test_interruption_redelivery_and_duplicate_effects(infra, fault):
    job_id = str(uuid.uuid4())
    prefix = infra["prefix"]
    state, s3 = infra["redis"], infra["s3"]
    created_at = "2026-01-01T00:00:00+00:00"
    event = {"job_id": job_id, "audio_key": f"audio/{job_id}", "created_at": created_at}
    s3.put_object(Bucket=prefix, Key=event["audio_key"], Body=b"RIFF" + b"\0" * 40)
    state.set(
        f"job:{job_id}",
        json.dumps({"status": "queued", "keys": {"audio": event["audio_key"]}}),
        ex=604800,
    )
    child = infra["start"](fault)
    infra["send"](job_id, event)
    eventually(lambda: state.get(f"{prefix}:barrier") == fault, "worker reached crash boundary")
    assert child.poll() is None
    assert infra["committed"]() < 1, "input must remain uncommitted at the fault boundary"
    if fault == "sigterm_retry":
        child.terminate()
        assert child.wait(timeout=10) == 0, "SIGTERM must exit cleanly during retry backoff"
        assert state.get(f"dedup:stt:{job_id}") is None, "retry shutdown must release the claim"
    else:
        if fault == "before_commit":
            assert state.get(f"dedup:stt:{job_id}") == "done"
        else:
            assert state.get(f"dedup:stt:{job_id}").startswith("processing:")
            assert s3.get_object(Bucket=prefix, Key=f"transcripts/{job_id}")["Body"].read()
        child.kill()
        assert child.wait(timeout=5) == -signal.SIGKILL
    assert infra["committed"]() < 1
    recovered = infra["start"]()
    eventually(lambda: infra["committed"]() == 1, "replacement committed redelivered input")
    assert recovered.poll() is None
    assert state.lrange(f"{prefix}:deliveries", 0, -1).count("0") >= 2
    result = eventually(infra["receive"], "confirmed transcript output")
    assert result.topic() == infra["topics"][1], "recovery must not dead-letter the input"
    assert result.key() == job_id.encode()
    output = json.loads(result.value())
    assert output["created_at"] == created_at
    assert output["transcript_key"] == f"transcripts/{job_id}"
    assert output["job_id"] == job_id
    document = json.loads(state.get(f"job:{job_id}"))
    assert document["keys"] == {
        "audio": f"audio/{job_id}",
        "transcript": f"transcripts/{job_id}",
    }
    assert document["status"] == "transcribing"
    assert state.get(f"dedup:stt:{job_id}") == "done"
    assert 604700 <= state.ttl(f"job:{job_id}") <= 604800
    assert 604700 <= state.ttl(f"dedup:stt:{job_id}") <= 604800
    expected_calls = 1 if fault == "before_commit" else 2
    assert int(state.get(f"{prefix}:handler_calls")) == expected_calls
    objects = s3.list_objects_v2(Bucket=prefix, Prefix="transcripts/")["Contents"]
    assert [obj["Key"] for obj in objects] == [f"transcripts/{job_id}"]

    # A second Kafka record carrying the completed job is acknowledged without
    # rerunning the handler or producing another output record.
    infra["send"](job_id, event)
    eventually(lambda: infra["committed"]() == 2, "duplicate input committed")
    assert int(state.get(f"{prefix}:handler_calls")) == expected_calls
    assert infra["receive"]() is None


def test_invalid_event_is_confirmed_to_dlq_before_commit(infra):
    job_id = str(uuid.uuid4())
    infra["start"]()
    infra["send"](job_id, {"job_id": job_id, "audio_key": "outside-contract/key"})
    eventually(lambda: infra["committed"]() == 1, "invalid input committed after DLQ delivery")
    message = eventually(infra["receive"], "DLQ record")
    assert message.topic() == infra["topics"][2]
    assert message.key() == job_id.encode()
    payload = json.loads(message.value())
    assert payload["error_type"] == "invalid"
    assert payload["attempts"] == 0
    assert payload["job_id"] == job_id
    state = json.loads(infra["redis"].get(f"job:{job_id}"))
    assert state["status"] == "failed"
    assert state["error"]["dlq_topic"] == infra["topics"][2]
    assert infra["redis"].get(f"{infra['prefix']}:handler_calls") is None
