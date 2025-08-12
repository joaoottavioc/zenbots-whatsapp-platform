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
from app.openai_client import classify_user_intent, get_ai_decision, extract_potential_items
from app.prompt_builder import create_tool_prompt
from app.tools_definition import tools_schema
from app.embedding_service import generate_embedding
from datetime import datetime, timedelta, timezone
from app.prompt_central import create_central_prompt


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

# app/whatsapp.py (conteúdo integral da função)

async def process_whatsapp_message(data: Dict[str, Any]):
    async with async_session() as session:
        contact_number = data["entry"][0]["changes"][0]["value"]["messages"][0]["from"]
        try:
            print("📥 Mensagem recebida do WhatsApp")

            value = data["entry"][0]["changes"][0]["value"]
            message_data = value["messages"][0]
            message_id, text_body, bot_number = (
                message_data["id"],
                message_data["text"]["body"],
                value["metadata"]["display_phone_number"],
            )

            print(f"📨 Mensagem ID: {message_id}")
            print(f"📱 Bot: {bot_number} | Usuário: {contact_number}")
            print(f"💬 Conteúdo: {text_body}")

            await mark_message_as_read(message_id)

            if await crud.is_message_processed(session, message_id):
                print("⏩ Mensagem já processada anteriormente. Ignorando.")
                return

            await crud.add_processed_message(session, message_id)

            bot = await crud.get_bot_by_number(session, bot_number)
            if not bot:
                print("❌ Bot não encontrado para o número informado.")
                return

            contact = await crud.get_or_create_contact(session, bot.id, contact_number)
            print(f"📇 Contact: id={contact.id}, número={contact.phone_number}, bot_id={bot.id}")
            cart = await crud.get_or_create_cart(session, contact.id)
            print(f"🛒 Carrinho: id={cart.id}, estado={cart.state}, itens={len(cart.items)}")

            # Expiração de sessão
            SESSION_TIMEOUT = timedelta(minutes=15)
            if datetime.now(timezone.utc) - cart.last_activity_at.replace(tzinfo=timezone.utc) > SESSION_TIMEOUT:
                await crud.clear_db_cart(session, cart.id)
                response_to_user = (
                "⏰ Ficamos um tempinho sem falar e, por segurança, esvaziamos seu carrinho 🛒\n\n"
                "Mas é só me dizer o que quer pedir e recomeçamos! 😄✅"
                )
                await send_whatsapp_message(contact_number, response_to_user)
                await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
                cart.last_activity_at = datetime.now(timezone.utc)
                session.add(cart)
                await session.commit()
                print("🧹 Sessão expirada e carrinho limpo.")
                return

            if cart.state == "AWAITING_ADDRESS":
                print("📍 Estado atual: aguardando endereço")

                # 1️⃣ Primeiro, classifica a intenção do usuário (mesmo em AWAITING_ADDRESS)
                cart_items_for_intent = [{"id": item.product_id, "name": item.product.name} for item in cart.items]
                intent = await classify_user_intent(text_body, cart_items_for_intent)
                print(f"🎯 Intenção classificada: {intent}")

                # 2️⃣ Se o usuário quiser limpar o carrinho mesmo nesse estado
                if intent == "CLEAR_CART":
                    await crud.clear_db_cart(session, cart.id)
                    cart.state = "GREETING"
                    session.add(cart)
                    response_to_user = "🛒 Carrinho esvaziado. Pode me dizer o que você deseja pedir!"
                    await send_whatsapp_message(contact_number, response_to_user)
                    await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
                    await session.commit()
                    return

                # 3️⃣ Tenta extrair o endereço via regex
                if re.search(r"\b[\w\s]{3,}\s+\d{1,5}\b", text_body, re.IGNORECASE):
                    await crud.save_customer_address(session, cart.id, text_body.strip())
                    cart.state = "AWAITING_PAYMENT_METHOD"
                    response_to_user = "Endereço salvo! Qual será a forma de pagamento? (PIX ou Cartão)"
                    print("📌 Endereço salvo via regex simples.")

                else:
                    print("🤖 Endereço não identificado via regex. Chamando IA.")

                    history_records = await crud.get_history_for_contact(session, bot.id, contact_number)
                    past_messages = [{"role": h.role, "content": h.content} for h in history_records]

                    await session.refresh(cart, attribute_names=['items'])
                    cart_items_for_prompt = [
                        {"product_id": item.product.id, "name": item.product.name, "quantity": item.quantity}
                        for item in cart.items
                    ]

                    prompt = create_central_prompt(
                        user_query=text_body,
                        history=past_messages,
                        restaurant_name=bot.restaurant_name,
                        cart_items=cart_items_for_prompt
                    )

                    print("🧠 Prompt gerado (AWAITING_ADDRESS):")
                    for p in prompt:
                        print(p)

                    from app.tools_definition import tools_schema
                    print("🧰 Tools disponíveis:", [t['function']['name'] for t in tools_schema])

                    ai_message = await get_ai_decision(prompt, tools_schema, force_tool=True)
                    print("📤 Resposta da IA (AWAITING_ADDRESS):", ai_message)

                    response_to_user = "Desculpe, não entendi. Pode reformular?"
                    if ai_message and ai_message.tool_calls:
                        tool_call = ai_message.tool_calls[0]
                        if tool_call.function.name == "process_order_with_address":
                                tool_args = json.loads(tool_call.function.arguments)
                                address = tool_args.get("customer_address")
                                if address:
                                    await crud.save_customer_address(session, cart.id, address)
                                    cart.state = "AWAITING_PAYMENT_METHOD"
                                    response_to_user = "Endereço salvo! Qual será a forma de pagamento? (PIX ou Cartão)"
                                    print("🏠 Endereço extraído e salvo com sucesso.")
                                    
                        elif tool_name == "bulk_modify_quantities":
                            for upd in tool_args.get("updates", []):
                                await crud.modify_item_quantity_in_db_cart(
                                session, cart.id,
                                upd.get("product_id"),
                                upd.get("new_quantity")
                                )
                            await session.refresh(cart, attribute_names=['items'])
                            response_to_user = _build_cart_summary_message(cart, "✏️") + "\n\nAlgo mais?"

                cart.last_activity_at = datetime.now(timezone.utc)
                session.add(cart)
                await send_whatsapp_message(contact_number, response_to_user)
                await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
                await session.commit()
                return


            # Classificação de intenção
            cart_items_for_intent = [{"id": item.product_id, "name": item.product.name} for item in cart.items]
            intent = await classify_user_intent(text_body, cart_items_for_intent)
            print(f"🎯 Intenção classificada: {intent}")

            response_to_user = "Desculpe, não entendi. Pode reformular?"

            from app.tools_definition import tools_schema

            if intent == "CLEAR_CART":
                await crud.clear_db_cart(session, cart.id)
                response_to_user = "🛒 Carrinho esvaziado."
                
            elif intent == "SHOW_CART":
                await session.refresh(cart, attribute_names=['items'])
                response_to_user = _build_cart_summary_message(cart)

            elif intent == "FINISH_ORDER":
                cart.state = "AWAITING_ADDRESS"
                session.add(cart)
                response_to_user = "Entendido. Para qual endereço será a entrega?"

            elif intent == "REQUEST_SUGGESTION":
                topic_items = await extract_potential_items(text_body)
                found_products = await crud.find_relevant_products(session, bot.id, topic_items or ["pratos principais"], limit_per_item=4)
                # recuperar sugestões anteriores
                recent_suggestions = None
                if cart.last_suggestions:
                    sug_res = await session.execute(select(Product).where(Product.id.in_(cart.last_suggestions)))
                    sug_map = {p.id: p for p in sug_res.scalars().all()}
                    recent_suggestions = [sug_map[id] for id in cart.last_suggestions if id in sug_map]

                history_records = await crud.get_history_for_contact(session, bot.id, contact_number)
                past_messages = [{"role": h.role, "content": h.content} for h in history_records]

                await session.refresh(cart, attribute_names=['items'])
                cart_items_for_prompt = [
                    {"product_id": item.product.id, "name": item.product.name, "quantity": item.quantity}
                    for item in cart.items
                ]

                prompt = create_central_prompt(
                    user_query=text_body,
                    history=past_messages,
                    restaurant_name=bot.restaurant_name,
                    cart_items=cart_items_for_prompt,
                    search_results=found_products,
                    recent_suggestions=recent_suggestions
                )

                print("📦 Prompt com sugestão gerado:")
                for p in prompt:
                    print(p)

                ai_message = await get_ai_decision(prompt, tools_schema, force_tool=True)
                print("📤 Resposta da IA:", ai_message)

                if ai_message and ai_message.tool_calls:
                    tool_call = ai_message.tool_calls[0]
                    tool_name = tool_call.function.name
                    tool_args = json.loads(tool_call.function.arguments)

                    if tool_name == "add_items_to_cart":
                        items_arg = tool_args.get("items", [])

                        # 🔒 Cinturão de segurança: valida itens
                        if not items_arg or not isinstance(items_arg, list):
                            # Não aceita lista vazia. Reforce o contexto de sugestões.
                            if cart.last_suggestions:
                                response_to_user = (
                                    "Acho que você se referiu às sugestões. "
                                    "Pode confirmar usando os números? Ex: '5 do primeiro e 3 do segundo'."
                                )
                            else:
                                response_to_user = (
                                    "Não identifiquei os itens para adicionar. "
                                    "Quer ver algumas sugestões ou repetir os nomes?"
                                )
                        else:
                            # ✅ Valida IDs antes de gravar
                            product_ids = [i.get("product_id") for i in items_arg if isinstance(i, dict)]
                            if not product_ids or any(pid is None for pid in product_ids):
                                response_to_user = (
                                    "Os itens vieram incompletos. Pode confirmar as quantidades e os itens?"
                                )
                            else:
                                # (opcional) valida se os IDs existem no catálogo do bot
                                existing = await session.execute(
                                    select(Product.id).where(Product.bot_id == bot.id, Product.id.in_(product_ids))
                                )
                                existing_ids = set(existing.scalars().all())
                                missing = [pid for pid in product_ids if pid not in existing_ids]

                                if missing:
                                    response_to_user = (
                                        "Alguns itens não foram reconhecidos no cardápio. "
                                        "Pode confirmar usando os números da lista de sugestões?"
                                    )
                                else:
                                    await crud.add_items_to_db_cart(session, cart.id, items_arg)
                                    used_ids = product_ids
                                    if not any(pid in (cart.last_suggestions or []) for pid in used_ids):
                                        cart.last_suggestions = None
                                    await session.refresh(cart, attribute_names=['items'])
                                    response_to_user = _build_cart_summary_message(cart, "✅") + "\n\nAlgo mais?"

                    elif tool_name == "answer_conversationally":
                        response_to_user = tool_args.get("response_text", response_to_user)

                    elif tool_name == "answer_with_found_products":
                        # Renderiza usando found_products (IDs + ordem estável), não os nomes soltos do modelo
                        if found_products:
                            response_to_user = "Essas são algumas sugestões para você:\n" + \
                                "\n".join(f"{i+1}. {p.name}" for i, p in enumerate(found_products))
                            cart.last_suggestions = [p.id for p in found_products] # ordem idêntica à exibida
                        else:
                            response_to_user = "No momento, não encontrei sugestões específicas para o que você pediu."
                            
                    elif tool_name == "bulk_modify_quantities":
                        for upd in tool_args.get("updates", []):
                            await crud.modify_item_quantity_in_db_cart(
                            session, cart.id,
                            upd.get("product_id"),
                            upd.get("new_quantity")
                            )
                        await session.refresh(cart, attribute_names=['items'])
                        response_to_user = _build_cart_summary_message(cart, "✏️") + "\n\nAlgo mais?"

                if found_products:
                    cart.last_suggestions = [p.id for p in found_products]

            else:
                # Casos ADD / REMOVE / MODIFY
                extracted_items = await extract_potential_items(text_body)
                found_products = await crud.find_relevant_products(session, bot.id, extracted_items) if extracted_items else []

                recent_suggestions = None
                if cart.last_suggestions:
                    sug_res = await session.execute(select(Product).where(Product.id.in_(cart.last_suggestions)))
                    sug_map = {p.id: p for p in sug_res.scalars().all()}
                    recent_suggestions = [sug_map[id] for id in cart.last_suggestions if id in sug_map]

                history_records = await crud.get_history_for_contact(session, bot.id, contact_number)
                past_messages = [{"role": h.role, "content": h.content} for h in history_records]

                await session.refresh(cart, attribute_names=['items'])
                cart_items_for_prompt = [
                    {"product_id": item.product.id, "name": item.product.name, "quantity": item.quantity}
                    for item in cart.items
                ]

                prompt = create_central_prompt(
                    user_query=text_body,
                    history=past_messages,
                    restaurant_name=bot.restaurant_name,
                    cart_items=cart_items_for_prompt,
                    search_results=found_products,
                    recent_suggestions=recent_suggestions
                )

                print("🧠 Prompt final:")
                for p in prompt:
                    print(p)

                ai_message = await get_ai_decision(prompt, tools_schema, force_tool=True)
                print("📤 Resposta da IA:", ai_message)

                if ai_message and ai_message.tool_calls:
                    tool_call = ai_message.tool_calls[0]
                    tool_name = tool_call.function.name
                    tool_args = json.loads(tool_call.function.arguments)

                    if tool_name == "add_items_to_cart":
                        items_arg = tool_args.get("items", [])

                        # 🔒 Cinturão de segurança: bloqueia add vazio
                        if not items_arg or not isinstance(items_arg, list):
                            if cart.last_suggestions:
                                response_to_user = (
                                    "Entendi a ideia, mas preciso dos itens. "
                                    "Pode dizer, por exemplo, '5 do primeiro e 3 do segundo'?"
                                )
                            else:
                                response_to_user = (
                                    "Não consegui mapear os itens. "
                                    "Pode repetir os nomes ou pedir sugestões?"
                                )
                        else:
                            product_ids = [i.get("product_id") for i in items_arg if isinstance(i, dict)]
                            if not product_ids or any(pid is None for pid in product_ids):
                                response_to_user = "Os itens vieram incompletos. Pode confirmar as quantidades e os itens?"
                            else:
                                existing = await session.execute(
                                    select(Product.id).where(Product.bot_id == bot.id, Product.id.in_(product_ids))
                                )
                                existing_ids = set(existing.scalars().all())
                                missing = [pid for pid in product_ids if pid not in existing_ids]

                                if missing:
                                    response_to_user = (
                                        "Alguns itens não batem com o cardápio. "
                                        "Pode confirmar usando os números da última lista?"
                                    )
                                else:
                                    await crud.add_items_to_db_cart(session, cart.id, items_arg)
                                    used_ids = product_ids
                                    if not any(pid in (cart.last_suggestions or []) for pid in used_ids):
                                        cart.last_suggestions = None
                                    await session.refresh(cart, attribute_names=['items'])
                                    response_to_user = _build_cart_summary_message(cart, "✅") + "\n\nAlgo mais?"

                    elif tool_name == "remove_items_from_cart":
                        ids_to_remove = tool_args.get("product_ids", [])
                        for product_id in ids_to_remove:
                            await crud.modify_item_quantity_in_db_cart(session, cart.id, product_id, 0)
                        await session.refresh(cart, attribute_names=['items'])
                        response_to_user = _build_cart_summary_message(cart, "❌") + "\n\nAlgo mais?"

                    elif tool_name == "modify_item_quantity":
                        await crud.modify_item_quantity_in_db_cart(session, cart.id, tool_args.get("product_id"), tool_args.get("new_quantity"))
                        await session.refresh(cart, attribute_names=['items'])
                        response_to_user = _build_cart_summary_message(cart, "✏️") + "\n\nAlgo mais?"
                        
                    elif tool_name == "bulk_modify_quantities":
                        for upd in tool_args.get("updates", []):
                            await crud.modify_item_quantity_in_db_cart(
                            session, cart.id,
                            upd.get("product_id"),
                            upd.get("new_quantity")
                            )
                        await session.refresh(cart, attribute_names=['items'])
                        response_to_user = _build_cart_summary_message(cart, "✏️") + "\n\nAlgo mais?"

                    elif tool_name == "answer_conversationally":
                        response_to_user = tool_args.get("response_text", response_to_user)
                    
                    elif tool_name == "answer_with_found_products":
                        product_names = tool_args.get("product_names", [])
                        if product_names:
                            response_to_user = "Essas são algumas sugestões para você:\n" + "\n".join(f"- {name}" for name in product_names)
                        else:
                            response_to_user = "No momento, não encontrei sugestões específicas para o que você pediu."

            cart.last_activity_at = datetime.now(timezone.utc)
            session.add(cart)
            await send_whatsapp_message(contact_number, response_to_user)
            await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
            await session.commit()

            print("✅ Mensagem processada e resposta enviada.")

        except Exception as e:
            print(f"❌ Erro crítico: {e}")
            if 'session' in locals() and session.is_active:
                await session.rollback()
            await send_whatsapp_message(contact_number, "Desculpe, ocorreu um erro inesperado.")


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