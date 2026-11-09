# ADR-006: Scale-to-zero for vLLM and worker-service

**Decision:** KEDA owns workload replica counts, with `minReplicaCount: 0` for
worker-service and the production vLLM deployment.

**Updated scope:** earlier references to Cluster Autoscaler and a shared
persistent model cache are superseded by Karpenter and per-pod ephemeral model
volumes. Dev pauses real vLLM at zero and routes the gateway to `vllm-mock`.

## Activation and scale-out

- worker-service activates from unread Kafka messages. Its ScaledObject polls
  every 15 seconds and has a 60-second idle cooldown.
- Production vLLM uses gateway request/error metrics for **0→1 activation**:
  vLLM cannot emit its own metrics while no pods exist. Its queue-depth metric
  contributes **1→N scaling**. The ScaledObject polls every 30 seconds and has
  a 300-second idle cooldown.
- Gateway readiness remains a self-check. When upstream capacity is unavailable,
  callers receive a retriable response; the gateway must remain reachable so
  demand can activate vLLM.
- Karpenter can remove unused GPU nodes. Baseline platform nodes and other AWS
  infrastructure remain running and billable.

## Tradeoffs

Each new vLLM pod gets its own generic ephemeral model-cache volume. It can
reuse files during a container restart in that pod, but deleting the pod loses
that cache. One shared RWO EBS claim would prevent replicas on different nodes
from attaching the volume. The startup probe allows ten minutes for model
startup; this is a timeout budget, not a measured cold-start time.

Cold starts depend on model size, storage/network throughput, image pulls, and
GPU availability. Measure them on actual hardware before claiming a latency
or cost improvement. Keeping a warm minimum, baking weights into an image, or
using suitable shared storage are future options with different cost and
operational tradeoffs.

See the [vLLM ScaledObject](../../services/vllm-inference/k8s/base/scaledobject.yaml),
[deployment](../../services/vllm-inference/k8s/base/deployment.yaml), and
[ADR-018](adr-018-just-in-time-node-provisioning-karpenter.md).
