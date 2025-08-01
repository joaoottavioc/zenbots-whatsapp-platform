# app/whatsapp.py
import asyncio, os, json, re
from typing import Any, Dict, List
import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from app import crud, payment_service
from app.database import async_session
from app.models import ProcessedMessage, Product, ShoppingCart
from app.openai_client import get_ai_decision, extract_potential_items, get_user_intent
from app.prompt_builder import create_tool_prompt
from app.tools_definition import tools_schema
from app.embedding_service import generate_embedding
from datetime import datetime, timedelta

load_dotenv()
router = APIRouter()
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")


@router.post("/webhook")
async def whatsapp_webhook(request: Request):
    data = await request.json()
    if data.get("object") == "whatsapp_business_account" and data.get("entry"):
        if data["entry"][0].get("changes")[0].get("value").get("messages"):
            asyncio.create_task(process_whatsapp_message(data))
    return JSONResponse(content={"status": "received"})

# Em app/whatsapp.py

async def process_whatsapp_message(data: Dict[str, Any]):
    """
    Orquestra o fluxo de conversa com gestão de ciclo de vida do carrinho
    baseado em inatividade, tratando o DB como a única fonte da verdade.
    """
    async with async_session() as session:
        contact_number = data["entry"][0]["changes"][0]["value"]["messages"][0]["from"]
        try:
            # 1. Extração de dados e setup inicial
            value = data["entry"][0]["changes"][0]["value"]
            message_data = value["messages"][0]
            message_id, text_body, bot_number = (
                message_data["id"],
                message_data["text"]["body"],
                value["metadata"]["display_phone_number"],
            )

            await mark_message_as_read(message_id)
            if await crud.is_message_processed(session, message_id): return
            await crud.add_processed_message(session, message_id)

            bot = await crud.get_bot_by_number(session, bot_number)
            if not bot: return
            
            # 2. Obtenção do Contato e do Carrinho Persistente
            contact = await crud.get_or_create_contact(session, bot.id, contact_number)
            cart = await crud.get_or_create_cart(session, contact.id)
            
            # --- LÓGICA DE EXPIRAÇÃO DE SESSÃO ---
            SESSION_TIMEOUT = timedelta(minutes=3)
            
            if (datetime.utcnow() - cart.last_activity_at) > SESSION_TIMEOUT:
                print(f"Sessão para o contato {contact_number} expirou. Limpando o carrinho.")
                await crud.clear_db_cart(session, cart.id)
                cart = await crud.get_or_create_cart(session, contact.id)

            # Lógica de Reinício explícito
            if "reinicie" in text_body.lower():
                await crud.clear_db_cart(session, cart.id)
                response_to_user = "🗑️ Carrinho esvaziado. Olá! Como posso ajudar?"
                await send_whatsapp_message(to=contact_number, message=response_to_user)
                await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
                await session.commit()
                return

            # 3. Lógica de estados explícita para checkout
            if cart.state == "AWAITING_ADDRESS":
                cart.state = "AWAITING_PAYMENT_METHOD"
                session.add(cart)
                line_totals = [(item.product.price * item.quantity) for item in cart.items]
                total_amount = sum(line_totals)
                response_to_user = f"📍 Ótimo, pedido para o endereço: {text_body}.\nO total é R$ {total_amount:.2f}. Qual será a forma de pagamento?"
                
                await send_whatsapp_message(to=contact_number, message=response_to_user)
                await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
                await session.commit()
                return

            # 4. Busca RAG e chamada da IA
            extracted_item_names = await extract_potential_items(text_body)
            found_products = []
            if extracted_item_names:
                found_products = await crud.find_relevant_products(session, bot.id, extracted_item_names)

            history_records = await crud.get_history_for_contact(session, bot.id, contact_number)
            past_messages = [{"role": h.role, "content": h.content} for h in history_records]
            
            await session.refresh(cart, attribute_names=['items'])

            cart_items_for_prompt = [{"product_id": item.product.id, "name": item.product.name, "quantity": item.quantity} for item in cart.items]

            prompt = create_tool_prompt(
                search_results=found_products, user_query=text_body,
                history=past_messages, restaurant_name=bot.restaurant_name,
                cart_items=cart_items_for_prompt, current_state=cart.state
            )
            ai_message = await get_ai_decision(prompt, tools_schema)

            # 5. Processamento da resposta da IA
            response_to_user = "Desculpe, não entendi. Pode reformular?"
            
            if ai_message and ai_message.tool_calls:
                tool_call = ai_message.tool_calls[0]
                tool_name = tool_call.function.name
                tool_args = json.loads(tool_call.function.arguments)
                
                if tool_name in ["add_items_to_cart", "modify_item_quantity", "remove_item_from_cart"]:
                    response_emoji = "🛒" # Emoji padrão
                    
                    if tool_name == "add_items_to_cart":
                        response_emoji = "✅" # Emoji para ADIÇÃO
                        await crud.add_items_to_db_cart(session, cart.id, tool_args.get("items", []))
                    
                    elif tool_name == "modify_item_quantity":
                        response_emoji = "✏️" # Emoji para MODIFICAÇÃO
                        await crud.modify_item_quantity_in_db_cart(session, cart.id, tool_args.get("product_id"), tool_args.get("new_quantity"))
                    
                    elif tool_name == "remove_item_from_cart":
                        response_emoji = "❌" # Emoji para REMOÇÃO
                        await crud.modify_item_quantity_in_db_cart(session, cart.id, tool_args.get("product_id"), 0)
                    
                    # Recarrega o carrinho para ter a visão mais recente
                    await session.refresh(cart, attribute_names=['items'])
                    # Passa o emoji escolhido para a função de resumo
                    response_to_user = _build_cart_summary_message(cart, emoji=response_emoji) + "\n\nAlgo mais?"

                elif tool_name == "request_customer_address":
                    cart.state = "AWAITING_ADDRESS"
                    session.add(cart)
                    response_to_user = "Entendido. Para qual endereço será a entrega?"
                
                elif tool_name == "answer_conversationally":
                    response_to_user = tool_args.get("response_text", response_to_user)

            elif ai_message and ai_message.content:
                response_to_user = ai_message.content

            # --- ATUALIZAÇÃO DA ÚLTIMA ATIVIDADE ---
            cart.last_activity_at = datetime.utcnow()
            session.add(cart)

            # 6. Envio e persistência
            await send_whatsapp_message(to=contact_number, message=response_to_user)
            await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
            await session.commit()

        except Exception as e:
            print(f"Erro crítico ao processar a mensagem do contato {contact_number}: {e}")
            if 'session' in locals() and session.is_active:
                await session.rollback()
            await send_whatsapp_message(
                to=contact_number,
                message="Desculpe, ocorreu um erro inesperado."
            )

