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
  description = "Enable RDS stop/start scheduling (dev only)"
  type        = bool
  default     = false
}

variable "rds_instance_id" {
  description = "RDS instance identifier (e.g. zenbots-dev)"
  type        = string
}

variable "stop_cron" {
  description = "Cron expression for stopping RDS (UTC)"
  type        = string
  default     = "cron(30 22 ? * MON-FRI *)" # 7:30 PM BRT
}

variable "start_cron" {
  description = "Cron expression for starting RDS (UTC)"
  type        = string
  default     = "cron(30 11 ? * MON-FRI *)" # 8:30 AM BRT
}

variable "log_retention_days" {
  type    = number
  default = 3
}
