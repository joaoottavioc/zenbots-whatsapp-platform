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

# Carrega configurações
AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
BUCKET_NAME = os.getenv("AWS_BUCKET_NAME")
REGION = os.getenv("AWS_REGION", "us-east-1")

# Cria o cliente S3 uma única vez
s3_client = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=REGION
)

def upload_bytes_to_s3(file_content: bytes, filename: str, content_type: str, folder: str = "uploads") -> str:
    """
    Faz upload de bytes brutos para o S3. Mais seguro para usar com asyncio.
    """
    import asyncio
    start = time.perf_counter_ns()
    try:
        # Gera nome único
        # Se filename for None ou vazio, usa .bin como fallback
        ext = filename.split(".")[-1] if filename and "." in filename else "bin"
        unique_filename = f"{folder}/{uuid4()}.{ext}"

        # Cria um objeto de arquivo em memória
        file_obj = io.BytesIO(file_content)

        s3_client.upload_fileobj(
            file_obj,
            BUCKET_NAME,
            unique_filename,
            ExtraArgs={'ContentType': content_type or "application/octet-stream"}
        )

        url = f"https://{BUCKET_NAME}.s3.{REGION}.amazonaws.com/{unique_filename}"
        elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(record_api_usage(
                None, "aws_s3", "s3_put", cost_usd=0.000005,
                quantity=len(file_content), duration_ms=elapsed_ms,
            ))
        except RuntimeError:
            pass  # No running loop (called outside async context)
        return url

    except NoCredentialsError:
        raise HTTPException(status_code=500, detail="Credenciais AWS não encontradas")
    except Exception as e:
        elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(record_api_usage(
                None, "aws_s3", "s3_put", cost_usd=0.0,
                duration_ms=elapsed_ms, success=False,
            ))
        except RuntimeError:
            pass
        logger.error("S3 upload failed: %s", e)
        raise HTTPException(status_code=500, detail="Falha ao fazer upload para S3")

# Mantemos a antiga para compatibilidade se usada em outros lugares
def upload_file_to_s3(file: UploadFile, folder: str = "uploads") -> str:
    return upload_bytes_to_s3(file.file.read(), file.filename, file.content_type, folder)