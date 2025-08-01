import json
from typing import List, Dict

# Importa a função correta que chama a API da OpenAI
from app.openai_client import get_extraction_response

def _create_extraction_prompt(menu_text: str) -> List[Dict]:
    """
    Cria um prompt otimizado que força a IA a extrair produtos e também
    a gerar palavras-chave relevantes para cada um.
    """
    
    prompt = f"""Sua tarefa é analisar o texto de um cardápio e extrair TODOS os itens.
O resultado final DEVE SER um único objeto JSON com uma única chave chamada "products".
O valor da chave "products" deve ser um array de objetos, onde cada objeto representa um item do cardápio.

REGRAS PARA CADA OBJETO DE PRODUTO:
- Cada objeto DEVE conter as chaves "name" (string), "price" (float), "description" (string), e "keywords" (array de strings).
- A chave "price" deve ser um número. Se não encontrar o preço, use null.
- A chave "keywords" DEVE conter uma lista de sinônimos, abreviações e termos de busca relevantes. Pense em como um cliente com pressa pediria por este item. Inclua o nome principal sem acentos e em singular/plural se aplicável.

EXEMPLO DE SAÍDA PERFEITA:
{{
  "products": [
    {{"name": "Confit de Canard", "description": "Coxa de pato confitada lentamente, servida com purê de batatas trufado.", "price": 86.00, "keywords": ["pato", "coxa de pato", "canard", "confit"]}},
    {{"name": "La Pêche du Jour", "description": "Peixe do dia grelhado com molho de limão siciliano.", "price": 78.00, "keywords": ["peixe", "peixe do dia", "peche du jour", "pescado"]}},
    {{"name": "HUITRES (6 unidades)", "description": "Ostras frescas de Santa Catarina.", "price": 58.00, "keywords": ["ostra", "ostras", "huitres", "frutos do mar"]}}
  ]
}}

CARDÁPIO PARA EXTRAÇÃO:
---
{menu_text}
---
"""
    return [
        {"role": "system", "content": "Você é um especialista em extrair dados de cardápios e formatá-los em um objeto JSON válido com uma chave 'products', onde cada produto inclui um campo 'keywords'."},
        {"role": "user", "content": prompt}
    ]


async def extract_products_from_text(menu_text: str) -> List[Dict]:
    """
    Usa a API da OpenAI para extrair uma lista de produtos, incluindo
    palavras-chave, e lida com o formato de resposta esperado.
    """
    print("--- Iniciando extração de produtos e palavras-chave com a API da OpenAI ---")
    extraction_prompt = _create_extraction_prompt(menu_text)
    
    json_string_response = await get_extraction_response(extraction_prompt)
    print(f"--- OpenAI API retornou: {json_string_response} ---")
    
    try:
        data = json.loads(json_string_response)
        
        if isinstance(data, dict) and "products" in data and isinstance(data["products"], list):
            print(f"--- JSON extraído com sucesso! {len(data['products'])} produtos encontrados. ---")
            return data["products"]
        else:
            print("--- Resposta do LLM não continha o formato esperado {'products': [...]} ---")
            return []
            
    except (json.JSONDecodeError, IndexError) as e:
        print(f"Erro ao decodificar o JSON da resposta do LLM: {e}")
        return []