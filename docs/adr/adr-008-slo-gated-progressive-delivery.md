# ADR-008: SLO-gated progressive delivery (Argo Rollouts) over ArgoCD auto-sync

**Decision:** Deliver user-facing services (starting with `echo-service`) as Argo Rollouts **canary** deployments whose promotion is gated by an `AnalysisTemplate` querying Prometheus, instead of letting ArgoCD apply the new ReplicaSet at 100% immediately.

**Reasoning:**
- A green CI pipeline does not prove a release is healthy *under real traffic*. The canary shifts 10% → 50% → 100% via nginx traffic routing, pausing to measure the canary pods' success-rate and p95 latency against the same thresholds as our SLOs.
- `failureLimit: 1` means a single breaching sample aborts the rollout and Argo Rollouts automatically reverts to the stable ReplicaSet — the error budget *governs* the deploy rather than just being charted.
- Metrics are scoped to canary pods via `rollouts-pod-template-hash` (copied onto series by the ServiceMonitor's `podTargetLabels`), so the analysis measures only the new version.
- An ArgoCD `AppProject` (`platform-apps`) adds a change-freeze sync window, separating app delivery governance from platform services.

**Trade-offs:** Rollouts add a CRD and controller, and require traffic routing (nginx here). The replicas transformer doesn't understand `Rollout`, so per-env replica/resource values are set with explicit JSON6902 patches. For purely internal/stateless jobs (worker-service) a canary adds little value, so those stay Deployments. Accepted: the safety and the demonstrable "bad deploy auto-reverted by its own SLO" loop are worth the moving parts on the request-serving path.
