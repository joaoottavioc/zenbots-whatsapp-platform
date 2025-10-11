# app/whatsapp.py
import asyncio, os, json, re
from typing import Any, Dict, List
import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from app import crud
from app.database import async_session
from app.models import ProcessedMessage, Product, ShoppingCart
from app.openai_client import classify_user_intent, get_ai_decision, extract_potential_items
from app.prompt_central import create_central_prompt
from app.tools_definition import tools_schema
from datetime import timedelta, timezone
import regex as re
from sqlmodel import select
from app.models import Product
from app.semantic_router import semantic_intent, THRESHOLDS
from app.address_service import get_address_from_cep
import httpx #SIMULADOR

# --- Imports Atualizados ---
# Helper para gerenciar o estado de "ação pendente"
from app.pending_action import save_pending, clear_pending, has_valid_pending, expire_if_needed
# Função padronizada para obter o tempo atual em UTC
from app.time import utcnow


load_dotenv()
router = APIRouter()
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

# Extrai "QTD + NOME" da pergunta de confirmação (ex.: "1 Gnocchis de la Mémé Forte, 2 X, ...")
_ITEM_FROM_Q_RE = re.compile(
    r"(\d{1,6})\s+([A-Za-zÀ-ÿ'´`^~\- ]+?)(?:\s+por\s*R\$\s*[\d.,]+|\s*(?:,| e |$))",
    re.IGNORECASE
)


@router.post("/webhook")
async def whatsapp_webhook(request: Request):
    data = await request.json()
    if data.get("object") == "whatsapp_business_account" and data.get("entry"):
        if data["entry"][0].get("changes")[0].get("value").get("messages"):
            asyncio.create_task(process_whatsapp_message(data))
    return JSONResponse(content={"status": "received"})


