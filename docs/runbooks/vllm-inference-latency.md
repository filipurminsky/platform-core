# Runbook: vLLM Inference Latency SLO Breach

**Alert:** `VllmLatencyBreach`
**Severity:** Critical
**Team:** Platform Engineering

---

## Symptoms

- vLLM p90 request latency > 5 seconds, sustained for > 5 minutes
- `vllm:e2e_request_latency_seconds` histogram showing degraded tail latency

---

## Immediate Checks

```bash
# Check vLLM metrics
kubectl port-forward -n apps svc/vllm-inference 8000:8000 &
curl http://localhost:8000/metrics | grep -E "vllm_(e2e|num_requests|gpu_cache)"

# Check GPU utilisation on the node
kubectl exec -n apps deploy/vllm-inference -- nvidia-smi

# Check how many requests are queued vs running
curl http://localhost:8000/metrics | grep vllm_num_requests
```

---

## Root Causes and Remediation

### Cause 1: GPU KV cache saturated

**Signal:** `vllm:gpu_cache_usage_perc` > 90%

Too many concurrent requests; KV cache evictions are causing latency spikes.

**Fix:**
- Reduce `max_num_seqs` in vLLM Helm values (limits concurrent sequences)
- Or scale up vLLM replicas manually: `kubectl scale deployment vllm-inference -n apps --replicas=2`
- If GPU node is not available, check Cluster Autoscaler: `kubectl logs -n kube-system -l app=cluster-autoscaler`

### Cause 2: Request queue growing (KEDA not scaling fast enough)

**Signal:** `vllm:num_requests_waiting` >> 5 and replica count still at 1

The `cooldownPeriod: 300` on the ScaledObject may be holding back scale-out.

**Fix:** Manually trigger scale-up while investigating:

```bash
kubectl scale deployment vllm-inference -n apps --replicas=2
```

Longer-term: reduce `lagThreshold` in the ScaledObject or increase `pollingInterval`.

### Cause 3: Model loaded on CPU instead of GPU

**Signal:** High latency but low GPU utilisation; `nvidia-smi` shows no GPU processes

vLLM may have fallen back to CPU if the GPU node taint/toleration is misconfigured.

```bash
kubectl describe pod -n apps -l app=vllm-inference | grep -A5 Tolerations
kubectl get node -l role=gpu -o jsonpath='{.items[*].status.conditions}'
```

### Cause 4: Cold start after scale-from-zero

vLLM takes 30–120 seconds to load a 7B model. This is expected and mitigated by:
- PVC model cache (avoids re-downloading weights)
- Readiness probe blocking traffic until model is ready

If cold starts are too frequent, increase `cooldownPeriod` to keep the pod warm longer.

---

## Escalation

If latency does not recover within 10 minutes:
1. Redirect traffic to a fallback (e.g., external OpenAI API via llm-gateway config change)
2. Page GPU infra on-call
