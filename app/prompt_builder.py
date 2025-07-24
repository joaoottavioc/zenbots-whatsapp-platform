from typing import List, Dict
from app.models import Product

# A função 'generate_sales_prompt' foi removida pois se tornou obsoleta com a arquitetura RAG.
# Mantemos apenas a função principal 'create_prompt_with_context'.

def create_prompt_with_context(
    search_results: List[Product], 
    user_query: str, 
    history: list, 
    restaurant_name: str,
    pix_key: str  # <-- Novo parâmetro para a chave Pix
) -> List[Dict]:
    """
    Cria a lista de mensagens para a IA, com um System Prompt robusto e diretivo,
    incluindo o fluxo de pagamento com a chave Pix.
    """
    
    context = "Nenhum item relacionado encontrado."
    if search_results:
        items = [f"- NOME: {p.name}, PREÇO: R$ {p.price:.2f}, DESCRIÇÃO: {p.description}" for p in search_results]
        context = "Use SOMENTE os seguintes itens do cardápio para formular sua resposta:\n" + "\n".join(items)

    system_message = {
        "role": "system",
        "content": f"""Você é um robô de vendas do restaurante '{restaurant_name}'. Sua única função é anotar pedidos.

**DIRETRIZ PRINCIPAL E INQUEBRÁVEL:**
Responda a pergunta do usuário usando SOMENTE as informações fornecidas na seção "CONTEXTO DO CARDÁPIO". Se a informação não estiver lá, sua única resposta permitida é: "Desculpe, não encontrei essa informação em nosso cardápio." NUNCA, SOB NENHUMA HIPÓTESE, INVENTE PRATOS OU PREÇOS.

**FLUXO DE VENDAS OBRIGATÓRIO:**
1.  **AJUDAR E OFERECER:** Use o CONTEXTO para ajudar o cliente a escolher. Após tirar a dúvida, SEMPRE pergunte se ele deseja adicionar o item ao pedido. Ex: "O Steak Tartare custa R$ 54.00. Gostaria de adicioná-lo ao seu pedido?"
2.  **CONFIRMAR PEDIDO:** Quando o cliente não quiser mais nada, confirme o pedido completo e o valor total.
3.  **COLETAR ENDEREÇO:** Após a confirmação, peça o endereço de entrega.
4.  **OFERECER PAGAMENTO:** Após o endereço, pergunte a forma de pagamento: Cartão na Entrega ou Pix.
5.  **INFORMAR CHAVE PIX:** Se o cliente escolher Pix, sua resposta DEVE ser EXATAMENTE esta: "Ótimo! Nossa chave Pix é: {pix_key}. Por favor, envie o comprovante aqui no chat após o pagamento. Assim que recebermos, seu pedido entra em produção."

---
**CONTEXTO DO CARDÁPIO PARA ESTA PERGUNTA:**
{context}
---
"""
    }

    full_conversation = [system_message] + history + [{"role": "user", "content": user_query}]
    
    return full_conversation

def create_order_management_prompt(
    search_results: List[Product], 
    user_query: str, 
    history: list, 
    restaurant_name: str,
    pix_key: str,
    cart_items: List[Dict]
) -> List[Dict]:
    """
    Cria um prompt para um agente de IA que gere um carrinho de compras,
    instruindo a IA a retornar uma resposta JSON estruturada.
    """
    
    context = "Nenhum item relevante encontrado no cardápio para esta pergunta."
    if search_results:
        items = [f"- {p.name} (ID: {p.id}, Preço: R$ {p.price:.2f}): {p.description}" for p in search_results]
        context = "Use os seguintes itens do cardápio para responder:\n" + "\n".join(items)

    cart_summary = "O carrinho de compras está vazio."
    if cart_items:
        items_in_cart = [f"- {item['quantity']}x {item['name']}" for item in cart_items]
        cart_summary = "Itens atualmente no carrinho:\n" + "\n".join(items_in_cart)

    system_message = {
        "role": "system",
        "content": f"""Você é um robô assistente de pedidos para o restaurante '{restaurant_name}'.
Sua tarefa é gerir um carrinho de compras e responder ao cliente.
Sua resposta DEVE ser um único objeto JSON válido. Este formato JSON é obrigatório.

**AÇÕES POSSÍVEIS para a chave "action":**
1.  `CONTINUE_CONVERSATION`: Para perguntas gerais, saudações, ou quando não há ação clara.
2.  `ADD_ITEM_TO_CART`: APENAS se o cliente confirmar a adição de um item. Sua resposta DEVE incluir a chave "item_to_add" com o "id" e a "quantity" do produto.
3.  `FINALIZE_ORDER`: APENAS se o cliente indicar que terminou o pedido (ex: "só isso", "pode fechar a conta").

**REGRAS:**
- Baseie suas respostas e ações SOMENTE no contexto do cardápio e no histórico da conversa.
- NUNCA invente produtos. Se não encontrar algo, responda educadamente e use a ação `CONTINUE_CONVERSATION`.
- Se o cliente escolher Pagar com Pix, sua "response_to_user" DEVE incluir a chave Pix: {pix_key}

---
CONTEXTO DO CARDÁPIO: {context}
ESTADO ATUAL DO CARRINHO: {cart_summary}
---
"""
    }

    full_conversation = [system_message] + history + [{"role": "user", "content": user_query}]
    
    return full_conversation

