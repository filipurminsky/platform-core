# vllm-inference

Kubernetes deployment of [vLLM](https://github.com/vllm-project/vllm), a high-throughput LLM serving engine. This service provides the raw inference power for the platform's summarization tasks.

## Integration

- **Version**: vLLM `v0.32.0` (V1 engine), image `vllm/vllm-openai:v0.32.0-cu129` (CUDA 12.9 build — the default CUDA 13 tag needs NVIDIA driver >= 580). Entrypoint is `vllm serve`.
- **Model**: mistralai/Mistral-7B-Instruct-v0.2 (Production), TinyLlama-1.1B (Development). Must match `LLM_MODEL` in llm-worker.
- **API**: OpenAI-compatible HTTP interface.
- **Gateway**: Never accessed directly by workers; all traffic is routed through the `llm-gateway` for rate limiting and telemetry.

## Deployment Details

### Hardware Requirements
- **Production**: Requires NVIDIA GPUs (A100/L4 recommended).
- **Development**: Patched onto the `vllm/vllm-openai-cpu` image with `bfloat16` (the CUDA image can't run on CPU, and the old `--device cpu` flag no longer exists). Dev's llm-gateway talks to `vllm-mock`, so the dev ScaledObject is paused at 0 replicas (`autoscaling.keda.sh/paused-replicas: "0"`); remove the annotation to try vLLM on CPU locally.

### Storage
- Each replica gets its own 50Gi **generic ephemeral volume** for model weights. A shared
  PVC is deliberately avoided: gp3/EBS is ReadWriteOnce, so one shared claim would
  Multi-Attach-deadlock every replica beyond the first node (KEDA allows up to 4).
  Trade-off: a brand-new pod cold-downloads the model before becoming Ready.

### Autoscaling (KEDA)
Two Prometheus triggers, because `vllm:num_requests_waiting` is exposed by the vLLM
pods themselves and therefore does not exist while the Deployment is at zero replicas:
- **Scale 1→N**: vLLM queue depth (`sum(vllm:num_requests_waiting)` — summed because KEDA rejects multi-series results).
- **Scale 0→1 (activation)**: request demand observed at `llm-gateway`
  (`gateway_requests_total` + `gateway_upstream_errors_total` rates) — the gateway is
  always running, so incoming traffic wakes vLLM from zero.
- **Min Replicas**: 0 (Scales to zero when idle).
- **Max Replicas**: 4.

## Monitoring

Exposes rich metrics via its native `/metrics` endpoint, which are scraped by Prometheus and used for:
- KEDA scaling decisions.
- Dashboards tracking token throughput, TTFT / inter-token latency and KV cache utilization (`vllm:kv_cache_usage_perc` — the V0 `vllm:gpu_cache_usage_perc` no longer exists).
- Failures: V1 has no `vllm:request_failure_total`; errors are `vllm:request_success_total{finished_reason="error"}`.
- Distributed tracing through the `llm-gateway`.

## Kustomize Overlays

- **`k8s/overlays/dev`**: Strips GPU requirements, switches to the CPU image + a small model, uses the `standard` storage class, and pauses KEDA at 0.
- **`k8s/overlays/prod`**: Configures GPU node selectors, large memory limits, and `gp3` storage.
