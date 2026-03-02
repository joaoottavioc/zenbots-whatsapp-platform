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
  type = string
}

variable "worker_service_name" {
  type = string
}

variable "target_group_arn_suffix" {
  type = string
}

variable "rds_instance_id" {
  type = string
}

variable "elasticache_replication_group_id" {
  type = string
}
