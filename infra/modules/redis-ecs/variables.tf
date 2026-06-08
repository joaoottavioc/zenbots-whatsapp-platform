variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "region" {
  type = string
}

# --- Networking ---

variable "vpc_id" {
  description = "VPC ID for Cloud Map private DNS namespace"
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnets for the Redis ECS task"
  type        = list(string)
}

variable "ecs_security_group_id" {
  description = "ECS security group (self-referencing rule will be added for port 6379)"
  type        = string
}

# --- ECS Cluster ---

variable "cluster_name" {
  description = "ECS cluster name to run the Redis service"
  type        = string
}

variable "capacity_providers" {
  type    = list(string)
  default = ["FARGATE"]
}

# Pin the on-demand FARGATE provider's base so Redis always keeps at least
# this many task(s) off Spot. Redis is the single dependency the backend's
# rate limiter and the ARQ queue both rely on; when it was Spot-only a
# capacity shortage during a reclaim took it to 0 (observed 2026-06-08, ~17
# min outage). With Redis unresolvable the rate limiter fails closed, so the
# web widget shows every customer "you're sending too fast". base=1 pins the
# Redis task to on-demand FARGATE; other providers (FARGATE_SPOT) stay base 0.
variable "fargate_base" {
  type    = number
  default = 0
}

variable "execution_role_arn" {
  description = "ECS task execution role ARN (for pulling images and logging)"
  type        = string
}

# --- Task Configuration ---

variable "cpu" {
  description = "CPU units for Redis task (256 = 0.25 vCPU)"
  type        = number
  default     = 256
}

variable "memory" {
  description = "Memory in MB for Redis task"
  type        = number
  default     = 512
}

variable "cpu_architecture" {
  type    = string
  default = "ARM64"
}

variable "maxmemory" {
  description = "Redis maxmemory in MB (should be less than task memory to leave room for OS)"
  type        = number
  default     = 384
}

variable "desired_count" {
  type    = number
  default = 1
}

# --- Logging ---

variable "log_retention_days" {
  type    = number
  default = 3
}