async def process_whatsapp_message(data: Dict[str, Any]):
    async with async_session() as session:
        contact_number = data["entry"][0]["changes"][0]["value"]["messages"][0]["from"]
        try:
            print("📥 Mensagem recebida do WhatsApp")
            value = data["entry"][0]["changes"][0]["value"]
            message_data, text_body, bot_number = (
                value["messages"][0],
                value["messages"][0]["text"]["body"],
                value["metadata"]["display_phone_number"],
            )
            message_id = message_data["id"]

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
            cart = await crud.get_or_create_cart(session, contact.id)
            
            # --- INÍCIO DA VERIFICAÇÃO DO INTERRUPTOR ---
            # Se o atendimento humano foi ativado para este cliente, o bot não faz nada.
            if cart.human_takeover_active:
                print(f"🤖 ATENDIMENTO HUMANO ATIVO para {contact_number}. Bot ignorando mensagem.")
                return # Interrompe todo o processamento da mensagem
            # --- FIM DA VERIFICAÇÃO ---

            # 1. Expira ação pendente se o tempo tiver passado
            expire_if_needed(cart)

            # Em app/whatsapp.py

            # 2. Expiração de sessão (inatividade) com lógica corrigida
            SESSION_TIMEOUT = timedelta(minutes=10)
            LONG_TIMEOUT = timedelta(hours=12)

            inactivity_duration = utcnow() - cart.last_activity_at.replace(tzinfo=timezone.utc)

            # Primeiro, checamos a condição de inatividade MAIS LONGA.
            if inactivity_duration > LONG_TIMEOUT:
                print(f"🧹 Inatividade longa ({inactivity_duration}). Limpando carrinho silenciosamente e continuando o fluxo.")
                clear_pending(cart)
                await crud.clear_db_cart(session, cart.id)
                # Nenhum 'return' aqui. A execução do código continua para processar a nova mensagem.

            # Se não for uma inatividade longa, checamos se é uma inatividade CURTA.
            elif inactivity_duration > SESSION_TIMEOUT:
                print(f"🧹 Sessão expirada após {inactivity_duration}. Avisando o usuário.")
                clear_pending(cart)
                await crud.clear_db_cart(session, cart.id)
                
                response_to_user = (
                    "⏰ Ficamos um tempinho sem falar e, por segurança, esvaziamos seu carrinho 🛒\n\n"
                    "Mas é só me dizer o que quer pedir e recomeçamos! 😄✅"
                )
                
                try:
                    async with httpx.AsyncClient() as client:
                        await client.post("http://host.docker.internal:9000/broadcast", json={"text": response_to_user})
                except httpx.RequestError as e:
                    print(f"❌ DEBUG: Erro ao conectar com o servidor SSE: {e}")
                
                await send_whatsapp_message(contact_number, response_to_user)
                await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
                cart.last_activity_at = utcnow()
                session.add(cart)
                await session.commit()
                return # O 'return' aqui é crucial para interromper o fluxo.
                
                
            # ▼▼▼ INÍCIO DA NOVA LÓGICA DE RESET DE ESTADO ▼▼▼
            # Se o cliente realizar uma ação de compra enquanto estivermos finalizando,
            # o bot entende que ele voltou a "fazer o pedido"    
            if cart.state in ["AWAITING_CEP", "AWAITING_NUMBER_COMPLEMENT", "AWAITING_CUSTOMER_NAME"] and not is_likely_shopping_intent(text_body):
                print(f"⏩ Estado '{cart.state}' detectado e mensagem não parece de compras. Pulando intenção.")
                intent = None # Definimos a intenção como None para pular a lógica de reset
            else:
                # Caso contrário, executamos a classificação de intenção normalmente
                await session.refresh(cart, attribute_names=["items"])
                cart_items_for_intent = [{"id": item.product_id, "name": item.product.name} for item in cart.items]
                intent = await resolve_intent(text_body, cart, cart_items_for_intent)
                print(f"[INTENT DEBUG] Texto: {text_body!r} → Intent escolhida: {intent}")

            # A lógica de reset de estado continua a mesma, mas agora só será
            # acionada por intenções de compra genuínas.
            intents_that_resume_shopping = ["ADD", "REMOVE", "MODIFY", "REQUEST_SUGGESTION", "SHOW_CART", "CLEAR_CART", "ADD_ITEMS", "REMOVE_ITEMS"]
            finalizing_states = ["AWAITING_CEP", "AWAITING_NUMBER_COMPLEMENT", "AWAITING_ADDRESS_CONFIRMATION", "AWAITING_PAYMENT_METHOD", "AWAITING_CUSTOMER_NAME"]

            if intent in intents_that_resume_shopping and cart.state in finalizing_states:
                print(f"🔄 Cliente voltou a comprar (Intent: {intent}). Resetando estado de '{cart.state}' para 'GREETING'.")
                cart.state = "GREETING"
                await session.flush()

            # Etapa 1: Aguardando o CEP do cliente
            if cart.state == "AWAITING_CEP":
                address_data = await get_address_from_cep(text_body)
                
                if not address_data:
                    response_to_user = "CEP inválido ou não encontrado. 🤔 Por favor, verifique e tente novamente."
                else:
                    cart.partial_address = address_data
                    cart.state = "AWAITING_NUMBER_COMPLEMENT"
                    response_to_user = (
                        f"Encontrei este endereço:\n\n"
                        f"📍 {address_data['street']}, {address_data['neighborhood']}\n"
                        f"{address_data['city']} - {address_data['state']}\n\n"
                        f"Se estiver correto, por favor, me informe o *número* e o *complemento* (se houver)."
                    )
                
                cart.last_activity_at = utcnow()
                session.add(cart)
                
                try:
                    async with httpx.AsyncClient() as client:
                        await client.post("http://host.docker.internal:9000/broadcast", json={"text": response_to_user})
                except httpx.RequestError as e:
                    print(f"❌ DEBUG: Erro ao conectar com o servidor SSE: {e}")
                
                await send_whatsapp_message(contact_number, response_to_user)
                await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
                await session.commit()
                return

            # Etapa 2: Aguardando número/complemento E PEDINDO CONFIRMAÇÃO
            elif cart.state == "AWAITING_NUMBER_COMPLEMENT":
                partial = cart.partial_address
                number_complement = text_body.strip()
                
                full_address = (
                    f"{partial['street']}, {number_complement}\n"
                    f"{partial['neighborhood']} - {partial['city']}/{partial['state']}\n"
                    f"CEP: {partial['cep']}"
                )
                
                # ▼▼▼ LÓGICA DE CONFIRMAÇÃO ▼▼▼
                # Em vez de salvar, guardamos o endereço e mudamos de estado.
                cart.pending_address = full_address
                cart.partial_address = None # Limpamos o dado parcial
                cart.state = "AWAITING_ADDRESS_CONFIRMATION"
                
                response_to_user = (
                    f"Tudo certo! Por favor, confirme se o endereço final está correto:\n\n"
                    f"🏠 *{full_address}*\n\n"
                    f"Posso confirmar? (Sim / Não)"
                )
                # ▲▲▲ FIM DA LÓGICA DE CONFIRMAÇÃO ▲▲▲

                cart.last_activity_at = utcnow()
                session.add(cart)
                
                try:
                    async with httpx.AsyncClient() as client:
                        await client.post("http://host.docker.internal:9000/broadcast", json={"text": response_to_user})
                except httpx.RequestError as e:
                    print(f"❌ DEBUG: Erro ao conectar com o servidor SSE: {e}")
                
                await send_whatsapp_message(contact_number, response_to_user)
                await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
                await session.commit()
                return

            # Etapa 3: Aguardando a confirmação final do endereço
            elif cart.state == "AWAITING_ADDRESS_CONFIRMATION":
                # A intenção aqui será 'CONFIRM' ou 'NEGATE'
                if intent == "CONFIRM":
                    final_address = cart.pending_address
                    # ▼▼▼ USE A NOVA FUNÇÃO AQUI ▼▼▼
                    await crud.save_address_to_cart(session, cart.id, final_address)
                    
                    cart.pending_address = None # Limpa o endereço pendente
                    cart.state = "AWAITING_CUSTOMER_NAME" # <-- 1. MUDE O ESTADO
                    response_to_user = "Endereço confirmado! 👍 Agora, por favor, me informe o nome completo para a entrega." # <-- 2. MUDE A PERGUNTA
                
                elif intent == "NEGATE":
                    cart.pending_address = None # Limpa o endereço pendente
                    cart.state = "AWAITING_CEP" # Volta para o início do fluxo
                    response_to_user = "Entendido. Vamos recomeçar. Por favor, me informe o seu CEP novamente."

                else:
                    # Se o usuário digitar algo diferente de sim/não
                    response_to_user = "Por favor, responda com 'Sim' para confirmar o endereço ou 'Não' para recomeçar."

                cart.last_activity_at = utcnow()
                session.add(cart)
                
                try:
                    async with httpx.AsyncClient() as client:
                        await client.post("http://host.docker.internal:9000/broadcast", json={"text": response_to_user})
                except httpx.RequestError as e:
                    print(f"❌ DEBUG: Erro ao conectar com o servidor SSE: {e}")
                
                await send_whatsapp_message(contact_number, response_to_user)
                await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
                await session.commit()
                return
             
            # Etapa 4: Aguardando o nome do cliente
            elif cart.state == "AWAITING_CUSTOMER_NAME":
                customer_name = text_body.strip()
            
                # Salva o nome no contato associado ao carrinho
                await crud.save_customer_name_to_contact(session, cart.contact_id, customer_name)
            
                cart.state = "AWAITING_PAYMENT_METHOD"
                # Personaliza a próxima pergunta com o primeiro nome do cliente
                response_to_user = f"Ótimo, {customer_name.split(' ')[0]}! Para finalizar, qual será a forma de pagamento? (PIX ou Cartão)"

                cart.last_activity_at = utcnow()
                session.add(cart)
            
                try:
                    async with httpx.AsyncClient() as client:
                        await client.post("http://host.docker.internal:9000/broadcast", json={"text": response_to_user})
                except httpx.RequestError as e:
                    print(f"❌ DEBUG: Erro ao conectar com o servidor SSE: {e}")
            
                await send_whatsapp_message(contact_number, response_to_user)
                await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
                await session.commit()
                return
                
            elif cart.state == "AWAITING_PAYMENT_METHOD":
                user_text = text_body.lower()
                order_created = False # Flag para saber se devemos limpar o carrinho

                # Verifica se a resposta é uma forma de pagamento válida
                if "pix" in user_text or "cartão" in user_text or "cartao" in user_text:
                    # Carrega todos os dados necessários para criar o pedido
                    await session.refresh(cart, attribute_names=["items", "contact"])
                    if not cart.contact:
                         raise Exception(f"Carrinho {cart.id} não possui um contato associado.")
                    
                    bot_id = cart.contact.bot_id
                    items_for_order = [{"product_id": item.product_id, "quantity": item.quantity} for item in cart.items]
                    address = getattr(cart, 'customer_address', None) # Usamos getattr para segurança

                    # Cria o pedido no banco de dados ANTES de responder ao usuário
                    await crud.create_order(session, bot_id=bot_id, items=items_for_order, customer_address=address)
                    order_created = True

                # Agora, monta a resposta com base na escolha
                if "pix" in user_text:
                    if bot.pix_key:
                        response_to_user = (
                            f"Ótima escolha! Seu pedido foi registrado com sucesso. ✅\n\n"
                            f"Você pode fazer o pagamento usando nossa chave PIX:\n🔑 *{bot.pix_key}*\n\n"
                            "Muito obrigado pela sua preferência!"
                        )
                    else:
                        response_to_user = "Puxa, parece que não temos uma chave PIX configurada. Poderia ser no Cartão?"
                        order_created = False # Impede a finalização se não há chave PIX
                
                elif "cartão" in user_text or "cartao" in user_text:
                    response_to_user = (
                        "Combinado! Seu pedido foi registrado. ✅\n\n"
                        "Nosso entregador levará a maquininha de cartão até você.\n\n"
                        "Muito obrigado pela sua preferência!"
                    )
                
                else:
                    response_to_user = "Não entendi. Por favor, escolha entre *PIX* ou *Cartão*."

                # Limpa o carrinho APENAS se o pedido foi criado com sucesso
                if order_created:
                    await crud.clear_db_cart(session, cart.id)

                cart.last_activity_at = utcnow()
                session.add(cart)
                
                try:
                    async with httpx.AsyncClient() as client:
                        await client.post("http://host.docker.internal:9000/broadcast", json={"text": response_to_user})
                except httpx.RequestError as e:
                    print(f"❌ DEBUG: Erro ao conectar com o servidor SSE: {e}")
                
                await send_whatsapp_message(contact_number, response_to_user)
                await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
                await session.commit()
                return
            # ▲▲▲ FIM DO BLOCO ADICIONADO ▲▲▲

            if intent in ("CONFIRM", "NEGATE"):
                # Primeiro, checa se há uma ação pendente (ex: "Confirma adicionar X item?").
                # Esta lógica tem prioridade máxima.
                if has_valid_pending(cart):
                    if intent == "CONFIRM":
                        response_to_user = await _execute_pending_action(session, cart, bot_id=bot.id)
                    else: # NEGATE
                        clear_pending(cart)
                        response_to_user = "Beleza, cancelei aquela opção. O que gostaria de fazer agora?"
                
                # Se NÃO houver ação pendente, tratamos como uma confirmação/negação genérica.
                # A chave aqui é IGNORAR os estados de finalização, pois eles têm sua própria lógica.
                elif cart.state in ["GREETING", "SHOPPING"]: # Adicione outros estados "seguros" se tiver
                    if intent == "CONFIRM":
                        # Se o bot acabou de dar sugestões, um "ok" pode ser para escolher.
                        if getattr(cart, "last_suggestions", None):
                            response_to_user = "Show! Qual deles você quer? Pode responder '1', 'o segundo' ou '2 do primeiro'."
                        else:
                            # O "ok" mais comum e seguro.
                            response_to_user = "Perfeito! O que mais deseja?"
                    else: # NEGATE
                        response_to_user = "Tranquilo! Quer ver algumas sugestões ou prefere me dizer o que deseja?"
                
                # Se a intenção for de confirmação/negação, mas o estado for de finalização,
                # a melhor ação é não fazer nada e deixar o fluxo continuar, pois a lógica
                # específica de cada estado (CEP, Endereço, Pagamento) irá tratar a resposta.
                # Para evitar um erro de variável não definida, damos uma resposta padrão segura.
                else:
                    if intent == "CONFIRM":
                        response_to_user = "Perfeito! O que mais deseja?"
                    else: # NEGATE
                        response_to_user = "Entendido. Como posso ajudar?"

                cart.last_activity_at = utcnow()
                session.add(cart)
                
                try:
                    async with httpx.AsyncClient() as client:
                        await client.post("http://host.docker.internal:9000/broadcast", json={"text": response_to_user})
                except httpx.RequestError as e:
                    print(f"❌ DEBUG: Erro ao conectar com o servidor SSE: {e}")
                
                await send_whatsapp_message(contact_number, response_to_user)
                await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
                await session.commit()
                return

            response_to_user = "Desculpe, não entendi. Pode reformular?"

            if intent == "CLEAR_CART":
                clear_pending(cart)
                await crud.clear_db_cart(session, cart.id)
                cart.last_suggestions = None
                response_to_user = "🛒 Carrinho esvaziado."
            elif intent == "SHOW_CART":
                response_to_user = _build_cart_summary_message(cart)
                
            elif intent == "FINISH_ORDER":
                # Garante que o contato associado ao carrinho seja carregado para checar o nome
                await session.refresh(cart, attribute_names=["items", "contact"])
                
                if not cart.items:
                    response_to_user = "Seu carrinho está vazio. O que você gostaria de pedir?"

                # CASO 1: Já temos um endereço COMPLETO E CONFIRMADO?
                elif cart.customer_address:
                    
                    # CASO 1.1: E TAMBÉM já temos o nome do cliente salvo? Ótimo! Vamos para o pagamento.
                    if cart.contact and cart.contact.name:
                        print(f"✅ Endereço e nome já salvos. Pulando para pagamento.")
                        cart.state = "AWAITING_PAYMENT_METHOD"
                        response_to_user = (
                            f"Notei que já temos seu endereço salvo:\n\n"
                            f"🏠 *{cart.customer_address}*\n\n"
                            f"E o pedido está no nome de *{cart.contact.name}*.\n\n"
                            f"Qual será a forma de pagamento? (PIX ou Cartão)"
                        )
                    
                    # CASO 1.2: MAS AINDA NÃO temos o nome? Então pedimos o nome.
                    else:
                        print(f"✅ Endereço salvo, mas nome não encontrado. Solicitando nome.")
                        cart.state = "AWAITING_CUSTOMER_NAME"
                        response_to_user = (
                            "Ok, vamos continuar. Já tenho seu endereço. "
                            "Agora, por favor, me informe o nome completo para a entrega."
                        )

                # CASO 2: Não temos endereço completo, mas temos um PENDENTE de confirmação?
                elif cart.pending_address:
                    print(f"▶️ Retomando fluxo: Aguardando confirmação do endereço.")
                    cart.state = "AWAITING_ADDRESS_CONFIRMATION"
                    response_to_user = (
                        f"Por favor, confirme se o endereço final está correto:\n\n"
                        f"🏠 *{cart.pending_address}*\n\n"
                        f"Posso confirmar? (Sim / Não)"
                    )
                
                # CASO 3: Não temos endereço pendente, mas temos um PARCIAL (do CEP)?
                elif cart.partial_address:
                    address_data = cart.partial_address
                    print(f"▶️ Retomando fluxo: Aguardando número/complemento para o CEP.")
                    cart.state = "AWAITING_NUMBER_COMPLEMENT"
                    response_to_user = (
                        f"Ok, vamos continuar de onde paramos. Encontrei este endereço:\n\n"
                        f"📍 {address_data['street']}, {address_data['neighborhood']}\n"
                        f"{address_data['city']} - {address_data['state']}\n\n"
                        f"Por favor, me informe o *número* e o *complemento* (se houver)."
                    )

                # CASO 4: Se não temos NADA, aí sim começamos do CEP.
                else:
                    cart.state = "AWAITING_CEP"
                    response_to_user = "Entendido. Para finalizar, por favor, me informe o seu CEP."
                    
            else:  # Lógica principal para ADD, REMOVE, MODIFY, REQUEST_SUGGESTION
                # Preparação comum para todos os intents deste bloco
                history_records = await crud.get_history_for_contact(session, bot.id, contact_number)
                past_messages = [{"role": h.role, "content": h.content} for h in history_records]
                await session.refresh(cart, attribute_names=['items'])
                cart_items = [{"product_id": item.product.id, "name": item.product.name, "quantity": item.quantity} for item in cart.items]
                recent_suggestions = None
                if cart.last_suggestions:
                    sug_res = await session.execute(select(Product).where(Product.id.in_(cart.last_suggestions)))
                    sug_map = {p.id: p for p in sug_res.scalars().all()}
                    recent_suggestions = [sug_map[id] for id in cart.last_suggestions if id in sug_map]

                # --- LÓGICA EXCLUSIVA PARA SUGESTÕES ---
                # --- LÓGICA DE SUGESTÃO COM TEMA PADRÃO ---
                if intent == "REQUEST_SUGGESTION":
                    concept = None # Linha 275: Inicializa a variável 'concept'.
                    # Linha 277: Mantém a chamada à IA para tentar extrair um conceito específico.
                    prompt_args = {
                        "user_query": text_body,
                        "history": past_messages,
                        "restaurant_name": bot.restaurant_name,
                        "cart_items": cart_items,
                        "search_results": [],
                        "recent_suggestions": recent_suggestions
                    }
                    # Linha 287: A chamada da função agora usa o dicionário desempacotado, garantindo que todos os argumentos sejam passados.
                    prompt = create_central_prompt(**prompt_args)
                    
                    ai_message = await get_ai_decision(prompt, tools_schema, force_tool=True)
                    
                    if ai_message and ai_message.tool_calls and ai_message.tool_calls[0].function.name == "search_catalog_for_suggestions":
                        tool_args = json.loads(ai_message.tool_calls[0].function.arguments)
                        concept = tool_args.get("search_concept")

                    # Linha 285: Adicionada a lógica principal do fallback.
                    # Se nenhum conceito foi extraído (ex: usuário disse apenas "sugestões")...
                    if not concept:
                        concept = "prato principal" # Linha 287: ...o tema padrão é aplicado.
                        print(f"🧠 Usando tema padrão para sugestão: '{concept}'") # Linha 288: Log de depuração.
                        title = "Claro! Aqui estão algumas das nossas sugestões da casa:" # Linha 289: Título genérico.
                    else:
                        # Linha 291: Este bloco 'else' trata o caso de sucesso, quando um conceito foi extraído.
                        print(f"🧠 Usando conceito extraído pela IA para sugestão: '{concept}'")
                        title = f"Claro! Encontrei estas opções relacionadas a '{concept}':"

                    # Linha 296: A busca de produtos agora é feita uma única vez, usando o conceito
                    # que foi definido (seja o específico da IA ou o padrão).
                    found_products = await crud.find_relevant_products(session, bot.id, [concept])
                    
                    # Linha 299: A lógica de resposta também é unificada.
                    if not found_products:
                        response_to_user = "Puxa, não encontrei nenhuma sugestão no momento. Mas nosso cardápio está cheio de delícias! O que você gostaria?"
                    else:
                        response_to_user = _format_product_suggestions_message(found_products, title)
                        cart.last_suggestions = [p.id for p in found_products]
                
                # --- LÓGICA PARA ADICIONAR, MODIFICAR, REMOVER ITENS ---
                else:
                    # 1. Extração e busca continuam iguais.
                    extracted_items = await extract_potential_items(text_body)
                    search_terms = extracted_items if extracted_items else [text_body]
                    found_products = await crud.find_relevant_products(session, bot.id, search_terms)

                    prompt = create_central_prompt(
                        user_query=text_body, history=past_messages, restaurant_name=bot.restaurant_name,
                        cart_items=cart_items, search_results=found_products, recent_suggestions=recent_suggestions
                    )
                    ai_message = await get_ai_decision(prompt, tools_schema, force_tool=True)

                    if ai_message and ai_message.tool_calls:
                        tool_call, tool_name, tool_args = ai_message.tool_calls[0], ai_message.tool_calls[0].function.name, json.loads(ai_message.tool_calls[0].function.arguments)

                        # ▼▼▼ INÍCIO DA CORREÇÃO COM VALIDAÇÃO ▼▼▼
                        if tool_name == "add_items_to_cart":
                            items_arg = tool_args.get("items", [])
                            
                            # Guarda contra uma chamada de ferramenta completamente vazia ou malformada da IA.
                            if not items_arg:
                                extracted_items_for_msg = await extract_potential_items(text_body)
                                items_str = " e ".join(f"'{item}'" for item in extracted_items_for_msg) if extracted_items_for_msg else "O item que você pediu"
                                response_to_user = f"Desculpe, não encontrei {items_str} em nosso cardápio. Gostaria de tentar outro item ou ver algumas sugestões?"
                            else:
                                clear_pending(cart)
                                # Usamos a soma das quantidades para verificar a mudança, pois é mais robusto.
                                qty_before = sum(item.quantity for item in cart.items)
                                
                                await crud.add_items_to_db_cart(session, cart.id, items_arg)
                                await session.refresh(cart, attribute_names=['items'])

                                qty_after = sum(item.quantity for item in cart.items)

                                # A validação sugerida por você: só mostramos o carrinho se ele mudou.
                                if qty_after > qty_before:
                                    # SUCESSO: O carrinho foi alterado, então mostramos o resumo.
                                    product_ids = [i.get("product_id") for i in items_arg if isinstance(i, dict) and i.get("product_id") is not None]
                                    if not any(pid in (cart.last_suggestions or []) for pid in product_ids): cart.last_suggestions = None
                                    response_to_user = _build_cart_summary_message(cart, "✅") + "\n\nAlgo mais?"
                                else:
                                    # FALHA SILENCIOSA: A operação não alterou o carrinho, então informamos que o item não foi encontrado.
                                    extracted_items_for_msg = await extract_potential_items(text_body)
                                    items_str = " e ".join(f"'{item}'" for item in extracted_items_for_msg) if extracted_items_for_msg else "O item que você pediu"
                                    response_to_user = f"Desculpe, não encontrei {items_str} em nosso cardápio. Gostaria de tentar outro item ou ver algumas sugestões?"
                        # ▲▲▲ FIM DA CORREÇÃO COM VALIDAÇÃO ▲▲▲
                        
                        elif tool_name in ("remove_items_from_cart", "modify_item_quantity", "bulk_modify_quantities"):
                            clear_pending(cart)
                            await session.refresh(cart, attribute_names=['items'])
                            current_ids = {it.product_id for it in cart.items}

                            if tool_name == "remove_items_from_cart":
                                raw_ids = tool_args.get("product_ids", []) or []
                                targets = []
                                for pid in raw_ids:
                                    try: pid = int(pid)
                                    except Exception: continue
                                    if pid in current_ids: targets.append(pid)

                                if not targets:
                                    response_to_user = "Não encontrei esses itens no seu carrinho ainda. Quer que eu adicione algo?"
                                else:
                                    for pid in targets:
                                        await crud.modify_item_quantity_in_db_cart(session, cart.id, pid, 0)
                                    await session.refresh(cart, attribute_names=['items'])
                                    response_to_user = _build_cart_summary_message(cart, "❌") + "\n\nAlgo mais?"

                            elif tool_name == "modify_item_quantity":
                                try:
                                    pid = int(tool_args.get("product_id"))
                                    newq = int(tool_args.get("new_quantity"))
                                except Exception: pid, newq = None, None

                                if pid is None or newq is None or pid not in current_ids:
                                    response_to_user = "Esse item ainda não está no carrinho. Quer que eu adicione para você?"
                                else:
                                    await crud.modify_item_quantity_in_db_cart(session, cart.id, pid, newq)
                                    await session.refresh(cart, attribute_names=['items'])
                                    response_to_user = _build_cart_summary_message(cart, "✏️") + "\n\nAlgo mais?"

                            else:  # bulk_modify_quantities
                                updates_raw = tool_args.get("updates", []) or []
                                updates = []
                                for upd in updates_raw:
                                    try:
                                        pid, newq = int(upd.get("product_id")), int(upd.get("new_quantity"))
                                    except Exception: continue
                                    if pid in current_ids: updates.append({"product_id": pid, "new_quantity": newq})

                                if not updates:
                                    response_to_user = "Não encontrei esses itens no seu carrinho. Posso sugerir opções para adicionar?"
                                else:
                                    for upd in updates:
                                        await crud.modify_item_quantity_in_db_cart(session, cart.id, upd["product_id"], upd["new_quantity"])
                                    await session.refresh(cart, attribute_names=['items'])
                                    response_to_user = _build_cart_summary_message(cart, "✏️") + "\n\nAlgo mais?"

                        elif tool_name == "propose_and_confirm_action":
                            question = tool_args.get("confirmation_question")
                            proposed = tool_args.get("proposed_action", {}) or {}
                            ptool, pargs = proposed.get("tool_name"), proposed.get("tool_args") or proposed.get("parameters") or {}
                            normalized_args = None
                            if ptool == "add_items_to_cart":
                                raw_items = pargs.get("items") or pargs.get("products")
                                resolved = await _resolve_items_for_proposal(session, bot.id, raw_items) if raw_items else []
                                if not resolved and question: resolved = await _items_from_confirmation_question(session, bot.id, question)
                                if resolved: normalized_args = {"items": resolved}
                            elif ptool == "modify_item_quantity":
                                pid, newq = pargs.get("product_id"), pargs.get("new_quantity")
                                if pid is not None and newq is not None:
                                    chk = await session.execute(select(Product.id).where(Product.bot_id == bot.id, Product.id == int(pid)))
                                    if chk.scalars().first(): normalized_args = {"product_id": int(pid), "new_quantity": int(newq)}
                            
                            if question and ptool and normalized_args:
                                save_pending(cart, ptool, normalized_args, question)
                                response_to_user = question
                            else:
                                response_to_user = "Não consegui montar a proposta com itens válidos do cardápio. Quer que eu adicione diretamente os itens do seu pedido?"
                            
                            try:
                                async with httpx.AsyncClient() as client:
                                    await client.post("http://host.docker.internal:9000/broadcast", json={"text": response_to_user})
                            except httpx.RequestError as e:
                                print(f"❌ DEBUG: Erro ao conectar com o servidor SSE: {e}")
                            
                            await send_whatsapp_message(contact_number, response_to_user)
                            await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
                            cart.last_activity_at = utcnow()
                            session.add(cart)
                            await session.commit()
                            return
                        
                        else:
                            final_intent = locals().get("final_intent", intent)
                            if final_intent in ("ADD", "ADD_ITEMS") and extracted_items:
                                items_str = " e ".join(f"'{item}'" for item in extracted_items)
                                response_to_user = f"Desculpe, não encontrei {items_str} em nosso cardápio. Gostaria de tentar outro item ou ver algumas sugestões?"
                            elif tool_name == "answer_conversationally":
                                response_to_user = tool_args.get("response_text", "Não entendi o que você quis dizer. Pode tentar de outra forma?")
                            else:
                                response_to_user = "Não consegui processar seu pedido. Pode tentar de outra forma?"

                    # Se a IA não retornar nenhuma ferramenta, também é uma falha.
                    else:
                        final_intent = locals().get("final_intent", intent)
                        if final_intent in ("ADD", "ADD_ITEMS") and extracted_items:
                            items_str = " e ".join(f"'{item}'" for item in extracted_items)
                            response_to_user = f"Puxa, não localizei {items_str} no nosso cardápio. Temos muitas outras delícias, quer uma sugestão?"
                        else:
                            response_to_user = "Desculpe, não consegui processar seu pedido. Pode reformular, por favor?"

            cart.last_activity_at = utcnow()
            session.add(cart)
            
            try:
                async with httpx.AsyncClient() as client:
                    await client.post("http://host.docker.internal:9000/broadcast", json={"text": response_to_user})
            except httpx.RequestError as e:
                print(f"❌ DEBUG: Erro ao conectar com o servidor SSE: {e}")
            
            await send_whatsapp_message(contact_number, response_to_user)
            await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
            await session.commit()
            print("✅ Mensagem processada e resposta enviada.")

        except Exception as e:
            print(f"❌ Erro crítico: {e}")
            if 'session' in locals() and session.is_active: await session.rollback()
            await send_whatsapp_message(contact_number, "Desculpe, ocorreu um erro inesperado.")


