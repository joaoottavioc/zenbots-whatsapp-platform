# app/prompt_builder.py
from typing import List, Dict, Optional
from app.models import Product

def create_tool_prompt_old(
    search_results: List[Product],
    user_query: str,
    history: list,
    restaurant_name: str,
    cart_items: List[Dict],
    current_state: str,
    recent_suggestions: Optional[List[Product]] = None
) -> List[Dict]:
    """
    Cria um prompt robusto que guia a IA por uma árvore de decisão clara.
    """
    context = "Nenhum item do cardápio foi encontrado que corresponda à pergunta do cliente."
    if search_results:
        items = []
        for p in search_results:
            line = f"- {p.name} (ID: {p.id}, Preço: R$ {p.price:.2f}): {p.description or ''}"
            items.append(line)
        context = "Com base na pergunta do cliente, aqui estão os itens mais relevantes do cardápio:\n" + "\n".join(items)

    cart_summary = "O carrinho de compras está vazio."
    if cart_items:
        items_in_cart_lines = []
        for item in cart_items:
            # Inclui o ID para a IA saber qual item modificar/remover
            line = f"- {item['quantity']}x {item['name']} (ID do Produto: {item['product_id']})"
            items_in_cart_lines.append(line)
        cart_summary = "Itens atualmente no carrinho:\n" + "\n".join(items_in_cart_lines)

     # ▼▼▼ NOVA SEÇÃO DINÂMICA DE CONTEXTO ▼▼▼
    suggestion_context = ""
    if recent_suggestions:
        suggestion_lines = [f"{i+1}. {p.name} (ID: {p.id})" for i, p in enumerate(recent_suggestions)]
        suggestion_context = (
            "--- \n"
            "**PRIORIDADE MÁXIMA: CONTEXTO DE SUGESTÕES RECENTES**\n"
            "Você ACABOU de sugerir os seguintes itens. Se a mensagem do cliente se referir a 'o primeiro', 'o segundo', ou usar o nome de um destes itens, USE ESTA LISTA para encontrar o ID do produto.\n"
            "\n".join(suggestion_lines) + "\n---"
        )

    system_message = {
        "role": "system",
        "content": f"""Você é um assistente de vendas especialista do restaurante '{restaurant_name}'.
Sua tarefa é seguir uma ÁRVORE DE DECISÃO LÓGICA e, ao final, escolher UMA ÚNICA FERRAMENTA para executar a ação correta.

---
**ÁRVORE DE DECISÃO OBRIGATÓRIA:**

**1. A INTENÇÃO É FINALIZAR O PEDIDO?**
   - A mensagem é curta e clara sobre terminar? (ex: "só isso", "pode fechar a conta").
   - **SE SIM:** Use a ferramenta `request_customer_address`. **PARE AQUI.**

**2. A INTENÇÃO É MODIFICAR O CARRINHO?**
   - A mensagem contém palavras como "retire", "remova", "não quero mais", ou "na verdade, quero X"?
   - **SE SIM:**
     a. Analise a frase para entender qual produto do `ESTADO ATUAL DO CARRINHO` ele quer modificar.
     b. Encontre o `ID do Produto` correspondente no `ESTADO ATUAL DO CARRINHO`.
     c. Determine a nova quantidade (se for para remover, a nova quantidade é 0).
     d. Use a ferramenta `modify_item_quantity` com o `product_id` e a `new_quantity`. **PARE AQUI.**

**3. SE NENHUMA DAS ANTERIORES, A INTENÇÃO É ADICIONAR ITENS.**
   - A mensagem contém nomes de produtos?
   - **SE SIM:**
     a. Identifique TODOS os produtos e suas quantidades.
     b. Encontre os `IDs` correspondentes no `CONTEXTO DO CARDÁPIO`.
     c. Monte uma lista completa.
     d. Use a ferramenta `add_items_to_cart` com a lista. **PARE AQUI.**
     e. **SE VOCÊ NÃO ENCONTRAR UM ITEM (ex: 'Lasanha') MAS ACHAR UMA ALTERNATIVA BOA (ex: 'Espaguete'):** Use a ferramenta `propose_and_confirm_action` para sugerir a troca e aguardar a confirmação do cliente.
     f. SE ENCONTRAR TODOS OS ITENS: Monte uma lista completa e use a ferramenta `add_items_to_cart`.

**4. SE NENHUMA DAS ANTERIORES, É UMA CONVERSA GERAL.**
   - **SE SIM:** Use a ferramenta `answer_conversationally`.

---
**INFORMAÇÕES DE CONTEXTO:**

**ESTADO ATUAL DA CONVERSA:** {current_state}
**CONTEXTO DO CARDÁPIO (Para adicionar novos itens):**
{context}
**ESTADO ATUAL DO CARRINHO (Para remover ou modificar itens):**
{cart_summary}
---
"""
    }

    full_conversation = [system_message] + history + [{"role": "user", "content": user_query}]
    return full_conversation

