output "endpoint" {
  value = aws_db_instance.this.endpoint
}

output "address" {
  value = aws_db_instance.this.address
}

output "port" {
  value = aws_db_instance.this.port
}

output "database_url" {
  description = "asyncpg connection string"
  value       = "postgresql+asyncpg://${var.db_username}:${var.db_password}@${aws_db_instance.this.address}:${aws_db_instance.this.port}/${var.db_name}"
  sensitive   = true
}
