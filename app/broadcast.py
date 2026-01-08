# app/broadcast.py
import os
import redis.asyncio as redis

# Usa o banco 0 (mesmo do Rate Limit) ou outro, tanto faz para Pub/Sub
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

async def broadcast_order_update(type: str, data: dict):
    """
    Envia um sinal para o Redis avisando que algo mudou.
    type: 'new_order', 'status_change', etc.
    """
    try:
        # Conexão rápida apenas para publicar
        r = redis.from_url(REDIS_URL)
        message = {
            "type": type,
            "payload": data
        }
        # Publica no canal 'dashboard_events'
        await r.publish("dashboard_events", str(message))
        await r.aclose()
        print(f"📡 Broadcast enviado: {type}")
    except Exception as e:
        print(f"⚠️ Falha no broadcast: {e}")