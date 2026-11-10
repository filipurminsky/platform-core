# Runbook: image-signature admission failures

Applies to Pod CREATE/UPDATE requests containing images matching
`*.dkr.ecr.*.amazonaws.com/platform-core/*`. The authoritative configuration is
[verify-image-signatures.yaml](../../kubernetes/platform/kyverno-policies/base/verify-image-signatures.yaml),
a CEL `policies.kyverno.io/v1` **ImageValidatingPolicy**. Containers, init
containers, and ephemeral containers are checked. Generated GHCR services do
not match this admission policy; their delivery workflow verifies signatures
at promotion instead.

## Enforcement and dependencies

The current fields are:

```yaml
validationActions: [Deny]
failurePolicy: Fail
webhookConfiguration:
  timeoutSeconds: 30
evaluation:
  background:
    enabled: false
```

`Deny` rejects a failed verification. `Fail` rejects webhook/evaluation errors;
changing it to `Ignore` does not turn an explicit verification denial into an
allow. They are separate controls. Keep both fail-closed. See the official
[ImageValidatingPolicy reference](https://kyverno.io/docs/policy-types/image-validating-policy/)
and [webhook failure semantics](https://kyverno.io/docs/installation/customization/#webhooks).

Verification needs registry access and the signature/trust/transparency material
required by the configured Sigstore path, including `rekor.sigstore.dev`. Do not
assume every request contacts Fulcio, a fixed cache TTL, or offline availability:
this repository has not verified those guarantees for the installed CEL engine.
A signing/admission dependency outage can block new Pods and scale-from-zero;
existing running Pods are not retroactively stopped by this admission policy.

## Diagnose before changing deployment state

Select the affected cluster explicitly:

```bash
export KUBE_CONTEXT=<affected-context>
kubectl --context "$KUBE_CONTEXT" get imagevalidatingpolicy verify-image-signatures -o yaml
kubectl --context "$KUBE_CONTEXT" -n apps get events --field-selector reason=FailedCreate
kubectl --context "$KUBE_CONTEXT" -n apps get replicasets
kubectl --context "$KUBE_CONTEXT" -n kyverno logs deploy/kyverno-admission-controller --since=10m
kubectl --context "$KUBE_CONTEXT" -n argocd get application kyverno-policies
```

Admission may reject a Pod before it exists. Inspect the owning ReplicaSet or
controller's `FailedCreate` event rather than expecting a Pending Pod. Separate
registry authentication, DNS/TLS/network failures, webhook readiness, policy
compilation errors, and actual signature/identity mismatches. Consult
[Sigstore status](https://status.sigstore.dev/) when external failures are implicated.

Verify the **same immutable digest** from a trusted machine with registry access:

```bash
cosign verify \
  --certificate-identity 'https://github.com/filipurminsky/platform-core/.github/workflows/docker-build.yaml@refs/heads/main' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  '<registry>/platform-core/<service>@sha256:<digest>'
```

A successful local verification narrows the investigation but does not prove
cluster reachability, registry credentials, or identical trust configuration.
A failed command is not automatically evidence of tampering: distinguish
transport/authentication errors from an unsigned image or wrong signer. For
missing/incorrect signatures, rebuild and promote through the authorized
workflow. For infrastructure failures, repair the failing registry, networking,
trust distribution, or Kyverno controller before retrying admission.

## Recovery and break-glass boundary

Prefer keeping existing healthy replicas, pausing the affected release, or
rolling back to a previously verified digest while restoring dependencies.
A rollback that creates new Pods is still subject to admission and may also be
blocked during the outage. This runbook does **not** prescribe changing `Fail`
to `Ignore`, switching the entire policy to `Audit`, or deleting webhooks.

An exceptional availability decision needs an explicitly approved, narrowly
scoped, expiring exception with its blast radius and restoration check recorded.
That exception mechanism has not been tested here; do not improvise a global
bypass from this runbook. The repository's desired policy remains fail-closed.

If policy drift is suspected, refresh the **owning Argo CD Application**, not
the ImageValidatingPolicy:

```bash
kubectl --context "$KUBE_CONTEXT" -n argocd annotate application kyverno-policies \
  argocd.argoproj.io/refresh=hard --overwrite
# Refresh updates Argo's comparison; it is not itself a forced sync.
# If reconciliation is paused, use the Argo CLI logged into this same instance:
argocd app sync kyverno-policies
```

After reconciliation, verify `validationActions: [Deny]`, `failurePolicy: Fail`,
policy readiness and the Application's successful operation. Retry a known
signed digest and an intentionally unsigned matching image with controlled
server-side dry runs in the affected environment. Record the actual allow/deny
results and cleanup; a `Synced` label alone does not prove signature enforcement.

## Remaining operational work

Test cold admission with registry/Sigstore connectivity loss, controller restart,
and actual signed/unsigned ECR digests. Evaluate supported trusted-root/bundle
verification or internal trust infrastructure against the pinned Kyverno version
before claiming offline resilience. No live AWS signature-admission or outage
exercise is established by the local development QA.
