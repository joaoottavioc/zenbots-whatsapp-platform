import logging
import os
from contextlib import asynccontextmanager

from app.logging_config import setup_logging

setup_logging()

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from dotenv import load_dotenv
from arq import create_pool
from arq.connections import RedisSettings
from app.payment_routes import router as payment_router
from app import utils

# Importações dos seus módulos locais
from app.database import engine
from app import whatsapp, auth, bot_routes, takeover_routes
from app.auth import get_user_from_token
from app import crud
from app.rate_limiter import consume_sse_ticket
import redis.asyncio as redis
import asyncio
import json
from app import billing_routes
from app import admin_routes
from app import menu_router
from app import health_routes
from app import monitoring_routes
from app.monitoring import start_flush_task, stop_flush_task
from app.email_service import validate_email_config

load_dotenv()

logger = logging.getLogger(__name__)

# Configurações do Redis para a Fila
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DATABASE = 1  # Mesmo banco definido no worker.py

# --- Startup env var validation ---
REQUIRED_ENV_VARS = [
    "DATABASE_URL",
    "SECRET_KEY",
    "ENCRYPTION_KEY",
    "OPENAI_API_KEY",
]

PRODUCTION_ENV_VARS = [
    "WHATSAPP_TOKEN",
    "CORS_ORIGINS",
]


def validate_required_env_vars():
    env = os.getenv("ENVIRONMENT", "development")
    required = list(REQUIRED_ENV_VARS)
    if env == "production":
        required.extend(PRODUCTION_ENV_VARS)
    missing = [v for v in required if not os.getenv(v)]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- INICIALIZAÇÃO (STARTUP) ---
    validate_required_env_vars()
    validate_email_config()
    logger.info("Inicializando aplicacao...")

    # Schema management is handled by Alembic migrations (alembic upgrade head)
    # in CI/CD pipeline — no runtime DDL needed.

    # 1. Cria o pool de conexão com o Redis da Fila (ARQ)
    logger.info("Conectando ao Redis Queue...")
    try:
        app.state.arq_redis = await create_pool(
            RedisSettings(host=REDIS_HOST, port=REDIS_PORT, database=REDIS_DATABASE)
        )
        logger.info("Conexao com Redis Queue estabelecida.")
    except Exception as e:
        logger.error("Falha ao conectar no Redis: %s", e)

    # 3. Pre-warm embedding model in background thread (avoids blocking
    #    health checks on first request)
    from app.embedding_service import _get_model

    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _get_model)
    logger.info("Embedding model pre-warm scheduled (background thread).")

    # 4. Start monitoring flush background task
    start_flush_task()

    yield  # O servidor roda aqui e atende as requisições

    # --- ENCERRAMENTO (SHUTDOWN) ---
    logger.info("Shutting down — draining connections")
    await stop_flush_task()
    logger.info("Fechando conexao com Redis Queue...")
    if hasattr(app.state, "arq_redis"):
        await app.state.arq_redis.close()
    await engine.dispose()
    logger.info("Aplicacao encerrada.")


# Criação única da aplicação com o ciclo de vida configurado
app = FastAPI(lifespan=lifespan)

# --- Configuração do CORS ---


