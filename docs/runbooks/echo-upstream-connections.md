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
