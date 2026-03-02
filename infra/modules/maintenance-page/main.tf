# S3 static website for Route 53 failover maintenance page

resource "aws_s3_bucket" "maintenance" {
  bucket = "${var.project}-${var.environment}-maintenance"

  tags = {
    Name        = "${var.project}-${var.environment}-maintenance"
    Environment = var.environment
    Project     = var.project
  }
}

resource "aws_s3_bucket_website_configuration" "maintenance" {
  bucket = aws_s3_bucket.maintenance.id

  index_document {
    suffix = "index.html"
  }

  error_document {
    key = "index.html"
  }
}

resource "aws_s3_bucket_public_access_block" "maintenance" {
  bucket = aws_s3_bucket.maintenance.id

  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "maintenance" {
  bucket = aws_s3_bucket.maintenance.id

  depends_on = [aws_s3_bucket_public_access_block.maintenance]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "PublicReadGetObject"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.maintenance.arn}/*"
      }
    ]
  })
}

resource "aws_s3_object" "index" {
  bucket       = aws_s3_bucket.maintenance.id
  key          = "index.html"
  content_type = "text/html"

  content = <<-HTML
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>ZenBots - Manuten${"\u00e7\u00e3"}o</title>
      <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
          font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
          background: #f8f9fa;
          color: #333;
          display: flex;
          align-items: center;
          justify-content: center;
          min-height: 100vh;
        }
        .container {
          text-align: center;
          max-width: 500px;
          padding: 2rem;
        }
        h1 { font-size: 2rem; margin-bottom: 1rem; color: #1a1a1a; }
        p { font-size: 1.1rem; color: #666; line-height: 1.6; }
        .status { margin-top: 2rem; font-size: 0.9rem; color: #999; }
      </style>
    </head>
    <body>
      <div class="container">
        <h1>Estamos em manuten${"\u00e7\u00e3"}o</h1>
        <p>O ZenBots est${"\u00e1"} passando por uma manuten${"\u00e7\u00e3"}o programada. Voltaremos em breve.</p>
        <div class="status">
          <p>Se o problema persistir, entre em contato conosco.</p>
        </div>
      </div>
    </body>
    </html>
  HTML
}
