import asyncio
import os
from typing import List, Dict
from dotenv import load_dotenv
from openai import OpenAI

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