def build_cors_kwargs() -> dict:
    """Build CORS middleware kwargs based on environment variables."""
    origins_raw = os.getenv("CORS_ORIGINS", "")
    origin_regex = os.getenv("CORS_ORIGIN_REGEX", "")
    environment = os.getenv("ENVIRONMENT", "development")

    kwargs: dict = dict(
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if origins_raw:
        origins = [o.strip() for o in origins_raw.split(",") if o.strip()]
        kwargs["allow_origins"] = origins
        logger.info("CORS: explicit origins=%s", origins)
    elif origin_regex:
        kwargs["allow_origin_regex"] = origin_regex
        logger.info("CORS: regex=%s", origin_regex)
    elif environment == "development":
        kwargs["allow_origin_regex"] = "https?://.*"
        logger.info("CORS: development fallback (allow all http/https origins)")
    else:
        # Production without explicit origins: restrictive default
        logger.warning(
            "CORS_ORIGINS not set in production. No origins will be allowed. "
            "Set CORS_ORIGINS env var with comma-separated origins."
        )
        kwargs["allow_origins"] = []

    return kwargs


from app.csrf import CSRFMiddleware

_cors_kwargs = build_cors_kwargs()
# Starlette LIFO: last added = outermost. CORS must be outermost so it
# adds headers to ALL responses (including CSRF 403 rejections).
# CSRFMiddleware is pure ASGI (not BaseHTTPMiddleware) to avoid the known
# Starlette bug where BaseHTTPMiddleware swallows CORS headers on
# error/empty-body responses (e.g. DELETE 204).
app.add_middleware(CSRFMiddleware)
app.add_middleware(CORSMiddleware, **_cors_kwargs)

# --- Inclusão das Rotas ---
app.include_router(whatsapp.router)
app.include_router(auth.router, prefix="/auth", tags=["Auth"])
app.include_router(bot_routes.router)
app.include_router(takeover_routes.router)
app.include_router(payment_router)
app.include_router(billing_routes.router)
app.include_router(admin_routes.router)
app.include_router(menu_router.router)
app.include_router(utils.router)
app.include_router(health_routes.router)
app.include_router(monitoring_routes.router)


# Rota de verificação de saúde (Health Check)
@app.get("/")
async def root():
    return {"status": "ZenBots API Online 🚀", "queue": "Active"}


@app.get("/stream")
async def stream_events(
    request: Request,
    ticket: str = Query(default=None),
):
    """
    Authenticated SSE endpoint.
    Supports auth methods (checked in order):
      1. ticket query param — short-lived, one-time-use (best practice)
      2. Cookie (access_token) — browser session auth
      3. Authorization: Bearer <jwt> header — standard header auth
    """
    from app.database import async_session

    user = None

    # --- Authentication gate ---
    if ticket:
        # Ticket-based auth: atomic consume from Redis (one-time use)
        user_id = await consume_sse_ticket(ticket)
        if not user_id:
            return JSONResponse(
                content={"detail": "Invalid or expired ticket"}, status_code=401
            )
        async with async_session() as session:
            user = await crud.get_user_by_id(session, user_id)
            if not user:
                return JSONResponse(
                    content={"detail": "User not found"}, status_code=401
                )
    else:
        # Try cookie auth
        cookie_token = request.cookies.get("access_token")
        if cookie_token:
            async with async_session() as session:
                user = await get_user_from_token(cookie_token, session)
                if not user:
                    return JSONResponse(
                        content={"detail": "Invalid or expired token"}, status_code=401
                    )

        if not user:
            # Extract JWT from Authorization header
            bearer_token = None
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("Bearer "):
                bearer_token = auth_header[7:]

            if bearer_token:
                async with async_session() as session:
                    user = await get_user_from_token(bearer_token, session)
                    if not user:
                        return JSONResponse(
                            content={"detail": "Invalid or expired token"},
                            status_code=401,
                        )
            else:
                return JSONResponse(
                    content={"detail": "Authentication required"}, status_code=401
                )

    async with async_session() as session:
        bots = await crud.list_user_bots(session, user.id)
        if not bots:
            return JSONResponse(content={"detail": "No bots found"}, status_code=404)

        bot_ids = [bot.id for bot in bots]

    async def event_generator():
        # 1. Configuração da Conexão Redis
        local_redis_url = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"

        r = redis.from_url(
            local_redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
        pubsub = r.pubsub()

        # Subscribe to per-bot channels only
        channels = [f"dashboard_events:{bid}" for bid in bot_ids]
        await pubsub.subscribe(*channels)

        try:
            logger.info("SSE client connected, bot_ids=%s", bot_ids)
            # Envia o ping inicial já em formato JSON correto
            yield f"data: {json.dumps({'type': 'ping', 'message': 'connected'})}\n\n"

            while True:
                # 2. Verifica desconexão do cliente
                if await request.is_disconnected():
                    logger.info("SSE client disconnected")
                    break

                # 3. Aguarda mensagem do Redis (Timeout curto p/ manter o loop rodando)
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )

                if message:
                    raw_data = message["data"]
                    # broadcast.py now publishes valid JSON, so parse directly
                    try:
                        json.loads(raw_data)  # validate
                        final_payload = raw_data
                    except json.JSONDecodeError:
                        logger.warning("Invalid JSON received from Redis PubSub")
                        final_payload = json.dumps(
                            {"type": "error", "message": "Dados inválidos do Redis"}
                        )

                    logger.info("Sending SSE event to client")
                    yield f"data: {final_payload}\n\n"
                else:
                    # 4. Heartbeat (Mantém a conexão viva em Load Balancers/Nginx)
                    yield ": keep-alive\n\n"

        except asyncio.CancelledError:
            logger.info("SSE client disconnected (CancelledError)")
        except Exception as e:
            logger.error("Critical error in SSE stream: %s", e)
            # Tenta avisar o front se a conexão ainda existir
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            # 5. Limpeza de recursos
            try:
                await pubsub.unsubscribe(*channels)
                await r.aclose()
            except:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Essencial para Nginx
        },
    )
