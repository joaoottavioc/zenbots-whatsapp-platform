# app/payment_service.py
import os
import mercadopago
from typing import Dict, Any
from datetime import datetime, timedelta

MERCADOPAGO_ACCESS_TOKEN = os.getenv("MERCADOPAGO_ACCESS_TOKEN")
# JÁ NÃO PRECISAMOS DO E-MAIL DE TESTE DO .ENV

sdk = mercadopago.SDK(MERCADOPAGO_ACCESS_TOKEN)

async def create_pix_payment(
    order_id: int,
    total_amount: float,
    bot_name: str, # <-- 1. ADICIONA O PARÂMETRO FALTANTE
    contact_phone: str
):
    """
    Cria uma cobrança PIX no Mercado Pago e retorna os dados para pagamento.
    """
    expiration_time = datetime.utcnow() + timedelta(minutes=15)
    expiration_date_iso = expiration_time.isoformat("T", "milliseconds") + "Z"

    payment_data = {
        "transaction_amount": round(total_amount, 2),
        "description": f"Pedido #{order_id} - {bot_name}", # <-- 2. USA O NOVO PARÂMETRO
        "payment_method_id": "pix",
        "date_of_expiration": expiration_date_iso,
        "payer": {
            "email": f"{contact_phone}@zenbots.com.br", # Usando um domínio próprio para o email
        },
        "external_reference": str(order_id),
        "notification_url": "https://34e94746f8d5.ngrok-free.app/webhooks/payment-confirm"
    }

    try:
        result = sdk.payment().create(payment_data)
        if result["status"] == 201:
            pix_data = result["response"]["point_of_interaction"]["transaction_data"]
            return {
                "qr_code_base64": pix_data["qr_code_base64"],
                "qr_code_url": pix_data["ticket_url"],
                "pix_copy_paste": pix_data["qr_code"]
            }
        else:
            # Adicione este print para ver a resposta em caso de falha (status diferente de 201)
            print("❌ Resposta do Mercado Pago (Falha):", result)
            return None
    except Exception as e:
        print(f"❌ Erro CRÍTICO ao chamar a API do Mercado Pago: {e}")
        return None