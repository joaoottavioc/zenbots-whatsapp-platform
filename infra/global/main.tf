module "ecr" {
  source  = "../modules/ecr"
  project = "zenbots"
}

# Route 53 hosted zone (shared across environments)
resource "aws_route53_zone" "main" {
  name = var.domain_name

  tags = {
    Name = "zenbots-zone"
  }
}
