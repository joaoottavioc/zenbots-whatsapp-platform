# RDS Stop/Start Scheduling via EventBridge Scheduler + Lambda
# Stops RDS 30 min after ECS scales down, starts 30 min before ECS scales up
# Saves ~$8/month on dev by running RDS only during business hours (~50 hrs/week)

# --- Lambda function (inline Python) ---

data "archive_file" "lambda" {
  count       = var.enable_scheduling ? 1 : 0
  type        = "zip"
  output_path = "${path.module}/lambda.zip"

  source {
    content  = <<-PYTHON
import json
import logging
import boto3
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

rds = boto3.client("rds")
INSTANCE_ID = os.environ["RDS_INSTANCE_ID"]


def get_instance_status():
    resp = rds.describe_db_instances(DBInstanceIdentifier=INSTANCE_ID)
    return resp["DBInstances"][0]["DBInstanceStatus"]


def handler(event, context):
    action = event.get("action")
    if action not in ("start", "stop"):
        raise ValueError(f"Invalid action: {action}. Must be 'start' or 'stop'.")

    status = get_instance_status()
    logger.info(f"RDS {INSTANCE_ID} current status: {status}, requested action: {action}")

    if action == "stop":
        if status == "stopped":
            logger.info("Instance already stopped, skipping.")
            return {"status": "already_stopped"}
        if status != "available":
            logger.warning(f"Cannot stop instance in state '{status}', skipping.")
            return {"status": "skipped", "reason": f"instance_state_{status}"}
        rds.stop_db_instance(DBInstanceIdentifier=INSTANCE_ID)
        logger.info("Stop command sent successfully.")
        return {"status": "stopping"}

    if action == "start":
        if status == "available":
            logger.info("Instance already available, skipping.")
            return {"status": "already_available"}
        if status != "stopped":
            logger.warning(f"Cannot start instance in state '{status}', skipping.")
            return {"status": "skipped", "reason": f"instance_state_{status}"}
        rds.start_db_instance(DBInstanceIdentifier=INSTANCE_ID)
        logger.info("Start command sent successfully.")
        return {"status": "starting"}
PYTHON
    filename = "lambda_function.py"
  }
}

resource "aws_lambda_function" "rds_scheduler" {
  count = var.enable_scheduling ? 1 : 0

  function_name = "${var.project}-${var.environment}-rds-scheduler"
  description   = "Stops/starts RDS instance ${var.rds_instance_id} on schedule"
  role          = aws_iam_role.lambda[0].arn
  handler       = "lambda_function.handler"
  runtime       = "python3.12"
  architectures = ["arm64"]
  timeout       = 60
  memory_size   = 128

  filename         = data.archive_file.lambda[0].output_path
  source_code_hash = data.archive_file.lambda[0].output_base64sha256

  environment {
    variables = {
      RDS_INSTANCE_ID = var.rds_instance_id
    }
  }

  tags = {
    Name = "${var.project}-${var.environment}-rds-scheduler"
  }
}

# --- CloudWatch Log Group for Lambda ---

resource "aws_cloudwatch_log_group" "lambda" {
  count             = var.enable_scheduling ? 1 : 0
  name              = "/aws/lambda/${var.project}-${var.environment}-rds-scheduler"
  retention_in_days = var.log_retention_days

  tags = {
    Name = "${var.project}-${var.environment}-rds-scheduler-logs"
  }
}

# --- EventBridge Scheduler rules ---

resource "aws_scheduler_schedule" "stop_rds" {
  count = var.enable_scheduling ? 1 : 0
  name  = "${var.project}-${var.environment}-rds-stop"

  schedule_expression          = var.stop_cron
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.rds_scheduler[0].arn
    role_arn = aws_iam_role.scheduler[0].arn

    input = jsonencode({ action = "stop" })
  }
}

resource "aws_scheduler_schedule" "start_rds" {
  count = var.enable_scheduling ? 1 : 0
  name  = "${var.project}-${var.environment}-rds-start"

  schedule_expression          = var.start_cron
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.rds_scheduler[0].arn
    role_arn = aws_iam_role.scheduler[0].arn

    input = jsonencode({ action = "start" })
  }
}
