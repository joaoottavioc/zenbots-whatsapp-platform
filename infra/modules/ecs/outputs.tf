output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "cluster_arn" {
  value = aws_ecs_cluster.this.arn
}

output "backend_service_name" {
  value = aws_ecs_service.backend.name
}

output "worker_service_name" {
  value = aws_ecs_service.worker.name
}

output "backend_task_definition_arn" {
  value = aws_ecs_task_definition.backend.arn
}

output "worker_task_definition_arn" {
  value = aws_ecs_task_definition.worker.arn
}

output "migrations_task_definition_arn" {
  value = aws_ecs_task_definition.migrations.arn
}

output "execution_role_arn" {
  value = aws_iam_role.execution.arn
}

output "task_role_arn" {
  value = aws_iam_role.task.arn
}
