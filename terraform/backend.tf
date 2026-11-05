terraform {
  # >= 1.10: S3-native state locking (use_lockfile). Cross-variable validation
  # in modules/eks also needs >= 1.9.
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.72"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.0"
    }
  }

  # Remote state — S3 with S3-native locking (a <key>.tflock object written with
  # conditional PUTs). Replaces the deprecated DynamoDB lock table.
  # Bootstrap: create the (versioned, encrypted) state bucket manually once,
  # then run terraform init from terraform/environments/{dev,prod} (not this
  # root module).
  #
  # Migrating an existing state from dynamodb_table → use_lockfile:
  #   1. Make sure no apply is running (nobody holds a DynamoDB lock).
  #   2. Optional belt-and-braces transition: temporarily set BOTH
  #      `use_lockfile = true` and `dynamodb_table = "platform-core-tflock"` so
  #      old and new clients lock against each other while CI/laptops upgrade.
  #   3. `terraform init -reconfigure` in each environment (state stays put —
  #      same bucket/key — only the locking mechanism changes).
  #   4. Once every client is on Terraform >= 1.10 with use_lockfile, drop
  #      dynamodb_table (as here) and delete the platform-core-tflock table by
  #      hand (it was created out-of-band, not managed by this repo).
  #   The state role/user needs s3:PutObject + s3:DeleteObject on <key>.tflock.
  backend "s3" {
    bucket       = "platform-core-tfstate"
    key          = "terraform.tfstate" # overridden per environment
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}
