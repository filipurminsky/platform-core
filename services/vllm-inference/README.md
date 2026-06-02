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
- Uses a 50Gi PersistentVolumeClaim (`vllm-model-cache`) to store model weights, reducing cold-start times.

### Autoscaling (KEDA)
Autoscales based on the number of waiting requests in the vLLM internal queue:
- **Trigger**: Prometheus metric `vllm:num_requests_waiting`.
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
