"""Error classification (permanent vs transient) and retry with backoff.

Permanent errors (bad input, missing object, 4xx from an upstream) can never
succeed on retry → the caller dead-letters immediately. Transient errors
(network, timeouts, 5xx, throttling, Kafka delivery) are retried with
exponential back-off + jitter, bounded by BOTH an attempt count and a
wall-clock budget so per-message time stays well under max.poll.interval.ms.

Some transient errors are *budget-only*: they consume wall-clock budget but not
attempts — e.g. an upstream that is cold-starting (HTTP 503 + Retry-After while
vLLM scales from zero). Those are retried until the budget runs out.
"""

from __future__ import annotations

import json
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from pydantic import ValidationError

# HTTP statuses
PERMANENT_HTTP = frozenset({400, 401, 403, 404, 405, 409, 410, 413, 415, 422})
# Upstream unavailable / overloaded / cold — retried against the time budget.
UNAVAILABLE_HTTP = frozenset({429, 502, 503, 504})

# S3 (botocore ClientError) codes
S3_PERMANENT_CODES = frozenset(
    {
        "NoSuchKey",
        "NoSuchBucket",
        "AccessDenied",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
        "InvalidObjectState",
        "InvalidArgument",
        "InvalidRequest",
        "EntityTooLarge",
        "404",
        "403",
        "400",
    }
)
S3_TRANSIENT_CODES = frozenset(
    {
        "SlowDown",
        "Throttling",
        "ThrottlingException",
        "RequestLimitExceeded",
        "RequestTimeout",
        "RequestTimeTooSkewed",
        "InternalError",
        "ServiceUnavailable",
        "503",
        "500",
    }
)


class PermanentError(Exception):
    """Raise to signal a failure that must not be retried (→ DLQ immediately)."""


class TransientError(Exception):
    """Raise to signal a retryable failure, optionally with a server-provided delay."""

    def __init__(self, message: str, *, retry_after: float | None = None, budget_only=False):
        super().__init__(message)
        self.retry_after = retry_after
        self.budget_only = budget_only


@dataclass(frozen=True)
class ErrorClass:
    retryable: bool
    reason: str
    retry_after: float | None = None
    # True → consumes only the time budget, not an attempt (cold start / 429).
    budget_only: bool = False


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse an HTTP Retry-After header (delta-seconds or HTTP-date) to seconds."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def _classify_http(exc) -> ErrorClass | None:
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is optional
        return None
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in UNAVAILABLE_HTTP:
            return ErrorClass(
                True,
                f"http_{status}",
                retry_after=parse_retry_after(exc.response.headers.get("Retry-After")),
                budget_only=True,
            )
        if status in PERMANENT_HTTP or 400 <= status < 500:
            return ErrorClass(False, f"http_{status}")
        return ErrorClass(True, f"http_{status}")  # other 5xx
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout):
        # Nothing listening yet (gateway/upstream starting) — wait it out.
        return ErrorClass(True, "http_connect", budget_only=True)
    if isinstance(exc, httpx.TransportError):  # read/write timeouts, protocol errors
        return ErrorClass(True, "http_transport")
    return None


def _classify_boto(exc) -> ErrorClass | None:
    try:
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:  # pragma: no cover - boto3 is an optional extra
        return None
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {}) or {}
        code = str(err.get("Code", ""))
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0
        if code in S3_TRANSIENT_CODES or status >= 500 or status == 429:
            return ErrorClass(True, f"s3_{code or status}")
        if code in S3_PERMANENT_CODES or 400 <= status < 500:
            return ErrorClass(False, f"s3_{code or status}")
        return ErrorClass(True, f"s3_{code or 'unknown'}")
    if isinstance(exc, BotoCoreError):
        # EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError, ...
        name = type(exc).__name__
        if name in {"NoCredentialsError", "ParamValidationError", "PartialCredentialsError"}:
            return ErrorClass(False, f"boto_{name}")
        return ErrorClass(True, f"boto_{name}")
    return None


def _classify_redis(exc) -> ErrorClass | None:
    try:
        import redis
    except ImportError:  # pragma: no cover - redis is an optional extra
        return None
    if isinstance(exc, redis.ConnectionError | redis.TimeoutError | redis.BusyLoadingError):
        return ErrorClass(True, "redis_unavailable")
    return None


