# ADR-003: Kustomize overlays over Helm values files per environment

**Decision:** Use Kustomize to patch manifests across environments; Helm only for packaging reusable charts.

**Reasoning:**
- Kustomize patches are surgical and diff-friendly — a PR that only changes a resource limit is one line, not a buried values file change
- Helm's `values.yaml` inheritance across environments is workable but requires templating complexity (`{{ if eq .Values.env "prod" }}`) that obscures intent
- Kustomize `overlays/dev` and `overlays/prod` make it immediately clear what differs between environments

**Trade-offs:** Kustomize doesn't support secrets generation or Helm hooks natively; External Secrets Operator handles secret rotation, and init containers handle lifecycle hooks.
