output "lambda_function_arn" {
  value = var.enable_scheduling ? aws_lambda_function.rds_scheduler[0].arn : null
}

output "lambda_function_name" {
  value = var.enable_scheduling ? aws_lambda_function.rds_scheduler[0].function_name : null
}

output "log_group_name" {
  value = var.enable_scheduling ? aws_cloudwatch_log_group.lambda[0].name : null
}
