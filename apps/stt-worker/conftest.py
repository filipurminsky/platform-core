"""Pytest config: disable OpenTelemetry export so tests don't block trying to
reach a collector. Set before `import main`, which configures tracing at import.
"""

import os

os.environ.setdefault("OTEL_SDK_DISABLED", "true")
