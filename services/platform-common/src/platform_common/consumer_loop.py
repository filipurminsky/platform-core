"""The shared poll → process → commit loop with graceful SIGTERM drain."""

from __future__ import annotations

import enum
import signal
import threading
from collections.abc import Callable

from confluent_kafka import KafkaError, KafkaException, TopicPartition

from platform_common.health import touch
from platform_common.observability import log
from platform_common.retry import ShutdownRequested

# Process-wide shutdown flag: set by SIGTERM/SIGINT; retry waits observe it.
SHUTDOWN = threading.Event()


class Outcome(enum.Enum):
    """What the loop should do with the offset of a processed message."""

    COMMIT = "commit"  # processed, dead-lettered, or a duplicate → commit
    RETRY_LATER = "retry_later"  # another pod holds the job's dedup lease → re-poll later


def install_signal_handlers(shutdown: threading.Event = SHUTDOWN) -> None:
    def _handle(sig, _frame):
        log.info("shutdown_signal_received", signal=sig)
        shutdown.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def run_consumer_loop(
    consumer,
    process: Callable[[object], Outcome | None],
    *,
    shutdown: threading.Event = SHUTDOWN,
    on_tick: Callable[[], None] | None = None,
    retry_later_pause_seconds: float = 5.0,
    poll_timeout_seconds: float = 1.0,
) -> None:
    """Consume until ``shutdown`` is set. The in-flight message always finishes.

    Offsets are committed synchronously and only after ``process`` returns —
    i.e. after the output event (or DLQ event) was confirmed by the broker.
    A ``RETRY_LATER`` outcome seeks back to the message so it is re-polled after
    a short pause, without committing.
    """
    while not shutdown.is_set():
        msg = consumer.poll(timeout=poll_timeout_seconds)
        touch()
        if on_tick is not None:
            on_tick()
        if msg is None:
            continue
        if msg.error():
            if msg.error().code() == KafkaError._PARTITION_EOF:
                continue
            raise KafkaException(msg.error())

        try:
            outcome = process(msg) or Outcome.COMMIT
        except ShutdownRequested:
            # SIGTERM arrived mid-retry: leave the offset uncommitted so the
            # message is redelivered to another consumer, and exit.
            log.info("shutdown_during_processing", offset=msg.offset())
            return

        if outcome is Outcome.RETRY_LATER:
            try:
                consumer.seek(TopicPartition(msg.topic(), msg.partition(), msg.offset()))
            except KafkaException as exc:
                # Partition revoked meanwhile — the new owner will see the message.
                log.warning("seek_failed", error=str(exc))
            shutdown.wait(retry_later_pause_seconds)
            continue

        try:
            consumer.commit(message=msg, asynchronous=False)
        except KafkaException as exc:
            # Processing completed; a rebalance can still reject the commit. The
            # redelivery is safe because processing is idempotent and deduped.
            log.warning("offset_commit_failed", error=str(exc))
