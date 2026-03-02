output "ecr_repository_url" {
  value = module.ecr.repository_url
}

output "route53_zone_id" {
  value = aws_route53_zone.main.zone_id
}

output "route53_name_servers" {
  value = aws_route53_zone.main.name_servers
}
