output "alb_dns_name" {
  value = module.alb.alb_dns_name
}

output "rds_endpoint" {
  value = module.rds.endpoint
}

output "redis_endpoint" {
  value = module.elasticache.primary_endpoint
}

output "ecs_cluster_name" {
  value = module.ecs.cluster_name
}

output "backend_service_name" {
  value = module.ecs.backend_service_name
}

output "worker_service_name" {
  value = module.ecs.worker_service_name
}

output "s3_bucket_name" {
  value = module.s3.bucket_name
}

output "secret_arns" {
  value = module.secrets.secret_arns
}

# Outputs needed by CI/CD workflows (migration RunTask network config)
output "private_subnet_ids" {
  value = join(",", module.vpc.private_subnet_ids)
}

output "ecs_security_group_id" {
  value = module.vpc.ecs_security_group_id
}

output "migrations_task_definition_arn" {
  value = module.ecs.migrations_task_definition_arn
}

output "waf_web_acl_arn" {
  value = module.waf.web_acl_arn
}
