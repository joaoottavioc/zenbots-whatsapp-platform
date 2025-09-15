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
   - Se os itens AINDA NÃO estiverem no carrinho → use SEMPRE `add_items_to_cart`.
   - Se os itens AINDA NÃO estiverem no carrinho → use SEMPRE `add_items_to_cart` (NUNCA `bulk_modify_quantities` nem `modify_item_quantity`).
   - Se for alteração de quantidade de itens que JÁ ESTÃO no carrinho → use `modify_item_quantity` (um item) ou `bulk_modify_quantities` (vários).

Regra de estoque:
- NUNCA avalie estoque, disponibilidade ou capacidade. NUNCA escreva “não temos X unidades”.
- Se o produto existe no cardápio (mesmo com plural/sinônimo), REGISTRE exatamente a quantidade pedida com `add_items_to_cart`.

Conversão de quantidades:
- Se a quantidade vier por extenso (ex.: “mil duzentos e vinte e quatro”), converta para inteiro no `quantity` (ex.: 1224).
- Não reduza quantidades sem pedido explícito do cliente.

4. Se for uma dúvida genérica (ex: “tem algo com chocolate?”) → use `answer_with_found_products` com nomes reais dos produtos encontrados.
5. Se o estado da conversa for AWAITING_ADDRESS → use `process_order_with_address` com o endereço completo.
6. Você **sempre** deve responder usando UMA chamada de ferramenta do catálogo (`tools_schema`), sem nunca responder em texto puro.
7. Se o pedido for uma SUGESTÃO GENÉRICA (ex: "tem algo com chocolate?", "quais as sobremesas?", "me ve um prato com pato", "o que tem com bacon aí?") → use a ferramenta `search_catalog_for_suggestions` e extraia apenas o conceito principal da comida para o parâmetro `search_concept`.
Nunca responda coisas vagas como “posso sugerir algumas opções?” — você já deve sugerir os nomes e produtos diretamente.
"Regra: NUNCA invente IDs nem produtos. Só use IDs que apareçam em **Cardápio relevante (RAG)** ou em **Sugestões recentes**. Se não houver item correspondente, use `answer_conversationally` pedindo esclarecimento."
“Se a mensagem for uma confirmação como ‘sim’ ou "claro", "ok", etc e não houver contexto de proposta pendente, interprete como ‘continuar pedido’ e pergunte algo como ‘O que mais deseja?’
Lembre-se: Você é um assistente de restaurante.  
- Responda apenas perguntas relacionadas ao cardápio, pedidos, funcionamento ou atendimento.  
- Se o cliente perguntar algo fora desse escopo, informe gentilmente que só pode ajudar com assuntos do restaurante.
Se a quantidade for escrita por extenso (ex.: trinta e dois, doze, quinze), converta para número inteiro no campo quantity.
Se um item pedido não existir ou estiver indisponível, use a ferramenta `answer_conversationally` para informar o cliente de forma educada.
- Exemplo de resposta: “Puxa, [Nome do Item] não faz parte do nosso cardápio no momento. Gostaria de ver nossas opções de sobremesa ou alguma outra sugestão?”
- "proposed_action" preenchida com a tool real e argumentos resolvidos (IDs/quantidades).
Após a confirmação do cliente, NÃO gere outra resposta: o backend executará a ação proposta.
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

    ex7_user = {"role": "user", "content": "quero cinco do primeiro e três do segundo"}
    ex7_assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "call-ex7",
            "type": "function",
            "function": {
                "name": "add_items_to_cart",
                "arguments": json.dumps({
                    # Assumindo que nas Sugestões recentes estão algo como:
                    # 1. Oeuf Poche Paul Bocuse (ID: 111)
                    # 2. Magret de Canard (ID: 222)
                    "items": [
                        {"product_id": 111, "quantity": 5},
                        {"product_id": 222, "quantity": 3}
                    ]
                })
            }
        }]
    }
    ex7_tool = {"role": "tool", "tool_call_id": "call-ex7", "content": "Itens adicionados."}

    # Exemplo: usuário escolhe itens usando "primeiro/segundo" da lista de sugestões recentes
    ex_ordinals_user = {
        "role": "user",
        "content": "quero 2 do segundo e 7 do quarto"
    }

    # Sugestões recentes imaginárias para o exemplo
    # 1. Crevettes à la Provençale (ID: 22)
    # 2. Poulpe à la Plancha (ID: 15)
    # 3. Oeuf Poche Paul Bocuse (ID: 9)
    # 4. Gnocchis de la Mémé Forte (ID: 23)

    ex_ordinals_assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "call-ex-ordinais",
            "type": "function",
            "function": {
                "name": "add_items_to_cart",
                "arguments": json.dumps({
                    "items": [
                        {"product_id": 15, "quantity": 2},
                        {"product_id": 23, "quantity": 7}
                    ]
                })
            }
        }]
    }

    ex_ordinals_tool = {
        "role": "tool",
        "tool_call_id": "call-ex-ordinais",
        "content": "Itens adicionados."
    }

    # Exemplo: quantidades por extenso + ordinais
    ex_ordinais_extenso_user = {
        "role": "user",
        "content": "quero treze do terceiro e quinze do primeiro"
    }

    # Sugestões recentes imaginárias para o exemplo:
    # 1. Crevettes à la Provençale (ID: 22)
    # 2. Poulpe à la Plancha (ID: 15)
    # 3. Oeuf Poche Paul Bocuse (ID: 9)
    # 4. Gnocchis de la Mémé Forte (ID: 23)

    ex_ordinais_extenso_assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "call-ex-ordinais-extenso",
            "type": "function",
            "function": {
                "name": "add_items_to_cart",
                "arguments": json.dumps({
                    "items": [
                        {"product_id": 9, "quantity": 13},   # treze → 13
                        {"product_id": 22, "quantity": 15}   # quinze → 15
                    ]
                })
            }
        }]
    }

    ex_ordinais_extenso_tool = {
        "role": "tool",
        "tool_call_id": "call-ex-ordinais-extenso",
        "content": "Itens adicionados."
    }

    ex_suggestion_user = {"role": "user", "content": "o que vcs tem de sobremesa pra hoje?"}
    ex_suggestion_assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-ex-suggestion",
                "type": "function",
                "function": {
                    "name": "search_catalog_for_suggestions",
                    "arguments": json.dumps(
                        {
                            "search_concept": "sobremesa"
                        }
                    ),
                },
            }
        ],
    }
    ex_suggestion_tool = {
        "role": "tool",
        "tool_call_id": "call-ex-suggestion",
        "content": "Busca por 'sobremesa' realizada.",
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
            ex7_user,
            ex7_assistant,
            ex7_tool,
            ex_ordinals_user,
            ex_ordinals_assistant,
            ex_ordinals_tool,
            ex_ordinais_extenso_user,
            ex_ordinais_extenso_assistant,
            ex_ordinais_extenso_tool,
            ex_suggestion_user,
            ex_suggestion_assistant,
            ex_suggestion_tool,
            {"role": "user", "content": user_query},
        ]
    )

    return prompt
