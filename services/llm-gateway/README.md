# llm-gateway

A thin reverse proxy that sits in front of vLLM (or any OpenAI-compatible API). It provides rate limiting, structured logging, and consolidated metrics for the LLM inference layer.

## Responsibilities

- **Proxying**: Forwards OpenAI-compatible requests (`/v1/*`) to the upstream vLLM service.
- **Authentication**: Bearer API keys on `/v1/*` (`GATEWAY_API_KEYS`). Missing/invalid → `401`. Keys are compared in constant time and never logged (a 16-hex-char SHA-256 prefix, `key_id`, is logged instead). Empty disables auth — allowed in dev only; the gateway refuses to start with `ENVIRONMENT=prod` and no keys. Probes and `/metrics` are unauthenticated.
- **Rate Limiting**: Sliding-window limiter (default 60 RPM) keyed by API key (`key_id`) when auth is on, else by client IP. `X-Forwarded-For` is honoured only when the direct peer is in `TRUSTED_PROXY_CIDRS` (right-most untrusted hop wins), so callers cannot spoof their bucket. Shared across replicas via Redis (single atomic Lua call) when `REDIS_URL` is set; fails open if Redis is down.
- **Request hygiene**: Bodies over `MAX_REQUEST_BYTES` → `413` (Content-Length checked first, cap enforced while reading). Hop-by-hop headers (and any named in `Connection`) plus the client's `Authorization`/`Cookie` are stripped before forwarding.
- **Error Mapping**: Upstream connect failure (vLLM scaled to zero / starting) → `503` + `Retry-After`; timeouts → `504`; other transport errors → `502`. All three increment `gateway_upstream_errors_total` (part of KEDA's 0→1 activation query).
- **Headers**: Injects Rate-Limit headers (`X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`); `429` also carries `Retry-After`.
- **Observability**: Traces end-to-end latency including upstream processing and exports Prometheus metrics.

## Technology Stack

- **Framework**: FastAPI (Python 3.14 in the container image; 3.14+ locally)
- **HTTP Client**: `httpx` (Async)
- **Package Manager**: [uv](https://github.com/astral-sh/uv)
- **Observability**: `prometheus-client`, OpenTelemetry (OTel), `structlog`

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET/POST/DELETE/OPTIONS` | `/v1/{path:path}` | Proxied OpenAI-compatible routes (bearer auth when enabled) |
| `GET`  | `/healthz` | Liveness probe (self-check) |
| `GET`  | `/readyz` | Readiness self-check (does not probe vLLM, which may be scaled to zero) |
| `GET`  | `/metrics` | Prometheus metrics |

## Metrics

| Metric Name | Type | Labels | Description |
|-------------|------|--------|-------------|
| `gateway_requests_total` | Counter | `method`, `path`, `status` | Total proxied requests |
| `gateway_request_duration_seconds` | Histogram | `path` | End-to-end latency |
| `gateway_upstream_errors_total` | Counter | - | Upstream connect (503), timeout (504) and transport (502) errors — **name is load-bearing for KEDA** |
| `gateway_rate_limited_total` | Counter | - | Rejected requests (429) |
| `gateway_rate_limiter_errors_total` | Counter | - | Redis limiter errors (fails open) |
| `gateway_auth_failures_total` | Counter | `reason` (`missing`/`invalid`) | Rejected requests (401) |
| `gateway_request_too_large_total` | Counter | - | Rejected requests (413) |

## Configuration

| Environment Variable | Default | Description |
|----------------------|---------|-------------|
| `VLLM_BASE_URL` | `http://vllm-inference:8000` | Upstream vLLM service address. |
| `RATE_LIMIT_RPM` | `60` | Requests per minute per API key (auth on) or client IP. |
| `REQUEST_TIMEOUT_SECONDS`| `120.0` | Max wait time for upstream response. |
| `ENVIRONMENT` | `dev` | `prod`/`production` makes `GATEWAY_API_KEYS` mandatory. |
| `GATEWAY_API_KEYS` | _(empty)_ | CSV of accepted bearer keys (secret). Empty disables auth (dev only). |
| `TRUSTED_PROXY_CIDRS` | _(empty)_ | CSV of CIDRs (e.g. ingress controller pods) whose `X-Forwarded-For` is honoured. |
| `UPSTREAM_UNAVAILABLE_RETRY_AFTER_SECONDS` | `30` | `Retry-After` on the 503 returned while vLLM is unreachable. |
| `MAX_REQUEST_BYTES` | `1048576` | Max proxied request body (413 above). |
| `REDIS_URL` / `REDIS_PASSWORD` | _(empty)_ | Shared Redis limiter across replicas (prod). |

## Deployment

- **Scale-to-Zero Integration**: Note that while this gateway is always running (replicas=2), it targets the `vllm-inference` service which is KEDA-managed scale-to-zero. Neither probe touches vLLM, so the gateway stays in Service endpoints while vLLM is at zero; its requests (and the 503s they get while vLLM is down) drive KEDA's 0→1 activation.
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

## Container image

The build context is `services/` (shared by all services):

```bash
docker build -f services/llm-gateway/Dockerfile services/
```

Multi-stage: `uv` resolves `uv.lock` into a venv in the builder stage only; the
runtime stage is `python:3.14.10-slim` (pinned by digest) running as UID 10001.
