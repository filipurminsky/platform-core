output "external_secrets_role_arn" {
  value = module.irsa_external_secrets.arn
}

output "crossplane_s3_role_arn" {
  value = module.irsa_crossplane_s3.arn
}

output "audio_pipeline_role_arn" {
  value = module.irsa_audio_pipeline.arn
}
