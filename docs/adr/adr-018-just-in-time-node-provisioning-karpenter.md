# ADR-018: Just-in-Time Node Provisioning with Karpenter

**Decision:** Use Karpenter for node provisioning in EKS, replacing static Managed Node Groups and Cluster Autoscaler.

**Reasoning:**
- **Efficiency** — Karpenter provisions nodes based on exact pod requirements (e.g., GPU, specific instance types), significantly reducing waste.
- **Speed** — much faster scaling than Cluster Autoscaler as it bypasses the overhead of waiting for node group state updates.
- **Granularity** — allows for heterogeneous clusters with different instance types and purchase models (Spot/On-Demand) mixed dynamically.

**Trade-offs:** Adds another controller to manage and requires specific IAM/tagging configurations.
