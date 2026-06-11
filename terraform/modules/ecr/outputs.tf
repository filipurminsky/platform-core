output "repository_urls" {
  description = "Map of app names to their ECR repository URLs."
  value       = { for app, repo in aws_ecr_repository.apps : app => repo.repository_url }
}
