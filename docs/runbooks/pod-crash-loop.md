# Runbook: Pod Crash Loop

**Alert:** `PodCrashLooping`
**Severity:** Critical
**Team:** Platform Engineering

---

## Symptoms

A pod has restarted > 5 times in the last 15 minutes. Kubernetes is in a CrashLoopBackOff state.

---

## Immediate Checks

```bash
# Identify the crashing pod
kubectl get pods -A | grep -v Running | grep -v Completed

# Get last termination reason
kubectl describe pod <pod-name> -n <namespace> | grep -A10 "Last State"

# Check logs from the previous (crashed) container instance
kubectl logs <pod-name> -n <namespace> --previous
```

---

## Common Causes

| Termination Reason | Likely Cause | Fix |
|---|---|---|
| `OOMKilled` | Memory limit too low | Increase `resources.limits.memory` in the Kustomize overlay |
| `Error` / exit 1 | Application crash on startup | Check logs; likely a bad config or missing dependency |
| `CreateContainerConfigError` | Missing ConfigMap or Secret | Verify referenced resources exist in the namespace |
| `ImagePullBackOff` | Bad image tag or ECR auth failure | Check ECR credentials; verify the image tag exists |

---

## Remediation

```bash
# For OOMKill: patch resource limits and apply
kubectl set resources deployment/<name> -n <namespace> \
  --limits=memory=512Mi --requests=memory=256Mi

# For missing secret: create it manually (dev) or check External Secrets sync (prod)
kubectl get externalsecret -n <namespace>
kubectl describe externalsecret <name> -n <namespace>

# Temporarily increase restart threshold while investigating
kubectl annotate pod <pod-name> -n <namespace> \
  kubectl.kubernetes.io/restartPolicy=OnFailure
```

---

## Escalation

If the pod cannot be stabilised within 15 minutes: scale down to 0 replicas to stop alert noise, investigate offline, then deploy a fix via PR.
