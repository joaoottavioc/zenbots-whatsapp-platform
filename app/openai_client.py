import asyncio
import base64
import json
import logging
import os
import time
from typing import List, Dict
from dotenv import load_dotenv
from openai import OpenAI

logger = logging.getLogger(__name__)
from app.tools_definition import tools_extraction
from app.monitoring import record_llm_usage

# Carrega as variáveis de ambiente do arquivo .env
load_dotenv()

# --- Cliente OpenAI (Extração de dados + chat + ferramentas) ---
client_openai_api = OpenAI(
    # A biblioteca lê a chave OPENAI_API_KEY automaticamente das variáveis de ambiente
    api_key=os.getenv("OPENAI_API_KEY"),
    timeout=120.0,
)

# --- Cliente Groq (Whisper STT para áudio do WhatsApp) ---
_groq_api_key = os.getenv("GROQ_API_KEY")
_groq_client: OpenAI | None = (
    OpenAI(
        api_key=_groq_api_key,
        base_url="https://api.groq.com/openai/v1",
        timeout=30.0,
    )
    if _groq_api_key
    else None
)


async def transcribe_audio(
    audio_bytes: bytes,
    prompt: str = "",
    bot_id: int | None = None,
) -> str:
    """Transcribe audio bytes via Groq Whisper (primary) or OpenAI Whisper (fallback).

    Args:
        audio_bytes: Raw audio data (OGG/Opus from WhatsApp).
        prompt: Conditioning prompt with restaurant vocabulary to boost accuracy.
        bot_id: For cost tracking via monitoring.

    Returns:
        Transcribed text, or empty string on failure.
    """
    start = time.perf_counter_ns()
    transcript = ""
    provider = "groq_whisper"

    # Primary: Groq Whisper Large v3 Turbo
    if _groq_client:
        try:
            result = await asyncio.to_thread(
                _groq_client.audio.transcriptions.create,
                file=("audio.ogg", audio_bytes),
                model="whisper-large-v3-turbo",
                language="pt",
                prompt=prompt[:800] if prompt else "",
            )
            transcript = (result.text or "").strip()
        except Exception as e:
            logger.warning("Groq Whisper failed, falling back to OpenAI: %s", e)
            provider = "openai_whisper"

    # Fallback: OpenAI Whisper
    if not transcript:
        try:
            provider = "openai_whisper"
            result = await asyncio.to_thread(
                client_openai_api.audio.transcriptions.create,
                file=("audio.ogg", audio_bytes),
                model="whisper-1",
                language="pt",
                prompt=prompt[:800] if prompt else "",
            )
            transcript = (result.text or "").strip()
        except Exception as e:
            logger.error("OpenAI Whisper fallback also failed: %s", e)

    duration_ms = (time.perf_counter_ns() - start) // 1_000_000

    try:
        await record_llm_usage(
            bot_id=bot_id,
            operation="transcribe_audio",
            model=provider,
            usage=None,
            duration_ms=duration_ms,
            success=bool(transcript),
        )
    except Exception:
        pass  # Don't fail the transcription if monitoring fails

    logger.info(
        "[STT] provider=%s duration=%dms transcript_len=%d prompt_len=%d",
        provider,
        duration_ms,
        len(transcript),
        len(prompt),
    )
    return transcript


