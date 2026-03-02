# Creates empty Secrets Manager entries — values populated manually after apply

locals {
  secret_names = [
    "database",
    "redis",
    "openai",
    "whatsapp",
    "mercadopago",
    "auth",
    "email",
    "google",
  ]
}

resource "aws_secretsmanager_secret" "this" {
  for_each = toset(local.secret_names)

  name        = "${var.project}/${var.environment}/${each.key}"
  description = "${var.project} ${var.environment} ${each.key} credentials"

  tags = {
    Name        = "${var.project}-${var.environment}-${each.key}"
    Environment = var.environment
  }
}

# Initial empty placeholder values — overwritten manually
resource "aws_secretsmanager_secret_version" "this" {
  for_each  = toset(local.secret_names)
  secret_id = aws_secretsmanager_secret.this[each.key].id

  secret_string = jsonencode({
    placeholder = "CHANGE_ME"
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}
