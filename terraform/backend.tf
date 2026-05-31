terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.25"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.12"
    }
  }

  # Remote state — S3 + DynamoDB locking
  # Bootstrap: run scripts/bootstrap-state.sh once before terraform init
  backend "s3" {
    bucket         = "platform-core-tfstate"
    key            = "terraform.tfstate" # overridden per environment
    region         = "eu-west-1"
    encrypt        = true
    dynamodb_table = "platform-core-tflock"
  }
}
