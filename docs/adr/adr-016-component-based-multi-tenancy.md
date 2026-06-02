# ADR-016: Component-based Multi-tenancy

**Decision:** Use Kustomize Components to share common security and resource policies across team namespaces.

**Reasoning:**
- **DRY policies** — shared NetworkPolicies and RBAC roles are defined once in `tenants/_template/` and applied to all tenants.
- **Flexible tiers** — `small`/`medium`/`large` presets (ResourceQuotas/LimitRanges) are applied as components, allowing easy "t-shirt sizing" for team namespaces.
- **Separation of concerns** — Roles are shared, but RoleBindings are per-tenant, ensuring strict isolation while maintaining a consistent governance model.

**Trade-offs:** Kustomize Components are more abstract than simple bases and require specialized knowledge.
