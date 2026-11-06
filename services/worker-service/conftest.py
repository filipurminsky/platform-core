"""Pytest config, applied before `import main` (which configures tracing/settings
at import): disable OpenTelemetry export and make retry back-off instant.
"""

import os

os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("RETRY_BASE_DELAY_SECONDS", "0")
os.environ.setdefault("RETRY_MAX_DELAY_SECONDS", "0")
