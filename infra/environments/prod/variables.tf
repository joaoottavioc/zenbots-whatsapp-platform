variable "project" {
  type    = string
  default = "zenbots"
}

variable "environment" {
  type    = string
  default = "prod"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "account_id" {
  type        = string
  description = "AWS Account ID"
}

variable "db_password" {
  type      = string
  sensitive = true
}

variable "nat_type" {
  type    = string
  default = "gateway"
}

variable "ecr_repository_url" {
  type = string
}

variable "image_tag" {
  type    = string
  default = "latest-prod"
}

variable "certificate_arn" {
  type    = string
  default = ""
}

variable "alert_email" {
  type    = string
  default = ""
}

variable "route53_zone_id" {
  type    = string
  default = ""
}

variable "domain_prefix" {
  type    = string
  default = "api"
}
