output "parameter_arns" {
  description = "Map of parameter path suffix to full ARN"
  value       = { for k, v in aws_ssm_parameter.this : k => v.arn }
}