def create_tool_prompt(
    user_query: str,
    intent: str,
    history: list,
    restaurant_name: str,
    cart_items: List[Dict],
    search_results: Optional[List[Product]] = None,
    recent_suggestions: Optional[List[Product]] = None
) -> List[Dict]:
    """
    Cria um prompt focado e reforçado para uma intenção específica, com instruções explícitas e exemplos avançados.
    """
    context = "Nenhum item relevante encontrado na busca."
    if search_results:
        items = [f"- {p.name} (ID: {p.id})" for p in search_results]
        context = "Itens relevantes encontrados na busca RAG geral:\n" + "\n".join(items)

    cart_summary = "O carrinho está vazio."
    if cart_items:
        items_in_cart_lines = [f"- {item['quantity']}x {item['name']} (ID do Produto: {item['product_id']})" for item in cart_items]
        cart_summary = "Itens atualmente no carrinho:\n" + "\n".join(items_in_cart_lines)

    suggestion_context = ""
    if recent_suggestions:
        suggestion_lines = [f"{i+1}. {p.name} (ID: {p.id})" for i, p in enumerate(recent_suggestions)]
        suggestion_context = (
            "--- \n"
            "**PRIORIDADE MÁXIMA: MEMÓRIA DE SUGESTÕES RECENTES**\n"
            "Você ACABOU de sugerir os seguintes itens. A resposta do cliente DEVE ser interpretada usando esta lista. Ignore a busca RAG geral se a resposta fizer sentido aqui.\n"
            "\n".join(suggestion_lines) + "\n---"
        )

    system_message = {
        "role": "system",
        "content": f"""Você é um assistente de vendas especialista do restaurante '{restaurant_name}'.
Sua tarefa é executar a intenção do cliente, que já foi classificada como '{intent}'. VOCÊ DEVE escolher a ferramenta mais apropriada e preencher TODOS os seus parâmetros corretamente.

{suggestion_context}

---
**EXEMPLOS AVANÇADOS DE ESCOLHA DE FERRAMENTA:**

1.  **Remoção Múltipla:**
    - Intenção: `REMOVE_ITEMS`
    - Mensagem do Cliente: "tira o jambon e o pato por favor"
    - Carrinho Atual: Jambon (ID: 1), Pato (ID: 3), Terrine (ID: 2)
    - Ação OBRIGATÓRIA: Chamar `remove_items_from_cart` com a lista `product_ids: [1, 3]`.

2.  **Contexto de Sugestão Múltipla:**
    - Intenção: `ADD_ITEMS`
    - Memória de Sugestões: 1. Gnocchi (ID: 10), 2. Lasanha (ID: 12), 3. Carbonara (ID: 15), 4. Alfredo (ID: 18)
    - Mensagem do Cliente: "quero um do segundo e dois do terceiro"
    - Ação OBRIGATÓRIA: Chamar `add_items_to_cart` com a lista `items: [{{"product_id": 12, "quantity": 1}}, {{"product_id": 15, "quantity": 2}}]`.
---

**INFORMAÇÕES DE CONTEXTO PARA SUA DECISÃO:**

**Contexto do Cardápio (Busca RAG geral):**
{context}

**Estado Atual do Carrinho:**
{cart_summary}
"""
    }

    full_conversation = [system_message] + history + [{"role": "user", "content": user_query}]
    return full_conversation