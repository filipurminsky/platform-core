"""Tiny stdlib HTTP server exposing /metrics + liveness/readiness probes.

Kept dependency-free (no web framework) since the workers are plain Kafka
consumers. Probe health is a *heartbeat*: the consume loop calls ``touch()`` on
every poll, and long waits inside message processing (retry back-off,
cold-start waits) touch it too. If no heartbeat arrives for ``stale_seconds``
the probes return 503 and the kubelet restarts a genuinely wedged pod.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from platform_common.observability import log


class Heartbeat:
    def __init__(self) -> None:
        self._ts = time.monotonic()

    def touch(self) -> None:
        self._ts = time.monotonic()

    def age(self) -> float:
        return time.monotonic() - self._ts


HEARTBEAT = Heartbeat()


def touch() -> None:
    """Update the process-wide poll heartbeat."""
    HEARTBEAT.touch()


def _make_handler(stale_seconds: float, heartbeat: Heartbeat):
    class MetricsHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/metrics":
                data = generate_latest()
                self.send_response(200)
                self.send_header("Content-Type", CONTENT_TYPE_LATEST)
                self.end_headers()
                self.wfile.write(data)
            elif self.path in ("/healthz", "/readyz"):
                if heartbeat.age() > stale_seconds:
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b'{"status":"unhealthy","reason":"consumer_poll_stale"}')
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"status":"ok"}')
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass  # suppress the default access log; structlog handles logging

    return MetricsHandler


def start_metrics_server(
    port: int,
    stale_seconds: float,
    *,
    host: str = "0.0.0.0",
    heartbeat: Heartbeat = HEARTBEAT,
) -> ThreadingHTTPServer:
    """Start the server on a daemon thread and return it (``.shutdown()`` to stop)."""
    heartbeat.touch()  # initialise so the probes pass during startup
    server = ThreadingHTTPServer((host, port), _make_handler(stale_seconds, heartbeat))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("metrics_server_started", port=server.server_address[1], stale_seconds=stale_seconds)
    return server
