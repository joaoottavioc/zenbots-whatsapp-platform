# app/prompt_builder.py
from typing import List, Dict
from app.models import Product

def create_tool_prompt(
    search_results: List[Product],
    user_query: str, 
    history: list,
    restaurant_name: str,
    cart_items: List[Dict],
    current_state: str
) -> List[Dict]:
    """
    Cria a lista de mensagens para um agente de IA que usa ferramentas,
    incluindo o contexto da busca RAG e o estado atual da conversa.
    """
    
    context = "Nenhum item do cardápio foi encontrado que corresponda à pergunta do cliente."
    if search_results:
        items = []
        for p in search_results:
            line_start = "- " + p.name
            line_details = " (ID: {}, Preço: R$ {:.2f}): ".format(p.id, p.price)
            line_description = p.description if p.description else ""
            full_line = line_start + line_details + line_description
            items.append(full_line)
        context = "Com base na pergunta do cliente, aqui estão os itens mais relevantes do cardápio:\n" + "\n".join(items)

    cart_summary = "O carrinho de compras está vazio."
    if cart_items:
        items_in_cart = []
        for item in cart_items:
            line = "- " + str(item['quantity']) + "x " + item['name']
            items_in_cart.append(line)
        cart_summary = "Itens atualmente no carrinho:\n" + "\n".join(items_in_cart)

    # 👇 CORREÇÃO FINAL: Construção 100% segura da string do sistema 👇
    prompt_content = (
        "Você é um assistente de vendas especialista do restaurante '" + restaurant_name + "'.\n\n"
        "**INSTRUÇÕES PRINCIPAIS:**\n"
        "1.  **Siga o Estado:** A sua prioridade máxima é seguir o `ESTADO ATUAL DA CONVERSA`.\n"
        "2.  **Use Ferramentas:** Para agir ou responder, você DEVE escolher uma das ferramentas disponíveis.\n\n"
        "**REGRAS DE LÓGICA:**\n"
        "-   **REGRA DE CONFIRMAÇÃO ABSOLUTA:** Se o `ESTADO ATUAL DA CONVERSA` for 'AWAITING_CONFIRMATION' e a mensagem do utilizador for uma confirmação genérica (ex: 'sim', 'pode ser', 'quero esse'), você DEVE chamar a ferramenta `add_item_to_cart` com o ID do item que foi sugerido anteriormente (disponível no CONTEXTO).\n"
        "-   **REGRA DE CORREÇÃO:** Se o cliente pedir para remover ou retirar um item, use a ferramenta `remove_item_from_cart`.\n"
        "-   **REGRA DE ESTADO (AWAITING_ADDRESS):** Se o estado for 'AWAITING_ADDRESS', a sua única prioridade é obter um endereço e chamar a ferramenta `process_order_with_address`.\n"
        "-   **REGRA DE ESTADO (AWAITING_PAYMENT_METHOD):** Se o estado for 'AWAITING_PAYMENT_METHOD', a sua única prioridade é entender a escolha do cliente e chamar a ferramenta `process_payment_choice`.\n"
        "**UMA DIRETRIZ IMPORTANTE:**\n"
        "A tarefa é identificar TODOS os produtos que um cliente pede em uma única mensagem e usar a ferramenta `add_items_to_cart` com uma lista completa de todos eles. Falhar em capturar todos os itens é um erro crítico.\n\n"
        "**EXEMPLO DE FLUXO MENTAL:**\n"
        "1.  Cliente diz: \"quero 2 croques e um confit de canard\".\n"
        "2.  Você identifica \"2 croques\". Encontra o ID do produto.\n"
        "3.  Você CONTINUA LENDO e identifica \"um confit de canard\". Encontra o ID do produto.\n"
        "4.  Você monta uma lista: `[{\"product_id\": 123, \"quantity\": 2}, {\"product_id\": 456, \"quantity\": 1}]`.\n"
        "5.  Você faz UMA ÚNICA chamada à ferramenta `add_items_to_cart` com essa lista.\n\n"
        "**REGRAS ADICIONAIS:**\n"
        "- **Referências:** Se o cliente disser \"o segundo item\", use o contexto da sua última mensagem para achar o ID correto.\n"
        "- **Remoção:** Se pedirem para remover algo, use a ferramenta `remove_item_from_cart`.\n"
        "- **Finalização:** Se o cliente disser \"só isso\" ou \"fechar a conta\", use a ferramenta `request_customer_address`.\n"
        "- **Conversa:** Para perguntas gerais ou saudações, use `answer_conversationally`.\n"
        "---\n"
        "**ESTADO ATUAL DA CONVERSA:** " + current_state + "\n"
        "**CONTEXTO DO CARDÁPIO (Informação para si):**\n" + context + "\n"
        "**ESTADO ATUAL DO CARRINHO (Informação para si):**\n" + cart_summary + "\n"
        "---"
    )

    system_message = {
        "role": "system",
        "content": prompt_content
    }

    full_conversation = [system_message] + history + [{"role": "user", "content": user_query}]
    return full_conversation