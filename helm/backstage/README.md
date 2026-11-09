# Backstage portal chart

The stock Backstage image provides the catalog, software templates, GitHub
scaffolder actions, guest authentication and Kubernetes backend. The chart does
not build custom Argo CD or Grafana frontend plugins; their optional proxy
configuration is disabled by default. Use their standalone UIs unless you build
and configure an image containing those plugins.

## Environments and authentication

```bash
helm template backstage helm/backstage -n platform -f helm/backstage/values-dev.yaml
helm template backstage helm/backstage -n platform -f helm/backstage/values-prod.yaml
```

The `backstage` ApplicationSet selects `values-dev.yaml` or `values-prod.yaml`
from the Argo CD cluster Secret's `environment` label. Unlabelled clusters are
excluded. Chart defaults use `environment: prod` and disable guest access.

The compiled application runs with `NODE_ENV=production` even on kind. Backstage's
[guest provider](https://backstage.io/docs/auth/guest/provider/) therefore needs
an explicit `dangerouslyAllowOutsideDevelopment: true` for local sign-in.
Only `values-dev.yaml` enables it. Helm rejects guest access for any environment
other than dev. Local guest users can use the catalog and scaffolder actions;
this convenience setting is unsuitable for a publicly reachable deployment.

Production remains closed to guest sign-in. A real identity provider, sign-in
resolver, permissions policy, production database and frontend sign-in experience
must be configured before treating the portal as production-ready. Health probes
only test process readiness and are not evidence that users can authenticate.

## Kubernetes workload lookup

Catalog components select the existing `app=<service>` pod labels in `apps`;
Kafka selects `strimzi.io/cluster=platform-kafka` in `platform`. An ID annotation
alone would look for a different label and show no workloads. Resource CPU/memory
lookup is disabled because this stack does not install metrics-server; use
Grafana/Prometheus for metrics. If metrics-server is installed, set
`kubernetes.skipMetricsLookup=false`; the chart then grants read-only pod metrics
permissions as well.

## GitHub integration

Public catalog reads work without a GitHub credential, subject to rate limits.
Publishing repositories or registration PRs requires an appropriately scoped
integration credential. Set `github.tokenSecret` to an existing Secret in the
release namespace and `github.tokenKey` to its key. The chart references the
Secret without storing its contents in values or the ConfigMap. A configured but
missing Secret prevents startup rather than silently disabling publishing.
For an organization deployment, prefer a separately configured GitHub App.

Templates are loaded from the configured GitHub `main` URLs, not the local
working tree. Unpublished template changes must not be presented as live portal
verification. The configuration checksum on the Deployment makes chart config
changes restart the pod so subPath-mounted settings are reloaded.

## Migrating from the static Application

Fresh installs require no migration. Existing installations used a static
`Application/backstage`; replacing it with `ApplicationSet/backstage` changes
ownership. Before syncing this change, a platform operator must preserve the
existing child resources when removing the old Application (remove its resource
finalizer and delete only the Application), then allow the ApplicationSet to
create and own the replacement Application. Coordinate this with the root
Application's automatic pruning; do not allow the old Application's finalizer to
cascade-delete the portal during the ownership change. No existing cluster is
modified by rendering this chart.
