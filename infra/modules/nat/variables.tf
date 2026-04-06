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

variable "enable_reverse_proxy" {
  description = "Install Caddy reverse proxy on NAT instance for HTTPS termination (dev only, replaces ALB)"
  type        = bool
  default     = false
}

variable "reverse_proxy_domain" {
  description = "Domain name for Caddy HTTPS (e.g. dev-api.zenbotz.com.br)"
  type        = string
  default     = ""
}

variable "reverse_proxy_upstream" {
  description = "Upstream address for Caddy to proxy to (e.g. backend.zenbots-dev.local:8000)"
  type        = string
  default     = ""
}
