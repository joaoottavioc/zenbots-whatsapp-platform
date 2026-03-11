# Menu storage bucket
resource "aws_s3_bucket" "menu" {
  bucket = "${var.project}-${var.environment}-menus"

  tags = {
    Name = "${var.project}-${var.environment}-menus"
  }
}

resource "aws_s3_bucket_versioning" "menu" {
  bucket = aws_s3_bucket.menu.id
  versioning_configuration {
    status = var.versioning_enabled ? "Enabled" : "Suspended"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "menu" {
  bucket = aws_s3_bucket.menu.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_cors_configuration" "menu" {
  bucket = aws_s3_bucket.menu.id

  cors_rule {
    allowed_origins = var.cors_allowed_origins
    allowed_methods = ["GET", "HEAD"]
    allowed_headers = ["*"]
    max_age_seconds = 3600
  }
}

resource "aws_s3_bucket_public_access_block" "menu" {
  bucket = aws_s3_bucket.menu.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
