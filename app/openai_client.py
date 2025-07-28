import asyncio
import os
from typing import List, Dict
from dotenv import load_dotenv
from openai import OpenAI
import json

# Carrega as variáveis de ambiente do arquivo .env
load_dotenv()

# --- Cliente 1: Conexão Local com o Ollama (Para o Chat em Tempo Real) ---
client_ollama = OpenAI(
    base_url=os.getenv("OLLAMA_BASE_URL", "http://ollama:11434/v1"),
    api_key="ollama"  # A chave é um valor fixo para o Ollama
)

# --- Cliente 2: Conexão com a API da OpenAI (Para Extração de Dados) ---
client_openai_api = OpenAI(
    # A biblioteca lê a chave OPENAI_API_KEY automaticamente das variáveis de ambiente
    api_key=os.getenv("OPENAI_API_KEY")
)


async def get_chat_response(messages: List[Dict]) -> str:
    """
    Usa um modelo pequeno e rápido rodando localmente no Ollama para o chat em tempo real.
    """
    def sync_call():
        return client_ollama.chat.completions.create(
            model="phi3:mini",  # Modelo otimizado para chat rápido
            messages=messages,
            temperature=0.2,
            max_tokens=250
        )
    try:
        response = await asyncio.to_thread(sync_call)
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Erro na chamada ao modelo de CHAT (Ollama): {e}")
        return "Desculpe, tive um problema para gerar sua resposta. Tente novamente."



async def get_extraction_response(messages: List[Dict]) -> str:
    """
    Usa um modelo potente da API da OpenAI para a tarefa de extração de dados,
    garantindo máxima precisão.
    """
    def sync_call():
        return client_openai_api.chat.completions.create(
            model="gpt-4o",  # Modelo mais potente para extração
            messages=messages,
            temperature=0.0,  # Zero criatividade para extração precisa
            response_format={"type": "json_object"}
        )
    try:
        response = await asyncio.to_thread(sync_call)
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Erro na chamada ao modelo de EXTRAÇÃO (OpenAI API): {e}")
        return "[]"  # Retorna um array JSON vazio em caso de erro


async def get_chat_response_gpt(messages: List[Dict]) -> str:
    """
    Usa um modelo rápido e de alta fiabilidade (gpt-4o-mini) da API da OpenAI
    para o chat em tempo real, forçando uma resposta em JSON.
    """
    def sync_call():
        return client_openai_api.chat.completions.create(
            model="gpt-4o-mini", # 👈 Modelo alterado
            messages=messages,
            temperature=0.2,
            max_tokens=300,
            # 👇 A instrução crucial para garantir a fiabilidade
            response_format={"type": "json_object"}
        )
    try:
        response = await asyncio.to_thread(sync_call)
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Erro na chamada ao modelo de CHAT (OpenAI API): {e}")
        # Retorna um JSON de fallback em caso de erro
        return '{"action": "CONTINUE_CONVERSATION", "response_to_user": "Desculpe, ocorreu um erro. Pode tentar novamente?"}'

async def get_ai_decision(messages: List[Dict], tools: List[Dict]) -> Dict:
    """
    Usa o gpt-4o-mini com a funcionalidade de "tools" para que a IA possa
    decidir qual ação tomar.
    """
    def sync_call():
        return client_openai_api.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            tools=tools,
            tool_choice="auto",  # Permite que a IA escolha a ferramenta
            temperature=0.1,
        )
    try:
        response = await asyncio.to_thread(sync_call)
        # Retorna a mensagem de resposta completa, que pode conter a chamada de uma ferramenta
        return response.choices[0].message
    except Exception as e:
        print(f"Erro na chamada à API com ferramentas (OpenAI): {e}")
        return None

async def extract_potential_items(user_query: str) -> List[str]:
    """
    Usa um modelo de IA potente para extrair os nomes dos itens de uma frase.
    Retorna uma lista de strings. Ex: "2 pizzas e uma coca" -> ["pizza", "coca-cola"]
    """
    prompt = f"""
    Analise a frase de um cliente de restaurante e extraia os nomes dos pratos ou bebidas que ele está pedindo.
    Ignore quantidades, adjetivos e frases de cortesia.
    Sua resposta DEVE ser um objeto JSON com uma única chave "items", que contém um array de strings.
    Se não encontrar nenhum item, retorne um array vazio.

    Frase: "Eu quero dois x-burger com queijo e uma porção de batata frita, por favor"
    Resultado: {{"items": ["x-burger com queijo", "batata frita"]}}

    Frase: "me vê um steak au poivre e um croque monsieur"
    Resultado: {{"items": ["steak au poivre", "croque monsieur"]}}
    
    Frase: "pode ser um ouef e dois moules"
    Resultado: {{"items": ["ouef", "moules"]}}

    Frase: "só uma água, obrigado"
    Resultado: {{"items": ["água"]}}
    
    Frase: "tem mais sugestoes?"
    Resultado: {{"items": []}}

    Frase: "{user_query}"
    Resultado:
    """
    
    def sync_call():
        return client_openai_api.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            response_format={"type": "json_object"}
        )
    
    try:
        response = await asyncio.to_thread(sync_call)
        data = json.loads(response.choices[0].message.content)
        items = data.get("items", [])
        if isinstance(items, list):
            return items
        return []
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"Erro ao extrair itens da frase: {e}")
        return []