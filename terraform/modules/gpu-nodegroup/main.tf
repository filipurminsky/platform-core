# GPU node group for vLLM inference (optional, prod only)
# Tainted so only vLLM pods — which tolerate the taint — schedule here.
# Cluster Autoscaler scales this group to zero when no vLLM replicas are running.

resource "aws_eks_node_group" "gpu" {
  cluster_name    = var.cluster_name
  node_group_name = "${var.cluster_name}-gpu"
  node_role_arn   = var.node_role_arn
  subnet_ids      = var.subnet_ids

  instance_types = [var.instance_type] # default: g4dn.xlarge (NVIDIA T4)
  ami_type       = "AL2_x86_64_GPU"    # Amazon Linux 2 with NVIDIA drivers

  scaling_config {
    desired_size = var.min_size # start at min; Cluster Autoscaler will expand
    min_size     = var.min_size
    max_size     = var.max_size
  }

  # Required for Cluster Autoscaler to discover and manage this group
  labels = {
    "node.kubernetes.io/instance-type" = var.instance_type
    "role"                             = "gpu"
    "workload"                         = "vllm"
  }

  taint {
    key    = "nvidia.com/gpu"
    value  = "true"
    effect = "NO_SCHEDULE" # prevents non-GPU pods from scheduling here
  }

  # Ensure the NVIDIA device plugin DaemonSet can label the node
  tags = merge(var.tags, {
    "k8s.io/cluster-autoscaler/enabled"             = "true"
    "k8s.io/cluster-autoscaler/${var.cluster_name}" = "owned"
  })

  lifecycle {
    ignore_changes = [scaling_config[0].desired_size] # CA manages desired_size
  }
}
