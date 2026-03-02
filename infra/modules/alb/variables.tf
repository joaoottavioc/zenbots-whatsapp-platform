variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "security_group_id" {
  type = string
}

variable "certificate_arn" {
  description = "ACM certificate ARN for HTTPS. Empty string disables HTTPS listener."
  type        = string
  default     = ""
}
