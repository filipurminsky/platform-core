# ADR-009: Sign and verify the software supply chain end to end

**Decision:** Application release images built by `docker-build.yaml` is vulnerability-scanned (Trivy), gets an SBOM and max-mode SLSA provenance attestation (BuildKit), and is **signed keyless with cosign** (Fulcio/Rekor, identity bound to the GitHub Actions workflow). That release workflow authenticates to AWS via **GitHub OIDC federation** — no long-lived AWS keys are needed for that workflow. Local Compose uses disposable object-store credentials.

**Reasoning:**
- A platform team's core promise is a *trusted* paved road. Unsigned images with no provenance undermine that; signing + attestation make image origin and contents verifiable.
- Keyless signing avoids long-lived signing keys: the signing identity is the ephemeral workflow OIDC token, logged in the Rekor transparency log.
- OIDC federation (`terraform/modules/github-oidc`) removes the single worst credential-handling smell — long-lived cloud keys in a CI secret — and scopes the IAM role to `ecr:...repository/platform-core/*`.

**Trade-offs:** Sigstore introduces an external dependency (Fulcio/Rekor); for an air-gapped environment you'd self-host or switch to key-pair signing. Accepted for a public, cloud-native showcase.


**Validation boundary:** the release jobs skip when `AWS_OIDC_ROLE_ARN` is not
configured. PR image builds alone therefore do not prove publishing, signing,
or admission. Generated standalone service repositories use a separate GHCR
workflow; the existing ECR admission policy does not match them. See
[ADR-010](adr-010-enforce-policy-at-admission-kyverno.md) and the
[evidence matrix](../evidence.md).
