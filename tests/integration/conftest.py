"""
Shared setup for cross-service audio pipeline integration tests.

Run via:
  make integration-test
  # or directly:
  cd services/audio-api && uv run --frozen pytest ../../tests/integration/ -q

Service modules are loaded with isolated app.* packages so that each service
resolves its own app/config.py, app/observability.py, etc. without collisions.
"""

import importlib
import importlib.util
import os
import sys
from pathlib import Path

# Must be set before any service is imported — services configure OTel at module level.
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

SERVICES_DIR = Path(__file__).parent.parent.parent / "services"

# Multiple services share metric names (e.g. pipeline_jobs_failed_total). Allow
# duplicate registration so all services can load in a single test process.
import prometheus_client.registry as _prom_registry  # noqa: E402

_original_register = _prom_registry.CollectorRegistry.register


def _lenient_register(self, collector):
    try:
        _original_register(self, collector)
    except ValueError:
        pass  # duplicate name — first registration wins


_prom_registry.CollectorRegistry.register = _lenient_register


def load_service(name: str):
    """Load a service's main.py under an isolated module name.

    Each service owns an app/ package with service-specific config, metrics,
    and observability. We clear app.* from sys.modules before each load so
    Python resolves the correct service's app/ directory via sys.path.

    The loaded module is cached under a unique name (e.g. _svc_stt_worker) so
    subsequent calls return the same module without re-executing it.
    """
    mod_name = f"_svc_{name.replace('-', '_')}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]

    service_dir = str(SERVICES_DIR / name)

    # Clear any app.* cached from a previous service load.
    for key in list(sys.modules.keys()):
        if key == "app" or key.startswith("app."):
            del sys.modules[key]

    sys.path.insert(0, service_dir)
    try:
        spec = importlib.util.spec_from_file_location(
            mod_name, os.path.join(service_dir, "main.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)

    return mod
