variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "parameters" {
  description = "Map of parameter path suffix (e.g. 'database/url') to description"
  type        = map(string)
}
