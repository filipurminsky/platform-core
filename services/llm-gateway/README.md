# llm-gateway

A thin reverse proxy that sits in front of vLLM (or any OpenAI-compatible API). It provides rate limiting, structured logging, and consolidated metrics for the LLM inference layer.

## Responsibilities

- **Proxying**: Forwards OpenAI-compatible requests (`/v1/*`) to the upstream vLLM service.
- **Rate Limiting**: Implements a per-client-IP sliding-window rate limiter (default 60 RPM).
- **Error Mapping**: Maps upstream connection issues to 502 and timeouts to 504.
- **Headers**: Injects Rate-Limit headers (`X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`).
- **Observability**: Traces end-to-end latency including upstream processing and exports Prometheus metrics.

## Technology Stack

- **Framework**: FastAPI (Python 3.12)
- **HTTP Client**: `httpx` (Async)
- **Package Manager**: [uv](https://github.com/astral-sh/uv)
- **Observability**: `prometheus-client`, OpenTelemetry (OTel), `structlog`

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `ANY`  | `/v1/{path:path}` | Proxied OpenAI-compatible routes |
| `GET`  | `/healthz` | Liveness probe (self-check) |
| `GET`  | `/readyz` | Readiness probe (probes upstream `/health`) |
| `GET`  | `/metrics` | Prometheus metrics |

## Metrics

| Metric Name | Type | Labels | Description |
|-------------|------|--------|-------------|
| `gateway_requests_total` | Counter | `method`, `path`, `status` | Total proxied requests |
| `gateway_request_duration_seconds` | Histogram | `path` | End-to-end latency |
| `gateway_upstream_errors_total` | Counter | - | Connection/timeout errors |
| `gateway_rate_limited_total` | Counter | - | Rejected requests (429) |

## Configuration

| Environment Variable | Default | Description |
|----------------------|---------|-------------|
| `VLLM_BASE_URL` | `http://vllm-inference:8000` | Upstream vLLM service address. |
| `RATE_LIMIT_RPM` | `60` | Requests per minute per client IP. |
| `REQUEST_TIMEOUT_SECONDS`| `120.0` | Max wait time for upstream response. |

## Deployment

- **Scale-to-Zero Integration**: Note that while this gateway is always running (replicas=2), it targets the `vllm-inference` service which is KEDA-managed scale-to-zero. The gateway's readiness probe uses `/healthz` (self) instead of `/readyz` (upstream) to avoid deadlock during scale-up.
- **Topology**: Uses `topologySpreadConstraints` to ensure high availability across nodes.

## Local Development

```bash
# Install dependencies
uv sync

# Run tests (uses a fake upstream)
uv run pytest

# Run locally
uv run uvicorn main:app --host 0.0.0.0 --port 8080
```
