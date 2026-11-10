# Tenant gateway access acceptance

Run against an explicitly selected, deployed **dev** cluster after reconciling
current gateway and tenant policies:

```bash
KUBE_CONTEXT=kind-platform-core-qa \
EVIDENCE_DIR=/tmp/tenant-access-evidence \
python3 scripts/test-tenant-access.py
```

The runner requires the existing `tenant-alpha` and `tenant-bravo` namespaces,
their platform-owned `platform.io/tenant` labels, a Ready gateway, and the dev mock
backend. It reads the configured gateway test key without printing it and mounts
an ephemeral copy only into its tenant client. It creates uniquely named probe
Pods, ServiceAccounts and one Secret, then deletes only those resources after
checking their UIDs. The current gateway image must be available on the nodes.
The operator needs permission to create those probes and read the gateway Secret;
this is an administrative acceptance test, not a tenant credential-discovery flow.

Assertions:

- Same-tenant traffic succeeds; cross-tenant traffic times out in both directions.
- The gateway reaches a live inference-policy probe (positive control).
- A tenant carrying `app=llm-gateway` still cannot reach that same inference
  endpoint: vLLM ingress requires both the apps namespace and the pod label.
- A tenant reaches `/v1/models` with a valid bearer key and receives HTTP 200.
- Missing or invalid keys receive HTTP 401.

The inference probe has the production vLLM policy's selector and port but runs a
small HTTP server. This makes the denial meaningful even though dev vLLM is
paused at zero. The real gateway still uses its configured dev backend. The test
does not start GPU inference or modify app deployments. Credentials remain an
application requirement after NetworkPolicy permits a connection.

`tenant-access.json` records all eight assertions and cleanup errors. A timeout
on the authenticated request is a failure, not successful isolation. Reconcile
policies through the cluster's Git source; a manual candidate apply can be undone
by Argo before the test executes. The test itself does not pause reconciliation
or change policies.
