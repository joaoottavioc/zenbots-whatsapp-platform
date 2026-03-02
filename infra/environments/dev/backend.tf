terraform {
  backend "s3" {
    bucket         = "zenbots-terraform-state"
    key            = "dev/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "zenbots-terraform-locks"
    encrypt        = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  required_version = ">= 1.5"
}

provider "aws" {
  region = var.region
}
