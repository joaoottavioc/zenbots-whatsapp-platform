import logging
import time
import boto3
import os
from uuid import uuid4
from fastapi import UploadFile, HTTPException
from botocore.exceptions import NoCredentialsError
import io
from app.monitoring import record_api_usage

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png", "webp"}

# Carrega configurações
BUCKET_NAME = os.getenv("AWS_BUCKET_NAME")
REGION = os.getenv("AWS_REGION", "us-east-1")

# Cria o cliente S3 uma única vez.
# In ECS, credentials come from the task IAM role automatically.
# Explicit keys (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY) are only needed
# for local development and are picked up by boto3 from env vars if present.
s3_client = boto3.client("s3", region_name=REGION)


def upload_bytes_to_s3(
    file_content: bytes, filename: str, content_type: str, folder: str = "uploads"
) -> str:
    """
    Faz upload de bytes brutos para o S3. Mais seguro para usar com asyncio.
    """
    import asyncio

    start = time.perf_counter_ns()
    try:
        # Gera nome único — sanitiza filename para evitar path traversal
        safe_name = os.path.basename(filename) if filename else ""
        if safe_name and "." in safe_name:
            ext = safe_name.rsplit(".", 1)[1].lower()
        else:
            ext = "bin"
        if ext not in ALLOWED_EXTENSIONS:
            ext = "bin"
        unique_filename = f"{folder}/{uuid4()}.{ext}"

        # Cria um objeto de arquivo em memória
        file_obj = io.BytesIO(file_content)

        s3_client.upload_fileobj(
            file_obj,
            BUCKET_NAME,
            unique_filename,
            ExtraArgs={"ContentType": content_type or "application/octet-stream"},
        )

        url = f"https://{BUCKET_NAME}.s3.{REGION}.amazonaws.com/{unique_filename}"
        elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                record_api_usage(
                    None,
                    "aws_s3",
                    "s3_put",
                    cost_usd=0.000005,
                    quantity=len(file_content),
                    duration_ms=elapsed_ms,
                )
            )
        except RuntimeError:
            pass  # No running loop (called outside async context)
        return url

    except NoCredentialsError:
        raise HTTPException(status_code=500, detail="Credenciais AWS não encontradas")
    except Exception as e:
        elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                record_api_usage(
                    None,
                    "aws_s3",
                    "s3_put",
                    cost_usd=0.0,
                    duration_ms=elapsed_ms,
                    success=False,
                )
            )
        except RuntimeError:
            pass
        logger.error("S3 upload failed: %s", e)
        raise HTTPException(status_code=500, detail="Falha ao fazer upload para S3")


def generate_presigned_url(s3_url: str, expiration: int = 3600) -> str:
    """Convert a raw S3 URL to a presigned URL (default 1h expiry)."""
    if not s3_url or not BUCKET_NAME:
        return s3_url or ""
    # Extract the S3 key from the URL
    prefix = f"https://{BUCKET_NAME}.s3.{REGION}.amazonaws.com/"
    if not s3_url.startswith(prefix):
        return s3_url
    key = s3_url[len(prefix) :]
    return s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET_NAME, "Key": key},
        ExpiresIn=expiration,
    )


def delete_s3_object(s3_url: str) -> bool:
    """Delete an object from S3 given its full URL. Returns True on success."""
    if not s3_url or not BUCKET_NAME:
        return False
    prefix = f"https://{BUCKET_NAME}.s3.{REGION}.amazonaws.com/"
    if not s3_url.startswith(prefix):
        return False
    key = s3_url[len(prefix) :]
    try:
        s3_client.delete_object(Bucket=BUCKET_NAME, Key=key)
        logger.info("Deleted S3 object: %s", key)
        return True
    except Exception as e:
        logger.error("Failed to delete S3 object %s: %s", key, e)
        return False


# Mantemos a antiga para compatibilidade se usada em outros lugares
def upload_file_to_s3(file: UploadFile, folder: str = "uploads") -> str:
    return upload_bytes_to_s3(
        file.file.read(), file.filename, file.content_type, folder
    )
