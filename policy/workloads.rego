package main

import rego.v1

# Pod-template guardrails for ALL workload kinds (Deployment, Rollout,
# StatefulSet, DaemonSet) — see workload_kinds in lib.rego. Previously these only
# matched `kind: Deployment`, so the echo-service Rollout (the canary reference
# workload) was silently exempt.

# Every container must declare resource requests.
deny contains msg if {
	some w in workloads
	some c in w.spec.template.spec.containers
	not c.resources.requests
	msg := sprintf("%s '%s': container '%s' is missing resource requests", [w.kind, w.metadata.name, c.name])
}

# Every container must declare resource limits.
deny contains msg if {
	some w in workloads
	some c in w.spec.template.spec.containers
	not c.resources.limits
	msg := sprintf("%s '%s': container '%s' is missing resource limits", [w.kind, w.metadata.name, c.name])
}

# Every container must have a liveness probe.
deny contains msg if {
	some w in workloads
	some c in w.spec.template.spec.containers
	not c.livenessProbe
	msg := sprintf("%s '%s': container '%s' is missing a liveness probe", [w.kind, w.metadata.name, c.name])
}

# Every container must have a readiness probe.
deny contains msg if {
	some w in workloads
	some c in w.spec.template.spec.containers
	not c.readinessProbe
	msg := sprintf("%s '%s': container '%s' is missing a readiness probe", [w.kind, w.metadata.name, c.name])
}

# Containers must not run as root, set either at the container or pod level.
deny contains msg if {
	some w in workloads
	some c in w.spec.template.spec.containers
	c.securityContext.runAsUser == 0
	msg := sprintf("%s '%s': container '%s' must not run as root (runAsUser: 0)", [w.kind, w.metadata.name, c.name])
}

deny contains msg if {
	some w in workloads
	w.spec.template.spec.securityContext.runAsUser == 0
	msg := sprintf("%s '%s': pod must not run as root (runAsUser: 0)", [w.kind, w.metadata.name])
}

# Workloads should carry the app.kubernetes.io/part-of label.
warn contains msg if {
	some w in workloads
	not w.metadata.labels["app.kubernetes.io/part-of"]
	msg := sprintf("%s '%s' is missing the 'app.kubernetes.io/part-of' label", [w.kind, w.metadata.name])
}
