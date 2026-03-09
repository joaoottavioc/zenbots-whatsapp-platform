# Creates SSM Parameter Store SecureString parameters.
# Values are populated manually (or via migration script) after apply.
# Free tier: up to 10,000 standard parameters at no cost.

resource "aws_ssm_parameter" "this" {
  for_each = var.parameters

  name        = "/${var.project}/${var.environment}/${each.key}"
  description = each.value
  type        = "SecureString"
  value       = "CHANGE_ME"

  tags = {
    Project     = var.project
    Environment = var.environment
  }

  lifecycle {
    ignore_changes = [value]
  }
}
