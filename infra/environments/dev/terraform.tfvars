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

# Domain
route53_zone_id = "Z0672127E335XW159Q8N"
domain_prefix   = "dev-api"

# ACM cert
certificate_arn = "arn:aws:acm:us-east-1:578761488332:certificate/c91277cb-e5c1-463a-a8b2-75d7b613b4b1"

# Monitoring
alert_email = "joao.ribeiro@zenbotz.com.br"
