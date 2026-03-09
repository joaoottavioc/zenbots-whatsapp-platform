variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "versioning_enabled" {
  description = "Enable S3 bucket versioning (use false for dev to reduce storage)"
  type        = bool
  default     = true
}
