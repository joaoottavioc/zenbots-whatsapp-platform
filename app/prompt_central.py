# app/prompt_central.py

import json
from typing import List, Dict, Optional
from app.models import Product


def create_central_prompt(
    user_query: str,
    history: List[Dict],
    restaurant_name: str,
    cart_items: List[Dict],
    search_results: Optional[List[Product]] = None,
    recent_suggestions: Optional[List[Product]] = None,
) -> List[Dict]:
    """
    Prompt central multishot para escolha de ferramenta,
    com exemplos que seguem o protocolo oficial de tool calling.
    """
    # --- CONTEXTO DO CARDÁPIO ---
    menu_context = "Nenhum item relevante encontrado."
    if search_results:
        menu_context = "\n".join(
            f"- {p.name} (ID: {p.id}) – {p.description or ''}" for p in search_results
        )

    # --- CONTEXTO DO CARRINHO ---
    cart_context = "O carrinho está vazio."
    if cart_items:
        cart_context = "\n".join(
            f"- {item['quantity']}x {item['name']} (ID: {item['product_id']})"
            for item in cart_items
        )

    # --- CONTEXTO DE SUGESTÕES RECENTES ---
    suggestion_context = ""
    if recent_suggestions:
        suggestion_context = "\n".join(
            f"{i+1}. {p.name} (ID: {p.id})" for i, p in enumerate(recent_suggestions)
        )
        suggestion_context = (
            "**Sugestões recentes:**\n"
            "Se a mensagem mencionar 'o primeiro', 'o segundo', etc., use esta lista:\n"
            f"{suggestion_context}\n"
        )

    # --- MENSAGEM DO SISTEMA ---
    system_message = {
        "role": "system",
        "content": f"""
Você é um assistente de pedidos para o restaurante '{restaurant_name}'.
Sua tarefa: interpretar a mensagem do cliente e escolher UMA ferramenta do catálogo (`tools_schema`) preenchendo todos os parâmetros.

Prioridade:
1. Se o cliente indicar que terminou o pedido → use `request_customer_address`.
2. Se o cliente pedir para limpar tudo → use `remove_items_from_cart` com todos os IDs do carrinho.
3. Se mencionar produtos → associe aos IDs corretos do cardápio ou das sugestões recentes.
   - Se for alteração de quantidade de APENAS UM item já no carrinho → use `modify_item_quantity`.
   - Se for alteração de quantidade de MAIS DE UM item já no carrinho → use `bulk_modify_quantities` com `updates` contendo todos os pares product_id/new_quantity.
4. Se for uma dúvida genérica (ex: “tem algo com chocolate?”) → use `answer_with_found_products` com nomes reais dos produtos encontrados.
5. Se o estado da conversa for AWAITING_ADDRESS → use `process_order_with_address` com o endereço completo.
6. Você **sempre** deve responder usando UMA chamada de ferramenta do catálogo (`tools_schema`), sem nunca responder em texto puro.
Nunca responda coisas vagas como “posso sugerir algumas opções?” — você já deve sugerir os nomes e produtos diretamente.
"Regra: NUNCA invente IDs nem produtos. Só use IDs que apareçam em **Cardápio relevante (RAG)** ou em **Sugestões recentes**. Se não houver item correspondente, use `answer_conversationally` pedindo esclarecimento."

---
**Cardápio relevante (RAG):**
{menu_context}

**Carrinho atual:**
{cart_context}

{suggestion_context}
---
""",
    }

    # --- EXEMPLOS MULTISHOT (com tool_calls + role:"tool") ---

    # ADD
    ex1_user = {"role": "user", "content": "quero 2 pizzas marguerita e 1 coca-cola"}
    ex1_assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-ex1",
                "type": "function",
                "function": {
                    "name": "add_items_to_cart",
                    "arguments": json.dumps(
                        {
                            "items": [
                                {"product_id": 101, "quantity": 2},
                                {"product_id": 203, "quantity": 1},
                            ]
                        }
                    ),
                },
            }
        ],
    }
    ex1_tool = {
        "role": "tool",
        "tool_call_id": "call-ex1",
        "content": "Itens adicionados ao carrinho.",
    }

    # REMOVE
    ex2_user = {"role": "user", "content": "tira a coca e o pastel"}
    ex2_assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-ex2",
                "type": "function",
                "function": {
                    "name": "remove_items_from_cart",
                    "arguments": json.dumps({"product_ids": [203, 305]}),
                },
            }
        ],
    }
    ex2_tool = {
        "role": "tool",
        "tool_call_id": "call-ex2",
        "content": "Itens removidos do carrinho.",
    }

    # MODIFY
    ex3_user = {"role": "user", "content": "muda a pizza marguerita para 3 unidades"}
    ex3_assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-ex3",
                "type": "function",
                "function": {
                    "name": "modify_item_quantity",
                    "arguments": json.dumps({"product_id": 101, "new_quantity": 3}),
                },
            }
        ],
    }
    ex3_tool = {
        "role": "tool",
        "tool_call_id": "call-ex3",
        "content": "Quantidade do item atualizada.",
    }

    # BULK MODIFY
    ex3b_user = {"role": "user", "content": "tira 3 coxas e 2 pães"}
    ex3b_assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-ex3b",
                "type": "function",
                "function": {
                    "name": "bulk_modify_quantities",
                    "arguments": json.dumps(
                        {
                            "updates": [
                                {"product_id": 101, "new_quantity": 10},
                                {"product_id": 202, "new_quantity": 9},
                            ]
                        }
                    ),
                },
            }
        ],
    }
    ex3b_tool = {
        "role": "tool",
        "tool_call_id": "call-ex3b",
        "content": "Quantidades de múltiplos itens atualizadas.",
    }

    # GREETING
    ex4_user = {"role": "user", "content": "oi, tudo bem?"}
    ex4_assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-ex4",
                "type": "function",
                "function": {
                    "name": "answer_conversationally",
                    "arguments": json.dumps(
                        {"response_text": "Olá! Tudo ótimo, e você?"}
                    ),
                },
            }
        ],
    }
    ex4_tool = {
        "role": "tool",
        "tool_call_id": "call-ex4",
        "content": "Mensagem enviada ao usuário.",
    }

    # REQUEST_SUGGESTION
    ex5_user = {"role": "user", "content": "tem algo com massas?"}
    ex5_assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-ex5",
                "type": "function",
                "function": {
                    "name": "answer_conversationally",
                    "arguments": json.dumps(
                        {
                            "response_text": "Claro! Aqui estão algumas opções de massas deliciosas que temos no cardápio."
                        }
                    ),
                },
            }
        ],
    }
    ex5_tool = {
        "role": "tool",
        "tool_call_id": "call-ex5",
        "content": "Sugestões enviadas ao usuário.",
    }

    # SUGGESTIONS
    ex5b_user = {"role": "user", "content": "quais pratos vocês têm com carnes?"}
    ex5b_assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-ex5b",
                "type": "function",
                "function": {
                    "name": "answer_with_found_products",
                    "arguments": json.dumps(
                        {
                            "product_names": [
                                "Polpetone Recheado",
                                "Entrecôte au Poivre",
                                "Magret de Canard",
                            ]
                        }
                    ),
                },
            }
        ],
    }
    ex5b_tool = {
        "role": "tool",
        "tool_call_id": "call-ex5b",
        "content": "Sugestões enviadas ao usuário.",
    }

    # ADDRESS
    ex6_user = {"role": "user", "content": "Avenida Paulista 783"}
    ex6_assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-ex6",
                "type": "function",
                "function": {
                    "name": "process_order_with_address",
                    "arguments": json.dumps(
                        {"customer_address": "Avenida Paulista 783"}
                    ),
                },
            }
        ],
    }
    ex6_tool = {
        "role": "tool",
        "tool_call_id": "call-ex6",
        "content": "Endereço registrado.",
    }

    # --- MONTAGEM FINAL ---
    prompt = (
        [system_message]
        + history[-5:]
        + [
            ex1_user,
            ex1_assistant,
            ex1_tool,
            ex2_user,
            ex2_assistant,
            ex2_tool,
            ex3_user,
            ex3_assistant,
            ex3_tool,
            ex4_user,
            ex4_assistant,
            ex4_tool,
            ex5_user,
            ex5_assistant,
            ex5_tool,
            ex5b_user,
            ex5b_assistant,
            ex5b_tool,
            ex6_user,
            ex6_assistant,
            ex6_tool,
            {"role": "user", "content": user_query},
        ]
    )

    return prompt
