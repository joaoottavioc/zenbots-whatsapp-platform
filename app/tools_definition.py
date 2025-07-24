# app/tools_definition.py

# Este ficheiro descreve as "ferramentas" que a nossa IA pode usar.
# A estrutura segue o padrão de "function calling" da OpenAI.

# app/tools_definition.py

tools_schema = [
    {
        "type": "function", "function": {
            "name": "add_item_to_cart",
            "description": "Adiciona um item ao carrinho de compras do cliente.",
            "parameters": {
                "type": "object", "properties": {
                    "product_id": {"type": "integer"}, "quantity": {"type": "integer"},
                }, "required": ["product_id", "quantity"],
            },
        },
    },
    {
        "type": "function", "function": {
            "name": "request_customer_address",
            "description": "Use esta ferramenta APENAS quando o cliente indicar que terminou de adicionar itens (ex: 'só isso', 'pode fechar a conta').",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function", "function": {
            "name": "process_order_with_address",
            "description": "Use esta ferramenta APENAS quando o ESTADO DA CONVERSA for 'AWAITING_ADDRESS' e o cliente tiver fornecido um endereço.",
            "parameters": {
                "type": "object", "properties": {
                     "customer_address": {"type": "string"},
                }, "required": ["customer_address"],
            },
        },
    },
    {
        "type": "function", "function": {
            "name": "process_payment_choice",
            "description": "Use esta ferramenta APENAS quando o ESTADO DA CONVERSA for 'AWAITING_PAYMENT_METHOD' e o cliente tiver escolhido um método de pagamento.",
            "parameters": {
                "type": "object", "properties": {
                     "method": {"type": "string", "enum": ["PIX", "CARD"], "description": "O método de pagamento escolhido."},
                }, "required": ["method"],
            },
        },
    },
    {
        "type": "function", "function": {
            "name": "answer_conversationally",
            "description": "Use para responder a saudações e perguntas gerais sobre o cardápio.",
            "parameters": {
                "type": "object", "properties": {
                     "response_text": {"type": "string"},
                }, "required": ["response_text"],
            },
        },
    }
]