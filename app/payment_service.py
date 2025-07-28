# app/payment_service.py
import os
import mercadopago
from typing import Dict, Any

MERCADOPAGO_ACCESS_TOKEN = os.getenv("MERCADOPAGO_ACCESS_TOKEN")
# JÁ NÃO PRECISAMOS DO E-MAIL DE TESTE DO .ENV

sdk = mercadopago.SDK(MERCADOPAGO_ACCESS_TOKEN)

def create_pix_payment(order_id: int, total_amount: float, restaurant_name: str) -> Dict[str, Any]:
    """
    Cria uma nova cobrança Pix no Mercado Pago, usando um pagador de teste genérico
    reconhecido pelo sandbox para garantir a aprovação automática.
    """
    try:
        payment_data = {
            "transaction_amount": round(total_amount, 2),
            "description": f"Pedido #{order_id} - {restaurant_name}",
            "payment_method_id": "pix",
            "payer": {
                # 👇 MUDANÇA CRÍTICA AQUI 👇
                "email": "test_user_12345678@testuser.com", # E-mail genérico de teste
                "first_name": "APRO",
                "last_name": "TEST_USER"
            },
            "notification_url": f"{os.getenv('BASE_URL')}/webhooks/payment-confirm",
            "external_reference": str(order_id),
        }

        payment_response = sdk.payment().create(payment_data)

        if payment_response["status"] == 201:
            return payment_response["response"]
        else:
            print("Erro ao criar cobrança Pix:", payment_response)
            return None

    except Exception as e:
        print(f"Erro inesperado ao comunicar com a API do Mercado Pago: {e}")
        return None