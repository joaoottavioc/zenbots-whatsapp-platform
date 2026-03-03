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
                                "notes": {
                                    "type": "string",
                                    "description": "Observações do cliente para este item. Ex: 'Sem cebola', 'Ponto da carne'.",
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
    {
        "type": "function",
        "function": {
            "name": "search_catalog_for_suggestions",
            "description": "Use esta ferramenta quando o usuário pedir uma sugestão genérica de comida ou bebida (ex: 'tem sobremesa?', 'algo com peixe', 'queria ver os vinhos'). Extraia APENAS o conceito principal da comida/bebida para a busca.",
            "parameters": {
                "type": "object",
                "properties": {
                    "search_concept": {
                        "type": "string",
                        "description": "O conceito principal e limpo da busca. Ex: 'sobremesa', 'peixe', 'vinho', 'pato', 'massa com frutos do mar'.",
                    }
                },
                "required": ["search_concept"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_item_observation",
            "description": "Atualiza ou adiciona uma observação a um item que JÁ está no carrinho. Use quando o cliente corrigir ou adicionar um detalhe depois.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "integer",
                        "description": "ID do produto para adicionar a nota",
                    },
                    "notes": {
                        "type": "string",
                        "description": "A nova observação completa. Ex: 'Sem maionese'",
                    },
                },
                "required": ["product_id", "notes"],
            },
        },
    },
]

# Ferramenta específica para a IA de Visão (Extração de Cardápio)
tools_extraction = [
    {
        "type": "function",
        "function": {
            "name": "save_extracted_products",
            "description": "Salva os produtos extraídos do cardápio",
            "parameters": {
                "type": "object",
                "properties": {
                    "products": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "description": {"type": "string"},
                                "price": {"type": "number"},
                                # ▼▼▼ NOVO CAMPO PARA A IA PREENCHER ▼▼▼
                                "category": {
                                    "type": "string",
                                    "description": "Categoria curta e lógica do item. Ex: 'Bebidas', 'Lanches', 'Pizzas', 'Sobremesas', 'Porções'.",
                                },
                                # ▲▲▲ FIM DA ADIÇÃO ▲▲▲
                            },
                            "required": [
                                "name",
                                "price",
                                "category",
                            ],  # Tornamos obrigatório
                        },
                    }
                },
                "required": ["products"],
            },
        },
    }
]