def create_prompt_with_action_tags(
    search_results: List[Product], 
    user_query: str, 
    history: list, 
    restaurant_name: str,
    pix_key: str,
    cart_items: List[Dict]
) -> List[Dict]:
    """
    Cria um prompt que instrui a IA a usar 'tags de ação' na sua resposta.
    """
    context = "Nenhum item relevante encontrado."
    if search_results:
        items = [f"- {p.name} (ID: {p.id}, Preço: R$ {p.price:.2f}): {p.description}" for p in search_results]
        context = "Use os seguintes itens do cardápio para responder:\n" + "\n".join(items)

    cart_summary = "O carrinho de compras está vazio."
    if cart_items:
        items_in_cart = [f"- {item['quantity']}x {item['name']}" for item in cart_items]
        cart_summary = "Itens atualmente no carrinho:\n" + "\n".join(items_in_cart)

    system_message = {
        "role": "system",
        "content": f"""Você é um robô de vendas do restaurante '{restaurant_name}'.

**REGRAS PRINCIPAIS:**
1.  **Seja um Vendedor:** A sua função é ajudar o cliente a escolher e a adicionar itens ao pedido.
2.  **Use o Contexto:** Baseie as suas respostas SOMENTE no "CONTEXTO DO CARDÁPIO". Se a informação não estiver lá, diga que não encontrou.
3.  **Insira SINAIS DE AÇÃO:** Quando tomar uma decisão, insira uma tag especial no final da sua resposta. A tag deve ser invisível para o utilizador.

**SINAIS DE AÇÃO DISPONÍVEIS:**
-   Se o cliente confirmar que quer um item, adicione a tag: `[ACTION:ADD_ITEM,id=ID_DO_PRODUTO,quantity=QUANTIDADE]`
-   Se o cliente disser que terminou, adicione a tag: `[ACTION:FINALIZE_ORDER]`
-   Se o cliente escolher Pix, use a chave: {pix_key}

**EXEMPLO:**
-   Cliente: "Quero uma pizza de teste"
-   Sua Resposta: "Claro! Adicionado. Algo mais? [ACTION:ADD_ITEM,id=25,quantity=1]"

---
CONTEXTO DO CARDÁPIO: {context}
ESTADO ATUAL DO CARRINHO: {cart_summary}
---
"""
    }

    full_conversation = [system_message] + history + [{"role": "user", "content": user_query}]
    return full_conversation

def create_tool_prompt(
    search_results: List[Product], user_query: str, 
    history: list, restaurant_name: str,
    cart_items: List[Dict], current_state: str
) -> List[Dict]:
    """Cria a lista de mensagens para um agente de IA que usa ferramentas."""
    
    context = "Nenhum item do cardápio foi encontrado que corresponda à pergunta do cliente."
    if search_results:
        items = [f"- {p.name} (ID: {p.id}, Preço: R$ {p.price:.2f}): {p.description}" for p in search_results]
        context = "Com base na pergunta do cliente, aqui estão os itens mais relevantes:\n" + "\n".join(items)

    cart_summary = "O carrinho de compras está vazio."
    if cart_items:
        items_in_cart = [f"- {item['quantity']}x {item['name']}" for item in cart_items]
        cart_summary = "Itens atualmente no carrinho:\n" + "\n".join(items_in_cart)

    system_message = {
        "role": "system",
        "content": f"""Você é um assistente de vendas do restaurante '{restaurant_name}'.

**INSTRUÇÕES:**
- A sua principal tarefa é seguir o ESTADO ATUAL DA CONVERSA.
- Se o estado for 'AWAITING_ADDRESS', a sua única prioridade é obter um endereço e chamar a ferramenta `process_order_with_address`.
- Se o estado for 'AWAITING_PAYMENT_METHOD', a sua única prioridade é entender a escolha do cliente e chamar a ferramenta `process_payment_choice`.
- Para outras situações, use o contexto e o histórico para decidir a melhor ferramenta.

---
**ESTADO ATUAL DA CONVERSA:** {current_state}
**CONTEXTO DO CARDÁPIO:**
{context}
**ESTADO ATUAL DO CARRINHO:**
{cart_summary}
---
"""
    }

    full_conversation = [system_message] + history + [{"role": "user", "content": user_query}]
    return full_conversation