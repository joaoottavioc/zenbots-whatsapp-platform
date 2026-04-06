output "nat_id" {
  value = var.nat_type == "gateway" ? aws_nat_gateway.this[0].id : aws_instance.nat[0].id
}

output "nat_security_group_id" {
  description = "Security group ID of the NAT instance (for ECS ingress rules)"
  value       = var.nat_type == "instance" ? aws_security_group.nat[0].id : ""
}

output "nat_eip_public_ip" {
  description = "Public IP of the NAT instance EIP (for Route 53 A record)"
  value       = var.nat_type == "instance" && var.enable_reverse_proxy ? aws_eip.nat_instance[0].public_ip : ""
}
