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
ObjectStorageBucket claim (team, namespaced)
        │  apiVersion: platform.io/v1alpha1
        ▼
XObjectStorageBucket (composite)  ──▶  Composition (platform-owned)
        │                                   │ function-patch-and-transform
        ▼                                   ▼
connection secret (bucket, region)   Bucket + Encryption + PublicAccessBlock + Versioning
                                            │ provider-aws-s3 (IRSA, no static keys)
                                            ▼
                                         AWS S3
```

- **XRD** (`config/definition.yaml`) — the platform API: a `region` + `versioning` claim surface, nothing else exposed.
- **Composition** (`config/composition.yaml`) — renders a *secure-by-default* bucket: AES256 encryption, all public access blocked, versioning. Teams can't opt out of the guardrails.
- **Provider auth** — `provider-aws-s3` runs as `crossplane-system:provider-aws-s3` with an IRSA role (`module.iam.crossplane_s3_role_arn`) scoped to `s3:::platform-core-<env>-*`. No access keys anywhere, same as the rest of the platform.

## Layout

```
kubernetes/platform/crossplane/
├── applicationset.yaml        # 2 ArgoCD apps: `crossplane` (Helm core) + `crossplane-config`
├── config/                    # synced by crossplane-config (Crossplane CRs only)
│   ├── provider.yaml          # Provider + DeploymentRuntimeConfig (IRSA SA annotation)
│   ├── function.yaml          # function-patch-and-transform
│   ├── providerconfig.yaml    # ProviderConfig (credentials source: IRSA)
│   ├── definition.yaml        # XRD — the platform S3 API
│   └── composition.yaml       # Composition — secure bucket implementation
└── examples/
    └── bucket-claim.yaml      # sample claim (manual apply — not synced)
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

Deleting the claim (`kubectl delete -f …`) deprovisions the bucket — Crossplane
garbage-collects the composed AWS resources.

## Notes & caveats

- **Version pins** (Crossplane chart, provider, function) are set to tested
  releases in the manifests; bump them deliberately and check cross-compatibility.
- **Async bootstrap**: provider/function package installs and the AWS CRDs they
  register are asynchronous, so `crossplane-config`'s first sync can transiently
  fail (ProviderConfig/Composition before their CRDs exist). `selfHeal` + retry
  converge once packages are Healthy — expected, not an error.
- **Extending**: the same pattern generalises to RDS, SQS, ElastiCache, etc.
  Add a provider + an XRD/Composition; the claim UX stays identical for teams.
