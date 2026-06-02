# ADR-007: App-of-Apps pattern for GitOps

**Decision:** One root ArgoCD Application points to `kubernetes/platform/`; each subdirectory is an independent Application.

**Reasoning:**
- Platform team controls which Applications exist (by merging to main)
- App teams get autonomy within their Application's source path
- Adding a new platform service = adding a directory + ArgoCD Application manifest, merged via PR with plan review

**Trade-offs:** More ArgoCD objects to manage. ApplicationSets would reduce boilerplate but add abstraction; App-of-Apps chosen for transparency.
