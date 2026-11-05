"""Generic audio-pipeline stage processor.

Every audio worker (stt / llm / tts) runs the same per-message protocol:

  extract trace context → validate event (invalid → DLQ) → atomic dedup claim
  → observe queue wait → span → in-progress job state → [handler → state →
  produce output, retried per policy] → success (dedup done) | DLQ + failed state

Only the handler (the actual stage work) and a few names differ per stage, so
the protocol lives here once instead of drifting across three copies.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import propagate, trace
from pydantic import BaseModel

from platform_common.consumer_loop import SHUTDOWN, Outcome
from platform_common.dedup import Claim, Deduper
from platform_common.events import (
    InvalidMessage,
    build_dlq_payload,
    decode_event,
    decode_json_object,
)
from platform_common.health import touch
from platform_common.job_state import merge_job_state
from platform_common.kafka_io import kafka_header_getter, kafka_header_setter, produce_confirmed
from platform_common.observability import log
from platform_common.pipeline_metrics import PIPELINE_JOBS_FAILED, observe_queue_wait
from platform_common.retry import ErrorClass, RetryExhausted, RetryPolicy, call_with_retry


@dataclass(frozen=True)
class StageSpec:
    name: str  # contract stage label: "stt" | "llm" | "tts"
    service: str  # "stt-worker" ...
    input_model: type[BaseModel]
    in_progress_status: str  # job-state status written when work starts
    jobs_total: Any  # Counter with a `status` label (success | dlq)
    errors_total: Any | None = None  # optional Counter bumped per failed attempt


@dataclass(frozen=True)
class StageConfig:
    topic_in: str
    topic_out: str
    topic_dlq: str
    job_state_ttl_seconds: int
    dedup_lease_seconds: int
    policy: RetryPolicy


@dataclass
class StageResult:
    """What a handler returns for one successful attempt."""

    out_event: BaseModel  # produced to topic_out (needs .to_json_bytes())
    state_update: dict | None = None  # merged into job:<id> before producing
    log_fields: dict = field(default_factory=dict)


def process_stage_message(
    spec: StageSpec,
    cfg: StageConfig,
    *,
    raw_value: bytes | None,
    producer,
    redis_client,
    handler: Callable[[Any], StageResult],
    headers: list[tuple[str, bytes]] | None = None,
    key: bytes | None = None,
    on_success: Callable[[Any, StageResult], None] | None = None,
    heartbeat: Callable[[], None] = touch,
) -> Outcome:
    context = propagate.extract(headers or [], getter=kafka_header_getter)
    tracer = trace.get_tracer(spec.service)

    def _dead_letter(*, original, error, attempts, error_type, job_id) -> None:
        dlq_headers: list[tuple[str, bytes]] = []
        propagate.inject(dlq_headers, setter=kafka_header_setter)
        # Delivery-confirmed: if the DLQ publish itself fails this raises out —
        # the run loop exits without committing and the message is redelivered.
        produce_confirmed(
            producer,
            cfg.topic_dlq,
            value=build_dlq_payload(
                original=original,
                raw_value=raw_value,
                error=error,
                stage=spec.name,
                attempts=attempts,
                error_type=error_type,
            ),
            key=job_id.encode() if job_id else key,
            headers=dlq_headers,
        )
        merge_job_state(
            redis_client,
            job_id,
            {
                "status": "failed",
                "stage": spec.name,
                "error": {"stage": spec.name, "message": error, "dlq_topic": cfg.topic_dlq},
            },
            cfg.job_state_ttl_seconds,
        )
        spec.jobs_total.labels(status="dlq").inc()
        PIPELINE_JOBS_FAILED.labels(stage=spec.name).inc()

    # 1. Validate — malformed input is dead-lettered, never silently dropped.
    try:
        event = decode_event(raw_value, spec.input_model)
    except InvalidMessage as exc:
        log.error("invalid_message", stage=spec.name, reason=exc.reason, job_id=exc.job_id)
        _dead_letter(
            original=exc.original,
            error=exc.reason,
            attempts=0,
            error_type="invalid",
            job_id=exc.job_id,
        )
        return Outcome.COMMIT

    job_id: str = event.job_id

    # 2. Atomic dedup claim (survives restarts/rebalances; no check-then-act race).
    deduper = Deduper(
        redis_client,
        spec.name,
        lease_seconds=cfg.dedup_lease_seconds,
        done_ttl_seconds=cfg.job_state_ttl_seconds,
    )
    claim = deduper.claim(job_id)
    if claim is Claim.ALREADY_DONE:
        log.info("duplicate_skipped", job_id=job_id)
        return Outcome.COMMIT
    if claim is Claim.IN_PROGRESS:
        log.info("job_claimed_elsewhere", job_id=job_id)
        return Outcome.RETRY_LATER

    try:
        # 3. Queue wait (§7) — once per message, right after dedup.
        observe_queue_wait(spec.name, event.created_at)

        with tracer.start_as_current_span(
            f"{spec.service}.process",
            context=context,
            attributes={
                "messaging.system": "kafka",
                "messaging.destination.name": cfg.topic_in,
                "job.id": job_id,
            },
        ) as span:
            merge_job_state(
                redis_client,
                job_id,
                {"status": spec.in_progress_status, "stage": spec.name},
                cfg.job_state_ttl_seconds,
            )

            def _attempt() -> StageResult:
                result = handler(event)
                if result.state_update:
                    merge_job_state(
                        redis_client, job_id, result.state_update, cfg.job_state_ttl_seconds
                    )
                out_headers: list[tuple[str, bytes]] = []
                propagate.inject(out_headers, setter=kafka_header_setter)
                produce_confirmed(
                    producer,
                    cfg.topic_out,
                    value=result.out_event.to_json_bytes(),
                    key=job_id.encode(),
                    headers=out_headers,
                )
                return result

            def _on_failed(exc: BaseException, attempt: int, ec: ErrorClass, delay) -> None:
                span.record_exception(exc)
                if spec.errors_total is not None:
                    spec.errors_total.inc()
                log.warning(
                    "job_attempt_failed",
                    job_id=job_id,
                    attempt=attempt,
                    error=str(exc),
                    retryable=ec.retryable,
                    reason=ec.reason,
                    retry_in_s=None if delay is None else round(delay, 2),
                )

            try:
                result = call_with_retry(
                    _attempt,
                    cfg.policy,
                    on_attempt_failed=_on_failed,
                    heartbeat=heartbeat,
                    abort=SHUTDOWN,
                )
            except RetryExhausted as exc:
                error_type = "permanent" if exc.permanent else "retries_exhausted"
                _dead_letter(
                    original=decode_json_object(raw_value),  # the event exactly as received
                    error=str(exc.last_exc),
                    attempts=exc.attempts,
                    error_type=error_type,
                    job_id=job_id,
                )
                log.error(
                    "job_sent_to_dlq",
                    job_id=job_id,
                    dlq_topic=cfg.topic_dlq,
                    error=str(exc.last_exc),
                    error_type=error_type,
                    attempts=exc.attempts,
                )
                deduper.mark_done(job_id)
                return Outcome.COMMIT

            spec.jobs_total.labels(status="success").inc()
            if on_success is not None:
                on_success(event, result)
            log.info("job_processed", job_id=job_id, **result.log_fields)
            deduper.mark_done(job_id)
            return Outcome.COMMIT
    except BaseException:
        # Unexpected (e.g. the DLQ publish failed): free the claim so the
        # redelivered message can be re-processed right away.
        deduper.release(job_id)
        raise
