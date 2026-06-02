# ADR-004: External Secrets Operator over sealed-secrets or SOPS

**Decision:** Use External Secrets Operator (ESO) pulling from AWS Secrets Manager.

**Reasoning:**
- Secrets never touch Git — not even encrypted. ESO syncs directly from Secrets Manager to Kubernetes Secrets at runtime.
- Rotation is automatic: when a secret rotates in Secrets Manager, ESO re-syncs the Kubernetes Secret within the configured `refreshInterval`
- IRSA ensures ESO's service account has least-privilege access to only the secrets in its environment prefix

**Trade-offs:** Requires AWS Secrets Manager (cost). For local dev, secrets are mounted from `kubectl create secret` commands run by `bootstrap.sh`.
