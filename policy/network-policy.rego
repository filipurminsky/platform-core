package main

import rego.v1

# Real relationship checks across the rendered manifest set (requires
# `conftest test --combine`). Instead of warning "ensure a NetworkPolicy exists"
# for every Deployment, we prove that some NetworkPolicy in the same namespace
# actually selects the workload's pod labels.

networkpolicies contains np if {
	some m in manifests
	m.kind == "NetworkPolicy"
	np := m
}

poddisruptionbudgets contains p if {
	some m in manifests
	m.kind == "PodDisruptionBudget"
	p := m
}

# A NetworkPolicy selects a workload when it is in the same namespace and its
# podSelector.matchLabels is a subset of the workload's pod labels (an empty
# podSelector selects all pods in the namespace — the default-deny case).
np_selects_workload(w) if {
	some np in networkpolicies
	np.metadata.namespace == w.metadata.namespace
	selector_matches(object.get(object.get(np.spec, "podSelector", {}), "matchLabels", {}), pod_labels(w))
}

# DENY: a workload in a segmented namespace with no NetworkPolicy selecting it.
# This is a hard failure — zero-trust requires every workload to be covered.
deny contains msg if {
	some w in workloads
	requires_netpol(w.metadata.namespace)
	not np_selects_workload(w)
	msg := sprintf(
		"%s '%s' in namespace '%s' has no NetworkPolicy selecting its pods (labels %v) — zero-trust requires explicit segmentation",
		[w.kind, w.metadata.name, w.metadata.namespace, pod_labels(w)],
	)
}

# A PDB protects a workload when same-namespace and its selector matches the pods.
pdb_protects_workload(w) if {
	some p in poddisruptionbudgets
	p.metadata.namespace == w.metadata.namespace
	selector_matches(object.get(object.get(p.spec, "selector", {}), "matchLabels", {}), pod_labels(w))
}

# KEDA scale-to-zero workloads don't need a PDB (a PDB with minAvailable: 1 is
# meaningless when the steady-state replica count is 0).
pdb_exempt(w) if startswith(w.metadata.name, "vllm")

pdb_exempt(w) if startswith(w.metadata.name, "worker")

# WARN (advisory): a non-exempt workload with no PDB protecting it.
warn contains msg if {
	some w in workloads
	not pdb_exempt(w)
	not pdb_protects_workload(w)
	msg := sprintf(
		"%s '%s': no PodDisruptionBudget selects its pods — voluntary disruptions (node drain) can take all replicas down at once",
		[w.kind, w.metadata.name],
	)
}
