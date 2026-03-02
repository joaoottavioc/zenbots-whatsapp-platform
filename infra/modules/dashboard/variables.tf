variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "region" {
  type = string
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

variable "alb_arn_suffix" {
  description = "ALB ARN suffix for CloudWatch metrics (e.g., app/my-alb/1234567890)"
  type        = string
}

variable "target_group_arn_suffix" {
  description = "Target group ARN suffix for CloudWatch metrics"
  type        = string
}

variable "rds_instance_id" {
  type = string
}

variable "elasticache_replication_group_id" {
  type = string
}
