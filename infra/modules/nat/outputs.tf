output "nat_id" {
  value = var.nat_type == "gateway" ? aws_nat_gateway.this[0].id : aws_instance.nat[0].id
}
