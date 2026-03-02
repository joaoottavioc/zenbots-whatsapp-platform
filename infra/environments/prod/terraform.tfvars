# Production environment configuration — Phase 4 (Month 2)
# Usage: terraform plan -var-file=terraform.tfvars -var="db_password=xxx" -var="account_id=xxx"

project     = "zenbots"
environment = "prod"
region      = "us-east-1"

# Managed NAT Gateway for prod ($32/mo)
nat_type = "gateway"

# Image tag — overridden by CI/CD
image_tag = "latest-prod"

# Domain (set after Route 53 zone is created in global)
# route53_zone_id = ""
# domain_prefix   = "api"

# ACM cert (set after requesting certificate)
# certificate_arn = ""

# Monitoring
# alert_email = "you@example.com"
