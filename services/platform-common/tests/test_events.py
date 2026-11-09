import base64
import json
import uuid

import pytest
from platform_common.events import (
    AudioJobEvent,
    InvalidMessage,
    ResultEvent,
    SummaryEvent,
    TranscriptEvent,
    build_dlq_payload,
    decode_event,
)

JOB = str(uuid.uuid4())


def test_valid_audio_job_and_default_key():
    ev = decode_event(json.dumps({"job_id": JOB, "bytes": 10}).encode(), AudioJobEvent)
    assert ev.audio_key == f"audio/{JOB}"
    assert ev.size_bytes == 10
    assert json.loads(ev.to_json_bytes())["bytes"] == 10


@pytest.mark.parametrize("job_id", [uuid.uuid4().hex, str(uuid.uuid4()), str(uuid.uuid4()).upper()])
@pytest.mark.parametrize("model", [AudioJobEvent, TranscriptEvent, SummaryEvent, ResultEvent])
def test_uuid_validation_preserves_job_identity_across_stages(job_id, model):
    event = decode_event(json.dumps({"job_id": job_id}).encode(), model)
    assert event.job_id == job_id
    assert json.loads(event.to_json_bytes())["job_id"] == job_id


def test_invalid_event_preserves_hex_job_id_for_failed_state():
    job_id = uuid.uuid4().hex
    with pytest.raises(InvalidMessage) as exc:
        decode_event(
            json.dumps({"job_id": job_id, "audio_key": "wrong/key"}).encode(), AudioJobEvent
        )
    assert exc.value.job_id == job_id


@pytest.mark.parametrize(
    "raw",
    [
        b"not json{",
        b"\xff\xfe",
        b"[1,2]",
        json.dumps({"audio_key": "audio/x"}).encode(),
        json.dumps({"job_id": "../etc"}).encode(),
        json.dumps({"job_id": ""}).encode(),
        json.dumps({"job_id": JOB, "audio_key": "speech/x"}).encode(),
        json.dumps({"job_id": JOB, "bytes": -1}).encode(),
        None,
    ],
)
def test_invalid_audio_jobs(raw):
    with pytest.raises(InvalidMessage):
        decode_event(raw, AudioJobEvent)


def test_invalid_message_keeps_job_id_when_valid():
    raw = json.dumps({"job_id": JOB, "transcript_key": 5}).encode()
    with pytest.raises(InvalidMessage) as exc:
        decode_event(raw, TranscriptEvent)
    assert exc.value.job_id == JOB
    assert exc.value.original["transcript_key"] == 5
    assert "transcript_key" in exc.value.reason


def test_other_models():
    assert decode_event(json.dumps({"job_id": JOB}).encode(), SummaryEvent).action_items == []
    out = ResultEvent(job_id=JOB, created_at="2026-01-01T00:00:00+00:00")
    assert json.loads(out.to_json_bytes()) == {
        "job_id": JOB,
        "created_at": "2026-01-01T00:00:00+00:00",
        "speech_key": f"speech/{JOB}",
        "status": "done",
    }


def test_dlq_payload_spreads_original_or_carries_raw():
    p = json.loads(
        build_dlq_payload(
            original={"job_id": JOB, "x": 1},
            raw_value=b"",
            error="boom",
            stage="stt",
            attempts=3,
            error_type="retries_exhausted",
        )
    )
    assert p["job_id"] == JOB and p["x"] == 1 and p["stage"] == "stt" and p["attempts"] == 3
    assert "failed_at" in p
    raw = b"\x00garbage"
    p = json.loads(
        build_dlq_payload(
            original=None, raw_value=raw, error="bad", stage="llm", attempts=0, error_type="invalid"
        )
    )
    assert base64.b64decode(p["raw_b64"]) == raw and p["error_type"] == "invalid"
