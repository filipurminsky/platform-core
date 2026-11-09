# ADR-010: Enforce policy at admission with Kyverno

**Decision:** Complement the CI OPA relationship checks with in-cluster admission
policies. The original `ClusterPolicy` implementation is superseded by the CEL
`ValidatingPolicy` and `ImageValidatingPolicy` types described in
[ADR-024](adr-024-kyverno-cel-policies-and-crossplane-v2.md).

Current boundaries:

- Signature verification denies matching `*.dkr.ecr.*.amazonaws.com/platform-core/*`
  images unless signed by the configured main-branch release workflow identity.
  It checks regular, init, and ephemeral containers and fails closed on errors.
- Tenant pod standards are scoped by the `platform.io/tenant` namespace label.
  They do not impose identical restrictions on every operator namespace.
- `:latest` is audited in dev and denied in prod for the policy's selected
  namespaces; the policy explicitly excludes several system namespaces.
- Upstream images and generated standalone GHCR service images are outside the
  ECR signature match. Generated workflows verify signatures during promotion;
  extending runtime admission requires explicitly reviewing their registry and
  signer identities.

**Reasoning:** CI checks can be bypassed by another admission path. API-server
admission provides a second enforcement point for the scope it actually matches.
Administrators can still alter policies; least privilege and review of policy
changes remain part of the trust model.

**Tradeoffs:** Fail-closed verification can block releases during webhook or
signature-service outages. Verification adds latency and requires correctly
configured registry access. See the [admission runbook](../runbooks/image-signature-admission.md)
for diagnosis and controlled recovery. A rendered policy is not proof of live
admission behavior; retain rejection/acceptance evidence when testing it.
