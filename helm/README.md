# Helm charts — packaging examples only

These charts are **not deployed by ArgoCD**. The live cluster is driven entirely
by Kustomize overlays under `services/<svc>/k8s/` and `kubernetes/platform/`.

| Chart | Purpose |
|-------|---------|
| `demo-app/` | Generic reusable chart for stateless services — reference for teams that prefer Helm over Kustomize |
| `vllm/` | vLLM packaging example; the kustomize tree at `services/vllm-inference/k8s/` is what actually runs |
| `platform-services/` | Umbrella stub (see `TODO.md §C`); not yet implemented |
| `backstage/` | Backstage Helm wrapper; ArgoCD deploys the upstream chart directly via `kubernetes/platform/backstage/` |

CI lints and renders all four charts with `helm lint` + `helm template | kubeconform`
to catch regressions in the templates, but no chart release or deployment is
produced from this directory.
