output "secret_arns" {
  description = "Map of secret name to ARN"
  value       = { for k, v in aws_secretsmanager_secret.this : k => v.arn }
}
