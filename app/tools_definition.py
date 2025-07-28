# app/tools_definition.py

# Este ficheiro descreve as "ferramentas" que a nossa IA pode usar.
# A estrutura segue o padrão de "function calling" da OpenAI.

# app/tools_definition.py

tools_schema = [
        {
        "type": "function",
        "function": {
            "name": "add_items_to_cart", # Nome no plural
            "description": "Adiciona UM OU MAIS itens ao carrinho. Use para todos os produtos que o cliente pedir em uma única mensagem.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "Uma lista de itens para adicionar, cada um com ID e quantidade.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "product_id": {"type": "integer", "description": "O ID do produto."},
                                "quantity": {"type": "integer", "description": "A quantidade."},
                            },
                            "required": ["product_id", "quantity"],
                        },
                    }
                },
                "required": ["items"],
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
    },
    { # 👇 NOVA FERRAMENTA 👇
        "type": "function", "function": {
            "name": "remove_item_from_cart",
            "description": "Remove um item específico do carrinho de compras do cliente quando ele pede para o fazer.",
            "parameters": {
                "type": "object", "properties": {
                     "product_id": {"type": "integer", "description": "O ID do produto a ser removido."},
                }, "required": ["product_id"],
            },
        },
    }
]