def _build_cart_summary_message(cart: ShoppingCart, emoji: str = "🛒") -> str:
    """Constrói a string formatada do resumo do carrinho, agora com emoji dinâmico."""
    if not cart.items:
        return "🗑️ *Seu carrinho agora está vazio.*"

    cart_summary_lines, total_amount = [], 0.0
    for item in cart.items:
        quantity, price, name = item.quantity, item.product.price, item.product.name
        line_total = price * quantity
        total_amount += line_total
        cart_summary_lines.append(f"- {quantity}x {name} (R$ {line_total:.2f})")
    
    cart_summary_text = "\n".join(cart_summary_lines)
    # Usa o emoji passado como parâmetro
    return f"{emoji} *Seu Pedido Atual:*\n{cart_summary_text}\n\nTotal: *R$ {total_amount:.2f}*"

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

async def mark_message_as_read(message_id: str):
    """
    Marca a mensagem do usuário como lida, criando a percepção de 'digitando'.
    """
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, headers=headers, json=data)
            response.raise_for_status()
            print(f"Mensagem {message_id} marcada como lida.")
        except httpx.HTTPStatusError as e:
            # Não paramos a execução se isso falhar, apenas registramos o erro.
            print(f"Erro ao marcar mensagem como lida: {e.response.text}")