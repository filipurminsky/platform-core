# Runbook: Canary aborted / auto-rollback

**Applies to:** `echo-service` (Argo Rollouts canary). Generalises to any Rollout
using the `echo-service-slo` AnalysisTemplate pattern.

## What happened

A canary deploy was automatically aborted and traffic reverted to the previous
(stable) ReplicaSet. Argo Rollouts does this when an `analysis` step exceeds its
`failureLimit` — i.e. the canary pods breached an SLO query:

- `success-rate` dropped below **99%** (non-5xx ratio),
- `p95-latency-seconds` exceeded **0.5s**, or
- the canary produced no usable request series during the warm-up allowance.

`Degraded` alone does not prove an SLO-triggered abort: image pull failures and
other rollout errors can produce a degraded release. Correlate the AnalysisRun
with the new revision, inspect its failed metric, and verify stable routing.

This is the system working as designed: a bad release was caught before it
reached 100% of traffic.

## Confirm

```bash
kubectl argo rollouts -n apps get rollout echo-service
# Look for: Status: ✖ Degraded  /  Message: RolloutAborted
kubectl argo rollouts -n apps status echo-service

# Which analysis run failed and why:
kubectl -n apps get analysisrun -l rollouts.argoproj.io/rollout=echo-service \
  --sort-by=.metadata.creationTimestamp
kubectl -n apps describe analysisrun <name>   # shows the measured values
```

Cross-check in Grafana / Prometheus:

```promql
# success-rate for the failed canary hash (from the AnalysisRun args)
sum(rate(echo_requests_total{status!~"5..",rollouts_pod_template_hash="<hash>"}[1m]))
/
sum(rate(echo_requests_total{rollouts_pod_template_hash="<hash>"}[1m]))
```

## Decide & act

1. **Confirm stable is healthy** (it should be — traffic already reverted):
   ```bash
   kubectl argo rollouts -n apps get rollout echo-service   # stable weight 100%
   curl --fail -H 'Host: echo.platform-core.local' http://localhost/   # kind; exercise application traffic through Envoy
   kubectl -n apps get httproute echo-service \
     -o jsonpath='{range .spec.rules[0].backendRefs[*]}{.name}={.weight}{"\n"}{end}'   # expect echo-service=100, canary=0
   ```
2. **Find the root cause** in the new revision: image bug, bad config, missing
   dependency, latency regression. Inspect canary pod logs from the aborted run
   if still present, or reproduce in dev.
3. **Fix forward** — push a corrected image/commit. The next sync starts a fresh
   canary; do **not** disable the analysis to force it through.
4. If you must ship urgently and understand the risk, you can promote manually,
   but document why:
   ```bash
   kubectl argo rollouts -n apps promote echo-service          # advance one step
   kubectl argo rollouts -n apps promote echo-service --full   # skip remaining analysis (last resort)
   ```

## If the rollback itself is stuck

```bash
kubectl argo rollouts -n apps get rollout echo-service        # check replica/health
kubectl argo rollouts -n apps undo echo-service               # roll back to a prior revision
kubectl -n apps describe httproute echo-service    # weights + Accepted/ResolvedRefs conditions
kubectl -n argo-rollouts logs deploy/argo-rollouts | grep -i gatewayapi   # plugin errors (RBAC, route not found)
```

## Prevent recurrence

- If the gate fired on a **legitimate** release, the thresholds may be too tight
  for normal variance — tune `successCondition` / `count` / `interval` in
  `services/echo-service/k8s/base/analysistemplate.yaml`, with data.
- Add the failure mode to pre-merge tests (see `tests/`) so CI catches it before
  a canary ever starts.
