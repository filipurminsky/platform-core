# Crossplane — cloud infrastructure as a Kubernetes self-service API

This platform uses **two IaC tools on purpose**, split by lifecycle:

| Layer | Tool | Owns |
|---|---|---|
| Day-0 foundation (the seed) | **Terraform** | VPC, EKS, IRSA/OIDC, GPU node group, and the IAM role Crossplane assumes |
| Day-2 app-facing infra (self-service) | **Crossplane** | S3 buckets (this slice), provisioned on demand by app teams |

Terraform is kept for the foundation because Crossplane runs *inside* the cluster — it can't bootstrap the very EKS cluster it lives on, and you don't want the VPC/cluster lifecycle coupled to the control plane's reconcile loop. Crossplane is introduced for the app-facing layer because that's where its value is: it turns infrastructure into Kubernetes CRDs, so app teams request a bucket with `kubectl apply` (or a Backstage template) and ArgoCD reconciles it like any other resource — including drift correction Terraform doesn't give you for free.

## What this slice demonstrates

A team self-serves an S3 bucket without touching AWS, IAM, or Terraform:

```
ObjectStorageBucket (namespaced composite resource / XR, team-owned)
        │  apiVersion: platform.io/v1alpha1   (Crossplane v2 — no claim)
        ▼
Composition (platform-owned, Pipeline: function-patch-and-transform)
        │
        ├──▶ connection Secret <spec.writeConnectionSecretToRef.name> (bucket, region)
        │      composed by function-patch-and-transform in the XR's namespace
        ▼
Bucket + Encryption + PublicAccessBlock + Versioning + Lifecycle
  (namespaced MRs, s3.aws.m.upbound.io, in the XR's namespace)
        │ provider-aws-s3 v2 via ClusterProviderConfig `default` (IRSA, no static keys)
        ▼
     AWS S3  (bucket name: platform-core-prod-<namespace>-<name>)
```

- **XRD** (`config/definition.yaml`, `apiextensions.crossplane.io/v2`, `scope: Namespaced`) — the platform API: `region`, `versioning`, retention parameters and the connection-secret name; nothing else exposed.
- **Composition** (`config/composition.yaml`) — renders a *secure-by-default* bucket: AES256 encryption, all public access blocked, versioning, lifecycle expiry. Teams can't opt out of the guardrails. The bucket's AWS name is set deterministically to `platform-core-prod-<namespace>-<name>` so it falls inside the IRSA policy prefix (keep namespace + name ≤ 44 chars — S3 names max out at 63).
- **Provider auth** — `provider-aws-s3` runs as `crossplane-system:provider-aws-s3` with an IRSA role (`module.iam.crossplane_s3_role_arn`) scoped to `s3:::platform-core-<env>-*`. The namespaced MRs reference the cluster-scoped `ClusterProviderConfig/default` (`aws.m.upbound.io`, `source: IRSA`). No access keys anywhere, same as the rest of the platform.

## Crossplane v2 model (what changed from v1)

Crossplane was upgraded from 1.17 to **v2.4** (chart `2.4.3`), the provider to
`provider-aws-s3` **v2.9.0** and `function-patch-and-transform` to **v0.11.2**
(both now pulled from `xpkg.crossplane.io/crossplane-contrib` — Upbound restricted
anonymous pulls of non-latest official providers from `xpkg.upbound.io`).

| v1 (before) | v2 (now) |
|---|---|
| Cluster-scoped `XObjectStorageBucket` + namespaced `ObjectStorageBucket` **claim** (`claimNames`) | One **namespaced** XR `ObjectStorageBucket` (`scope: Namespaced`); claims are deprecated |
| XRD `apiextensions.crossplane.io/v1` | XRD `apiextensions.crossplane.io/v2` |
| Cluster-scoped MRs `s3.aws.upbound.io` | Namespaced MRs `s3.aws.m.upbound.io`, created in the XR's namespace |
| `ProviderConfig` (`aws.upbound.io`) | `ClusterProviderConfig` (`aws.m.upbound.io`); MRs set `providerConfigRef.kind` |
| Native XR/claim connection secrets (`connectionSecretKeys`, `writeConnectionSecretsToNamespace`) | Removed for XRs — function-patch-and-transform composes the Secret, named by the XR's `spec.writeConnectionSecretToRef.name` |
| Single-element blocks are arrays (`versioningConfiguration[0]`) | Objects in the `.m` APIs (`versioningConfiguration.status`, `applyServerSideEncryptionByDefault.sseAlgorithm`) |