def classify(exc: BaseException) -> ErrorClass:
    """Decide whether ``exc`` is worth retrying."""
    if isinstance(exc, PermanentError):
        return ErrorClass(False, "permanent")
    if isinstance(exc, TransientError):
        return ErrorClass(True, "transient", exc.retry_after, exc.budget_only)
    for specific in (_classify_http, _classify_boto, _classify_redis):
        result = specific(exc)
        if result is not None:
            return result
    try:
        from confluent_kafka import KafkaException

        if isinstance(exc, KafkaException):
            return ErrorClass(True, "kafka")
    except ImportError:  # pragma: no cover
        pass
    # Bad data: decoding/validation/type errors are deterministic.
    if isinstance(
        exc,
        ValidationError
        | json.JSONDecodeError
        | UnicodeDecodeError
        | ValueError
        | KeyError
        | TypeError
        | ImportError
        | NotImplementedError,
    ):
        return ErrorClass(False, type(exc).__name__)
    if isinstance(exc, TimeoutError | ConnectionError):
        return ErrorClass(True, type(exc).__name__)
    # Unknown → retry, bounded by attempts + budget.
    return ErrorClass(True, f"unknown_{type(exc).__name__}")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 2.0
    max_delay: float = 30.0
    budget_seconds: float = 120.0

    @classmethod
    def from_settings(cls, settings, **overrides) -> RetryPolicy:
        values = {
            "max_attempts": settings.max_retries,
            "base_delay": settings.retry_base_delay_seconds,
            "max_delay": settings.retry_max_delay_seconds,
            "budget_seconds": settings.retry_budget_seconds,
        }
        values.update(overrides)
        return cls(**values)

    def backoff(self, retry_number: int) -> float:
        """Exponential back-off with "equal jitter": in [d/2, d], d = base·2^(n-1) ≤ max."""
        ceiling = min(self.max_delay, self.base_delay * (2 ** max(0, retry_number - 1)))
        return ceiling / 2 + random.uniform(0, ceiling / 2)


class ShutdownRequested(Exception):  # noqa: N818 - control flow, not an error
    """A retry wait was interrupted by SIGTERM: stop, don't commit, don't DLQ."""


class RetryExhausted(Exception):
    """Raised by ``call_with_retry`` when giving up. ``__cause__`` is the last error."""

    def __init__(self, last_exc: BaseException, attempts: int, error_class: ErrorClass):
        super().__init__(str(last_exc))
        self.last_exc = last_exc
        self.attempts = attempts
        self.error_class = error_class

    @property
    def permanent(self) -> bool:
        return not self.error_class.retryable


def sleep_with_heartbeat(
    seconds: float,
    heartbeat: Callable[[], None] | None = None,
    *,
    sleep: Callable[[float], Any] | None = None,
    slice_seconds: float = 5.0,
    abort: threading.Event | None = None,
) -> None:
    """Sleep in slices, calling ``heartbeat`` between them so liveness stays green.

    If ``abort`` gets set (SIGTERM) the wait stops with ``ShutdownRequested`` so
    the pod can exit within its termination grace period; the message is left
    uncommitted and is redelivered.
    """
    sleep = sleep or time.sleep
    remaining = seconds
    while remaining > 0:
        if abort is not None and abort.is_set():
            raise ShutdownRequested("shutdown requested during retry back-off")
        step = min(slice_seconds, remaining)
        sleep(step)
        remaining -= step
        if heartbeat is not None:
            heartbeat()
    if abort is not None and abort.is_set():
        raise ShutdownRequested("shutdown requested during retry back-off")


def call_with_retry[T](
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    on_attempt_failed: Callable[[BaseException, int, ErrorClass, float | None], None] | None = None,
    heartbeat: Callable[[], None] | None = None,
    classifier: Callable[[BaseException], ErrorClass] = classify,
    sleep: Callable[[float], Any] | None = None,
    clock: Callable[[], float] | None = None,
    abort: threading.Event | None = None,
) -> T:
    """Run ``fn`` until it succeeds, a permanent error occurs, or the policy gives up.

    ``on_attempt_failed(exc, attempt, error_class, next_delay)`` is called after
    each failure; ``next_delay`` is None when no retry will follow.
    Raises ``RetryExhausted`` (chained from the last error) when giving up.
    """
    clock = clock or time.monotonic
    start = clock()
    attempt = 0
    counted = 0
    while True:
        attempt += 1
        try:
            return fn()
        except Exception as exc:
            ec = classifier(exc)
            delay: float | None = None
            if ec.retryable:
                if not ec.budget_only:
                    counted += 1
                if counted < policy.max_attempts:
                    # Retry-After is a floor, never a reason to hammer faster
                    # than our own back-off.
                    delay = max(ec.retry_after or 0.0, policy.backoff(attempt))
                    if clock() - start + delay > policy.budget_seconds:
                        delay = None  # would blow the budget → give up now
            if on_attempt_failed is not None:
                on_attempt_failed(exc, attempt, ec, delay)
            if delay is None:
                raise RetryExhausted(exc, attempt, ec) from exc
            sleep_with_heartbeat(delay, heartbeat, sleep=sleep, abort=abort)
