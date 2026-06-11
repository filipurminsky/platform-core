"""Tiny stdlib HTTP server exposing /metrics + probes on a background thread."""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.config import METRICS_PORT
from app.observability import log

# GPU TTS inference budget (kokoro cold start included); 1200 s (20 min) catches
# a fully wedged loop while accommodating legitimate slow processing.
_STALE_SECONDS = 1200
_POLL_TS: list[float] = [0.0]


def touch() -> None:
    """Update the poll heartbeat. Call once per consumer.poll() iteration."""
    _POLL_TS[0] = time.monotonic()


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            data = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.end_headers()
            self.wfile.write(data)
        elif self.path in ("/healthz", "/readyz"):
            if time.monotonic() - _POLL_TS[0] > _STALE_SECONDS:
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
        pass  # suppress default access log


def start_metrics_server():
    touch()  # initialise heartbeat so the probe passes during startup
    server = ThreadingHTTPServer(("0.0.0.0", METRICS_PORT), MetricsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("metrics_server_started", port=METRICS_PORT)
