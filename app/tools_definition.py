# app/tools_definition.py

# Este ficheiro descreve as "ferramentas" que a nossa IA pode usar.
# A estrutura segue o padrão de "function calling" da OpenAI.

# app/tools_definition.py

tools_schema = [
    {
        "type": "function",
        "function": {
            "name": "add_items_to_cart",
            "description": "Adiciona um ou mais itens ao carrinho.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "Itens para adicionar (ID, quantidade, nome).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "product_id": {
                                    "type": "integer",
                                    "description": "ID do produto.",
                                },
                                "notes": {
                                    "type": "string",
                                    "description": "Observações. Ex: 'sem cebola'.",
                                },
                                "quantity": {
                                    "type": "integer",
                                    "description": "Quantidade.",
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
            "description": "Responde saudações e perguntas sobre o restaurante.",
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
            "name": "remove_items_from_cart",
            "description": "Remove um ou mais itens do carrinho.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_ids": {
                        "type": "array",
                        "description": "IDs dos produtos a remover.",
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
            "description": "Altera a quantidade de um item já no carrinho. 0 = remover.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "integer",
                        "description": "ID do produto.",
                    },
                    "new_quantity": {
                        "type": "integer",
                        "description": "Nova quantidade final.",
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
            "description": "Sugere alternativa quando item não encontrado. Aguarda confirmação.",
            "parameters": {
                "type": "object",
                "properties": {
                    "confirmation_question": {
                        "type": "string",
                        "description": "Pergunta de confirmação ao usuário.",
                    },
                    "proposed_action": {
                        "type": "object",
                        "description": "Ação a executar se confirmado.",
                        "properties": {
                            "tool_name": {
                                "type": "string",
                                "enum": ["add_items_to_cart", "modify_item_quantity"],
                            },
                            "tool_args": {
                                "type": "object",
                                "description": "Argumentos da ferramenta proposta.",
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
            "description": "Sugere produtos encontrados pela busca semântica.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Nomes dos produtos sugeridos.",
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
            "description": "Altera quantidades de vários itens do carrinho de uma vez.",
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
            "description": "Busca sugestões genéricas no cardápio. Extraia o conceito principal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "search_concept": {
                        "type": "string",
                        "description": "Conceito da busca. Ex: 'sobremesa', 'peixe', 'vinho'.",
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
            "description": "Atualiza observação de item já no carrinho.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "integer",
                        "description": "ID do produto.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Nova observação. Ex: 'sem maionese'.",
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
