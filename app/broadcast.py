# app/broadcast.py
import json
import logging
import os
import redis.asyncio as redis

logger = logging.getLogger(__name__)

# Usa o banco 0 (mesmo do Rate Limit) ou outro, tanto faz para Pub/Sub
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

async def broadcast_order_update(type: str, data: dict, bot_id: int = None):
    """
    Envia um sinal para o Redis avisando que algo mudou.
    type: 'new_order', 'status_change', etc.
    bot_id: when provided, publishes to 'dashboard_events:{bot_id}' instead of the global channel.
    """
    r = None
    try:
        # Conexão rápida apenas para publicar
        r = redis.from_url(
            REDIS_URL,
            socket_timeout=2, socket_connect_timeout=2,
        )
        message = {
            "type": type,
            "payload": data
        }
        channel = f"dashboard_events:{bot_id}" if bot_id else "dashboard_events"
        await r.publish(channel, json.dumps(message))
        logger.info("Broadcast sent: type=%s, channel=%s", type, channel)
    except Exception as e:
        logger.error("Broadcast failed for type=%s bot_id=%s: %s", type, bot_id, e)
    finally:
        if r is not None:
            await r.aclose()
