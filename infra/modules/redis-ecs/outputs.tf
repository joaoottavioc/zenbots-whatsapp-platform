output "redis_host" {
  description = "Cloud Map DNS name for the Redis service"
  value       = "redis.${aws_service_discovery_private_dns_namespace.this.name}"
}

output "redis_port" {
  value = "6379"
}

output "redis_url" {
  description = "Full Redis URL for DB 0 (rate limiting)"
  value       = "redis://redis.${aws_service_discovery_private_dns_namespace.this.name}:6379/0"
}

output "service_name" {
  description = "ECS service name (for scheduling module)"
  value       = aws_ecs_service.redis.name
}

output "namespace_id" {
  description = "Cloud Map namespace ID (reusable for Phase 4 backend service discovery)"
  value       = aws_service_discovery_private_dns_namespace.this.id
}
