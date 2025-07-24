# app/payment_service.py
import os
import mercadopago
from typing import Dict, Any

# Carrega o Access Token de teste a partir das suas variáveis de ambiente
MERCADOPAGO_ACCESS_TOKEN = os.getenv("MERCADOPAGO_ACCESS_TOKEN")

# Inicializa o SDK do Mercado Pago com as suas credenciais
sdk = mercadopago.SDK(MERCADOPAGO_ACCESS_TOKEN)


def create_pix_payment(order_id: int, total_amount: float, customer_email: str, restaurant_name: str) -> Dict[str, Any]:
    """
    Cria uma nova cobrança Pix no Mercado Pago para um pedido específico.

    Returns:
        Um dicionário contendo os dados da cobrança, incluindo o QR Code e o Pix Copia e Cola.
    """
    try:
        # Define os dados da cobrança de acordo com a documentação da API do Mercado Pago
        payment_data = {
            "transaction_amount": total_amount,
            "description": f"Pedido #{order_id} - {restaurant_name}",
            "payment_method_id": "pix",
            "payer": {
                "email": customer_email,
                # Poderíamos adicionar o nome e o CPF do cliente se os tivéssemos
            },
            # URL para onde o Mercado Pago enviará a notificação (webhook) quando o pagamento for confirmado
            "notification_url": f"{os.getenv('BASE_URL')}/webhooks/payment-confirm",
            # Identificador externo para ligar esta cobrança ao nosso pedido no banco de dados
            "external_reference": str(order_id),
        }

        # Faz a chamada à API do Mercado Pago
        payment_response = sdk.payment().create(payment_data)

        # Verifica se a chamada à API foi bem-sucedida
        if payment_response["status"] == 201:
            print("Cobrança Pix criada com sucesso:", payment_response["response"])
            return payment_response["response"]
        else:
            print("Erro ao criar cobrança Pix:", payment_response)
            return None

    except Exception as e:
        print(f"Erro inesperado ao comunicar com a API do Mercado Pago: {e}")
        return None