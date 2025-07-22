import json
from typing import List, Dict

# Importa a função correta que chama a API da OpenAI
from app.openai_client import get_extraction_response

def _create_extraction_prompt(menu_text: str) -> List[Dict]:
    """
    Cria um prompt otimizado que força a IA a retornar um objeto JSON
    contendo uma lista de produtos.
    """
    
    prompt = f"""Sua tarefa é analisar o texto de um cardápio e extrair TODOS os itens.
O resultado final DEVE SER um único objeto JSON com uma única chave chamada "products".
O valor da chave "products" deve ser um array de objetos, onde cada objeto representa um item do cardápio.

REGRAS PARA CADA OBJETO DE PRODUTO:
- Cada objeto DEVE conter as chaves "name" (string), "price" (float), e "description" (string).
- A chave "price" deve ser um número. Se não encontrar o preço, use null.
- Se não houver descrição, use uma string vazia "".

EXEMPLO DE SAÍDA PERFEITA:
{{
  "products": [
    {{"name": "Pizza de Muçarela", "description": "massa fina, deliciosa mussarela e manjeiricao", "price": 45.50}},
    {{"name": "Refrigerante Lata", "description": "350ml", "price": 8.00}}
  ]
}}

CARDÁPIO PARA EXTRAÇÃO:
---
{menu_text}
---
"""
    return [
        {"role": "system", "content": "Você é um especialista em extrair dados de textos e formatá-los em um objeto JSON válido com uma chave 'products' que contém um array de itens."},
        {"role": "user", "content": prompt}
    ]


async def extract_products_from_text(menu_text: str) -> List[Dict]:
    """
    Usa a API da OpenAI para extrair uma lista de produtos e lida com
    o formato de resposta esperado {"products": [...]}.
    """
    print("--- Iniciando extração de produtos do texto com a API da OpenAI ---")
    extraction_prompt = _create_extraction_prompt(menu_text)
    
    json_string_response = await get_extraction_response(extraction_prompt)
    print(f"--- OpenAI API retornou: {json_string_response} ---")
    
    try:
        data = json.loads(json_string_response)
        
        # Agora, esperamos um dicionário com a chave "products"
        if isinstance(data, dict) and "products" in data and isinstance(data["products"], list):
            print(f"--- JSON extraído com sucesso! {len(data['products'])} produtos encontrados. ---")
            return data["products"]
        else:
            print("--- Resposta do LLM não continha o formato esperado {'products': [...]} ---")
            return []
            
    except (json.JSONDecodeError, IndexError) as e:
        print(f"Erro ao decodificar o JSON da resposta do LLM: {e}")
        return []