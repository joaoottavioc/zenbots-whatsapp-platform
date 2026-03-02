variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "region" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "public_subnet_id" {
  description = "Public subnet for NAT instance/gateway placement"
  type        = string
}

variable "private_route_table_ids" {
  description = "Route table IDs for private subnets"
  type        = list(string)
}

variable "private_subnet_cidrs" {
  description = "CIDRs allowed to route through NAT"
  type        = list(string)
  default     = ["10.0.10.0/24", "10.0.11.0/24"]
}

variable "nat_type" {
  description = "NAT type: 'instance' (dev, $3/mo) or 'gateway' (prod, $32/mo)"
  type        = string
  default     = "instance"

  validation {
    condition     = contains(["instance", "gateway"], var.nat_type)
    error_message = "nat_type must be 'instance' or 'gateway'."
  }
}
