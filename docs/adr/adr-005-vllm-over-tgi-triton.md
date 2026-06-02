# ADR-005: vLLM over TGI (Text Generation Inference) or Triton

**Decision:** Use vLLM as the inference engine.

**Reasoning:**
- **OpenAI-compatible API** — zero application code changes to swap from OpenAI to self-hosted; the llm-gateway proxies the same `/v1/chat/completions` endpoint
- **PagedAttention** — vLLM's KV cache management enables higher throughput on the same GPU vs. TGI at comparable model sizes
- **Built-in Prometheus metrics** — `vllm:num_requests_waiting`, `vllm:e2e_request_latency_seconds`, and GPU cache metrics are available out of the box with no exporter sidecar needed
- **KEDA integration** — `vllm:num_requests_waiting` feeds directly into a KEDA Prometheus trigger for scale-to-zero

**Trade-offs:** TGI has better support for speculative decoding and some quantisation modes. Triton is more flexible for custom model serving. vLLM chosen for its developer experience and observability story.
