# ADR-012: Co-location of Application Source and Kubernetes Manifests

**Decision:** Store Kubernetes manifests (`k8s/` folder) alongside the application source code in `services/<svc>/`.

**Reasoning:**
- **Single-PR delivery** — code changes and their corresponding infrastructure updates (e.g., environment variables, resource limits) are committed together, ensuring they stay in sync.
- **Developer autonomy** — app teams own their deployment definitions within their service's folder, reducing friction between dev and platform teams.
- **Simplified CI context** — it's immediately clear which manifests belong to which codebase.

**Trade-offs:** Can lead to structural drift across services if not governed by shared policies (Kyverno/OPA).
