variable "project" {
  type    = string
  default = "zenbots"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "account_id" {
  type        = string
  description = "AWS Account ID"
}

# --- Database ---

variable "db_password" {
  type      = string
  sensitive = true
}

# --- Networking ---

variable "nat_type" {
  type    = string
  default = "instance"
}

# --- ECS ---

variable "ecr_repository_url" {
  type        = string
  description = "ECR repository URL (from global output)"
}

variable "image_tag" {
  type    = string
  default = "latest-dev"
}

# --- ACM ---

variable "certificate_arn" {
  type    = string
  default = ""
}

# --- Monitoring ---

variable "alert_email" {
  type    = string
  default = ""
}

# --- Domain ---

variable "route53_zone_id" {
  type    = string
  default = ""
}

variable "domain_prefix" {
  type    = string
  default = "dev-api"
}
