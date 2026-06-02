# ADR-014: Dynamic Environment Selection via Cluster Labels

**Decision:** Use ArgoCD ApplicationSet cluster generators and labels (e.g., `environment: prod`) to dynamically select Kustomize overlays.

**Reasoning:**
- **Decoupled definitions** — application manifests are environment-agnostic; the target cluster's metadata determines which overlay is applied.
- **Simplified scaling** — adding a new environment only requires labelling a new cluster/secret rather than editing multiple application manifests.
- **Consistency** — ensures the same ApplicationSet logic can drive multiple environment types (dev/test/prod) without duplication.

**Trade-offs:** Adds an abstraction layer that can make tracing the source of truth slightly more complex for new users.