The user-facing manifest is unchanged (`kind: ObjectStorageBucket`,
`platform.io/v1alpha1`, same `spec.parameters` and `writeConnectionSecretToRef.name`),
so consumers (`audio-pipeline-conn` → `bucket`/`region` keys in `apps`) keep working.

**Upgrading a live v1 cluster** (the AWS path is authored but has not run on a
live account; if it ever did):
1. Crossplane requires stepping through the latest **1.20.x** before 2.x, with no
   deprecated features in use (native P&T "Resources" mode, ControllerConfig,
   external secret stores) — this repo already uses Pipeline mode +
   DeploymentRuntimeConfig.
2. The new XRD reuses the `objectstoragebuckets.platform.io` name that the v1 XRD
   registered for its claim CRD, so the old XRD must be gone first — and deleting
   claims deletes buckets. To keep data: set `managementPolicies: ["Observe"]`
   (or `deletionPolicy: Orphan`) on the existing v1 Bucket MRs, delete the claims
   + old XRD, sync the v2 config, then let the v2 Bucket adopt the existing bucket
   via its `crossplane.io/external-name`.

## Layout

```
kubernetes/platform/crossplane/
├── applicationset.yaml        # 2 ArgoCD apps: `crossplane` (Helm core) + `crossplane-config`
├── config/                    # synced by crossplane-config (Crossplane CRs only)
│   ├── provider.yaml          # Provider + DeploymentRuntimeConfig (IRSA SA annotation)
│   ├── function.yaml          # function-patch-and-transform
│   ├── providerconfig.yaml    # ClusterProviderConfig (credentials source: IRSA)
│   ├── definition.yaml        # XRD (v2, Namespaced) — the platform S3 API
│   ├── composition.yaml       # Composition — secure bucket implementation
│   └── audio-bucket-claim.yaml # audio-pipeline ObjectStorageBucket XR (apps ns)
└── examples/
    └── bucket-claim.yaml      # sample XR (manual apply — not synced)
```

## AWS-only by design

Both ApplicationSets use a cluster generator scoped to `environment: prod`, so the
slice deploys **only on the AWS/EKS cluster** (`bootstrap.sh --mode=aws`) and is
skipped entirely on a local kind cluster, where provisioning real S3 buckets
makes no sense. See the environment-label mechanism in `CLAUDE.md`.

## Run the demo (AWS mode)

```bash
# 1. Provision the foundation + IRSA role, then bootstrap.
cd terraform/environments/prod && terraform apply && cd -
./scripts/bootstrap.sh --mode=aws

# 2. Wire the provider's IRSA role ARN into the SA annotation.
ROLE=$(cd terraform/environments/prod && terraform output -raw crossplane_s3_role_arn)
#   set eks.amazonaws.com/role-arn: "$ROLE" in
#   kubernetes/platform/crossplane/config/provider.yaml  (commit → ArgoCD syncs)

# 3. Wait for the provider + function to report Healthy.
kubectl get providers.pkg.crossplane.io
kubectl get functions.pkg.crossplane.io

# 4. Self-serve a bucket and watch it provision.
kubectl apply -f kubernetes/platform/crossplane/examples/bucket-claim.yaml
kubectl -n apps get objectstoragebucket worker-artifacts -w
kubectl -n apps get secret worker-artifacts-conn -o jsonpath='{.data.bucket}' | base64 -d
```

Deleting the `ObjectStorageBucket` (`kubectl delete -f …`) deprovisions the
bucket — Crossplane garbage-collects the composed AWS resources.

## Notes & caveats

- **Version pins** (Crossplane chart, provider, function) are set to tested
  releases in the manifests; bump them deliberately and check cross-compatibility.
- **Async bootstrap**: provider/function package installs and the AWS CRDs they
  register are asynchronous, so `crossplane-config`'s first sync can transiently
  fail (ClusterProviderConfig/Composition/XR before their CRDs exist). `selfHeal` + retry
  converge once packages are Healthy — expected, not an error.
- **Extending**: the same pattern generalises to RDS, SQS, ElastiCache, etc.
  Add a provider + an XRD/Composition; the namespaced-XR UX stays identical for teams.
- **MRD activation**: Crossplane v2 only serves managed-resource CRDs that a
  `ManagedResourceActivationPolicy` activates; the chart's default policy
  activates everything (`provider.defaultActivations: ["*"]`). Narrow it to the
  five `*.s3.aws.m.upbound.io` kinds if API-server CRD load matters.
