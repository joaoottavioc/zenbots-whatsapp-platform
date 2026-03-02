variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "security_group_id" {
  type = string
}

variable "instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "allocated_storage" {
  type    = number
  default = 20
}

variable "max_allocated_storage" {
  type    = number
  default = 50
}

variable "db_name" {
  type    = string
  default = "botbuilder"
}

variable "db_username" {
  type    = string
  default = "zenbots"
}

variable "db_password" {
  type      = string
  sensitive = true
}

variable "multi_az" {
  type    = bool
  default = false
}

variable "backup_retention_days" {
  type    = number
  default = 7
}
