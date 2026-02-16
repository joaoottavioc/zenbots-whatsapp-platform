# app/payment_routes.py
import os
import httpx
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

router = APIRouter(prefix="/payments", tags=["payments"])

# Credenciais da SUA aplicação (O ZenBotz)
MP_CLIENT_ID = os.getenv("MP_CLIENT_ID")
MP_CLIENT_SECRET = os.getenv("MP_CLIENT_SECRET")
# A URL para onde o Mercado Pago devolve o usuário (Frontend)
MP_REDIRECT_URI = os.getenv("MP_REDIRECT_URI", "http://localhost:3000/pagamentos")

@router.get("/auth-url")
async def get_auth_url(current_user: User = Depends(get_current_user)):
    """
    Gera a URL para o usuário logar no Mercado Pago e autorizar o ZenBotz.
    """
    if not MP_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Servidor mal configurado: MP_CLIENT_ID ausente.")
    
    # State pode ser usado para segurança ou para passar o ID do bot, se necessário.
    url = (
        f"https://auth.mercadopago.com.br/authorization"
        f"?client_id={MP_CLIENT_ID}"
        f"&response_type=code"
        f"&platform_id=mp"
        f"&redirect_uri={MP_REDIRECT_URI}"
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

    # 1. Troca o CODE pelo TOKEN
    async with httpx.AsyncClient() as client:
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
        print(f"❌ Erro MP OAuth: {resp.text}")
        raise HTTPException(status_code=400, detail="Falha ao conectar com Mercado Pago.")

    data = resp.json()
    access_token = data.get("access_token")
    public_key = data.get("public_key")
    refresh_token = data.get("refresh_token")
    user_id_mp = data.get("user_id")

    # 2. Descobre qual é o Bot desse usuário (Para MVP assumimos 1 bot por user)
    # Se tiver mais, precisaria passar o bot_id no state ou selecionar antes
    stmt = select(Bot).where(Bot.user_id == current_user.id)
    result = await session.execute(stmt)
    bot = result.scalars().first()

    if not bot:
        raise HTTPException(status_code=404, detail="Você precisa criar um Bot antes de configurar pagamentos.")

    # 3. Salva ou Atualiza a Configuração
    # Carrega a config existente se houver
    await session.refresh(bot, attribute_names=["payment_config"])
    
    if bot.payment_config:
        config = bot.payment_config
        config.access_token = access_token
        config.public_key = public_key
        config.is_active = True
    else:
        config = PaymentConfig(
            bot_id=bot.id,
            provider="mercadopago",
            access_token=access_token,
            public_key=public_key,
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
