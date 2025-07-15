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
    print("--- PASSO 3: DENTRO de get_phi3_response_with_context. ---")
    
    def sync_call():
        print("--- PASSO 4: EXECUTANDO a chamada síncrona na thread. ---")
        response = client.chat.completions.create(
            model="phi3:mini",
            messages=messages,
            temperature=0.2,
            max_tokens=250,
            timeout=180.0
        )
        print("--- PASSO 5: CHAMADA SÍNCRONA CONCLUÍDA. ---")
        return response
    
    try:
        response = await asyncio.to_thread(sync_call)
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Erro na chamada à IA ou timeout: {e}")
        return "Desculpe, tive um problema para gerar sua resposta. Tente novamente."