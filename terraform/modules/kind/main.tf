# Local kind cluster for zero-cost development (used by bootstrap.sh --mode=local).
#
# The single source of truth is the committed kind-config.yaml next to this file
# (Cilium instead of kindnet, digest-pinned kindest/node matching the EKS fleet
# version, ingress port mappings). This module used to re-render that file from
# an inline heredoc, which had silently drifted (no image pin, default CNI) and
# would have overwritten the committed config on apply — so it now only exposes
# the path. The cluster itself is created by:
#   kind create cluster --config terraform/modules/kind/kind-config.yaml

output "kind_config_path" {
  description = "Path to the committed kind cluster config"
  value       = "${path.module}/kind-config.yaml"
}
