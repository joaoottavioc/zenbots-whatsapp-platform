# app/payment_routes.py
import logging
import os
import httpx

logger = logging.getLogger(__name__)
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_session
from app.auth import get_current_user
from app.models import User, Bot, PaymentConfig
from fastapi import Request
from sqlalchemy.orm import selectinload
from app.models import Order, OrderStatus
from app import crud
from app.rate_limiter import store_oauth_state, consume_oauth_state
from datetime import timedelta
from app.encryption import encrypt_value
from app.time import utcnow

router = APIRouter(prefix="/payments", tags=["payments"])

# Credenciais da SUA aplicação (O ZenBotz)
MP_CLIENT_ID = os.getenv("MP_CLIENT_ID")
MP_CLIENT_SECRET = os.getenv("MP_CLIENT_SECRET")
# A URL para onde o Mercado Pago devolve o usuário (Frontend)
MP_REDIRECT_URI = os.getenv("MP_REDIRECT_URI", "http://localhost:3000/pagamentos")

@router.get("/auth-url")
async def get_auth_url(
    bot_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """
    Gera a URL para o usuário logar no Mercado Pago e autorizar o ZenBotz.
    Requer bot_id para vincular a autorização ao bot correto.
    """
    if not MP_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Servidor mal configurado: MP_CLIENT_ID ausente.")

    # Validate bot ownership
    bot = await crud.get_bot_by_id(session, bot_id=bot_id)
    if not bot or bot.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Acesso negado ao bot.")

    # Generate CSRF state token
    state = await store_oauth_state(user_id=current_user.id, bot_id=bot_id)

    url = (
        f"https://auth.mercadopago.com.br/authorization"
        f"?client_id={MP_CLIENT_ID}"
        f"&response_type=code"
        f"&platform_id=mp"
        f"&redirect_uri={MP_REDIRECT_URI}"
        f"&state={state}"
    )
    return {"url": url}

@router.post("/callback")
async def exchange_token(
    body: dict,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """
    Recebe o 'code' do Frontend, troca por Access Token e salva no Bot do usuário.
    """
    code = body.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Código inválido.")

    # 0. Validate CSRF state token
    state_token = body.get("state")
    if not state_token:
        raise HTTPException(status_code=400, detail="State token ausente.")

    state_data = await consume_oauth_state(state_token)
    if not state_data:
        raise HTTPException(status_code=400, detail="State token inválido ou expirado.")

    state_user_id, state_bot_id = state_data
    if state_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="State token não corresponde ao usuário.")

    # 1. Troca o CODE pelo TOKEN
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.mercadopago.com/oauth/token",
            data={
                "client_secret": MP_CLIENT_SECRET,
                "client_id": MP_CLIENT_ID,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": MP_REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )

    if resp.status_code != 200:
        logger.error("MP OAuth token exchange failed, status_code=%s", resp.status_code)
        raise HTTPException(status_code=400, detail="Falha ao conectar com Mercado Pago.")

    data = resp.json()
    access_token = data.get("access_token")
    public_key = data.get("public_key")
    refresh_token = data.get("refresh_token")
    expires_in = data.get("expires_in")  # seconds until expiry
    user_id_mp = data.get("user_id")

    # 2. Select the specific bot from the state token (not .first())
    bot = await crud.get_bot_by_id(session, bot_id=state_bot_id)

    if not bot or bot.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Bot não encontrado ou acesso negado.")

    # 3. Salva ou Atualiza a Configuração
    # Carrega a config existente se houver
    await session.refresh(bot, attribute_names=["payment_config"])

    encrypted_access_token = encrypt_value(access_token) if access_token else ""
    encrypted_public_key = encrypt_value(public_key) if public_key else ""
    encrypted_refresh_token = encrypt_value(refresh_token) if refresh_token else ""
    token_expires_at = utcnow() + timedelta(seconds=expires_in) if expires_in else None

    if bot.payment_config:
        config = bot.payment_config
        config.access_token = encrypted_access_token
        config.public_key = encrypted_public_key
        config.refresh_token = encrypted_refresh_token
        config.token_expires_at = token_expires_at
        config.is_active = True
    else:
        config = PaymentConfig(
            bot_id=bot.id,
            provider="mercadopago",
            access_token=encrypted_access_token,
            public_key=encrypted_public_key,
            refresh_token=encrypted_refresh_token,
            token_expires_at=token_expires_at,
            is_active=True
        )
        session.add(config)

    await session.commit()
    return {"status": "connected", "bot_name": bot.restaurant_name}

@router.get("/status")
async def get_status(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    stmt = select(Bot).where(Bot.user_id == current_user.id)
    result = await session.execute(stmt)
    bot = result.scalars().first()
    
    if bot:
        await session.refresh(bot, attribute_names=["payment_config"])
        if bot.payment_config and bot.payment_config.is_active:
             return {"is_active": True, "provider": "mercadopago"}
             
    return {"is_active": False}
