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

async def get_ai_decision(messages: List[Dict], tools: List[Dict], force_tool: bool = False) -> Dict:
    """
    Usa o gpt-4o-mini com a funcionalidade de "tools" para que a IA possa
    decidir qual ação tomar. Pode forçar o uso de uma ferramenta.
    """
    choice = "required" if force_tool else "auto"

    def sync_call():
        return client_openai_api.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            tools=tools,
            tool_choice=choice,
            temperature=0.1,
        )
    try:
        response = await asyncio.to_thread(sync_call)
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

async def get_user_intent(user_query: str, cart_items: List[Dict]) -> str:
    """
    Usa a IA para uma tarefa simples de classificação de intenção.
    Retorna "ADD", "REMOVE", "MODIFY", "FINISH" ou "CONVERSE".
    """
    # Se o carrinho estiver vazio, a intenção não pode ser remover ou modificar.
    possible_intents = ["ADD", "FINISH", "CONVERSE"]
    if cart_items:
        possible_intents.extend(["REMOVE", "MODIFY"])

    prompt = f"""
    Analise a frase do cliente e classifique sua intenção principal em uma das seguintes categorias: {', '.join(possible_intents)}.
    - ADD: O cliente quer adicionar um ou mais itens ao pedido.
    - REMOVE: O cliente quer remover, retirar, cancelar ou indica que não quer mais um item.
    - MODIFY: O cliente quer alterar a quantidade de um item que já está no carrinho.
    - FINISH: O cliente indica que terminou o pedido (ex: "só isso", "pode fechar a conta").
    - CONVERSE: Qualquer outra coisa (saudações, perguntas, etc.).

    Sua resposta DEVE ser um objeto JSON com uma única chave "intent".

    Frase: "quero duas pizzas e uma coca" -> {{"intent": "ADD"}}
    Frase: "mudei de ideia, nao quero mais as coxas" -> {{"intent": "REMOVE"}}
    Frase: "pensando bem, também nao vou querer a saladinha" -> {{"intent": "REMOVE"}}
    Frase: "pode tirar a salada?" -> {{"intent": "REMOVE"}}
    Frase: "na verdade, quero 2 nhoques" -> {{"intent": "MODIFY"}}
    Frase: "só isso" -> {{"intent": "FINISH"}}
    Frase: "oi tudo bem?" -> {{"intent": "CONVERSE"}}

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
        intent = data.get("intent", "CONVERSE")
        # Garante que a intenção seja válida
        return intent if intent in possible_intents else "CONVERSE"
    except Exception:
        return "CONVERSE" # Fallback seguro

# ▼▼▼ NOVA FUNÇÃO CENTRAL: O CLASSIFICADOR DE INTENÇÕES ▼▼▼
async def classify_user_intent(user_query: str, cart_items: List[Dict]) -> str:
    """
    Classifica a intenção principal do usuário com base na sua mensagem e no estado do carrinho.
    Este é o cérebro que substitui as listas de palavras-chave, agora com exemplos ricos.
    """
    possible_intents = ["ADD_ITEMS", "REQUEST_SUGGESTION", "FINISH_ORDER", "GREETING_OR_QUESTION", "SHOW_CART"]
    if cart_items:
        # Apenas se o carrinho não estiver vazio, estas intenções são possíveis.
        possible_intents.extend(["REMOVE_ITEMS", "MODIFY_QUANTITY", "CLEAR_CART"])

    prompt = f"""
    Analise a frase do cliente e classifique sua intenção principal em UMA das seguintes categorias: {possible_intents}.
    Sua resposta DEVE ser um objeto JSON com uma única chave "intent".

    ---
    **EXEMPLOS DE CLASSIFICAÇÃO:**

    **intent: "ADD_ITEMS"**
    - "quero uma pizza e uma coca"
    - "me ve um x-salada"
    - "Poderia me enviar um steak au poivre, por favor?"
    - "manda dois bife a cavalo pra viagem"
    - "vou querer 3 porções de fritas"
    - "queria um fetuccine alfredo"
    - "tem aquele prato com pato?"
    - "vou querer a costela de cordeiro e uma água com gás"
    - "uma picanha pra um"
    - "acho que vou de lasanha hoje"

    **intent: "REMOVE_ITEMS"**
    - "tira a salada do pedido"
    - "não vou mais querer o pato, obrigado"
    - "pode remover o suco e a água por favor"
    - "cancela o bife de chorizo"
    - "mudei de ideia sobre o gnocchi"
    - "acho melhor não pegar o steak tartare"
    - "remove o fetuccini"
    - "daquele pedido, tira o último item"
    - "sem a coca"
    - "tem como tirar as batatas?"

    **intent: "MODIFY_QUANTITY"**
    - "na verdade, quero 2 nhoques"
    - "pode ser só 1 terrine"
    - "Poderia aumentar a quantidade de águas para 4?"
    - "bota mais um bife aí, por favor"
    - "ops, pedi duas cocas, mas quero só uma"
    - "troca pra 3 porções de fritas"
    - "acho que vou precisar de mais um steak"
    - "dobra a quantidade do pato"
    - "dá pra mudar as costelas pra 3?"
    - "aumenta o fetuchini pra dois"

    **intent: "CLEAR_CART"**
    - "esvazie meu carrinho"
    - "pode limpar meu pedido, por favor?"
    - "zera tudo aí, mano"
    - "quero começar de novo"
    - "cancela o pedido todo"
    - "esquece, deixa pra lá"
    - "hmm, acho que vou repensar. pode limpar?"
    - "limpa o carrinho"
    - "Gostaria de cancelar todos os itens."
    - "tem como cancelar tudo?"

    **intent: "REQUEST_SUGGESTION"**
    - "tem sugestões?"
    - "o que você me recomenda de massa?"
    - "o que tem de bom aí hoje?"
    - "poderia me dar algumas opções de carne?"
    - "o que você me indica?"
    - "qual a especialidade da casa?"
    - "não sei o que pedir, me ajuda?"
    - "tô em dúvida entre peixe e frango, o que sugere?"
    - "tem alguma sugestão pra dividir?"
    - "queria algo mais leve, o que você tem?"

    **intent: "FINISH_ORDER"**
    - "só isso"
    - "é só isso, obrigado"
    - "pode fechar a conta"
    - "pronto, pode finalizar"
    - "por enquanto é isso"
    - "fechou"
    - "Gostaria de concluir meu pedido."
    - "podemos fechar?"
    - "já pedi tudo"
    - "acabou"

    **intent: "GREETING_OR_QUESTION"**
    - "oi"
    - "olá"
    - "boa tarde"
    - "e aí, beleza?"
    - "oi tudo bem?"
    - "bom dia"
    - "qual o horário de funcionamento?"
    - "vocês entregam na Vila Madalena?"
    - "qual o pix de vocês?"
    - "vcs aceitam cartão?"

    **intent: "SHOW_CART"**
    - "qual meu pedido atual?"
    - "o que tem no meu carrinho?"
    - "me mostra meu pedido"
    - "como está meu pedido até agora?"
    - "quantos itens tem no carrinho?"
    - "o que pedi até agora?"
    - "pode me lembrar o que eu pedi?"
    - "confirma meu pedido"
    - "revisa meu pedido, por favor"
    - "me mostra tudo que pedi"
    ---

    Frase do Cliente: "{user_query}"
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
        intent = data.get("intent", "GREETING_OR_QUESTION")
        return intent if intent in possible_intents else "GREETING_OR_QUESTION"
    except Exception as e:
        print(f"Erro ao classificar intenção: {e}")
        return "GREETING_OR_QUESTION"