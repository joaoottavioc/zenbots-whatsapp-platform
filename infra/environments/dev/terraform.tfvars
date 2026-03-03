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
route53_zone_id = "Z09896471XJVSM51SC7S3"
domain_prefix   = "dev-api"

# ACM cert (enable after certificate is validated)
# certificate_arn = "arn:aws:acm:us-east-1:578761488332:certificate/f17d26df-e7d5-4d79-8dcd-e429f0c6cac4"

# Monitoring
alert_email = "joao.ottavio.cruzeiro@gmail.com"
