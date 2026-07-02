resource "aws_ecr_repository" "this" {
  name                 = "${var.project}/app"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Name = "${var.project}-ecr"
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  repository = aws_ecr_repository.this.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = {
          type = "expire"
        }
      },
      # NOTE: tagPrefixList uses AND semantics — an image must have tags
      # matching ALL prefixes in the list to match the rule. The original
      # single rule ["dev-", "prod-", "latest"] matched nothing (no image
      # carries all three prefixes), so tagged images accumulated unbounded
      # (347 tagged images by 2026-07). One rule per prefix is required.
      # Each deploy pushes 3 images (backend, worker, migrations), so
      # keep-9 = 3 deploys of rollback history.
      {
        rulePriority = 2
        description  = "Keep last 9 dev images (3 deploys)"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["dev-"]
          countType     = "imageCountMoreThan"
          countNumber   = 9
        }
        action = {
          type = "expire"
        }
      },
      {
        rulePriority = 3
        description  = "Keep last 9 prod images (3 deploys)"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["prod-"]
          countType     = "imageCountMoreThan"
          countNumber   = 9
        }
        action = {
          type = "expire"
        }
      },
      {
        rulePriority = 4
        description  = "Keep last 3 latest-* tags"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["latest"]
          countType     = "imageCountMoreThan"
          countNumber   = 3
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}
