# ADR-006: Scale-to-zero for both vLLM and worker-service

**Decision:** Both vLLM and worker-service have `minReplicaCount: 0` in their KEDA ScaledObjects.

**Reasoning:**
- GPU nodes are expensive ($0.50–$1.50/hr for g4dn.xlarge). Scaling to zero and letting Cluster Autoscaler terminate the GPU node when idle eliminates idle cost entirely.
- worker-service has no traffic at night/weekends — running 2 replicas 24/7 wastes resources and makes SLO math misleading (high availability of an idle service).
- KEDA's `activationThreshold` and `cooldownPeriod` control the cold-start behaviour: vLLM has a 300 s cooldown (model loading is slow); worker-service champion has 60 s.

**Trade-offs:** Cold starts. vLLM takes 30–120 s to load a 7B model from a PVC-cached checkpoint. Mitigated by the PVC model cache (avoids re-downloading) and a readiness probe that blocks traffic until the model is loaded.
