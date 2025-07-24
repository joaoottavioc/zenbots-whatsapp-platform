import asyncio
import os
import json
import re
from typing import Any, Dict, List

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app import crud, payment_service
from app.database import async_session
from app.models import ProcessedMessage, Product
from app.openai_client import get_chat_response_gpt
from app.prompt_builder import create_order_management_prompt
from app.openai_client import get_ai_decision
from app.prompt_builder import create_tool_prompt
from app.tools_definition import tools_schema

load_dotenv()

router = APIRouter()

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

# Dicionário em memória para o carrinho de compras
shopping_carts = {}

@router.post("/webhook")
async def whatsapp_webhook(request: Request):
    data = await request.json()
    if data.get("object") == "whatsapp_business_account" and data.get("entry"):
        if data["entry"][0].get("changes")[0].get("value").get("messages"):
            asyncio.create_task(process_whatsapp_message(data))
    return JSONResponse(content={"status": "received"})

async def process_whatsapp_message(data: Dict[str, Any]):
    """
    Orquestra o fluxo de conversa usando uma IA com ferramentas e uma máquina de estados.
    """
    async with async_session() as session:
        try:
            # --- 1. PARSE DA MENSAGEM E SETUP ---
            value = data["entry"][0]["changes"][0]["value"]
            message_data = value["messages"][0]
            message_id, from_number, text_body, bot_number = (
                message_data["id"],
                message_data["from"],
                message_data["text"]["body"],
                value["metadata"]["display_phone_number"],
            )

            processed_msg = await session.execute(select(ProcessedMessage).where(ProcessedMessage.message_id == message_id))
            if processed_msg.scalar_one_or_none(): return
            
            bot = await crud.get_bot_by_number(session, bot_number)
            if not bot: return
            
            session.add(ProcessedMessage(message_id=message_id))
            
            contact_number = from_number
            if contact_number not in shopping_carts:
                shopping_carts[contact_number] = {"items": [], "state": "GREETING"}
            
            cart_info = shopping_carts.get(contact_number)
            cart_items, current_state = cart_info.get("items", []), cart_info.get("state", "GREETING")
            
            # --- 2. FLUXO DO AGENTE ---
            
            # Foca a atenção da IA, ignorando a busca RAG quando não é necessária
            if current_state in ["AWAITING_ADDRESS", "AWAITING_PAYMENT_METHOD"]:
                found_products = []
            else:
                found_products = await crud.search_products_by_similarity(session, bot.id, text_body)

            history_records = await crud.get_history_for_contact(session, bot.id, contact_number)
            past_messages = [{"role": h.role, "content": h.content} for h in history_records]

            prompt = create_tool_prompt(
                search_results=found_products, user_query=text_body,
                history=past_messages, restaurant_name=bot.restaurant_name,
                cart_items=cart_items, current_state=current_state
            )
            ai_message = await get_ai_decision(prompt, tools_schema)
            
            response_to_user = "Desculpe, não entendi. Pode reformular?"

            if ai_message and ai_message.tool_calls:
                tool_call = ai_message.tool_calls[0]
                tool_name = tool_call.function.name
                tool_args = json.loads(tool_call.function.arguments)

                if tool_name == "add_item_to_cart":
                    product = await session.get(Product, tool_args.get("product_id"))
                    if product:
                        shopping_carts[contact_number]["items"].append({
                            "product_id": product.id, "name": product.name,
                            "quantity": tool_args.get("quantity", 1), "price": product.price
                        })
                        shopping_carts[contact_number]["state"] = "ORDERING"
                        response_to_user = f"Adicionado: {tool_args.get('quantity', 1)}x {product.name}. Algo mais?"

                elif tool_name == "request_customer_address":
                    shopping_carts[contact_number]["state"] = "AWAITING_ADDRESS"
                    response_to_user = "Entendido. Para qual endereço será a entrega?"

                elif tool_name == "process_order_with_address":
                    order_items = cart_items
                    if order_items:
                        new_order = await crud.create_order(session, bot.id, order_items)
                        response_to_user = f"Ótimo, pedido para o endereço: {tool_args.get('customer_address')}. O total é R$ {new_order.total_amount:.2f}. Qual será a forma de pagamento, Pix ou Cartão na Entrega?"
                        shopping_carts[contact_number]["state"] = "AWAITING_PAYMENT_METHOD"
                
                elif tool_name == "process_payment_choice":
                    method = tool_args.get("method")
                    order_items = cart_items # Pega os itens antes de o carrinho ser apagado
                    
                    if not order_items:
                        response_to_user = "O seu carrinho está vazio. Vamos adicionar alguns itens primeiro?"
                    
                    elif method == "PIX":
                        # 👇 INTEGRAÇÃO COM O MERCADO PAGO ACONTECE AQUI 👇
                        new_order = await crud.create_order(session, bot.id, order_items)
                        customer_email_placeholder = f"{contact_number}@zenbots.com"

                        pix_payment_data = payment_service.create_pix_payment(
                            order_id=new_order.id,
                            total_amount=new_order.total_amount,
                            customer_email=customer_email_placeholder,
                            restaurant_name=bot.restaurant_name
                        )
                        
                        if pix_payment_data:
                            new_order.psp_charge_id = str(pix_payment_data.get("id"))
                            session.add(new_order)
                            await session.commit()
                            
                            pix_copy_paste = pix_payment_data['point_of_interaction']['transaction_data']['qr_code']
                            response_to_user = f"Pedido finalizado! Para pagar, use o Pix Copia e Cola abaixo. Ele expira em 15 minutos.\n\n`{pix_copy_paste}`"
                            del shopping_carts[contact_number]
                        else:
                            response_to_user = "Desculpe, não consegui gerar a cobrança Pix neste momento."

                    elif method == "CARD":
                        await crud.create_order(session, bot.id, order_items)
                        response_to_user = "Entendido, pagamento com cartão na entrega. O seu pedido já está a ser preparado!"
                        del shopping_carts[contact_number]

                elif tool_name == "answer_conversationally":
                    response_to_user = tool_args.get("response_text", "")
            
            elif ai_message and ai_message.content:
                response_to_user = ai_message.content

            await send_whatsapp_message(to=contact_number, message=response_to_user)
            await crud.add_interaction_to_history(
                session, bot.id, contact_number, text_body, response_to_user
            )

        except Exception as e:
            print(f"Erro crítico ao processar a mensagem: {e}")


@router.get("/webhook")
async def verify_webhook(request: Request):
    params = dict(request.query_params)
    verify_token = os.getenv("META_VERIFY_TOKEN")
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == verify_token:
        return PlainTextResponse(content=params.get("hub.challenge"))
    return PlainTextResponse(content="Invalid verification", status_code=403)


async def send_whatsapp_message(to: str, message: str):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "to": to, "text": {"body": message}}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, headers=headers, json=data)
            response.raise_for_status()
            print(f"Mensagem enviada para {to}: {response.json()}")
        except httpx.HTTPStatusError as e:
            print(f"Erro ao enviar mensagem para a API do WhatsApp: {e.response.text}")