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
    unavailable_products: Optional[List[Product]] = None,
    available_categories: Optional[List[str]] = None,
) -> List[Dict]:
    """
    Prompt central multishot para escolha de ferramenta,
    com exemplos que seguem o protocolo oficial de tool calling.
    """
    # --- CONTEXTO DO CARDÁPIO ---
    menu_context = "Nenhum item relevante encontrado."
    if search_results:
        capped = search_results[:15]
        menu_context = "\n".join(
            f"- [{p.category or 'Geral'}] {p.name} (ID: {p.id}) – {(p.description or '')[:80]}"
            for p in capped
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
            f"{i + 1}. {p.name} (ID: {p.id})" for i, p in enumerate(recent_suggestions)
        )
        suggestion_context = (
            "**Sugestões recentes:**\n"
            "Se a mensagem mencionar 'o primeiro', 'o segundo', etc., use esta lista:\n"
            f"{suggestion_context}\n"
        )

    # --- T1-1: STATIC SYSTEM MESSAGE (cacheable prefix) ---
    static_system_message = {
        "role": "system",
        "content": f"""
Você é um assistente de pedidos para o restaurante '{restaurant_name}'.
Sua tarefa: interpretar a mensagem do cliente e escolher UMA ferramenta do catálogo (`tools_schema`) preenchendo todos os parâmetros.

Prioridade:
1. Se o cliente pedir para limpar tudo → use `remove_items_from_cart` com todos os IDs do carrinho.
2. Se mencionar produtos → associe aos IDs corretos do cardápio ou das sugestões recentes.
   - Se for alteração de quantidade de APENAS UM item já no carrinho → use `modify_item_quantity`.
   - Se for alteração de quantidade de MAIS DE UM item já no carrinho → use `bulk_modify_quantities` com `updates` contendo todos os pares product_id/new_quantity.
   - Se os itens AINDA NÃO estiverem no carrinho → use SEMPRE `add_items_to_cart`. SEMPRE inclua `product_name` com o nome do produto como o cliente pediu.
   - Se for alteração de quantidade de itens que JÁ ESTÃO no carrinho → use `modify_item_quantity` (um item) ou `bulk_modify_quantities` (vários).

--- REGRAS PARA PEDIDOS E OBSERVAÇÕES (IMPORTANTE) ---
1. Quando o cliente pedir uma alteração (ex: "sem cebola", "com gelo", "bem passado"), NUNCA inclua isso no nome do produto na busca.
2. Se o item AINDA NÃO está no carrinho → use o campo 'notes' da ferramenta 'add_items_to_cart' para essas modificações.
3. Se o item JÁ ESTÁ no carrinho (verifique o "Carrinho atual" abaixo) e o cliente pede para adicionar/alterar uma observação → use `update_item_observation` com o product_id do carrinho. NÃO use `add_items_to_cart` para isso.
4. Se o cliente pedir algo que não está explícito no cardápio (ex: "X-Bacon sem bacon"), aceite e anote a observação.

Regra de estoque:
- NUNCA avalie estoque, disponibilidade ou capacidade. NUNCA escreva “não temos X unidades”.
- Se o produto existe no cardápio (mesmo com plural/sinônimo), REGISTRE exatamente a quantidade pedida com `add_items_to_cart`.

Regra de correspondência de produtos:
- Só use `add_items_to_cart` se o produto no cardápio REALMENTE corresponde ao que o cliente pediu.
- Se o nome do produto no cardápio é muito diferente do que o cliente pediu (ex: cliente pediu “Johns Simples” mas no cardápio só tem “John's Paranaense”), NÃO adicione. Use `answer_conversationally` para dizer que não temos esse item e sugira o que temos de parecido.
- Sinônimos e variações leves são OK (ex: “coca” → nome completo do refrigerante, “x-burger” → nome completo do lanche).

Regra de adicionais vs pratos principais:
- Se houver ambiguidade entre um prato principal e um adicional/complemento (ex: “brownie” como sobremesa vs “Adicional de Brownie”), prefira o prato principal.
- Só adicione itens da categoria Adicionais/Extras/Complementos quando o cliente pedir EXPLICITAMENTE (ex: “quero um adicional de brownie”, “com extra de queijo”).

Conversão de quantidades:
- Se a quantidade vier por extenso (ex.: “mil duzentos e vinte e quatro”), converta para inteiro no `quantity` (ex.: 1224).
- Não reduza quantidades sem pedido explícito do cliente.

3. Se for uma dúvida genérica (ex: “tem algo com chocolate?”) → use `answer_with_found_products` com nomes reais dos produtos encontrados.
4. Você **sempre** deve responder usando UMA chamada de ferramenta do catálogo (`tools_schema`), sem nunca responder em texto puro.
7. Se o pedido for uma SUGESTÃO GENÉRICA (ex: "tem algo com chocolate?", "quais as sobremesas?", "me ve um prato com pato", "o que tem com bacon aí?") → use a ferramenta `search_catalog_for_suggestions` e extraia apenas o conceito principal da comida para o parâmetro `search_concept`.
Nunca responda coisas vagas como “posso sugerir algumas opções?” — você já deve sugerir os nomes e produtos diretamente.
"Regra: NUNCA invente IDs nem produtos. Só use IDs que apareçam em **Cardápio relevante (RAG)** ou em **Sugestões recentes**. Se não houver item correspondente, use `answer_conversationally` pedindo esclarecimento."
“Se a mensagem for uma confirmação como ‘sim’ ou "claro", "ok", etc e não houver contexto de proposta pendente, interprete como ‘continuar pedido’ e pergunte algo como ‘O que mais deseja?’
Lembre-se: Você é um assistente de restaurante.  
- Responda apenas perguntas relacionadas ao cardápio, pedidos, funcionamento ou atendimento.  
- Se o cliente perguntar algo fora desse escopo, informe gentilmente que só pode ajudar com assuntos do restaurante.
Se a quantidade for escrita por extenso (ex.: trinta e dois, doze, quinze), converta para número inteiro no campo quantity.
Se um item pedido não existir no cardápio, use `answer_conversationally` para informar educadamente que não temos esse item.
Se um item estiver listado em **Produtos indisponíveis no momento**, use `answer_conversationally` para dizer que o item existe mas está em falta, e sugira alternativas do cardápio.
- Exemplo (não existe): “Puxa, [Item] não faz parte do nosso cardápio. Gostaria de ver nossas opções?”
- Exemplo (em falta): “[Item] está em falta no momento. Que tal um [alternativa]?”
- "proposed_action" preenchida com a tool real e argumentos resolvidos (IDs/quantidades).
Após a confirmação do cliente, NÃO gere outra resposta: o backend executará a ação proposta.
""",
    }

    # --- CONTEXTO DE PRODUTOS INDISPONÍVEIS ---
    unavailable_context = ""
    if unavailable_products:
        unavailable_names = ", ".join(p.name for p in unavailable_products)
        unavailable_context = (
            f"\n**Produtos indisponíveis no momento:**\n"
            f"{unavailable_names}\n"
            f"Se o cliente pediu algum desses, informe que está em falta e sugira outras opções do cardápio.\n"
        )

    # --- CONTEXTO DE CATEGORIAS DO CARDÁPIO ---
    categories_context = ""
    if available_categories:
        cats = ", ".join(available_categories)
        categories_context = (
            f"\n**Categorias do cardápio:**\n{cats}\n"
            "NUNCA mencione tipos de comida, categorias ou produtos que não estejam listados acima. "
            "Se o cliente pedir algo genérico, sugira as categorias reais do cardápio.\n"
        )

    # --- T1-1: DYNAMIC SYSTEM MESSAGE (per-request context, not cached) ---
    dynamic_system_message = {
        "role": "system",
        "content": f"""---
**Cardápio relevante (RAG):**
{menu_context}

**Carrinho atual:**
{cart_context}

{suggestion_context}{unavailable_context}{categories_context}---
""",
    }

    # --- EXEMPLOS MULTISHOT (com tool_calls + role:"tool") ---
    # T1-2: Reduced from 13 to 10 examples by removing redundant ones:
    # - Removed ex7 (ordinals with numbers, covered by ex_ordinais_extenso)
    # - Removed ex_ordinals (ordinals with numbers, covered by ex_ordinais_extenso)
    # - Removed ex_notes_simple (simple notes, covered by ex_notes_complex)

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
    ex3b_user = {
        "role": "user",
        "content": "muda a pizza pra 10 e o prato do dia pra 9",
    }
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
        "content": "Quantidades atualizadas.",
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

    # T1-2: Removed ex7 and ex_ordinals (redundant with ex_ordinais_extenso)

    # Exemplo: quantidades por extenso + ordinais (covers both ordinals and written-out numbers)
    ex_ordinais_extenso_user = {
        "role": "user",
        "content": "quero treze do terceiro e quinze do primeiro",
    }

    # Sugestões recentes imaginárias para o exemplo:
    # 1. Crevettes à la Provençale (ID: 22)
    # 2. Poulpe à la Plancha (ID: 15)
    # 3. Oeuf Poche Paul Bocuse (ID: 9)
    # 4. Gnocchis de la Mémé Forte (ID: 23)

    ex_ordinais_extenso_assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-ex-ordinais-extenso",
                "type": "function",
                "function": {
                    "name": "add_items_to_cart",
                    "arguments": json.dumps(
                        {
                            "items": [
                                {"product_id": 9, "quantity": 13},  # treze → 13
                                {"product_id": 22, "quantity": 15},  # quinze → 15
                            ]
                        }
                    ),
                },
            }
        ],
    }

    ex_ordinais_extenso_tool = {
        "role": "tool",
        "tool_call_id": "call-ex-ordinais-extenso",
        "content": "Itens adicionados.",
    }

    ex_suggestion_user = {
        "role": "user",
        "content": "o que vcs tem de sobremesa pra hoje?",
    }
    ex_suggestion_assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-ex-suggestion",
                "type": "function",
                "function": {
                    "name": "search_catalog_for_suggestions",
                    "arguments": json.dumps({"search_concept": "sobremesa"}),
                },
            }
        ],
    }
    ex_suggestion_tool = {
        "role": "tool",
        "tool_call_id": "call-ex-suggestion",
        "content": "Busca por 'sobremesa' realizada.",
    }

    # T1-2: Removed ex_notes_simple (redundant with ex_notes_complex)

    # Exemplo: Adicionar com observação complexa
    ex_notes_complex_user = {
        "role": "user",
        "content": "Me vê um X-Tudo mas tira a cebola e coloca queijo extra",
    }
    ex_notes_complex_assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-ex-notes-complex",
                "type": "function",
                "function": {
                    "name": "add_items_to_cart",
                    "arguments": json.dumps(
                        {
                            "items": [
                                # Assumindo ID 55 para X-Tudo
                                {
                                    "product_id": 55,
                                    "quantity": 1,
                                    "notes": "sem cebola, com queijo extra",
                                }
                            ]
                        }
                    ),
                },
            }
        ],
    }
    ex_notes_complex_tool = {
        "role": "tool",
        "tool_call_id": "call-ex-notes-complex",
        "content": "Item adicionado com nota.",
    }

    # Exemplo 3: Atualizar observação de item existente
    ex_notes_update_user = {
        "role": "user",
        "content": "Ah, o refrigerante é sem gelo, tá?",
    }
    ex_notes_update_assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-ex-notes-update",
                "type": "function",
                "function": {
                    "name": "update_item_observation",
                    "arguments": json.dumps(
                        {
                            # Assumindo ID 89 para o Refrigerante que já está no carrinho
                            "product_id": 89,
                            "notes": "sem gelo",
                        }
                    ),
                },
            }
        ],
    }
    ex_notes_update_tool = {
        "role": "tool",
        "tool_call_id": "call-ex-notes-update",
        "content": "Observação atualizada.",
    }

    # --- MONTAGEM FINAL ---
    # T1-1: Order optimized for prefix caching:
    #   static system -> examples -> dynamic system -> history -> user query
    # T1-2: 11 examples (reduced from 13, but fixed ex3b which was dead code)
    examples = [
        ex1_user,
        ex1_assistant,
        ex1_tool,
        ex2_user,
        ex2_assistant,
        ex2_tool,
        ex3_user,
        ex3_assistant,
        ex3_tool,
        ex3b_user,
        ex3b_assistant,
        ex3b_tool,
        ex4_user,
        ex4_assistant,
        ex4_tool,
        ex5_user,
        ex5_assistant,
        ex5_tool,
        ex5b_user,
        ex5b_assistant,
        ex5b_tool,
        ex_ordinais_extenso_user,
        ex_ordinais_extenso_assistant,
        ex_ordinais_extenso_tool,
        ex_suggestion_user,
        ex_suggestion_assistant,
        ex_suggestion_tool,
        ex_notes_complex_user,
        ex_notes_complex_assistant,
        ex_notes_complex_tool,
        ex_notes_update_user,
        ex_notes_update_assistant,
        ex_notes_update_tool,
    ]

    prompt = (
        [static_system_message]
        + examples
        + [dynamic_system_message]
        + history[-5:]
        + [{"role": "user", "content": user_query}]
    )

    return prompt
