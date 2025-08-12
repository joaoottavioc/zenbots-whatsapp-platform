# app/tools_definition.py

# Este ficheiro descreve as "ferramentas" que a nossa IA pode usar.
# A estrutura segue o padrão de "function calling" da OpenAI.

# app/tools_definition.py

tools_schema = [
    {
        "type": "function",
        "function": {
            "name": "add_items_to_cart",  # Nome no plural
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
                                "product_id": {
                                    "type": "integer",
                                    "description": "O ID do produto.",
                                },
                                "quantity": {
                                    "type": "integer",
                                    "description": "A quantidade.",
                                },
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
        "type": "function",
        "function": {
            "name": "request_customer_address",
            "description": "Use esta ferramenta APENAS quando o cliente indicar que terminou de adicionar itens (ex: 'só isso', 'pode fechar a conta').",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "process_order_with_address",
            "description": "Use esta ferramenta APENAS quando o ESTADO DA CONVERSA for 'AWAITING_ADDRESS' e o cliente tiver fornecido um endereço.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_address": {"type": "string"},
                },
                "required": ["customer_address"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "process_payment_choice",
            "description": "Use esta ferramenta APENAS quando o ESTADO DA CONVERSA for 'AWAITING_PAYMENT_METHOD' e o cliente tiver escolhido um método de pagamento.",
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["PIX", "CARD"],
                        "description": "O método de pagamento escolhido.",
                    },
                },
                "required": ["method"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "answer_conversationally",
            "description": "Use para responder a saudações e perguntas relacionadas ao cardápio, pratos, pedidos e funcionamento do restaurante. Ignore perguntas não relacionadas.",
            "parameters": {
                "type": "object",
                "properties": {
                    "response_text": {"type": "string"},
                },
                "required": ["response_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_items_from_cart",  # <-- NOME NO PLURAL
            "description": "Remove UM OU MAIS itens do carrinho. Use quando o cliente pedir para remover, tirar ou cancelar itens.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_ids": {  # <-- ACEITA UMA LISTA
                        "type": "array",
                        "description": "Uma lista de IDs dos produtos a serem completamente removidos.",
                        "items": {"type": "integer"},
                    }
                },
                "required": ["product_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "modify_item_quantity",
            "description": "Use esta ferramenta quando o cliente quiser ALTERAR A QUANTIDADE de um item que já está no carrinho, mencionando um NOVO NÚMERO. Ex: 'na verdade, quero 2 polvos', 'pode ser só 1 terrine'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "integer",
                        "description": "O ID do produto cuja quantidade será modificada.",
                    },
                    "new_quantity": {
                        "type": "integer",
                        "description": "A nova quantidade final para o produto. Se a nova quantidade for 0, o item será removido.",
                    },
                },
                "required": ["product_id", "new_quantity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_and_confirm_action",
            "description": "Use esta ferramenta QUANDO a busca por um item falhar e você quiser sugerir uma alternativa. A ferramenta fará a pergunta de confirmação e aguardará um 'sim' ou 'não'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "confirmation_question": {
                        "type": "string",
                        "description": "A pergunta exata a ser feita ao usuário. Ex: 'Não temos espaguete, mas posso adicionar 2 Gnocchis em vez disso. Pode ser?'",
                    },
                    "proposed_action": {
                        "type": "object",
                        "description": "A ação de ferramenta que será executada se o cliente disser 'sim'.",
                        "properties": {
                            "tool_name": {
                                "type": "string",
                                "enum": ["add_items_to_cart", "modify_item_quantity"],
                            },
                            "tool_args": {
                                "type": "object",
                                "description": "Os argumentos para a ferramenta proposta.",
                            },
                        },
                        "required": ["tool_name", "tool_args"],
                    },
                },
                "required": ["confirmation_question", "proposed_action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "answer_with_found_products",
            "description": "Sugere produtos com base na busca semântica",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Lista de nomes dos produtos sugeridos",
                    }
                },
                "required": ["product_names"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bulk_modify_quantities",
            "description": "Altera quantidades de vários itens de uma só vez. Use números absolutos (new_quantity).",
            "parameters": {
                "type": "object",
                "properties": {
                    "updates": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "product_id": {"type": "integer"},
                                "new_quantity": {"type": "integer"},
                            },
                            "required": ["product_id", "new_quantity"],
                        },
                    }
                },
                "required": ["updates"],
            },
        },
    },
]
