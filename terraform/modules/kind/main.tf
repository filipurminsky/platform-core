# Local kind cluster for zero-cost development
# Mirrors the prod EKS topology as closely as possible.
# Used by bootstrap.sh --mode=local

# kind-config.yaml is read by the CLI — this file documents what it contains.
# The actual cluster is created by: kind create cluster --config kind-config.yaml

resource "local_file" "kind_config" {
  filename = "${path.module}/kind-config.yaml"
  content  = <<-YAML
    kind: Cluster
    apiVersion: kind.x-k8s.io/v1alpha4
    name: platform-core
    nodes:
      - role: control-plane
        kubeadmConfigPatches:
          - |
            kind: InitConfiguration
            nodeRegistration:
              kubeletExtraArgs:
                node-labels: "ingress-ready=true"
        extraPortMappings:
          - containerPort: 80
            hostPort: 80
            protocol: TCP
          - containerPort: 443
            hostPort: 443
            protocol: TCP
      - role: worker
        labels:
          role: platform
      - role: worker
        labels:
          role: apps
    networking:
      podSubnet: "10.244.0.0/16"
      serviceSubnet: "10.96.0.0/12"
  YAML
}