def _build_cart_summary_message(cart: ShoppingCart, emoji: str = "🛒") -> str:
    if not cart.items:
        return "🗑️ *Seu carrinho agora está vazio.*"
    cart_summary_lines, total_amount = [], 0.0
    for item in cart.items:
        line_total = item.product.price * item.quantity
        total_amount += line_total
        cart_summary_lines.append(f"- {item.quantity}x {item.product.name} (R$ {line_total:.2f})")
    return f"{emoji} *Seu Pedido Atual:*\n" + "\n".join(cart_summary_lines) + f"\n\nTotal: *R$ {total_amount:.2f}*"

@router.get("/webhook")
async def verify_webhook(request: Request):
    params = dict(request.query_params)
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == os.getenv("META_VERIFY_TOKEN"):
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
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, headers=headers, json=data)
            response.raise_for_status()
            print(f"Mensagem {message_id} marcada como lida.")
        except httpx.HTTPStatusError as e:
            print(f"Erro ao marcar mensagem como lida: {e.response.text}")

async def _execute_pending_action(session: AsyncSession, cart: ShoppingCart, bot_id: int) -> str:
    tool = cart.pending_action_tool
    args = cart.pending_action_args or {}
    if isinstance(args, str):
        try: args = json.loads(args)
        except json.JSONDecodeError: args = {}
    print(f"EXECUTANDO AÇÃO PENDENTE -> Ferramenta: {tool}, Argumentos: {args}")
    if not tool:
        clear_pending(cart)
        return "Não encontrei nenhuma ação para confirmar. Quer continuar seu pedido?"
    if tool == "add_items_to_cart":
        items_to_add = args.get("items", [])
        if not items_to_add:
            rebuilt = await _items_from_confirmation_question(session, bot_id, cart.pending_action_question)
            if rebuilt: items_to_add = rebuilt
        if not items_to_add:
            clear_pending(cart)
            return "A proposta para adicionar itens estava incompleta. Pode repetir o que deseja?"
        item_ids = [item.get("product_id") for item in items_to_add if isinstance(item, dict) and item.get("product_id") is not None]
        if not item_ids:
            clear_pending(cart)
            return "Não consegui identificar os produtos na proposta. Poderia me dizer novamente?"
        res = await session.execute(select(Product.id).where(Product.bot_id == bot_id, Product.id.in_(item_ids)))
        valid_product_ids = set(res.scalars().all())
        valid_items = [item for item in items_to_add if isinstance(item, dict) and item.get("product_id") in valid_product_ids]
        if not valid_items:
            clear_pending(cart)
            return "Os itens propostos não foram encontrados em nosso cardápio. Quer ver outras opções?"
        await crud.add_items_to_db_cart(session, cart.id, valid_items)
        await session.flush()
        await session.refresh(cart, attribute_names=['items'])
        clear_pending(cart)
        return _build_cart_summary_message(cart, "✅") + "\n\nAlgo mais?"
    if tool == "modify_item_quantity":
        pid, newq = args.get("product_id"), args.get("new_quantity")
        if pid is None or newq is None:
            clear_pending(cart)
            return "A proposta para modificar o item estava incompleta. Pode repetir?"
        await crud.modify_item_quantity_in_db_cart(session, cart.id, int(pid), int(newq))
        await session.flush()
        await session.refresh(cart, attribute_names=['items'])
        clear_pending(cart)
        return _build_cart_summary_message(cart, "✏️") + "\n\nAlgo mais?"
    if tool == "remove_items_from_cart":
        ids = args.get("product_ids", [])
        for pid in ids:
            await crud.modify_item_quantity_in_db_cart(session, cart.id, int(pid), 0)
        await session.flush()
        await session.refresh(cart, attribute_names=['items'])
        clear_pending(cart)
        return _build_cart_summary_message(cart, "❌") + "\n\nAlgo mais?"
    if tool == "answer_with_found_products":
        names = args.get("product_names", [])
        text = "Essas são algumas sugestões para você:\n" + "\n".join(f"{i+1}. {n}" for i, n in enumerate(names)) if names else "Posso sugerir algumas opções, se quiser."
        if names:
            res = await session.execute(select(Product).where(Product.bot_id == bot_id, Product.name.in_(names)))
            prods = res.scalars().all()
            if prods: cart.last_suggestions = [p.id for p in prods]
        clear_pending(cart)
        return text
    clear_pending(cart)
    return "A proposta não pôde ser executada. Pode me dizer de novo o que deseja?"

