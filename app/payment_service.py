# app/payment_service.py
import logging
import os
import mercadopago
import httpx
from typing import Dict, Any, Optional
from datetime import timedelta
from app.time import utcnow
from app.encryption import encrypt_value, decrypt_value

logger = logging.getLogger(__name__)

MP_CLIENT_ID = os.getenv("MP_CLIENT_ID")
MP_CLIENT_SECRET = os.getenv("MP_CLIENT_SECRET")


async def refresh_mp_token(config, session) -> Optional[str]:
    """
    Refresh an expired Mercado Pago access token using the stored refresh_token.
    Updates config in-place and commits. Returns the new plaintext access_token or None.
    """
    if not config.refresh_token:
        logger.warning("No refresh_token stored for PaymentConfig %s", config.id)
        return None

    plaintext_refresh = decrypt_value(config.refresh_token)
    if not plaintext_refresh:
        logger.warning(
            "Empty refresh_token after decryption for PaymentConfig %s", config.id
        )
        return None

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.mercadopago.com/oauth/token",
                data={
                    "client_secret": MP_CLIENT_SECRET,
                    "client_id": MP_CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": plaintext_refresh,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if resp.status_code != 200:
            logger.error("MP token refresh failed, status_code=%s", resp.status_code)
            return None

        data = resp.json()
        new_access = data.get("access_token")
        new_refresh = data.get("refresh_token")
        expires_in = data.get("expires_in")

        if not new_access:
            logger.error("MP token refresh response missing access_token")
            return None

        config.access_token = encrypt_value(new_access)
        if new_refresh:
            config.refresh_token = encrypt_value(new_refresh)
        if expires_in:
            config.token_expires_at = utcnow() + timedelta(seconds=expires_in)
        config.updated_at = utcnow()

        session.add(config)
        await session.commit()

        logger.info(
            "OAUTH_TOKEN_REFRESH_SUCCESS config_id=%s bot_id=%s provider=mercadopago",
            config.id,
            config.bot_id,
        )
        return new_access

    except Exception as e:
        logger.error("MP token refresh error: %s", e)
        return None


async def get_valid_access_token(config, session) -> Optional[str]:
    """
    Return a valid plaintext access token, refreshing if expired.
    Uses a distributed lock to prevent concurrent refresh race conditions.
    """
    if config.token_expires_at and utcnow() >= config.token_expires_at:
        logger.info("MP token expired for PaymentConfig %s, refreshing...", config.id)
        from app.distributed_lock import _get_client

        lock = _get_client().lock(
            name=f"mp_token_refresh:{config.id}",
            timeout=30,
            blocking_timeout=35,
            sleep=0.5,
        )
        try:
            if await lock.acquire(blocking=True):
                try:
                    # Re-check after acquiring lock (another process may have refreshed)
                    await session.refresh(config)
                    if config.token_expires_at and utcnow() >= config.token_expires_at:
                        return await refresh_mp_token(config, session)
                    # Token was refreshed by another process while we waited
                    return (
                        decrypt_value(config.access_token)
                        if config.access_token
                        else None
                    )
                finally:
                    await lock.release()
            else:
                # Lock not acquired within timeout; re-read and try to use current token
                logger.warning(
                    "Could not acquire refresh lock for PaymentConfig %s", config.id
                )
                await session.refresh(config)
                return (
                    decrypt_value(config.access_token) if config.access_token else None
                )
        except Exception as e:
            logger.error("Error during locked token refresh: %s", e)
            return None
    return decrypt_value(config.access_token) if config.access_token else None


async def create_pix_payment(
    order_id: int,
    total_amount: float,
    bot_name: str,
    contact_phone: str,
    access_token_cliente: str,  # <--- OBRIGATÓRIO: O Token do dono do bot
    webhook_token: str = "",  # Token for webhook URL authentication
) -> Optional[Dict[str, Any]]:
    """
    Cria uma cobrança PIX no Mercado Pago usando o TOKEN DO CLIENTE ESPECÍFICO.
    """

    if not access_token_cliente:
        logger.error(
            "Attempted to create payment without access token for order_id=%s", order_id
        )
        return None

    # ▼▼▼ INICIALIZAÇÃO DINÂMICA (A MÁGICA ACONTECE AQUI) ▼▼▼
    # O SDK é criado instantaneamente apenas para esta transação
    try:
        sdk = mercadopago.SDK(access_token_cliente)
    except Exception as e:
        logger.error("Failed to initialize MP SDK for order_id=%s: %s", order_id, e)
        return None

    expiration_time = utcnow() + timedelta(minutes=15)
    expiration_date_iso = expiration_time.isoformat("T", "milliseconds") + "Z"

    # URL base para Webhook (Produção ou Ngrok)
    base_url = os.getenv("BASE_URL")
    if not base_url:
        logger.warning("BASE_URL not configured, payment webhook may fail")

    payment_data = {
        "transaction_amount": round(total_amount, 2),
        "description": f"Pedido #{order_id} - {bot_name}",
        "payment_method_id": "pix",
        "date_of_expiration": expiration_date_iso,
        "payer": {
            "email": f"{contact_phone}@zenbotz.com.br",  # Email fictício para o pagador (MP exige email)
        },
        "external_reference": str(order_id),
        # O Webhook precisa ser notificado na sua URL global
        "notification_url": f"{base_url}/payments/webhooks/payment-confirm/{order_id}?token={webhook_token}",
    }

    try:
        # Chamada real ao Mercado Pago
        request_options = mercadopago.config.RequestOptions()
        request_options.custom_headers = {
            "x-idempotency-key": str(
                order_id
            )  # Evita cobrança duplicada se tentar gerar 2x
        }

        result = sdk.payment().create(payment_data, request_options)

        if result["status"] in [200, 201]:
            # Captura os dados do PIX tanto se for novo (201) quanto se já existir (200)
            pix_data = (
                result["response"]
                .get("point_of_interaction", {})
                .get("transaction_data")
            )

            if pix_data:
                return {
                    "qr_code_base64": pix_data.get("qr_code_base64"),
                    "qr_code_url": pix_data.get("ticket_url"),
                    "pix_copy_paste": pix_data.get("qr_code"),
                }
            else:
                logger.error(
                    "MP payment creation failed for order_id=%s, status=%s",
                    order_id,
                    result.get("status"),
                )
                return None

    except Exception:
        logger.exception("Critical error calling MP API for order_id=%s", order_id)
        return None
