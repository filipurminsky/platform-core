output "cluster_name" {
  description = "EKS cluster name"
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "Kubernetes API server endpoint"
  value       = module.eks.cluster_endpoint
}

output "cluster_certificate_authority_data" {
  description = "Base64-encoded CA cert for the cluster"
  value       = module.eks.cluster_certificate_authority_data
  sensitive   = true
}

output "cluster_oidc_issuer_url" {
  description = "OIDC issuer URL — used for IRSA TrustPolicy"
  value       = module.eks.cluster_oidc_issuer_url
}

output "oidc_provider_arn" {
  description = "ARN of the OIDC provider"
  value       = data.aws_iam_openid_connect_provider.eks.arn
}

# ─── Karpenter (null unless enable_karpenter = true) ─────────────────────────
# These feed the Karpenter Helm values + EC2NodeClass:
#   karpenter_controller_role_arn → serviceAccount IRSA annotation
#   karpenter_node_iam_role_name  → EC2NodeClass .spec.role
#   karpenter_interruption_queue  → Helm settings.interruptionQueue
output "karpenter_controller_role_arn" {
  description = "IAM role ARN for the Karpenter controller ServiceAccount (IRSA)"
  value       = try(module.karpenter[0].iam_role_arn, null)
}

output "karpenter_node_iam_role_name" {
  description = "IAM role name for Karpenter-provisioned nodes (EC2NodeClass .spec.role)"
  value       = try(module.karpenter[0].node_iam_role_name, null)
}

output "karpenter_interruption_queue" {
  description = "SQS interruption queue name for Karpenter (Helm settings.interruptionQueue)"
  value       = try(module.karpenter[0].queue_name, null)
}
