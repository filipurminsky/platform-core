# echo-service

A lightweight FastAPI service used to validate platform connectivity, observability wiring, and progressive delivery (canary) gates. It reflects request metadata and handles simple JSON body echoing.

## Features

- **Request Reflection**: Returns headers, path, query parameters, and JSON body in the response.
- **Middleware**: Custom middleware records request count and latency metrics for Prometheus.
- **Distributed Tracing**: Automatically instrumented with OpenTelemetry to trace incoming requests.
- **Canary Validation**: Used as a target for Argo Rollouts canary demos.

## Technology Stack

- **Framework**: FastAPI (Python 3.12)
- **Package Manager**: [uv](https://github.com/astral-sh/uv)
- **Observability**: `prometheus-client`, OpenTelemetry (OTel), `structlog`

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/` | Echoes request metadata and JSON body |
| `GET`  | `/healthz` | Liveness probe |
| `GET`  | `/readyz` | Readiness probe |
| `GET`  | `/metrics` | Prometheus metrics |

## Configuration

| Environment Variable | Default | Description |
|----------------------|---------|-------------|
| `APP_VERSION` | `dev` | Version string shown in echo responses. |
| `OTEL_SERVICE_NAME` | `echo-service` | Service name for tracing. |
| `OTEL_EXPORTER_OTLP_ENDPOINT`| `http://otel-collector...` | OTLP collector endpoint. |

## Deployment & Operations

### Argo Rollouts (Canary)
This service is deployed using a `Rollout` instead of a plain `Deployment`.
- **Strategy**: Canary with steps (10% -> 50% -> 100%).
- **Analysis**: Uses `AnalysisTemplate` to gate rollouts based on Prometheus queries:
    - **Success Rate**: Aborts if non-5xx responses fall below 99%.
    - **Latency**: Aborts if p95 latency exceeds 500ms.

### Kubernetes Resources
- **Base**: `k8s/base/`
- **Dev Overlay**: Scales to 1 replica, reduces resource limits.
- **Prod Overlay**: Scales to 3 replicas, enforces immutable image digests.

## Local Development

```bash
# Install dependencies
uv sync

# Run tests
uv run pytest

# Run locally
uv run uvicorn main:app --host 0.0.0.0 --port 8080
```
