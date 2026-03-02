output "website_endpoint" {
  value = aws_s3_bucket_website_configuration.maintenance.website_endpoint
}

output "bucket_name" {
  value = aws_s3_bucket.maintenance.id
}

output "bucket_hosted_zone_id" {
  description = "S3 website hosted zone ID for Route 53 alias"
  value       = aws_s3_bucket.maintenance.hosted_zone_id
}
