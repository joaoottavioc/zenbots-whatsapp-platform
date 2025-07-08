from openai import OpenAI
import os
import asyncio


#client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
client = OpenAI(
    base_url="http://ollama:11434/v1",
    api_key="ollama" # A chave pode ser qualquer coisa
)


async def get_openai_response(user_message: str, system_prompt: str):
    def sync_call():
        return client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
        )
    
    response = await asyncio.to_thread(sync_call)
    return response.choices[0].message.content.strip()
    
# A função agora recebe uma lista de dicionários (a conversa toda)
async def get_phi3_response(messages: list) -> str:
    """
    Executa a chamada síncrona à API da OpenAI em uma thread separada
    para não bloquear o loop de eventos principal da aplicação.
    """
    def sync_call():
        # A chamada síncrona que faz o trabalho pesado
        return client.chat.completions.create(
            model="phi3:mini",
            messages=messages,
            # Adicionar um timeout é uma boa prática para não esperar para sempre
            timeout=180.0 # Timeout de 3 minutos
        )

    try:
        # 👇 Aqui está a mágica: executa a função síncrona de forma assíncrona
        response = await asyncio.to_thread(sync_call)
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Erro na chamada à IA ou timeout: {e}")
        return "Desculpe, tive um problema para gerar sua resposta. Tente novamente."