async def _product_ids_for_bot(session: AsyncSession, bot_id: int) -> set[int]:
    res = await session.execute(select(Product.id).where(Product.bot_id == bot_id))
    return set(res.scalars().all())

async def _resolve_items_for_proposal(session: AsyncSession, bot_id: int, items_arg: list[dict] | None) -> list[dict]:
    if not items_arg: return []
    valid_ids, resolved = await _product_ids_for_bot(session, bot_id), []
    for it in items_arg:
        if not isinstance(it, dict): return []
        q = int(it.get("quantity", 0) or 0)
        if q <= 0: return []
        pid = it.get("product_id")
        if pid is not None:
            try: pid = int(pid)
            except Exception: return []
            if pid not in valid_ids: return []
            resolved.append({"product_id": pid, "quantity": q})
            continue
        pname = (it.get("product_name") or it.get("name") or "").strip()
        if not pname: return []
        found = await session.execute(select(Product).where(Product.bot_id == bot_id, Product.name.ilike(f"%{pname}%")).limit(1))
        p = found.scalars().first()
        if not p: return []
        resolved.append({"product_id": p.id, "quantity": q})
    return resolved

async def _items_from_confirmation_question(session: AsyncSession, bot_id: int, question: str) -> list[dict]:
    if not question: return []
    cands = []
    for qty_txt, name in _ITEM_FROM_Q_RE.findall(question):
        try: qty = int(qty_txt)
        except Exception: continue
        name = name.strip()
        if qty > 0 and name: cands.append({"product_name": name, "quantity": qty})
    return await _resolve_items_for_proposal(session, bot_id, cands) if cands else []
    
