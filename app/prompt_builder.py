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