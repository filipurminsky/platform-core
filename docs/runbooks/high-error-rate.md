# Runbook: High Error Rate (SLO Burn Rate)

**Alert:** `HighErrorRate`
**Severity:** Critical
**Team:** Platform Engineering

---

## Symptoms

- HTTP 5xx error rate burn rate > 14× the 0.5% SLO threshold, sustained for 1 hour
- Error budget projected to exhaust within 3 days at current burn rate

---

## Immediate Checks

```bash
# Which service is generating errors?
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
kubectl rollout history deployment/echo-service -n apps

# Rollback
kubectl rollout undo deployment/echo-service -n apps
kubectl rollout status deployment/echo-service -n apps
```

To prevent future bad deployments, verify the CI pipeline runs `helm template | kubeconform` and `kube-linter` on every PR.

### Cause 2: Downstream dependency failure

Check if the error is propagating from an upstream (database, external API, vLLM):

```bash
kubectl exec -n apps deploy/echo-service -- \
  curl -sf http://vllm-inference.apps.svc:8000/health || echo "vLLM unavailable"
```

### Cause 3: Resource exhaustion (OOMKilled)

```bash
kubectl describe pods -n apps -l app=echo-service | grep -A5 OOMKilled
```

**Fix:** Increase memory limits via a Kustomize overlay patch and open a PR.

---

## Error Budget Policy

Per `docs/slo-definitions.md`:
- If error budget > 50% remaining: deployments proceed normally
- If error budget 10–50% remaining: require extra test coverage on PRs
- If error budget < 10% remaining: freeze deployments; platform team approval required
