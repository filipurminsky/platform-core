import json
from datetime import UTC, datetime

import httpx
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from confluent_kafka import KafkaException
from platform_common.retry import (
    PermanentError,
    RetryExhausted,
    RetryPolicy,
    TransientError,
    call_with_retry,
    classify,
    parse_retry_after,
)
from pydantic import BaseModel, ValidationError


def _http_error(status, headers=None):
    req = httpx.Request("POST", "http://gw/v1/chat/completions")
    resp = httpx.Response(status, headers=headers or {}, request=req)
    return httpx.HTTPStatusError("boom", request=req, response=resp)


def _client_error(code, status):
    return ClientError(
        {"Error": {"Code": code, "Message": "x"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "GetObject",
    )


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_http_4xx_is_permanent(status):
    assert classify(_http_error(status)).retryable is False


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_http_unavailable_is_budget_only_transient(status):
    ec = classify(_http_error(status, {"Retry-After": "7"}))
    assert ec.retryable and ec.budget_only and ec.retry_after == 7.0


def test_http_500_and_timeouts_are_transient():
    assert classify(_http_error(500)).retryable
    assert classify(httpx.ReadTimeout("slow")).retryable
    ec = classify(httpx.ConnectError("refused"))
    assert ec.retryable and ec.budget_only


def test_s3_classification():
    assert classify(_client_error("NoSuchKey", 404)).retryable is False
    assert classify(_client_error("AccessDenied", 403)).retryable is False
    assert classify(_client_error("SlowDown", 503)).retryable is True
    assert classify(_client_error("InternalError", 500)).retryable is True
    assert classify(EndpointConnectionError(endpoint_url="http://s3")).retryable is True


def test_misc_classification():
    class M(BaseModel):
        x: int

    with pytest.raises(ValidationError) as ve:
        M.model_validate({"x": "nope"})
    assert classify(ve.value).retryable is False
    with pytest.raises(json.JSONDecodeError) as je:
        json.loads("{")
    assert classify(je.value).retryable is False
    assert classify(ValueError("bad op")).retryable is False
    assert classify(PermanentError("x")).retryable is False
    assert classify(KafkaException("delivery")).retryable is True
    assert classify(ConnectionResetError()).retryable is True
    assert classify(RuntimeError("??")).retryable is True
    ec = classify(TransientError("cold", retry_after=3, budget_only=True))
    assert ec.retryable and ec.budget_only and ec.retry_after == 3


def test_parse_retry_after():
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None
    now = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after("Thu, 05 Nov 2026 12:00:30 GMT", now=now) == 30.0


def test_backoff_is_exponential_with_jitter_and_capped():
    p = RetryPolicy(base_delay=2, max_delay=10)
    for n, ceiling in [(1, 2), (2, 4), (3, 8), (4, 10), (9, 10)]:
        d = p.backoff(n)
        assert ceiling / 2 <= d <= ceiling


def test_succeeds_after_transient_failures(no_sleep):
    calls = []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("flaky")
        return "ok"

    assert call_with_retry(fn, RetryPolicy(max_attempts=3, base_delay=1)) == "ok"
    assert len(calls) == 3
    assert len(no_sleep) > 0


def test_permanent_error_is_not_retried(no_sleep):
    calls = []

    def fn():
        calls.append(1)
        raise PermanentError("bad input")

    with pytest.raises(RetryExhausted) as exc:
        call_with_retry(fn, RetryPolicy(max_attempts=5))
    assert exc.value.permanent and exc.value.attempts == 1 and len(calls) == 1
    assert no_sleep == []


def test_gives_up_after_max_attempts(no_sleep):
    seen = []

    def fn():
        raise ConnectionError("down")

    with pytest.raises(RetryExhausted) as exc:
        call_with_retry(
            fn,
            RetryPolicy(max_attempts=3, base_delay=0.01),
            on_attempt_failed=lambda e, a, ec, d: seen.append((a, d)),
        )
    assert exc.value.attempts == 3 and not exc.value.permanent
    assert seen[-1][1] is None and all(d is not None for _, d in seen[:-1])


def test_budget_only_errors_retry_until_budget_honouring_retry_after():
    clock = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        clock[0] += s

    def fn():
        raise TransientError("cold start", retry_after=20, budget_only=True)

    beats = []
    with pytest.raises(RetryExhausted) as exc:
        call_with_retry(
            fn,
            RetryPolicy(max_attempts=2, base_delay=1, max_delay=5, budget_seconds=100),
            sleep=sleep,
            clock=lambda: clock[0],
            heartbeat=lambda: beats.append(1),
        )
    # Not limited by max_attempts=2; stopped by the 100 s budget; slept in ≤5 s slices.
    assert exc.value.attempts == 6  # 5 waits of 20 s fill the 100 s budget
    assert clock[0] <= 100
    assert max(slept) <= 5.0
    assert len(beats) == len(slept)


def test_shutdown_interrupts_backoff():
    import threading

    from platform_common.retry import ShutdownRequested

    abort = threading.Event()

    def fn():
        abort.set()  # SIGTERM arrives while the first attempt fails
        raise ConnectionError("down")

    with pytest.raises(ShutdownRequested):
        call_with_retry(fn, RetryPolicy(base_delay=10), sleep=lambda s: None, abort=abort)