async def get_extraction_response(
    messages: List[Dict], model: str = "gpt-4o-mini"
) -> str:
    """
    Calls the OpenAI API for structured data extraction (JSON mode).
    Defaults to gpt-4o-mini for cost/speed balance. Use model="gpt-4o"
    when higher accuracy is needed (e.g., complex image-based menus).
    """

    def sync_call():
        return client_openai_api.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.0,
            response_format={"type": "json_object"},
        )

    start = time.perf_counter_ns()
    try:
        response = await asyncio.to_thread(sync_call)
        elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000
        await record_llm_usage(
            bot_id=None,
            operation="get_extraction_response",
            model=model,
            usage=response.usage,
            duration_ms=elapsed_ms,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000
        await record_llm_usage(
            bot_id=None,
            operation="get_extraction_response",
            model=model,
            usage=None,
            duration_ms=elapsed_ms,
            success=False,
        )
        logger.error("Extraction model call failed: %s", e)
        return "[]"


async def get_chat_response_gpt(messages: List[Dict]) -> str:
    """
    Usa um modelo rápido e de alta fiabilidade (gpt-4o-mini) da API da OpenAI
    para o chat em tempo real, forçando uma resposta em JSON.
    """

    def sync_call():
        return client_openai_api.chat.completions.create(
            model="gpt-4o-mini",  # 👈 Modelo alterado
            messages=messages,
            temperature=0.2,
            max_tokens=300,
            # 👇 A instrução crucial para garantir a fiabilidade
            response_format={"type": "json_object"},
        )

    start = time.perf_counter_ns()
    try:
        response = await asyncio.to_thread(sync_call)
        elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000
        await record_llm_usage(
            bot_id=None,
            operation="get_chat_response_gpt",
            model="gpt-4o-mini",
            usage=response.usage,
            duration_ms=elapsed_ms,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000
        await record_llm_usage(
            bot_id=None,
            operation="get_chat_response_gpt",
            model="gpt-4o-mini",
            usage=None,
            duration_ms=elapsed_ms,
            success=False,
        )
        logger.error("Chat model call failed: %s", e)
        # Retorna um JSON de fallback em caso de erro
        return '{"action": "CONTINUE_CONVERSATION", "response_to_user": "Desculpe, ocorreu um erro. Pode tentar novamente?"}'


async def get_ai_decision(
    messages: List[Dict], tools: List[Dict], force_tool: bool = False
) -> Dict:
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

    start = time.perf_counter_ns()
    try:
        response = await asyncio.to_thread(sync_call)
        elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000
        await record_llm_usage(
            bot_id=None,
            operation="get_ai_decision",
            model="gpt-4o-mini",
            usage=response.usage,
            duration_ms=elapsed_ms,
        )
        return response.choices[0].message
    except Exception as e:
        elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000
        await record_llm_usage(
            bot_id=None,
            operation="get_ai_decision",
            model="gpt-4o-mini",
            usage=None,
            duration_ms=elapsed_ms,
            success=False,
        )
        logger.error("Tool-calling API request failed: %s", e)
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

    Frase: "quero duas nega maluca e um café"
    Resultado: {{"items": ["nega maluca", "café"]}}

    Frase: "pode me mandar uma tainha assada"
    Resultado: {{"items": ["tainha assada"]}}

    Frase: "vou querer um x polenta pra viagem"
    Resultado: {{"items": ["x polenta"]}}

    Frase: "quero um bolo de macadamias"
    Resultado: {{"items": ["bolo de macadamias"]}}

    Frase: "quero um XXXXXX"
    Resultado: {{"items": ["XXXXXX"]}}

    Frase: "{user_query}"
    Resultado:
    """

    def sync_call():
        return client_openai_api.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            response_format={"type": "json_object"},
        )

    start = time.perf_counter_ns()
    try:
        response = await asyncio.to_thread(sync_call)
        elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000
        await record_llm_usage(
            bot_id=None,
            operation="extract_potential_items",
            model="gpt-4o-mini",
            usage=response.usage,
            duration_ms=elapsed_ms,
        )
        data = json.loads(response.choices[0].message.content)
        items = data.get("items", [])
        if isinstance(items, list):
            return items
        return []
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000
        await record_llm_usage(
            bot_id=None,
            operation="extract_potential_items",
            model="gpt-4o-mini",
            usage=None,
            duration_ms=elapsed_ms,
            success=False,
        )
        logger.error("Failed to extract items from user query: %s", e)
        return []


async def classify_user_intent(user_query: str, cart_items: List[Dict]) -> str:
    """
    Inclui CONFIRM e NEGATE para confirmações/negações curtas.
    """
    possible_intents = [
        "ADD_ITEMS",
        "REQUEST_SUGGESTION",
        "FINISH_ORDER",
        "GREETING_OR_QUESTION",
        "SHOW_CART",
        "CONFIRM",
        "NEGATE",
    ]
    if cart_items:
        possible_intents.extend(["REMOVE_ITEMS", "MODIFY_QUANTITY", "CLEAR_CART"])

    prompt = f"""
    Classifique a intenção em UMA destas: {", ".join(possible_intents)}.

    Regras:
    - "CONFIRM": mensagem curta, equivalente a "sim", "ok", "claro", "perfeito", "fechou", "uhum", "aham", "👍".
      Use APENAS quando a mensagem for essencialmente só isso.
    - "NEGATE": mensagem curta equivalente a "não", "nope", "nah". Só isso.
    - "FINISH_ORDER": "só isso", "pode fechar".
    - "REQUEST_SUGGESTION": pede sugestão.
    - "ADD_ITEMS": pede itens.
    - "REMOVE_ITEMS"/"MODIFY_QUANTITY"/"CLEAR_CART": ajustes do carrinho.
    - "SHOW_CART": quer ver o carrinho.
    - "GREETING_OR_QUESTION": saudações/perguntas gerais do restaurante.

    Sua resposta DEVE ser JSON {{ "intent": "<UMA_INTENCAO>" }}.

    Exemplos:
    "sim" -> {{"intent":"CONFIRM"}}
    "ok" -> {{"intent":"CONFIRM"}}
    "não" -> {{"intent":"NEGATE"}}
    "sim, quero mais" -> {{"intent":"ADD_ITEMS"}}
    "pode fechar a conta" -> {{"intent":"FINISH_ORDER"}}
    "tem pratos com carne?" -> {{"intent":"REQUEST_SUGGESTION"}}

    Frase: "{user_query}"
    Resultado:
    """

    def sync_call():
        return client_openai_api.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            response_format={"type": "json_object"},
        )

    start = time.perf_counter_ns()
    try:
        response = await asyncio.to_thread(sync_call)
        elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000
        await record_llm_usage(
            bot_id=None,
            operation="classify_user_intent",
            model="gpt-4o-mini",
            usage=response.usage,
            duration_ms=elapsed_ms,
        )
        data = json.loads(response.choices[0].message.content)
        intent = data.get("intent", "GREETING_OR_QUESTION")
        return intent if intent in possible_intents else "GREETING_OR_QUESTION"
    except Exception as e:
        elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000
        await record_llm_usage(
            bot_id=None,
            operation="classify_user_intent",
            model="gpt-4o-mini",
            usage=None,
            duration_ms=elapsed_ms,
            success=False,
        )
        logger.error("Intent classification failed: %s", e)
        return "GREETING_OR_QUESTION"


# Função auxiliar para codificar imagem
def encode_image(image_file):
    return base64.b64encode(image_file).decode("utf-8")


async def extract_products_from_image(
    image_bytes: bytes, media_type: str
) -> list[dict]:
    """
    Envia uma imagem (cardápio) para o GPT-4o-mini e extrai os produtos estruturados.
    Uses gpt-4o-mini for speed (~5-15s vs 30-60s with gpt-4o).
    """
    base64_image = encode_image(image_bytes)

    prompt = (
        "Você é um assistente especializado em digitalizar cardápios. "
        "Analise esta imagem. Extraia TODOS os itens que possuem preço, "
        "incluindo adicionais, complementos, extras, acompanhamentos, combos e promoções. "
        "Para cada item, identifique: nome, descrição, preço, CATEGORIA e KEYWORDS. "
        "Regras de Categoria: agrupe itens similares (ex: Coca, Água, Suco -> 'Bebidas'). "
        "Adicionais/extras devem ter categoria 'Adicionais'. "
        "Use APENAS categorias que existem no cardápio. NÃO invente categorias. "
        "Use nomes curtos e em Português. "
        "Ex: 'Entradas', 'Pratos Principais', 'Sobremesas', 'Lanches', 'Porções', 'Adicionais'. "
        "Keywords: inclua sinônimos, abreviações, erros de digitação comuns e variações "
        "(ex: 'burguer' para 'burger', 'refri' para refrigerante, nome sem acentos, singular/plural)."
    )

    def sync_call():
        return client_openai_api.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media_type};base64,{base64_image}",
                                "detail": "high",
                            },
                        },
                    ],
                }
            ],
            tools=tools_extraction,
            tool_choice={
                "type": "function",
                "function": {"name": "save_extracted_products"},
            },
            temperature=0.2,
            max_tokens=16384,
        )

    start = time.perf_counter_ns()
    try:
        response = await asyncio.to_thread(sync_call)
        elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000
        await record_llm_usage(
            bot_id=None,
            operation="extract_products_from_image",
            model="gpt-4o-mini",
            usage=response.usage,
            duration_ms=elapsed_ms,
        )

        tool_calls = response.choices[0].message.tool_calls
        if tool_calls:
            args = json.loads(tool_calls[0].function.arguments)
            return args.get("products", [])
        return []

    except Exception as e:
        elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000
        await record_llm_usage(
            bot_id=None,
            operation="extract_products_from_image",
            model="gpt-4o-mini",
            usage=None,
            duration_ms=elapsed_ms,
            success=False,
        )
        logger.error("Vision AI extraction failed: %s", e)
        return []
