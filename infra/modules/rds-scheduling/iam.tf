# IAM role for the Lambda function
resource "aws_iam_role" "lambda" {
  count = var.enable_scheduling ? 1 : 0
  name  = "${var.project}-${var.environment}-rds-scheduler-lambda"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
    }]
  })

  tags = {
    Name = "${var.project}-${var.environment}-rds-scheduler-lambda"
  }
}

resource "aws_iam_role_policy" "lambda_rds" {
  count = var.enable_scheduling ? 1 : 0
  name  = "rds-stop-start"
  role  = aws_iam_role.lambda[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "rds:StopDBInstance",
          "rds:StartDBInstance",
          "rds:DescribeDBInstances"
        ]
        Resource = "arn:aws:rds:${var.region}:${var.account_id}:db:${var.rds_instance_id}"
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${var.region}:${var.account_id}:log-group:/aws/lambda/${var.project}-${var.environment}-rds-scheduler:*"
      }
    ]
  })
}

# IAM role for EventBridge Scheduler to invoke Lambda
resource "aws_iam_role" "scheduler" {
  count = var.enable_scheduling ? 1 : 0
  name  = "${var.project}-${var.environment}-rds-scheduler-eventbridge"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "scheduler.amazonaws.com"
      }
    }]
  })

  tags = {
    Name = "${var.project}-${var.environment}-rds-scheduler-eventbridge"
  }
}

resource "aws_iam_role_policy" "scheduler_invoke" {
  count = var.enable_scheduling ? 1 : 0
  name  = "invoke-lambda"
  role  = aws_iam_role.scheduler[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.rds_scheduler[0].arn
    }]
  })
}
