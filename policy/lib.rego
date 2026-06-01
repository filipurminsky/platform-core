package main

import rego.v1

# Shared helpers. These policies are designed to run with `conftest test --combine`,
# so the whole rendered overlay is available as one set and we can check
# RELATIONSHIPS between objects (e.g. does a NetworkPolicy actually select a
# workload's pods?), not just per-document shape.

# All parsed manifests, regardless of whether conftest ran with --combine
# (input = [{path, contents}, ...]) or per-document (input = <doc>).
manifests contains m if {
	is_array(input)
	some doc in input
	m := doc.contents
}

manifests contains input if {
	not is_array(input)
}

# Workload kinds whose pod templates we govern. Argo Rollout is included
# deliberately: echo-service is a Rollout, not a Deployment, so Deployment-only
# checks would miss the most important app path.
workload_kinds := {"Deployment", "Rollout", "StatefulSet", "DaemonSet"}

workloads contains w if {
	some m in manifests
	workload_kinds[m.kind]
	w := m
}

# Deployment/Rollout/StatefulSet/DaemonSet all carry the pod template at the
# same path, so one accessor covers them.
pod_labels(w) := object.get(w.spec.template.metadata, "labels", {})

# Namespaces that must be network-segmented (zero-trust): the app namespace and
# every tenant namespace.
requires_netpol(ns) if ns == "apps"

requires_netpol(ns) if startswith(ns, "tenant-")

# A label selector (matchLabels) selects a pod template when every key/value in
# the selector is present on the pod labels. An empty selector matches all pods.
selector_matches(match_labels, labels) if {
	every k, v in match_labels {
		labels[k] == v
	}
}
