# ADR-001: ArgoCD over Flux

**Decision:** Use ArgoCD as the GitOps controller.

**Reasoning:**
- ArgoCD's UI gives hiring managers and teammates a visual overview of sync state across all Applications — important for a showcase project and for real-world incident response
- RBAC model maps naturally to platform-team-vs-app-team permissions (project-scoped Applications)
- ArgoCD ApplicationSets enable templating multiple environments from a single definition
- Broader industry adoption in 2024–2025

**Trade-offs:** Flux has a lighter footprint and tighter Helm OCI support; either would work. ArgoCD chosen for visibility.
