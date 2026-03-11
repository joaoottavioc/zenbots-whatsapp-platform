variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "cors_allowed_origins" {
  description = "Allowed origins for S3 CORS (frontend URLs)"
  type        = list(string)
  default     = []
}

variable "versioning_enabled" {
  description = "Enable S3 bucket versioning (use false for dev to reduce storage)"
  type        = bool
  default     = true
}
