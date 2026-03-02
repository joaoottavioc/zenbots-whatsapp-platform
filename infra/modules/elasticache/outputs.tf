output "primary_endpoint" {
  value = aws_elasticache_replication_group.this.primary_endpoint_address
}

output "port" {
  value = 6379
}

output "redis_url" {
  description = "Redis connection URL for rate limiter (DB 0)"
  value       = "redis://${aws_elasticache_replication_group.this.primary_endpoint_address}:6379/0"
}

output "redis_host" {
  value = aws_elasticache_replication_group.this.primary_endpoint_address
}

output "replication_group_id" {
  value = aws_elasticache_replication_group.this.replication_group_id
}
