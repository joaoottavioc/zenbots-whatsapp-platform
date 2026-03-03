# Dev environment configuration

project     = "zenbots"
environment = "dev"
region      = "us-east-1"

account_id         = "578761488332"
ecr_repository_url = "578761488332.dkr.ecr.us-east-1.amazonaws.com/zenbots/app"

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
