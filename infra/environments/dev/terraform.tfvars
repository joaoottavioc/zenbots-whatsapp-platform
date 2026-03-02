# Dev environment configuration
# Usage: terraform plan -var-file=terraform.tfvars -var="db_password=xxx" -var="account_id=xxx"

project     = "zenbots"
environment = "dev"
region      = "us-east-1"

# NAT Instance for dev ($3/mo vs $32/mo gateway)
nat_type = "instance"

# Image tag — overridden by CI/CD
image_tag = "latest-dev"

# Domain (set after Route 53 zone is created in global)
# route53_zone_id = ""
# domain_prefix   = "dev-api"

# ACM cert (set after requesting certificate)
# certificate_arn = ""

# Monitoring
# alert_email = "you@example.com"
