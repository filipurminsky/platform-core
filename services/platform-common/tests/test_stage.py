import json
import uuid

import pytest
from botocore.exceptions import ClientError
from confluent_kafka import KafkaException
from platform_common.consumer_loop import Outcome
from platform_common.events import AudioJobEvent, TranscriptEvent
from platform_common.retry import RetryPolicy
from platform_common.stage import StageConfig, StageResult, StageSpec, process_stage_message
from prometheus_client import REGISTRY, Counter

JOBS = Counter("teststage_jobs_total", "test", ["status"])
ERRORS = Counter("teststage_errors_total", "test")
SPEC = StageSpec(
    name="stt",
    service="stt-worker",
    input_model=AudioJobEvent,
    in_progress_status="transcribing",
    jobs_total=JOBS,
    errors_total=ERRORS,
)
CFG = StageConfig(
    topic_in="audio.jobs",
    topic_out="audio.transcripts",
    topic_dlq="audio.stt-dlq",
    job_state_ttl_seconds=604800,
    dedup_lease_seconds=900,
    policy=RetryPolicy(max_attempts=3, base_delay=0, max_delay=0, budget_seconds=60),
)


def _ok_handler(event):
    return StageResult(
        out_event=TranscriptEvent(job_id=event.job_id, created_at=event.created_at),
        state_update={"status": "transcribing", "keys": {"transcript": "t"}},
        log_fields={"x": 1},
    )


def _run(raw, producer, redis, handler=_ok_handler, **kw):
    return process_stage_message(
        SPEC, CFG, raw_value=raw, producer=producer, redis_client=redis, handler=handler, **kw
    )


def _event(job_id=None):
    return json.dumps(
        {"job_id": job_id or str(uuid.uuid4()), "created_at": "2026-01-01T00:00:00+00:00"}
    ).encode()


def test_success_produces_sets_state_and_marks_done(producer, fake_redis):
    raw = _event()
    job_id = json.loads(raw)["job_id"]
    assert _run(raw, producer, fake_redis) is Outcome.COMMIT
    (out,) = producer.on("audio.transcripts")
    assert out["job_id"] == job_id and out["created_at"] == "2026-01-01T00:00:00+00:00"
    assert producer.produced[0]["key"] == job_id.encode()
    assert fake_redis.state(job_id)["keys"]["transcript"] == "t"
    assert fake_redis.store[f"dedup:stt:{job_id}"] == "done"
    # duplicate → skipped
    assert _run(raw, producer, fake_redis) is Outcome.COMMIT
    assert len(producer.produced) == 1


@pytest.mark.parametrize("raw", [b"{nope", json.dumps({"no_job_id": 1}).encode()])
def test_invalid_message_goes_to_dlq(producer, fake_redis, raw):
    before = REGISTRY.get_sample_value("pipeline_jobs_failed_total", {"stage": "stt"}) or 0
    assert _run(raw, producer, fake_redis, key=b"k") is Outcome.COMMIT
    (dlq,) = producer.on("audio.stt-dlq")
    assert dlq["error_type"] == "invalid" and dlq["attempts"] == 0 and dlq["stage"] == "stt"
    assert producer.produced[0]["key"] == b"k"
    after = REGISTRY.get_sample_value("pipeline_jobs_failed_total", {"stage": "stt"})
    assert after == before + 1


def test_invalid_with_valid_job_id_marks_failed(producer, fake_redis):
    job_id = str(uuid.uuid4())
    raw = json.dumps({"job_id": job_id, "audio_key": "elsewhere/x"}).encode()
    _run(raw, producer, fake_redis)
    st = fake_redis.state(job_id)
    assert st["status"] == "failed" and st["error"]["dlq_topic"] == "audio.stt-dlq"


def test_permanent_error_dlqs_without_retry(producer, fake_redis):
    calls = []

    def handler(_e):
        calls.append(1)
        raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

    raw = _event()
    _run(raw, producer, fake_redis, handler=handler)
    (dlq,) = producer.on("audio.stt-dlq")
    assert len(calls) == 1
    assert dlq["error_type"] == "permanent" and dlq["attempts"] == 1
    job_id = json.loads(raw)["job_id"]
    assert fake_redis.store[f"dedup:stt:{job_id}"] == "done"


def test_transient_error_retries_then_dlqs(producer, fake_redis):
    calls = []

    def handler(_e):
        calls.append(1)
        raise ConnectionError("s3 down")

    _run(_event(), producer, fake_redis, handler=handler)
    (dlq,) = producer.on("audio.stt-dlq")
    assert len(calls) == 3 and dlq["attempts"] == 3 and dlq["error_type"] == "retries_exhausted"


def test_failed_output_delivery_is_retried(fake_redis):
    from platform_common.testing import FakeProducer

    producer = FakeProducer(fail_topics={"audio.transcripts"})
    _run(_event(), producer, fake_redis)
    assert len([m for m in producer.produced if m["topic"] == "audio.transcripts"]) == 3
    assert len(producer.on("audio.stt-dlq")) == 1


def test_claim_held_elsewhere_retries_later(producer, fake_redis):
    raw = _event()
    job_id = json.loads(raw)["job_id"]
    fake_redis.store[f"dedup:stt:{job_id}"] = "processing:other-pod:abc"
    assert _run(raw, producer, fake_redis) is Outcome.RETRY_LATER
    assert producer.produced == []


def test_dlq_publish_failure_releases_claim_and_raises(fake_redis):
    from platform_common.testing import FakeProducer

    producer = FakeProducer(fail_topics={"audio.stt-dlq"})

    def handler(_e):
        raise ValueError("bad")

    raw = _event()
    with pytest.raises(KafkaException):
        _run(raw, producer, fake_redis, handler=handler)
    assert f"dedup:stt:{json.loads(raw)['job_id']}" not in fake_redis.store
