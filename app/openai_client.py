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


async def get_forced_tool_call(
    messages: List[Dict], tool: Dict, model: str = "gpt-4o-mini"
) -> str:
    """Call the LLM forcing a single tool; return the tool_call arguments as
    a JSON string. The OpenAI API validates tool_call args against the tool
    schema, making this path far more reliable than `response_format=json_object`
    when you need a specific shape. Returns an empty JSON object ("{}") on
    failure so callers can parse uniformly.
    """
    tool_name = tool["function"]["name"]

    def sync_call():
        return client_openai_api.chat.completions.create(
            model=model,
            messages=messages,
            tools=[tool],
            tool_choice={"type": "function", "function": {"name": tool_name}},
            temperature=0.0,
        )

    start = time.perf_counter_ns()
    try:
        response = await asyncio.to_thread(sync_call)
        elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000
        await record_llm_usage(
            bot_id=None,
            operation=f"tool:{tool_name}",
            model=model,
            usage=response.usage,
            duration_ms=elapsed_ms,
        )
        tool_calls = response.choices[0].message.tool_calls
        if tool_calls:
            return tool_calls[0].function.arguments
        return "{}"
    except Exception as e:
        elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000
        await record_llm_usage(
            bot_id=None,
            operation=f"tool:{tool_name}",
            model=model,
            usage=None,
            duration_ms=elapsed_ms,
            success=False,
        )
        logger.error("Forced tool call (%s) failed: %s", tool_name, e)
        return "{}"


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


# Função auxiliar para codificar imagem
def encode_image(image_file):
    return base64.b64encode(image_file).decode("utf-8")


async def extract_products_from_image(
    image_bytes: bytes,
    media_type: str,
    page_text: str | None = None,
    retry_hint: bool = False,
    expected_item_count: int | None = None,
) -> list[dict]:
    """Extract products from one menu page image via gpt-4o-mini vision.

    Args:
        image_bytes: the page rendered as image bytes.
        media_type: MIME type of the image ("image/jpeg", "image/png").
        page_text: optional fitz-extracted text of the same page. Even when
            multi-column layout makes the raw text jumbled, it carries
            correct prices and product names the vision model can
            cross-reference.
        retry_hint: when True, the prompt adds a "you missed items last time"
            framing. Used by the menu_extraction retry path for pages that
            returned 0 products on the first pass.
        expected_item_count: number of price patterns fitz found on this page.
            Seeds the prompt so the model has a concrete target and knows
            NOT to return an empty list when items demonstrably exist.
    """
    base64_image = encode_image(image_bytes)

    # Chain-of-thought: force the model to enumerate names, then prices,
    # then pair them. Eliminates mis-assignment on dense multi-column
    # layouts where description-to-item distance is unstable.
    prompt_parts = [
        "Você é um assistente especializado em digitalizar cardápios.",
        "",
        "PROCESSO (siga nesta ordem, internamente):",
        "1. Liste TODOS os nomes de produtos visíveis — incluindo sidebars, "
        "rodapés, callouts, textos pequenos, tabelas e imagens com legenda. "
        "Não pule nenhum item.",
        "2. Liste TODOS os preços visíveis na página.",
        "3. Pareie cada produto com seu preço correspondente usando "
        "proximidade visual e contexto de seção.",
        "4. Chame save_extracted_products UMA VEZ com todos os pareamentos.",
        "",
        "REGRAS DE ITEM:",
        "- Extraia TODOS os itens com preço: adicionais, complementos, "
        "extras, acompanhamentos, combos e promoções.",
        "- Para cada item: nome, descrição (se visível), preço (número), "
        "categoria (curta, Título em Português), keywords "
        "(sinônimos, abreviações, nome sem acentos, singular/plural).",
        "",
        "REGRAS DE CATEGORIA:",
        "- Use APENAS categorias que existem no cardápio. NÃO invente.",
        "- Exemplos: 'Entradas', 'Cortes', 'Saladas', 'Acompanhamentos', "
        "'Bebidas', 'Sobremesas'.",
        "- Adicionais/extras: categoria 'Adicionais'.",
        "- Não mescle Bebidas, Sobremesas e Adicionais entre si.",
    ]

    if retry_hint:
        if expected_item_count and expected_item_count > 0:
            # We KNOW items exist — fitz counted the prices. Drop the
            # "maybe nothing here" escape hatch and be direct.
            retry_msg = (
                f"ATENÇÃO: a primeira extração retornou 0 produtos, mas foram "
                f"detectados {expected_item_count} padrões de preço no texto "
                "desta página. Os itens EXISTEM. Olhe a página inteira "
                "(incluindo grids, sidebars, callouts, rodapés e texto "
                "pequeno) e extraia TODOS os itens com preço. Não retorne "
                "lista vazia."
            )
        else:
            retry_msg = (
                "ATENÇÃO: a primeira extração desta página retornou 0 "
                "produtos. Olhe a página inteira com mais atenção — itens "
                "podem estar em grids, sidebars, callouts, rodapés ou texto "
                "pequeno. Se realmente não há produtos com preço, retorne "
                "lista vazia."
            )
        prompt_parts.insert(0, retry_msg)
    elif expected_item_count and expected_item_count > 0:
        # Soft hint on first-pass: gives the model a target count without
        # being alarmist.
        prompt_parts.append("")
        prompt_parts.append(
            f"DICA: o texto desta página contém {expected_item_count} "
            "padrões de preço. Espera-se aproximadamente esse número de itens."
        )

    if page_text:
        # Truncate aggressively — we just need prices and names. Even jumbled
        # text helps the model anchor its OCR on correct spellings/prices.
        snippet = page_text[:3000]
        prompt_parts.append("")
        prompt_parts.append(
            "TEXTO DESTA PÁGINA (extraído automaticamente — pode estar "
            "embaralhado em layouts multi-coluna; use como referência de "
            "ortografia e preços, mas o layout visual é autoritativo):"
        )
        prompt_parts.append("---")
        prompt_parts.append(snippet)
        prompt_parts.append("---")

    prompt = "\n".join(prompt_parts)

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
