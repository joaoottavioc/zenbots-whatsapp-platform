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
from app.models import ProcessedMessage, Product
from app.openai_client import get_ai_decision, extract_potential_items
from app.prompt_builder import create_tool_prompt
from app.tools_definition import tools_schema

load_dotenv()
router = APIRouter()
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

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
    Orquestra o fluxo de conversa com lógica de estados explícita e a
    estratégia "Extrair e Buscar" para garantir a precisão dos pedidos.
    """
    async with async_session() as session:
        try:
            # 1. Extração dos dados da mensagem
            value = data["entry"][0]["changes"][0]["value"]
            message_data = value["messages"][0]
            message_id, from_number, text_body, bot_number = (
                message_data["id"], message_data["from"],
                message_data["text"]["body"], value["metadata"]["display_phone_number"],
            )
            
            # 2. Feedback visual e verificação de duplicatas
            await mark_message_as_read(message_id)
            if await crud.is_message_processed(session, message_id):
                return
            await crud.add_processed_message(session, message_id)
            
            # 3. Busca do bot e inicialização do estado da conversa
            bot = await crud.get_bot_by_number(session, bot_number)
            if not bot: return
            
            contact_number = from_number
            if contact_number not in shopping_carts:
                shopping_carts[contact_number] = {"items": [], "state": "GREETING"}
            
            cart_info = shopping_carts.get(contact_number)
            cart_items = cart_info.get("items", [])
            current_state = cart_info.get("state", "GREETING")
            
            response_to_user = "Desculpe, não entendi. Pode reformular?"

            # --- CORREÇÃO DA PRIORIDADE 2: LÓGICA DE ESTADOS EXPLÍCITA ---
            if current_state == "AWAITING_ADDRESS":
                # Se estamos esperando um endereço, a mensagem do usuário É o endereço.
                # Não precisamos de IA para esta etapa.
                shopping_carts[contact_number]["state"] = "AWAITING_PAYMENT_METHOD"
                total_amount = sum(item['price'] * item['quantity'] for item in cart_items)
                response_to_user = f"Ótimo, pedido para o endereço: {text_body}. O total é R$ {total_amount:.2f}. Qual será a forma de pagamento, Pix ou Cartão na Entrega?"
                
                await send_whatsapp_message(to=contact_number, message=response_to_user)
                await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
                await session.commit()
                return # Encerra o fluxo aqui, pois esta etapa está completa.

            elif current_state == "AWAITING_PAYMENT_METHOD":
                # Futuramente, a lógica de pagamento entrará aqui.
                # Por enquanto, vamos deixar a IA decidir o que fazer.
                pass

            # --- CORREÇÃO DA PRIORIDADE 1: LÓGICA "EXTRAIR E BUSCAR" ROBUSTA ---
            # A IA primeiro extrai os nomes dos itens da frase do usuário.
            extracted_item_names = await extract_potential_items(text_body)
            
            found_products = []
            if extracted_item_names:
                # Para cada item extraído, fazemos uma busca focada no banco de dados.
                for item_name in extracted_item_names:
                    search_results = await crud.search_products_by_similarity(session, bot.id, item_name, limit=1)
                    if search_results:
                        found_products.extend(search_results)
            else:
                # Fallback: se a extração falhar, faz a busca ampla como antes.
                found_products = await crud.search_products_by_similarity(session, bot.id, text_body)

            # 6. Preparação do contexto para a IA, agora com produtos muito mais precisos
            history_records = await crud.get_history_for_contact(session, bot.id, contact_number)
            past_messages = [{"role": h.role, "content": h.content} for h in history_records]

            prompt = create_tool_prompt(
                search_results=found_products, user_query=text_body,
                history=past_messages, restaurant_name=bot.restaurant_name,
                cart_items=cart_items, current_state=current_state
            )
            ai_message = await get_ai_decision(prompt, tools_schema)

            # 7. Processamento da decisão da IA
            if ai_message and ai_message.tool_calls:
                items_to_add_this_turn = []
                for tool_call in ai_message.tool_calls:
                    tool_name = tool_call.function.name
                    tool_args = json.loads(tool_call.function.arguments)

                    if tool_name == "add_items_to_cart":
                        items_to_add_this_turn.extend(tool_args.get("items", []))
                    elif tool_name == "request_customer_address":
                        shopping_carts[contact_number]["state"] = "AWAITING_ADDRESS"
                        response_to_user = "Entendido. Para qual endereço será a entrega?"
                    elif tool_name == "answer_conversationally":
                        response_to_user = tool_args.get("response_text", "")
                
                # 8. Lógica de adição de itens ao carrinho
                if items_to_add_this_turn:
                    added_summary_names = []
                    for item_data in items_to_add_this_turn:
                        product = await session.get(Product, item_data.get("product_id"))
                        if product:
                            shopping_carts[contact_number]["items"].append({
                                "product_id": product.id, "name": product.name,
                                "quantity": item_data.get("quantity"), "price": product.price
                            })
                            summary_line = str(item_data.get('quantity')) + "x " + product.name
                            added_summary_names.append(summary_line)
                    
                    if added_summary_names:
                        shopping_carts[contact_number]["state"] = "ORDERING"
                        updated_cart_items = shopping_carts[contact_number]["items"]
                        cart_summary_lines, total_amount = [], 0.0
                        for item in updated_cart_items:
                            # Prevenção de erro se 'quantity' for None
                            quantity = item.get('quantity', 0)
                            price = item.get('price', 0.0)
                            line_total = price * quantity
                            total_amount += line_total
                            
                            line_text = "- " + str(quantity) + "x " + item['name']
                            price_text = " (R$ {:.2f})".format(line_total)
                            cart_summary_lines.append(line_text + price_text)
                        
                        cart_summary_text = "\n".join(cart_summary_lines)
                        added_text = ", ".join(added_summary_names)
                        
                        response_to_user = (
                            "✅ Adicionado: " + added_text + ".\n\n"
                            "🛒 *Seu Pedido Atual:*\n" + cart_summary_text + "\n\n"
                            "Total: *R$ {:.2f}*\n\nAlgo mais?".format(total_amount)
                        )

            elif ai_message and ai_message.content:
                response_to_user = ai_message.content

            # 9. Envio da resposta e persistência dos dados
            await send_whatsapp_message(to=contact_number, message=response_to_user)
            await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
            await session.commit()

        except Exception as e:
            print(f"Erro crítico ao processar a mensagem: {e}")

# ... (o resto do ficheiro continua igual)

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