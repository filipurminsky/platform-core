# Runbook: High Error Rate (SLO Burn Rate)

**Alert:** `HighErrorRate`
**Severity:** Critical
**Team:** Platform Engineering

---

## Symptoms

- echo-service 5xx ratio > 14.4× the 0.5% budget in both the 5m and 1h windows, held for 2m
- At sustained 14.4× burn, a full 30-day request budget would be consumed in about 2.1 days; actual remaining budget and traffic matter

---

## Immediate Checks

```bash
# Check resource pressure (this does not identify HTTP errors)
kubectl top pods -A | sort -k3 -rn | head -20

# Check recent pod restarts
kubectl get pods -A | grep -v Running | grep -v Completed

# Inspect error logs for the affected service
kubectl logs -n apps -l app=echo-service --tail=200 | grep -E "ERROR|5[0-9]{2}"

# Check ArgoCD for recent deployments that may have introduced regression
kubectl get applications -n argocd -o wide
```

---

## Root Causes and Remediation

### Cause 1: Bad deployment — rollback

```bash
# Identify the bad deployment
kubectl argo rollouts history echo-service -n apps

# Rollback
kubectl argo rollouts undo echo-service -n apps
kubectl argo rollouts status echo-service -n apps
```

To prevent future bad deployments, verify the CI pipeline runs `helm template | kubeconform` and `kube-linter` on every PR.

### Cause 2: Routing or telemetry mismatch

The current alert measures echo-service, which does not depend on vLLM. Check
the echo HTTPRoute, stable/canary Service selectors, recent AnalysisRuns, and
Prometheus labels before attributing errors to an unrelated backend. Use the
[canary runbook](canary-rollback.md) to verify serving recovery and then reconcile
the desired image in Git; an imperative rollback alone is not a Git revert.

### Cause 3: Resource exhaustion (OOMKilled)

```bash
kubectl describe pods -n apps -l app=echo-service | grep -A5 OOMKilled
```

**Fix:** Increase memory limits via a Kustomize overlay patch and open a PR.

---

## Error Budget Policy

The following is the proposed human operating policy in
[SLO definitions](../slo-definitions.md), not an automated budget gate:
- If error budget > 50% remaining: deployments proceed normally
- If error budget 10–50% remaining: require extra test coverage on PRs
- If error budget < 10% remaining: freeze deployments; platform team approval required
