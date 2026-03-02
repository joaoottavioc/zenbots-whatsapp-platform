variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "alb_arn" {
  description = "ARN of the ALB to associate with the WAF"
  type        = string
}

variable "rate_limit" {
  description = "Max requests per 5-minute window per IP"
  type        = number
  default     = 2000
}
