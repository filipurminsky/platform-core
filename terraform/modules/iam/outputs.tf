output "external_secrets_role_arn" {
  value = module.irsa_external_secrets.iam_role_arn
}

output "cluster_autoscaler_role_arn" {
  value = module.irsa_cluster_autoscaler.iam_role_arn
}

output "ecr_pull_role_arn" {
  value = module.irsa_ecr_pull.iam_role_arn
}

output "crossplane_s3_role_arn" {
  value = module.irsa_crossplane_s3.iam_role_arn
}

output "audio_pipeline_role_arn" {
  value = module.irsa_audio_pipeline.iam_role_arn
}
