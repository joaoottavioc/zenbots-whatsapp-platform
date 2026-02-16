# app/payment_service.py
import os
import mercadopago
from typing import Dict, Any, Optional
from datetime import datetime, timedelta

# NÃO inicializamos mais o SDK globalmente aqui fora.
# sdk = mercadopago.SDK(...) <- REMOVER ISSO

async def create_pix_payment(
    order_id: int,
    total_amount: float,
    bot_name: str,
    contact_phone: str,
    access_token_cliente: str # <--- OBRIGATÓRIO: O Token do dono do bot
) -> Optional[Dict[str, Any]]:
    """
    Cria uma cobrança PIX no Mercado Pago usando o TOKEN DO CLIENTE ESPECÍFICO.
    """
    
    if not access_token_cliente:
        print(f"❌ Erro: Tentativa de criar pagamento sem Access Token para o pedido #{order_id}")
        return None

    # ▼▼▼ INICIALIZAÇÃO DINÂMICA (A MÁGICA ACONTECE AQUI) ▼▼▼
    # O SDK é criado instantaneamente apenas para esta transação
    try:
        sdk = mercadopago.SDK(access_token_cliente)
    except Exception as e:
        print(f"❌ Erro ao inicializar SDK do MP com token fornecido: {e}")
        return None

    expiration_time = datetime.utcnow() + timedelta(minutes=15)
    expiration_date_iso = expiration_time.isoformat("T", "milliseconds") + "Z"

    # URL base para Webhook (Produção ou Ngrok)
    base_url = os.getenv("BASE_URL")
    if not base_url:
        print("⚠️ AVISO: BASE_URL não configurada no .env. Webhook pode falhar.")

    payment_data = {
        "transaction_amount": round(total_amount, 2),
        "description": f"Pedido #{order_id} - {bot_name}", 
        "payment_method_id": "pix",
        "date_of_expiration": expiration_date_iso,
        "payer": {
            "email": f"{contact_phone}@zenbotz.com.br", # Email fictício para o pagador (MP exige email)
        },
        "external_reference": str(order_id),
        
        # O Webhook precisa ser notificado na sua URL global
        "notification_url": f"{base_url}/payments/webhooks/payment-confirm/{order_id}"
    }

    try:
        # Chamada real ao Mercado Pago
        request_options = mercadopago.config.RequestOptions()
        request_options.custom_headers = {
            'x-idempotency-key': str(order_id) # Evita cobrança duplicada se tentar gerar 2x
        }

        result = sdk.payment().create(payment_data, request_options)

        if result["status"] in [200, 201]:
            # Captura os dados do PIX tanto se for novo (201) quanto se já existir (200)
            pix_data = result["response"].get("point_of_interaction", {}).get("transaction_data")
    
            if pix_data:
                return {
                    "qr_code_base64": pix_data.get("qr_code_base64"),
                    "qr_code_url": pix_data.get("ticket_url"),
                    "pix_copy_paste": pix_data.get("qr_code")
                }
            else:
                print(f"❌ Erro Mercado Pago (Status {result.get('status')}):", result.get("response"))
                return None

    except Exception as e:
        print(f"❌ Erro CRÍTICO ao chamar API do Mercado Pago: {e}")
        return None