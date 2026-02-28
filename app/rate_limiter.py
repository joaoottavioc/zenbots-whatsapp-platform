# app/rate_limiter.py
import json
import logging
import secrets
import redis.asyncio as redis
import os

logger = logging.getLogger(__name__)
from typing import Optional, Tuple

# Pega a URL do ambiente (definida no docker-compose) ou usa localhost como fallback
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Lazy singleton — avoids creating a connection at import time
_client = None

def _get_client():
    global _client
    if _client is None:
        _client = redis.from_url(
            REDIS_URL, decode_responses=True,
            socket_timeout=2, socket_connect_timeout=2,
        )
    return _client


async def is_spamming(user_phone: str, limit: int = 15, window_seconds: int = 60) -> bool:
    """
    Verifica se o usuário excedeu o limite de mensagens na janela de tempo.
    Retorna True se for SPAM (deve bloquear).
    """
    if not user_phone:
        return False

    # Cria uma chave única para este usuário
    key = f"spam:{user_phone}"

    try:
        r = _get_client()
        # Pipelining agrupa comandos para ser mais rápido (1 viagem ao Redis)
        pipe = r.pipeline()
        pipe.incr(key) # Incrementa contador
        pipe.ttl(key)  # Pega quanto tempo falta para expirar
        result = await pipe.execute()

        current_count = result[0]
        ttl = result[1]

        # Se é a primeira mensagem (ou expirou), define o tempo de vida (TTL)
        if current_count == 1 or ttl == -1:
            await r.expire(key, window_seconds)

        # Se passou do limite, é spam
        if current_count > limit:
            return True

        return False

    except redis.RedisError as e:
        # Se o Redis cair, bloqueamos por segurança (Fail Closed).
        # O ARQ worker também depende do Redis, então mensagens não seriam
        # processadas de qualquer forma.
        logger.error("Redis rate limiter error in is_spamming: %s", e)
        return True


async def is_rate_limited(key: str, limit: int, window_seconds: int) -> bool:
    """
    Generic rate limiter. Returns True if the key has exceeded `limit`
    requests within the sliding window. Fails closed on Redis errors.
    """
    try:
        r = _get_client()
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.ttl(key)
        result = await pipe.execute()

        current_count = result[0]
        ttl = result[1]

        if current_count == 1 or ttl == -1:
            await r.expire(key, window_seconds)

        return current_count > limit

    except redis.RedisError as e:
        logger.error("Redis rate limiter error in is_rate_limited for key=%s: %s", key, e)
        return True


async def store_oauth_state(user_id: int, bot_id: int) -> str:
    """
    Generate a CSRF-safe state token for OAuth flows.
    Stores user_id and bot_id in Redis with 10-minute TTL.
    Returns the token string.
    """
    token = secrets.token_urlsafe(32)
    key = f"oauth_state:{token}"
    value = json.dumps({"user_id": user_id, "bot_id": bot_id})
    r = _get_client()
    await r.set(key, value, ex=600)  # 10 minutes
    return token


async def consume_oauth_state(token: str) -> Optional[Tuple[int, int]]:
    """
    Atomically retrieve and delete an OAuth state token.
    Returns (user_id, bot_id) or None if the token is invalid/expired.
    """
    if not token:
        return None
    key = f"oauth_state:{token}"
    r = _get_client()
    raw = await r.getdel(key)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return (data["user_id"], data["bot_id"])
    except (json.JSONDecodeError, KeyError):
        return None


async def create_sse_ticket(user_id: int) -> str:
    """
    Create a short-lived, one-time-use ticket for SSE authentication.
    Stores sse_ticket:{token} → {"user_id": N} with 60s TTL.
    """
    token = secrets.token_urlsafe(32)
    key = f"sse_ticket:{token}"
    value = json.dumps({"user_id": user_id})
    r = _get_client()
    await r.set(key, value, ex=60)
    return token


async def consume_sse_ticket(ticket: str) -> Optional[int]:
    """
    Atomically retrieve and delete an SSE ticket.
    Returns user_id or None if the ticket is invalid/expired.
    """
    if not ticket:
        return None
    key = f"sse_ticket:{ticket}"
    r = _get_client()
    raw = await r.getdel(key)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data["user_id"]
    except (json.JSONDecodeError, KeyError):
        return None
