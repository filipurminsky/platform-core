# Multi-tenancy — namespace per team

Each team gets an isolated namespace provisioned entirely through GitOps. There
is **no `kubectl create namespace`** anywhere — adding a folder under this
directory is the only onboarding step.

## How it fits together

```
kubernetes/tenants/
├── _template/            # kustomize Component — shared, identical for every tenant
│   ├── network-policies.yaml   # zero-trust: default-deny + explicit allows
│   └── rbac-roles.yaml         # tenant-admin / tenant-viewer Role definitions
├── tiers/                # kustomize Components — ResourceQuota + LimitRange presets
│   ├── small/            #   2 CPU / 4Gi req,  20 pods
│   ├── medium/           #   4 CPU / 8Gi req,  40 pods
│   └── large/            #   8 CPU /16Gi req,  80 pods, 2 GPU
├── team-alpha/           # a tenant = Namespace + RoleBindings + chosen template/tier
│   ├── kustomization.yaml
│   ├── namespace.yaml
│   └── rolebindings.yaml
└── team-bravo/

kubernetes/platform/tenants/applicationset.yaml   # ArgoCD ApplicationSet
```

The **ApplicationSet** (git directory generator) watches `team-*` and renders one
ArgoCD `Application` per folder, named `tenant-<team>`. ArgoCD then syncs each
tenant's namespace, quota, limits, network policies and RBAC.

## What each tenant gets

| Concern | Mechanism |
|---|---|
| **Isolation** | Dedicated `Namespace` with Pod Security Admission labels |
| **Resource caps** | `ResourceQuota` (cpu/mem/pods/pvc/gpu) from the chosen tier |
| **Sane defaults** | `LimitRange` injects default requests/limits + min/max bounds |
| **Network** | `default-deny-all` + explicit allows (DNS, intra-ns, ingress, monitoring, platform) |
| **Access** | `tenant-admin` (workloads, not guardrails) and `tenant-viewer` (read-only, no secrets) Roles, bound to OIDC groups |

## Onboard a new team

1. Copy an existing folder: `cp -r team-alpha team-charlie`
2. In `team-charlie/kustomization.yaml` — set `namespace: tenant-charlie`,
   the `platform.io/tenant: charlie` label, and the desired
   `tiers/{small,medium,large}` component.
3. In `namespace.yaml` — set `name: tenant-charlie` and the PSA level.
4. In `rolebindings.yaml` — point the subjects at the team's OIDC groups.
5. Open a PR. On merge, the ApplicationSet provisions everything.

Validate locally before pushing:

```bash
kustomize build kubernetes/tenants/team-charlie | kubeconform -strict -summary
```

## Design notes

- **Roles are shared, RoleBindings are per-tenant** — the *what they can do*
  (`_template/rbac-roles.yaml`) is identical everywhere; only the *who*
  (`rolebindings.yaml` subjects) changes per team.
- **Teams cannot widen their own limits** — `tenant-admin` has read-only access
  to `ResourceQuota`, `LimitRange` and `NetworkPolicy`. Those are
  platform-owned and only mutable via this repo.
- **`tenant-viewer` cannot read Secrets** — the core API group is enumerated
  explicitly and omits `secrets`.
- **PSA per workload type** — `team-alpha` runs `restricted`; `team-bravo`
  runs `baseline` because GPU/vLLM containers need capabilities `restricted`
  forbids.
