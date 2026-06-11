# vllm-inference

Kubernetes deployment of [vLLM](https://github.com/vllm-project/vllm), a high-throughput LLM serving engine. This service provides the raw inference power for the platform's summarization tasks.

## Integration

- **Model**: mistralai/Mistral-7B-Instruct-v0.2 (Production), TinyLlama-1.1B (Development).
- **API**: OpenAI-compatible HTTP interface.
- **Gateway**: Never accessed directly by workers; all traffic is routed through the `llm-gateway` for rate limiting and telemetry.

## Deployment Details

### Hardware Requirements
- **Production**: Requires NVIDIA GPUs (A100/L4 recommended).
- **Development**: Patched to run on CPU using `float32` and `device: cpu` for local testing on Kind/Docker.

### Storage
- Each replica gets its own 50Gi **generic ephemeral volume** for model weights. A shared
  PVC is deliberately avoided: gp3/EBS is ReadWriteOnce, so one shared claim would
  Multi-Attach-deadlock every replica beyond the first node (KEDA allows up to 4).
  Trade-off: a brand-new pod cold-downloads the model before becoming Ready.

### Autoscaling (KEDA)
Two Prometheus triggers, because `vllm:num_requests_waiting` is exposed by the vLLM
pods themselves and therefore does not exist while the Deployment is at zero replicas:
- **Scale 1→N**: vLLM queue depth (`vllm:num_requests_waiting`).
- **Scale 0→1 (activation)**: request demand observed at `llm-gateway`
  (`gateway_requests_total` + `gateway_upstream_errors_total` rates) — the gateway is
  always running, so incoming traffic wakes vLLM from zero.
- **Min Replicas**: 0 (Scales to zero when idle).
- **Max Replicas**: 4.

## Monitoring

Exposes rich metrics via its native `/metrics` endpoint, which are scraped by Prometheus and used for:
- KEDA scaling decisions.
- Dashboards tracking token throughput and KV cache utilization.
- Distributed tracing through the `llm-gateway`.

## Kustomize Overlays

- **`k8s/overlays/dev`**: Strips GPU requirements, switches to a small CPU-only model, and uses the `standard` storage class.
- **`k8s/overlays/prod`**: Configures GPU node selectors, large memory limits, and `gp3` storage.
