from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from app.database import get_session, async_session
from app.models import ProcessedMessage
from app import crud
# 👇 1. Importa a função correta
from app.openai_client import get_phi3_response 
import os
import httpx
import asyncio
from dotenv import load_dotenv

load_dotenv()

router = APIRouter()

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")


@router.post("/webhook")
async def whatsapp_webhook(request: Request):
    data = await request.json()
    asyncio.create_task(process_whatsapp_message(data))
    return JSONResponse(content={"status": "received"})


async def process_whatsapp_message(data):
    async with async_session() as session:
        try:
            entry = data["entry"][0]
            changes = entry["changes"][0]
            value = changes["value"]
            messages = value.get("messages")
            if not messages:
                print("Webhook recebido sem mensagem (ex: notificação de status).")
                return

            message = messages[0]
            message_id = message["id"]
            from_number = message["from"]
            text_body = message["text"]["body"]
            bot_number = value["metadata"]["display_phone_number"]

            query = select(ProcessedMessage).where(ProcessedMessage.message_id == message_id)
            result = await session.execute(query)
            if result.scalar_one_or_none():
                print(f"Mensagem {message_id} já processada.")
                return

            bot = await crud.get_bot_by_number(session, bot_number)
            if not bot:
                print(f"Bot com o número {bot_number} não encontrado.")
                return
            
            # Adiciona a mensagem à lista de processadas para evitar duplicidade
            session.add(ProcessedMessage(message_id=message_id))

            system_prompt = {"role": "system", "content": bot.system_prompt}
            
            history = await crud.get_history_for_contact(session, bot_id=bot.id, contact_number=from_number)
            past_messages = [{"role": h.role, "content": h.content} for h in history]

            new_user_message = {"role": "user", "content": text_body}
            full_conversation = [system_prompt] + past_messages + [new_user_message]
            
            # 👇 2. Chama a função de IA com o nome correto
            bot_response_text = await get_phi3_response(full_conversation)

            # 👇 3. Substitui as duas chamadas antigas pela nova função otimizada
            await crud.add_interaction_to_history(
                session=session,
                bot_id=bot.id,
                contact_number=from_number,
                user_content=text_body,
                assistant_content=bot_response_text
            )

            await send_whatsapp_message(to=from_number, message=bot_response_text)

        except Exception as e:
            print(f"Erro ao processar a mensagem: {e}")


@router.get("/webhook")
async def verify_webhook(request: Request):
    params = dict(request.query_params)
    verify_token = os.getenv("META_VERIFY_TOKEN")
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == verify_token:
        return PlainTextResponse(content=params.get("hub.challenge"))
    return PlainTextResponse(content="Invalid verification", status_code=403)


async def send_whatsapp_message(to: str, message: str):
    url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
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