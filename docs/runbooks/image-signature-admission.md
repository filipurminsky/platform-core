# Runbook: Image-signature admission failures (cosign keyless / sigstore)

**Applies to:** any `*.dkr.ecr.*.amazonaws.com/platform-core/*` Pod. Enforced by
the Kyverno `verify-image-signatures` ClusterPolicy
(`kubernetes/platform/kyverno-policies/base/verify-image-signatures.yaml`).

## What this policy does (and its dependency)

Every admission of one of our images runs keyless cosign verification: Kyverno
checks the image's signature against the **public Fulcio/Rekor** transparency log
(`rekor.sigstore.dev`), requiring the signer identity to be our
`docker-build.yaml` workflow on `refs/heads/main`.

The policy is deliberately **fail-closed**:

```yaml
validationFailureAction: Enforce
failurePolicy: Fail
background: false
webhookTimeoutSeconds: 30
```

This is the right security posture (an unsigned/tampered image is rejected), but
it couples Pod admission to an **external dependency**: if Kyverno cannot reach
sigstore, or sigstore is degraded, verification times out and the Pod is
**rejected**. Steady state is mostly protected — Kyverno caches verified digests
(`imageVerifyCache`, ~60 min TTL), so re-admitting an already-verified digest does
not re-hit Rekor — but **cold paths still depend on sigstore**:

- first rollout of a newly promoted digest,
- KEDA scale-from-zero pulling a digest not in cache,
- node replacement / mass reschedule after the cache TTL,
- cluster or Kyverno restart (cache is in-memory).

## Symptoms

- Pods stuck `Pending`; `kubectl describe pod` shows an admission webhook denial
  from `kyverno` mentioning image verification or a webhook timeout.
- `kubectl -n kyverno logs deploy/kyverno-admission-controller` shows errors
  reaching `rekor.sigstore.dev` / `fulcio.sigstore.dev` (timeout / TLS / DNS).
- Sigstore status is degraded: https://status.sigstore.dev

## Confirm it's sigstore, not a genuinely-bad image

```bash
# Is the digest actually signed by our workflow? (from a machine with egress)
cosign verify \
  --certificate-identity-regexp 'https://github.com/.+/platform-core/.github/workflows/docker-build.yaml@refs/heads/main' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  <repo>@sha256:<digest>
```

- **Verifies OK** but admission fails → it's reachability/availability of
  sigstore (or egress from the nodes). Proceed to break-glass.
- **Does not verify** → the image is genuinely unsigned/tampered. Do **not**
  break glass; rebuild/repromote through `docker-build.yaml` so it gets signed.

## Break-glass (temporary, time-boxed)

Only after confirming the image itself is legitimately signed. This trades the
admission guarantee for availability while sigstore is unreachable.

```bash
# Option A: make the webhook fail-open so admission proceeds if Kyverno can't
# evaluate (least invasive — keeps the rule, drops the hard dependency).
kubectl patch clusterpolicy verify-image-signatures --type merge \
  -p '{"spec":{"failurePolicy":"Ignore"}}'

# Option B: drop to Audit if Fulcio/Rekor are fully down (reports, doesn't block).
kubectl patch clusterpolicy verify-image-signatures --type merge \
  -p '{"spec":{"validationFailureAction":"Audit"}}'
```

**Revert as soon as sigstore recovers** — these are GitOps-managed, so ArgoCD
will also revert the live patch on the next sync; do not commit the relaxed
values to Git.

```bash
kubectl annotate clusterpolicy verify-image-signatures \
  argocd.argoproj.io/refresh=hard --overwrite   # force ArgoCD back to desired state
```

## Permanent hardening (backlog)

- Run a **private Rekor/Fulcio mirror** (or use the cached TUF root + an internal
  transparency log) so verification doesn't depend on public sigstore egress.
- Pre-warm the verify cache on critical paths, or raise `imageVerifyCache` TTL.
- Ensure prod nodes have reliable egress to sigstore (they sit behind the
  per-workload NetworkPolicies; Kyverno runs in `kyverno`, which must reach 443).
