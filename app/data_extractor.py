# app/data_extractor.py
import json
from typing import List, Dict
# Usaremos a mesma função de chamada de IA que já temos
from app.openai_client import get_phi3_response

def _create_extraction_prompt(menu_text: str) -> List[Dict]:
    """Cria um prompt otimizado e mais robusto para a tarefa de extração de dados."""
    
    # Adicionamos mais regras e reforçamos o formato de saída.
    prompt = f"""Sua tarefa é extrair itens de um cardápio e retorná-los em um array JSON.

REGRAS RÍGIDAS:
1. Sua saída deve ser APENAS o array JSON. Não inclua NENHUM texto, explicação ou bloco de código como \`\`\`json.
2. Cada objeto no array deve conter EXATAMENTE as chaves: "name" (string), "price" (float), e "description" (string).
3. A chave "price" deve conter apenas números. Extraia apenas o valor numérico do preço. Se o preço for "R$45,50", o valor deve ser 45.50.
4. Se um item não tiver descrição, use uma string vazia "".
5. Se não conseguir identificar um preço para um item, use o valor null.

EXEMPLO DE ENTRADA:
"Pizza de Muçarela - A clássica, com muito queijo - R$45,50. Refrigerante Lata - 350ml - 8,00"

EXEMPLO DE SAÍDA JSON PERFEITA:
[
  {{"name": "Pizza de Muçarela", "description": "A clássica, com muito queijo", "price": 45.50}},
  {{"name": "Refrigerante Lata", "description": "350ml", "price": 8.00}}
]

Agora, execute a tarefa para o cardápio abaixo. Lembre-se, sua resposta deve ser somente o array JSON.

CARDÁPIO PARA EXTRAÇÃO:
---
{menu_text}
---
"""
    return [{"role": "user", "content": prompt}]


async def extract_products_from_text(menu_text: str) -> List[Dict]:
    """
    Usa um LLM para extrair uma lista de produtos estruturados de um texto de cardápio.
    """
    print("--- Iniciando extração de produtos do texto ---")

    # 1. Cria o prompt de extração
    extraction_prompt = _create_extraction_prompt(menu_text)

    # 2. Chama o LLM para obter a string JSON
    # Para esta tarefa, podemos usar um modelo mais potente se necessário,
    # mas vamos testar com o phi3:mini primeiro.
    json_string_response = await get_phi3_response(extraction_prompt)
    print(f"--- LLM retornou o JSON (como string): {json_string_response} ---")

    # 3. Tenta converter a string de resposta em um objeto Python
    try:
        # LLMs frequentemente retornam o JSON dentro de blocos de código markdown (```json ... ```)
        if "```json" in json_string_response:
            # Extrai o conteúdo de dentro do bloco de código
            json_string_response = json_string_response.split("```json\n")[1].split("```")[0]

        products = json.loads(json_string_response)
        if isinstance(products, list):
            return products
        return []
    except (json.JSONDecodeError, IndexError) as e:
        print(f"Erro ao decodificar o JSON da resposta do LLM: {e}")
        return [] # Retorna lista vazia em caso de erro