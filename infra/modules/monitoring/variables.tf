variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "alert_email" {
  type    = string
  default = ""
}

variable "ecs_cluster_name" {
  type = string
}

variable "backend_service_name" {
  type = string
}

variable "alb_arn_suffix" {
  description = "ALB ARN suffix for alarms. Empty string disables ALB alarms (e.g. dev uses Caddy on NAT)."
  type        = string
  default     = ""
}

variable "worker_service_name" {
  type = string
}

variable "target_group_arn_suffix" {
  description = "Target group ARN suffix for alarms. Empty string disables ALB target group alarms."
  type        = string
  default     = ""
}

variable "rds_instance_id" {
  type = string
}

variable "elasticache_replication_group_id" {
  description = "ElastiCache replication group ID for alarms. Empty string disables ElastiCache alarms (e.g. dev uses Redis on ECS)."
  type        = string
  default     = ""
}
