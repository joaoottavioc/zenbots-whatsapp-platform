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

# --- Networking ---

variable "private_subnet_ids" {
  type = list(string)
}

variable "ecs_security_group_id" {
  type = string
}

variable "target_group_arn" {
  type    = string
  default = ""
}

variable "enable_alb" {
  description = "Whether to attach ALB load_balancer block to backend service. False for dev (Caddy on NAT)."
  type        = bool
  default     = true
}

variable "service_discovery_arn" {
  description = "Cloud Map service ARN for backend service discovery. Empty string disables service registries."
  type        = string
  default     = ""
}

# --- Cluster ---

variable "capacity_providers" {
  type    = list(string)
  default = ["FARGATE"]
}

# Guaranteed on-demand FARGATE tasks for the backend service. With Spot-only
# (base=0) a single-task backend can drop to 0 running when its Spot task is
# interrupted and Spot capacity is simultaneously unavailable (observed on
# ARM64 in us-east-1). Setting base=1 pins the first backend task to on-demand
# FARGATE; scale-out beyond it still uses cheap Spot via the weighted strategy.
variable "backend_fargate_base" {
  type    = number
  default = 0
}

# Same protection for the worker (ARQ consumer). The worker is what drains the
# job queue — web-widget replies, WhatsApp messages, and cron jobs all flow
# through it. With Spot-only (base=0) a single-task worker that gets its Spot
# task interrupted during an ARM64 capacity shortage drops to 0 running, so
# enqueued jobs pile up and the widget appears dead (message sent, no reply).
# base=1 pins the first worker task to on-demand FARGATE.
variable "worker_fargate_base" {
  type    = number
  default = 0
}

variable "container_insights" {
  type    = bool
  default = false
}

variable "use_ssm_parameters" {
  description = "Use SSM Parameter Store instead of Secrets Manager for secret injection"
  type        = bool
  default     = false
}

# --- Runtime ---

variable "cpu_architecture" {
  type    = string
  default = "ARM64"
}

# --- Backend ---

variable "backend_image" {
  type = string
}

variable "backend_cpu" {
  type    = number
  default = 256
}

variable "backend_memory" {
  type    = number
  default = 512
}

variable "backend_desired_count" {
  type    = number
  default = 1
}

variable "backend_max_count" {
  type    = number
  default = 1
}

variable "backend_environment" {
  description = "Non-sensitive environment variables for backend"
  type = list(object({
    name  = string
    value = string
  }))
  default = []
}

variable "backend_secrets" {
  description = "Secrets Manager ARN references for backend"
  type = list(object({
    name      = string
    valueFrom = string
  }))
  default = []
}

# --- Worker ---

variable "worker_image" {
  type = string
}

variable "worker_cpu" {
  type    = number
  default = 256
}

variable "worker_memory" {
  type    = number
  default = 512
}

variable "worker_desired_count" {
  type    = number
  default = 1
}

variable "worker_max_count" {
  type    = number
  default = 1
}

variable "worker_stop_timeout" {
  type    = number
  default = 120
}

variable "worker_environment" {
  description = "Non-sensitive environment variables for worker"
  type = list(object({
    name  = string
    value = string
  }))
  default = []
}

variable "worker_secrets" {
  description = "Secrets Manager ARN references for worker"
  type = list(object({
    name      = string
    valueFrom = string
  }))
  default = []
}

# --- Migrations ---

variable "migrations_image" {
  type = string
}

# --- Deployment ---

variable "health_check_grace_period" {
  description = "Seconds to wait before ALB health checks count against new tasks (must cover startup time)"
  type        = number
  default     = 360
}

variable "minimum_healthy_percent" {
  type    = number
  default = 100
}

variable "maximum_percent" {
  type    = number
  default = 200
}

# --- Logging ---

variable "log_retention_days" {
  type    = number
  default = 7
}

# --- S3 ---

variable "s3_bucket_name" {
  type = string
}
