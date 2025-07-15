import asyncio
import os
from typing import Any, Dict

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app import crud
from app.database import async_session
from app.models import ProcessedMessage
from app.openai_client import get_phi3_response
from app.prompt_builder import create_prompt_with_context

load_dotenv()

router = APIRouter()

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")


@router.post("/webhook")
async def whatsapp_webhook(request: Request):
    """
    Recebe o webhook da Meta, responde imediatamente com 200 OK,
    e cria uma tarefa em segundo plano para processar a mensagem.
    """
    data = await request.json()
    
    if data.get("object") == "whatsapp_business_account" and data.get("entry"):
        if data["entry"][0].get("changes")[0].get("value").get("messages"):
            asyncio.create_task(process_whatsapp_message(data))
        
    return JSONResponse(content={"status": "received"})


async def process_whatsapp_message(data: Dict[str, Any]):
    """
    Processa a mensagem recebida usando o fluxo RAG completo com busca semântica.
    """
    async with async_session() as session:
        try:
            # 1. PARSE DA MENSAGEM
            value = data["entry"][0]["changes"][0]["value"]
            message_data = value["messages"][0]
            
            message_id = message_data["id"]
            from_number = message_data["from"]
            text_body = message_data["text"]["body"]
            bot_number = value["metadata"]["display_phone_number"]

            # 2. LÓGICA DE IDEMPOTÊNCIA E BUSCA DO BOT
            processed_msg = await session.execute(select(ProcessedMessage).where(ProcessedMessage.message_id == message_id))
            if processed_msg.scalar_one_or_none():
                print(f"Mensagem {message_id} já processada.")
                return

            bot = await crud.get_bot_by_number(session, bot_number)
            if not bot:
                print(f"Bot com o número {bot_number} não encontrado.")
                return

            # --- FLUXO RAG (Retrieval-Augmented Generation) ---

            # 3. RETRIEVAL: Busca semântica por produtos relevantes no catálogo
            found_products = await crud.search_products_by_similarity(
                session=session,
                bot_id=bot.id,
                query_text=text_body,
                limit=3
            )

            # 4. RETRIEVAL: Recupera o histórico da conversa
            history_records = await crud.get_history_for_contact(session, bot_id=bot.id, contact_number=from_number)
            past_messages = [{"role": h.role, "content": h.content} for h in history_records]

            # 5. AUGMENTATION: Gera o prompt dinâmico com o contexto encontrado
            # 👇 CORREÇÃO APLICADA AQUI 👇
            full_conversation_prompt = create_prompt_with_context(
                search_results=found_products,
                user_query=text_body,
                history=past_messages,
                restaurant_name=bot.restaurant_name # Passa o nome do restaurante
            )

            # 6. GENERATION: Chama a IA com o prompt inteligente
            bot_response_text = await get_phi3_response(full_conversation_prompt)

            # 7. SALVAR E RESPONDER
            session.add(ProcessedMessage(message_id=message_id))
            await crud.add_interaction_to_history(
                session=session,
                bot_id=bot.id,
                contact_number=from_number,
                user_content=text_body,
                assistant_content=bot_response_text
            )
            
            await send_whatsapp_message(to=from_number, message=bot_response_text)

        except Exception as e:
            print(f"Erro crítico ao processar a mensagem: {e}")


@router.get("/webhook")
async def verify_webhook(request: Request):
    """Verifica o token do webhook da Meta."""
    params = dict(request.query_params)
    verify_token = os.getenv("META_VERIFY_TOKEN")
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == verify_token:
        return PlainTextResponse(content=params.get("hub.challenge"))
    return PlainTextResponse(content="Invalid verification", status_code=403)


async def send_whatsapp_message(to: str, message: str):
    """Envia a mensagem de texto final para o usuário via API do WhatsApp."""
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "text": {"body": message}
    }
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, headers=headers, json=data)
            response.raise_for_status()
            print(f"Mensagem enviada para {to}: {response.json()}")
        except httpx.HTTPStatusError as e:
            print(f"Erro ao enviar mensagem para a API do WhatsApp: {e.response.text}")