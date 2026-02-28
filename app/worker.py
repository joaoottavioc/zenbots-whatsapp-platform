# app/worker.py
import os
import logging
from arq.connections import RedisSettings
from arq.cron import cron
# Importamos a função pesada que já existe
from app.whatsapp import process_whatsapp_message
from app.database import async_session
from app.crud import cancel_expired_pix_orders

logger = logging.getLogger(__name__)


async def run_cancel_expired_pix_orders(ctx):
    async with async_session() as session:
        count = await cancel_expired_pix_orders(session)
        if count:
            logger.info("Cron: canceled %d expired PIX orders", count)

# Configurações do Redis (Pega do env ou usa default)
# Note o database=1 para separar da fila do RateLimit
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DATABASE = 1

# Configurações do Worker do ARQ
class WorkerSettings:
    # Configuração da conexão
    redis_settings = RedisSettings(
        host=REDIS_HOST,
        port=REDIS_PORT,
        database=REDIS_DATABASE
    )

    # Funções que este worker sabe executar
    functions = [process_whatsapp_message]

    # Cron jobs — cancel expired PIX orders every 5 minutes
    cron_jobs = [
        cron(run_cancel_expired_pix_orders, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
    ]

    # Configurações extras
    max_jobs = 10  # Quantas mensagens processar ao mesmo tempo por worker
    poll_delay = 0.5 # Tempo de espera se a fila estiver vazia

    # Retry & reliability
    max_tries = 3          # Retry failed jobs up to 3 times
    job_timeout = 300      # 5-minute timeout per job
    keep_result = 3600     # Keep results for 1 hour (debugging)

    @staticmethod
    async def on_job_error(ctx, job, exc_info):
        """Log job errors for observability."""
        exc_type, exc_value, _ = exc_info
        logger.error(
            "Job %s failed: %s: %s",
            job.function, exc_type.__name__, exc_value,
        )

    # Executado quando o worker inicia
    async def on_startup(self):
        logger.info("Message worker started, waiting for jobs")

    # Executado quando o worker desliga
    async def on_shutdown(self):
        logger.info("Message worker shutting down")