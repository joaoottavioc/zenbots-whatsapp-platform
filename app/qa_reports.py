# app/qa_reports.py
"""
QA test report storage in S3, organized by environment.

S3 structure:
  s3://{bucket}/qa-reports/{env}/runs/{timestamp}.json

Each report is a JSON file with run metadata, per-restaurant results,
and per-scenario pass/fail details.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

BUCKET_NAME = os.getenv("AWS_BUCKET_NAME")
REGION = os.getenv("AWS_REGION", "us-east-1")
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

s3_client = boto3.client("s3", region_name=REGION)

# Map environment to short prefix
_ENV_PREFIX = (
    "dev"
    if "dev" in (ENVIRONMENT or "").lower()
    else ("prod" if "prod" in (ENVIRONMENT or "").lower() else "dev")
)


def _reports_prefix() -> str:
    return f"qa-reports/{_ENV_PREFIX}/runs/"


def upload_qa_report(report: dict) -> Optional[str]:
    """Upload a QA test report to S3. Returns the S3 key or None on failure."""
    if not BUCKET_NAME:
        logger.warning("AWS_BUCKET_NAME not set, skipping QA report upload")
        return None

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    key = f"{_reports_prefix()}{timestamp}.json"

    try:
        s3_client.put_object(
            Bucket=BUCKET_NAME,
            Key=key,
            Body=json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        logger.info("QA report uploaded to s3://%s/%s", BUCKET_NAME, key)
        return key
    except Exception as e:
        logger.error("Failed to upload QA report: %s", e)
        return None


def list_qa_reports(limit: int = 50) -> list[dict]:
    """List recent QA reports from S3, newest first."""
    if not BUCKET_NAME:
        return []

    try:
        response = s3_client.list_objects_v2(
            Bucket=BUCKET_NAME,
            Prefix=_reports_prefix(),
            MaxKeys=limit,
        )
        objects = response.get("Contents", [])
        # Sort newest first
        objects.sort(key=lambda o: o["LastModified"], reverse=True)
        return [
            {
                "key": obj["Key"],
                "filename": obj["Key"].rsplit("/", 1)[-1],
                "last_modified": obj["LastModified"].isoformat(),
                "size_bytes": obj["Size"],
            }
            for obj in objects[:limit]
        ]
    except Exception as e:
        logger.error("Failed to list QA reports: %s", e)
        return []


def get_qa_report(key: str) -> Optional[dict]:
    """Fetch a single QA report from S3 by key."""
    if not BUCKET_NAME:
        return None

    try:
        response = s3_client.get_object(Bucket=BUCKET_NAME, Key=key)
        body = response["Body"].read().decode("utf-8")
        return json.loads(body)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return None
        logger.error("Failed to fetch QA report %s: %s", key, e)
        return None
    except Exception as e:
        logger.error("Failed to fetch QA report %s: %s", key, e)
        return None
