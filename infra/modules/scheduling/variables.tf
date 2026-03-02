variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "region" {
  type = string
}

variable "account_id" {
  type = string
}

variable "enable_scheduling" {
  description = "Enable off-hours scaling (dev only)"
  type        = bool
  default     = false
}

variable "ecs_cluster_name" {
  type = string
}

variable "backend_service_name" {
  type = string
}

variable "worker_service_name" {
  type = string
}

variable "backend_desired_count" {
  type    = number
  default = 1
}

variable "worker_desired_count" {
  type    = number
  default = 1
}
