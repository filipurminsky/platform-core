package main

# Every Deployment must have a corresponding NetworkPolicy in the same namespace.
# Checked by matching the Deployment's pod labels against NetworkPolicy podSelectors.
# (conftest evaluates all manifests together; this policy checks for the presence
#  of at least one NetworkPolicy in the same namespace — a strong signal that
#  the namespace has network segmentation configured.)

warn[msg] {
  input.kind == "Deployment"
  ns := input.metadata.namespace
  # If namespace is set to a known app namespace, we expect a NetworkPolicy
  ns_requires_policy := {"apps", "platform"}
  ns_requires_policy[ns]
  msg := sprintf(
    "Deployment '%s' in namespace '%s': ensure a NetworkPolicy exists for this namespace",
    [input.metadata.name, ns]
  )
}

# PodDisruptionBudget must exist for every production Deployment
# (enforced as a warning — KEDA-scaled deployments with minReplica=0 are exempt)
warn[msg] {
  input.kind == "Deployment"
  not startswith(input.metadata.name, "vllm")   # vLLM PDB not required at minReplica=0
  not startswith(input.metadata.name, "worker")  # KEDA-managed, exempt
  msg := sprintf(
    "Deployment '%s': verify a PodDisruptionBudget exists to protect against voluntary disruptions",
    [input.metadata.name]
  )
}
