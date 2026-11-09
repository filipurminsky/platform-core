# Runbook: vLLM inference latency

**Alert:** `VllmLatencyBreach` — p90 request latency above 5 seconds for 5 minutes.
**Scope:** real vLLM in prod. Dev intentionally uses a mock and pauses vLLM at zero.
The AWS/GPU path has not been verified live.

## Establish the failure boundary

```bash
kubectl -n apps get deploy vllm-inference
kubectl -n apps get pods -l app=vllm-inference -o wide
kubectl -n apps describe scaledobject vllm-inference
kubectl -n apps describe pod -l app=vllm-inference
kubectl -n apps logs deploy/vllm-inference --tail=100
kubectl get nodepool,nodeclaim
```

Pending pods point toward capacity, taints, storage, or quota. Image pull or
startup failures require inspecting pod events and model-download logs before
tuning request concurrency. Inspect Karpenter controller logs and NodeClaim
conditions for GPU provisioning failures; this platform does not use Cluster
Autoscaler for GPU capacity.

When a vLLM pod is ready, port-forward in a separate terminal:

```bash
kubectl -n apps port-forward svc/vllm-inference 8000:8000
```

```bash
curl --fail http://localhost:8000/metrics | grep -E 'vllm:(e2e|num_requests|kv_cache)'
kubectl -n apps exec deploy/vllm-inference -- nvidia-smi
```

## Diagnose and act

| Observation | Investigation / response |
|---|---|
| KV cache ratio near 1 | Check concurrency and context length; evaluate lower `--max-model-len` / `--max-num-seqs` or additional GPU capacity |
| Queue grows while replicas stay fixed | Inspect KEDA/HPA conditions, Prometheus query output, maximum replica limit, and node capacity |
| Deployment remains zero despite demand | Check gateway request/error metrics used for activation; vLLM's own metrics cannot activate a nonexistent pod |
| New pod is downloading/loading | Inspect progress against startup probe budget; measure actual cold-start time |
| GPU unavailable | Compare workload `role=gpu`, tolerations, GPU requests, NodePool requirements, device plugin, and runtime configuration |

The ScaledObject's `cooldownPeriod` controls scale-to-zero after inactivity; it
is not the explanation for delayed 1→N scale-out. Its Prometheus trigger uses
`threshold`, not the Kafka scaler's `lagThreshold`. Make desired replica/trigger
changes through GitOps and account for KEDA ownership before an emergency
manual override.

Model caches are **per-pod generic ephemeral volumes**. A container restart may
reuse the pod's cache, but pod replacement requires downloading again. Do not
mount a shared RWO EBS claim across replicas. A warm minimum, baked weights, or
suitable shared storage each require a measured cost/latency tradeoff.

The gateway does not implement automatic routing to an external model provider.
Do not assume an external API fallback exists. If capacity cannot recover,
communicate the outage, preserve retriable responses and queued work, and use
the worker retry/DLQ runbook to assess affected jobs.

## Confirm recovery

Verify ready GPU pods, declining queue depth, successful authenticated requests
through llm-gateway, and sustained latency recovery. Capture the query window,
model, hardware, and request shape with the incident record. A short successful
request does not establish the long-window availability objective.
