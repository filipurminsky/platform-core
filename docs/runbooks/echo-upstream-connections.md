# Echo upstream connection failures

An Envoy `503` with flag `UC` and detail
`upstream_reset_before_response_started{connection_termination}` means the
upstream connection closed before response headers arrived. It does not by
itself establish why the connection closed. Check pod replacement, application
errors, resource pressure, and idle connection timing before assigning a cause.

The echo image explicitly sets Uvicorn's keepalive timeout to five seconds.
The route's [BackendTrafficPolicy](../../services/echo-service/k8s/base/backendtrafficpolicy.yaml)
retires idle upstream connections after two seconds, before the server closes
them. It applies to both stable and canary backends. It does not set a response
deadline, enable retries, or change canary routing weights. See the
[Envoy timeout reference](https://gateway.envoyproxy.io/v1.10/api/extension_types/#httptimeout)
and [Uvicorn settings](https://uvicorn.dev/settings/#timeouts).

## Verify the deployed change

Use an explicit development context and confirm the policy's `Accepted=True`
condition reports the current generation:

```bash
kubectl --context kind-platform-core -n apps get backendtrafficpolicy echo-service -o yaml
kubectl --context kind-platform-core -n apps get pods -l app=echo-service -o wide
```

Run both sustained traffic and the idle-boundary profile through the Gateway.
Use the host port selected during bootstrap; the retained QA cluster uses 18000.
These commands require k6; the same pinned image as `tests/load/k6-job.yaml`
can run the scripts from a read-only bind mount instead.

```bash
TARGET=http://localhost:18000 HOST=echo.platform-core.local k6 run tests/load/echo-load.js
TARGET=http://localhost:18000 HOST=echo.platform-core.local k6 run tests/load/echo-idle.js
```

The idle profile varies bursts and pauses around the five-second server
boundary. It requires every response to be HTTP 200 with an echo payload and
does not retry HTTP errors. The sustained profile retains the canary's SLO
threshold, so inspect its actual failure count as well as its exit status.

Capture the policy status, effective Envoy upstream HTTP idle timeout, image
identity, pod UIDs/restarts, request/error counts, latency, and matching Envoy
and echo logs for the same time range. In Loki, useful filters are:

```logql
{namespace="envoy-gateway-system"} | json | response_code >= 400
{namespace="apps", app="echo-service"} | json | status >= 400
```

A successful finite load run is regression evidence, not a zero-error guarantee.
If comparing a candidate against a baseline, use the same load profile and
record both revisions. Do not disable production policies to reproduce a race.

## Controlled connection-close regression

```bash
make connection-test
```

This opt-in Docker test builds the current echo image and starts a uniquely named
Compose project with no host ports and a 256 MiB combined memory limit. It uses
real Envoy and echo processes. A small TCP proxy deliberately delays forwarding
the backend's connection-close signal by 300 ms, creating a known stale pooled
connection window after Uvicorn's five-second keepalive expires.

The test warms a connection, waits 5.1 seconds, and sends another request without
retrying. It requires three baseline requests with a 3600-second Envoy pool idle
timeout to return **503**, each correlated by its own request marker with Envoy
`UC` and `upstream_reset_before_response_started{connection_termination}`. It then
requires three requests with the repository's shorter pool timeout to return
**200** with the real echo payload. Startup/readiness failures cannot satisfy
these assertions.

The candidate timeout is read from the actual echo `BackendTrafficPolicy`; it
must be positive and shorter than the server's timeout. The runner also checks
the built image command against the current Dockerfile and requires the explicit
five-second backend setting used by this fault window. A policy/image drift
fails with an actionable error instead of silently testing a hardcoded candidate.
If intentionally changing the server timeout, update the controlled fault window
and its assertions together.

The runner prints an evidence directory containing `result.json`, per-cycle HTTP
results, correlated Envoy access logs, rendered Envoy configurations and the echo
image identity. `EVIDENCE_DIR` can select an empty destination; nonempty
directories are rejected to prevent a failed rerun retaining an earlier pass.
The infrastructure
recovery workflow runs this check after the worker/Redis tests and retains the
connection evidence with its other artifacts. Cleanup removes only this run's
containers and network, including on assertion failure.

The local controlled run on 2026-11-10 reproduced all three baseline failures and
passed all three candidate requests, including exact access-log correlation.
This demonstrates the benefit of retiring pooled connections before the backend
closes them **under this injected delayed-FIN fault**. It does not prove the
cause of the earlier six QA responses: subsequent seven-minute baseline and
candidate load runs both had zero failures. It also does not exercise Kubernetes
policy translation, multiple Envoy workers, HTTP/2, or arbitrary network faults;
use the live policy/status and load checks above for deployment evidence.
