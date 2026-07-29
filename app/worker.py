# app/worker.py
import os
import logging
from arq.connections import RedisSettings
from arq.cron import cron
from app.logging_config import setup_logging
from app.monitoring import start_flush_task, stop_flush_task, run_aggregate_daily_costs

# Importamos a função pesada que já existe
from app.whatsapp import process_whatsapp_message, process_chat_message
from app.menu_extraction import process_menu_extraction
from app.database import async_session
from app.crud import cancel_expired_pix_orders, cleanup_old_conversation_history

logger = logging.getLogger(__name__)


async def run_cancel_expired_pix_orders(ctx):
    async with async_session() as session:
        canceled = await cancel_expired_pix_orders(session)
        if canceled:
            logger.info("Cron: canceled %d expired PIX orders", len(canceled))
            from app.whatsapp import send_customer_notification
            from app.encryption import decrypt_value

            for info in canceled:
                try:
                    wa_token = (
                        decrypt_value(info["bot_token"])
                        if info.get("bot_token")
                        else None
                    )
                    await send_customer_notification(
                        channel=info.get("channel"),
                        bot_id=info.get("bot_id"),
                        contact_phone=info["contact_phone"],
                        message=(
                            "Seu código PIX expirou. 😕 "
                            "Se ainda quiser pedir, é só mandar uma mensagem!"
                        ),
                        whatsapp_token=wa_token,
                        whatsapp_phone_id=info.get("bot_phone_id"),
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to notify customer about expired PIX order #%s: %s",
                        info["order_id"],
                        e,
                    )


async def run_cleanup_conversation_history(ctx):
    async with async_session() as session:
        count = await cleanup_old_conversation_history(session)
        if count:
            logger.info("Cron: cleaned up %d old conversation history records", count)


# Configurações do Redis (Pega do env ou usa default)
# Note o database=1 para separar da fila do RateLimit
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DATABASE = 1


# Configurações do Worker do ARQ
class WorkerSettings:
    # Configuração da conexão
    redis_settings = RedisSettings(
        host=REDIS_HOST, port=REDIS_PORT, database=REDIS_DATABASE
    )

    # Funções que este worker sabe executar
    functions = [
        process_whatsapp_message,
        process_chat_message,
        process_menu_extraction,
    ]

    # Cron jobs — cancel expired PIX orders every 5 minutes + daily cost aggregation at 3 AM UTC
    cron_jobs = [
        cron(
            run_cancel_expired_pix_orders,
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
        ),
        cron(run_aggregate_daily_costs, hour={3}, minute={0}),
        cron(run_cleanup_conversation_history, hour={4}, minute={0}),
    ]

    # Configurações extras
    max_jobs = 10  # Quantas mensagens processar ao mesmo tempo por worker
    poll_delay = 0.5  # Tempo de espera se a fila estiver vazia

    # Retry & reliability
    max_tries = 3  # Retry failed jobs up to 3 times
    job_timeout = 300  # 5-minute timeout per job
    keep_result = 3600  # Keep results for 1 hour (debugging)

    @staticmethod
    async def on_job_error(ctx, job, exc_info):
        """Log job errors for observability."""
        exc_type, exc_value, _ = exc_info
        logger.error(
            "Job %s failed: %s: %s",
            job.function,
            exc_type.__name__,
            exc_value,
        )

    # Executado quando o worker inicia
    async def on_startup(self):
        setup_logging()
        start_flush_task()

        # Pre-warm the embedding model before polling for jobs. This process
        # has its own copy of the lazy singleton in embedding_service.py —
        # separate from the backend's, which pre-warms itself in main.py's
        # lifespan. Without this, the model loads on-demand inside the first
        # real job of the day (e.g. right after the scheduled 9 AM ECS
        # scale-up), stalling that customer's reply. Awaited (not
        # fire-and-forget) because no jobs are dequeued until this returns,
        # so blocking here is free; any job enqueued meanwhile just waits in
        # Redis. Guarded so a load failure degrades gracefully instead of
        # crashing worker startup.
        import asyncio
        from app.embedding_service import _get_model, EmbeddingsUnavailable

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, _get_model)
            logger.info("Embedding model pre-warmed on worker startup")
        except EmbeddingsUnavailable as e:
            logger.warning("Embedding model pre-warm failed on worker startup: %s", e)

        logger.info("Message worker started, waiting for jobs")

    # Executado quando o worker desliga
    async def on_shutdown(self):
        await stop_flush_task()
        logger.info("Message worker shutting down")
