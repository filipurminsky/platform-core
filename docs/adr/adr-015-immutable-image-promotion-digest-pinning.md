# ADR-015: Immutable Image Promotion via Digest Pinning

**Decision:** Pin application images in production using their unique SHA256 digest rather than mutable tags (like `:latest` or `:v1.0.0`).

**Reasoning:**
- **Guarantee of integrity** — ensures the exact image built, scanned, and verified in CI is what runs in production.
- **Atomic updates** — avoids race conditions where a tag might be updated while a deployment is in progress.
- **Auditability** — the digest is an immutable reference to the exact binary state of the service.

**Trade-offs:** Requires CI automation to update digests in manifests as they are not human-readable.