async def resolve_intent(text_body, cart, cart_items_for_intent, found_products=None):
    intent_llm = await classify_user_intent(text_body, cart_items_for_intent)
    router_intent, router_score = None, 0.0
    try:
        r_intent, r_score, matched = await semantic_intent(text_body)
        router_intent, router_score = r_intent, r_score
        print(f"[ROUTER] {router_intent}@{router_score:.2f} (match='{matched}')")
    except Exception as e:
        print(f"[ROUTER] fail: {e!r}")
    thresh = THRESHOLDS.get(router_intent, 0.80) if router_intent else 1.0
    if not getattr(cart, "items", []):
        if router_intent in ("MODIFY", "REMOVE"):
            thresh += 0.05
    if found_products and router_intent == "ADD":
        thresh -= 0.02
    final_intent = router_intent if router_intent and router_score >= thresh else intent_llm
    print(f"[INTENT] llm={intent_llm} | router={router_intent}@{router_score:.2f}/{thresh:.2f} -> final={final_intent}")
    return final_intent

def _format_product_suggestions_message(products: List[Product], title: str) -> str:
    if not products:
        return "Puxa, não encontrei nenhuma sugestão específica no momento. Mas nosso cardápio está cheio de delícias! O que você gostaria?"
    message_parts = [f"*{title}* ✨\n"]
    for i, p in enumerate(products):
        price_formatted = f"R$ {p.price:.2f}".replace('.', ',')
        item_str = f"{i+1}️⃣ *{p.name.upper()}* - `{price_formatted}`"
        if p.description:
            item_str += f"\n_{p.description}_"
        message_parts.append(item_str)
    footer = "\nÉ só me dizer o número ou o nome do que você mais gostou! 😉"
    return "\n\n".join(message_parts) + footer
    
def is_likely_shopping_intent(text: str) -> bool:
    """Verifica se o texto contém palavras-chave que indicam uma intenção de compra."""
    shopping_keywords = [
        "quero", "gostaria", "adiciona", "mais", "tira", "remove", 
        "muda", "troca", "quanto custa", "cardapio", "menu", "ver",
        "pedido", "carrinho", "esvaziar", "limpar", "cancelar",
        "tirar", "remover", "modificar", "tire", "remova"
    ]
    text_lower = text.lower()
    # Usamos \b para garantir que estamos pegando a palavra inteira (evita "quero" em "qualquer")
    return any(re.search(r'\b' + keyword + r'\b', text_lower) for keyword in shopping_keywords)