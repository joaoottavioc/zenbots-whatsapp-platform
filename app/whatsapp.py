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
from app.models import ProcessedMessage, Product, ShoppingCart, DeliveryMethod, Bot, OrderStatus
from app.openai_client import classify_user_intent, get_ai_decision, extract_potential_items
from app.prompt_central import create_central_prompt
from app.tools_definition import tools_schema
from datetime import datetime, timedelta, timezone
import regex as re
from sqlmodel import select
from app.semantic_router import semantic_intent, THRESHOLDS
from app.address_service import get_address_from_cep
from app.payment_service import create_pix_payment
import httpx #SIMULADOR
from arq import ArqRedis
from sqlalchemy.exc import IntegrityError
import mercadopago

# --- Imports Atualizados ---
# Helper para gerenciar o estado de "ação pendente"
from app.pending_action import save_pending, clear_pending, has_valid_pending, expire_if_needed
# Função padronizada para obter o tempo atual em UTC
from app.time import utcnow
import pytz
from app.rate_limiter import is_spamming
from app.broadcast import broadcast_order_update


load_dotenv()
router = APIRouter()


# Extrai "QTD + NOME" da pergunta de confirmação (ex.: "1 Gnocchis de la Mémé Forte, 2 X, ...")
_ITEM_FROM_Q_RE = re.compile(
    r"(\d{1,6})\s+([A-Za-zÀ-ÿ'´`^~\- ]+?)(?:\s+por\s*R\$\s*[\d.,]+|\s*(?:,| e |$))",
    re.IGNORECASE
)


async def process_whatsapp_message(ctx, data: Dict[str, Any]):
    async with async_session() as session:
        try:
            # 1. Extração segura dos dados
            # Garante que a estrutura básica existe antes de tentar acessar
            entry = data.get("entry", [])[0]
            changes = entry.get("changes", [])[0]
            value = changes.get("value", {})

            # --- CORREÇÃO 1: FILTRO DE MENSAGENS ---
            # Se não tiver 'messages', provavelmente é um status update. Ignoramos.
            if "messages" not in value:
                # Opcional: logar para debug se quiser ver os status
                # print(f"ℹ️ Status update recebido (ignorado).") 
                return
                
            message_data = value["messages"][0]
            contact_number = message_data["from"]
            
            # Usa .get() para 'text' pois pode ser mensagem de mídia/botão
            text_body = message_data.get("text", {}).get("body", "")
            message_id = message_data["id"]

            if is_spamming(contact_number, limit=20, window_seconds=60):
                print(f"🚫 RATE LIMIT: Bloqueando {contact_number} por excesso de mensagens.")
                # Retorna SILENCIOSAMENTE. Não responda ao spammer.
                return
            
            # NOVOS CAMPOS: Pegamos o ID do metadata para saber qual bot foi chamado
            incoming_phone_id = value["metadata"]["phone_number_id"]
            bot_display_phone = value["metadata"]["display_phone_number"]

            print(f"📥 Mensagem recebida. De: {contact_number} | Para ID: {incoming_phone_id}")

            # 2. BUSCA O BOT NO BANCO (Pelo ID do telefone ou fallback pelo número)
            # Tenta pelo ID exato da Meta (Mais seguro)
            result = await session.execute(select(Bot).where(Bot.phone_number_id == incoming_phone_id))
            bot = result.scalars().first()

            if not bot:
                # Fallback: Tenta pelo número visual se o ID não bater
                print(f"⚠️ Bot não achado por ID {incoming_phone_id}. Tentando número {bot_display_phone}...")
                bot = await crud.get_bot_by_number(session, bot_display_phone)
            
            if not bot:
                print("❌ Bot não encontrado para esta mensagem. Ignorando.")
                return
                
            current_token = bot.whatsapp_token
            current_phone_id = bot.phone_number_id

            # 3. Marcar como lida e processar duplicação (AGORA TEMOS AS CREDENCIAIS DO BOT)
            await mark_message_as_read(message_id, bot.whatsapp_token, bot.phone_number_id)
            
            if await crud.is_message_processed(session, message_id):
                print("⏩ Mensagem já processada anteriormente. Ignorando.")
                return
            try:
                await crud.add_processed_message(session, message_id)
            except IntegrityError:
                # Se der erro de chave duplicada, significa que outro worker foi mais rápido.
                # Apenas ignoramos e abortamos.
                print("⏩ Mensagem processada concorrentemente (Check 2). Ignorando.")
                await session.rollback() # Limpa o erro da sessão
                return

            if not is_store_open(bot):
                print(f"🔒 Loja fechada. Enviando mensagem enriquecida para {contact_number}.")

                # 1. Calcula quando volta
                next_opening = get_next_opening_text(bot)
                
                # 2. URL do Cardápio (Mesma lógica do Greeting)
                menu_url = "https://imagebucket1824.s3.us-east-1.amazonaws.com/JohnsHotDog.jpeg"

                # 3. Monta uma mensagem amigável
                # Usa a mensagem configurada no banco OU um padrão, + a info dinâmica
                base_msg = bot.closing_message or "No momento não estamos atendendo. 🌙"
                
                rich_closing_msg = (
                    f"{base_msg}\n\n"
                    f"⏰ *Voltamos {next_opening}*\n"
                    "---------------------------------\n"
                    "Enquanto isso, *confira nosso cardápio na imagem acima* e já vá escolhendo seu pedido. Será um prazer atendê-lo assim que possível! 👆😋"
                )

                
                # 5. Envia IMAGEM + TEXTO (Caption)
                # Assim o cliente não fica de mãos vazias
                await send_whatsapp_message(
                    to=contact_number, 
                    message=rich_closing_msg, 
                    token=bot.whatsapp_token, 
                    phone_id=bot.phone_number_id,
                    media_url=menu_url,
                    media_type="image"
                )
                
                # 6. Salva no histórico
                await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, rich_closing_msg)
                
                return # Encerra o processamento

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
                
                
                await send_whatsapp_message(contact_number, response_to_user, token=bot.whatsapp_token, phone_id=bot.phone_number_id)
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
                
            # INTERCEPTADOR DE BOAS-VINDAS COM IMAGEM
            if intent == "GREETING_OR_QUESTION" and cart.state == "GREETING":
                response_to_user = (
                    f"Olá! Bem-vindo ao *{bot.restaurant_name or 'nosso restaurante'}*! 🍕\n\n"
                    "👆 *Dê uma olhada no nosso cardápio na imagem acima!* 👆\n\n"
                    "Eu sou seu assistente virtual. Pode me dizer o que deseja pedir (escrevendo ou por áudio) que eu monto seu pedido!\n\n"
                    "Ex: _'Quero uma pizza de calabresa e uma coca'_"
                )
                menu_url = "https://imagebucket1824.s3.us-east-1.amazonaws.com/JohnsHotDog.jpeg" 
                

                await send_whatsapp_message(to=contact_number, message=response_to_user, token=bot.whatsapp_token, phone_id=bot.phone_number_id, media_url=menu_url, media_type="image")
                await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, "Enviou Cardápio (Imagem)")
                
                # ▼▼▼ CORREÇÃO 2: Atualização robusta do estado e tempo ▼▼▼
                cart.state = "SHOPPING"
                cart.last_activity_at = utcnow() # Atualiza o tempo para não expirar logo em seguida
                session.add(cart)
                await session.commit()
                print(f"✅ [GREETING] Estado do carrinho {cart.id} atualizado para SHOPPING.")
                return

            # A lógica de reset de estado continua a mesma, mas agora só será
            # acionada por intenções de compra genuínas.
            intents_that_resume_shopping = ["ADD", "REMOVE", "MODIFY", "REQUEST_SUGGESTION", "SHOW_CART", "CLEAR_CART", "ADD_ITEMS", "REMOVE_ITEMS"]
            finalizing_states = ["AWAITING_CEP", "AWAITING_NUMBER_COMPLEMENT", "AWAITING_ADDRESS_CONFIRMATION", "AWAITING_CUSTOMER_NAME"]

            if intent in intents_that_resume_shopping and cart.state in finalizing_states:
                print(f"🔄 Cliente voltou a comprar (Intent: {intent}). Resetando estado de '{cart.state}' para 'GREETING'.")
                cart.state = "GREETING"
                await session.flush()
            
            # Etapa 1: Escolha entre entrega e retirada
            if cart.state == "AWAITING_DELIVERY_METHOD":
                user_text = text_body.lower().strip()
                
                # Se o cliente escolher ENTREGA
                if "entrega" in user_text or user_text == "1":
                    cart.delivery_method = DeliveryMethod.DELIVERY
                    cart.state = "AWAITING_CEP" # Progride para o fluxo de endereço
                    response_to_user = "Ótimo, faremos a entrega! Para começar, por favor, me informe o seu CEP."
                
                # Se o cliente escolher RETIRADA
                elif "retirada" in user_text or "buscar" in user_text or user_text == "2":
                    cart.delivery_method = DeliveryMethod.PICKUP
                    # Pula o fluxo de endereço e vai direto para a coleta do nome
                    await session.refresh(cart, attribute_names=["contact"])
                    if cart.contact and cart.contact.name:
                        cart.state = "AWAITING_PAYMENT_METHOD"
                        
                        # 1. Gerar o resumo ANTES de perguntar o pagamento
                        final_summary = _build_cart_summary_message(cart, bot, "🛍️") # Emoji de sacola para retirada

                        # 2. Montar a resposta completa
                        response_to_user = (
                            f"Combinado, pedido para retirada no nome de *{cart.contact.name}*! ✅\n\n"
                            f"{final_summary}\n\n" # <-- Resumo incluído aqui
                            "Qual será a forma de pagamento? (PIX ou Cartão)"
                        )
                    else:
                        cart.state = "AWAITING_CUSTOMER_NAME"
                        response_to_user = "Combinado, pedido para retirada! Para registrar, por favor, me informe o seu nome completo."
                
                # Se a resposta for inválida
                else:
                    response_to_user = "Não entendi. Por favor, escolha entre *Entrega* (1) ou *Retirada* (2)."
                
                cart.last_activity_at = utcnow()
                session.add(cart)

                await send_whatsapp_message(contact_number, response_to_user, token=bot.whatsapp_token, phone_id=bot.phone_number_id)
                await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
                await session.commit()
                return
                
            # Etapa 2: Aguardando o CEP do cliente
            elif cart.state == "AWAITING_CEP":
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
                
                
                await send_whatsapp_message(contact_number, response_to_user, token=bot.whatsapp_token, phone_id=bot.phone_number_id)
                await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
                await session.commit()
                return

            # Etapa 3: Aguardando número/complemento E PEDINDO CONFIRMAÇÃO
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
               
                
                await send_whatsapp_message(contact_number, response_to_user, token=bot.whatsapp_token, phone_id=bot.phone_number_id)
                await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
                await session.commit()
                return

            # Etapa 4: Aguardando a confirmação final do endereço
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
                
                
                await send_whatsapp_message(contact_number, response_to_user, token=bot.whatsapp_token, phone_id=bot.phone_number_id)
                await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
                await session.commit()
                return
             
            # Etapa 5: Aguardando o nome do cliente
            elif cart.state == "AWAITING_CUSTOMER_NAME":
                customer_name = text_body.strip()
            
                # Salva o nome no contato associado ao carrinho
                await crud.save_customer_name_to_contact(session, cart.contact_id, customer_name)
            
                cart.state = "AWAITING_PAYMENT_METHOD"
                
                # --- LÓGICA DE MENSAGEM APRIMORADA ---
                # 1. Gera o resumo final e completo do pedido, já com a taxa de entrega.
                final_summary = _build_cart_summary_message(cart, bot, "📦")

                # 2. Monta a mensagem final com o título e os detalhes
                response_to_user = (
                    f"Ótimo, {customer_name.split(' ')[0]}! Seu pedido foi confirmado. ✅\n\n"
                    "----- *Detalhes do Pedido* -----\n"
                    f"{final_summary}\n"
                    "--------------------------------\n\n"
                    "Para finalizar, qual será a forma de pagamento? (PIX ou Cartão)"
                )
                # --- FIM DA LÓGICA APRIMORADA ---

                cart.last_activity_at = utcnow()
                session.add(cart)
            
            
                await send_whatsapp_message(contact_number, response_to_user, token=bot.whatsapp_token, phone_id=bot.phone_number_id)
                await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
                await session.commit()
                return
                
            elif cart.state == "AWAITING_PAYMENT_METHOD":
                user_text = text_body.lower()
                order_created = False # Flag para saber se devemos limpar o carrinho
                pix_code_to_send = None

                # Ação principal: criar o pedido no banco de dados ANTES de processar o pagamento
                if "pix" in user_text or "cartão" in user_text or "cartao" in user_text:
                    await session.refresh(cart, attribute_names=["items", "contact"])
                    if not cart.contact:
                        raise Exception(f"Carrinho {cart.id} não possui um contato associado.")

                    bot_id = cart.contact.bot_id
                    items_for_order = [{"product_id": item.product_id, "quantity": item.quantity, "notes": item.notes} for item in cart.items]
                    
                    # Inclui a taxa de entrega no total do pedido ANTES de salvar
                    total_amount = sum(item.product.price * item.quantity for item in cart.items)
                    if cart.delivery_method == DeliveryMethod.DELIVERY and bot.delivery_fee > 0:
                        total_amount += bot.delivery_fee

                    order = await crud.create_order(
                        session,
                        bot_id=bot_id,
                        items=items_for_order,
                        customer_address=cart.customer_address,
                        total_amount=total_amount,
                        contact_id=contact.id                        # Passa o total já calculado
                    )
                else:
                    order = None

                # Agora, com o pedido criado, montamos a resposta específica
                if "pix" in user_text:
                    if order:
                        # ▼▼▼ NOVA LÓGICA MULTI-CONTA ▼▼▼
                        # 1. Garante que temos a configuração de pagamento do bot carregada
                        await session.refresh(bot, attribute_names=["payment_config"])
                        
                        client_token = None
                        if bot.payment_config and bot.payment_config.is_active:
                            client_token = bot.payment_config.access_token

                        if not client_token:
                            print(f"❌ Erro: Bot {bot.id} não tem token MP configurado.")
                            response_to_user = "Desculpe, o pagamento via PIX está temporariamente indisponível neste restaurante. Tente cartão."
                            await crud.delete_order(session, order.id) # Reverte
                        else:
                            # 2. Chama o serviço passando o token DO CLIENTE
                            pix_info = await create_pix_payment(
                                order_id=order.id,
                                total_amount=order.total_amount,
                                bot_name=bot.restaurant_name,
                                contact_phone=contact.phone_number,
                                access_token_cliente=client_token # <--- Passando o token
                            )

                            if pix_info:
                                response_to_user = (
                                    "Seu pedido foi registrado! ✅\n\n"
                                    "Use o PIX Copia e Cola acima para fazer o pagamento em até 15 minutos. Enviaremos uma confirmação assim que for aprovado.\n\n"
                                )
                                pix_code_to_send = pix_info['pix_copy_paste']
                                order_created = True
                            else:
                                response_to_user = "Tivemos um problema técnico ao gerar o PIX. Por favor, tente pagar com Cartão."
                                await crud.delete_order(session, order.id)
                        # ▲▲▲ FIM DA NOVA LÓGICA ▲▲▲
                    else:
                        response_to_user = "Não entendi. Por favor, escolha entre *PIX* ou *Cartão*."

                elif "cartão" in user_text or "cartao" in user_text:
                    if order:
                        order_created = True
                        if cart.delivery_method == DeliveryMethod.DELIVERY:
                            response_to_user = (
                                "Combinado! Seu pedido foi registrado. ✅\n\n"
                                "Nosso entregador levará a maquininha de cartão até você.\n\n"
                                "Muito obrigado pela sua preferência!"
                            )
                        else:
                            response_to_user = (
                                "Combinado! Seu pedido foi registrado. ✅\n\n"
                                "O pagamento com cartão será feito no balcão ao retirar o pedido.\n\n"
                                "Muito obrigado pela sua preferência!"
                            )
                    else:
                        response_to_user = "Não entendi. Por favor, escolha entre *PIX* ou *Cartão*."
                
                else:
                    response_to_user = "Não entendi. Por favor, escolha entre *PIX* ou *Cartão*."

                # Limpa o carrinho APENAS se o pedido foi criado e o pagamento iniciado com sucesso
                if order_created:
                    await crud.clear_db_cart(session, cart.id)
                    
                    # ▼▼▼ INÍCIO DA ADIÇÃO (BROADCAST) ▼▼▼
                    # Avisa o Dashboard que tem pedido novo na área!
                    try:
                        # Monta um payload resumido e útil para o front
                        display_items = [
                            f"{item.quantity}x {item.product.name}" 
                            for item in cart.items
                        ]
                        
                        await broadcast_order_update("new_order", {
                            "order_id": order.id,
                            "customer_name": cart.contact.name,
                            "customer_phone": contact_number,
                            "total": order.total_amount,
                            "status": "PENDING", # ou o status inicial do seu modelo
                            "items": display_items,
                            "created_at": str(utcnow())
                        })
                    except Exception as e:
                        print(f"Erro ao enviar broadcast: {e}")
                    # ▲▲▲ FIM DA ADIÇÃO ▲▲▲

                cart.last_activity_at = utcnow()
                session.add(cart)
                
                
                # Envia a Mensagem 2 (O CÓDIGO) se ela existir
                if pix_code_to_send:
                    await send_whatsapp_message(contact_number, pix_code_to_send, token=bot.whatsapp_token, phone_id=bot.phone_number_id)
                
                
                await send_whatsapp_message(contact_number, response_to_user, token=bot.whatsapp_token, phone_id=bot.phone_number_id)
                await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
                await session.commit()
                return
            # ▲▲▲ FIM DO BLOCO ADICIONADO ▲▲▲

            if intent in ("CONFIRM", "NEGATE"):
                # Primeiro, checa se há uma ação pendente (ex: "Confirma adicionar X item?").
                # Esta lógica tem prioridade máxima.
                if has_valid_pending(cart):
                    if intent == "CONFIRM":
                        response_to_user = await _execute_pending_action(session, cart, bot=bot)
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
                
                
                await send_whatsapp_message(contact_number, response_to_user, token=bot.whatsapp_token, phone_id=bot.phone_number_id)
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
                response_to_user = _build_cart_summary_message(cart, bot)
                
            elif intent == "FINISH_ORDER":
                # Garante que temos os dados mais recentes do carrinho e contato
                await session.refresh(cart, attribute_names=["items", "contact"])
                
                if not cart.items:
                    response_to_user = "Seu carrinho está vazio. O que você gostaria de pedir?"
                    
                # ▼▼▼ 2. VALIDAÇÃO DE PEDIDO MÍNIMO (NOVA) ▼▼▼
                current_total = sum(item.product.price * item.quantity for item in cart.items)
                
                # Verifica se existe valor mínimo configurado (> 0) e se o total é menor que ele
                if bot.min_order_value and bot.min_order_value > 0 and current_total < bot.min_order_value:
                    missing = bot.min_order_value - current_total
                    response_to_user = (
                        f"⚠️ *Pedido Mínimo não atingido*\n\n"
                        f"O valor mínimo para pedidos é *R$ {bot.min_order_value:.2f}*.\n"
                        f"Seu carrinho está em R$ {current_total:.2f}.\n\n"
                        f"Faltam apenas *R$ {missing:.2f}*! Que tal adicionar uma bebida ou sobremesa? 🥤🍫"
                    )
                    

                    await send_whatsapp_message(contact_number, response_to_user, token=bot.whatsapp_token, phone_id=bot.phone_number_id)
                    await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
                    await session.commit()
                    return
                
                # NOVO PASSO: Se o método de entrega ainda não foi escolhido, esta é a primeira pergunta.
                elif cart.delivery_method is None:
                    cart.state = "AWAITING_DELIVERY_METHOD"
                    
                    # 1. Define a linha de "Entrega", sempre mostrando o valor
                    entrega_line = f"1️⃣  *Entrega* 🛵 (R$ {bot.delivery_fee:.2f})"

                    # 2. Define a linha de "Retirada", sempre mostrando R$ 0.00
                    retirada_line = "2️⃣  *Retirada no local* 🛍️ (R$ 0.00)"

                    # 3. Monta a mensagem completa
                    response_to_user = (
                        "Entendido. Para finalizar, seu pedido será para:\n\n"
                        f"{entrega_line}\n"
                        f"{retirada_line}\n\n"
                        "Por favor, responda com o número ou a palavra."
                    )
                
                # Se o método de entrega já foi definido, o fluxo inteligente anterior continua
                elif cart.customer_address and cart.contact and cart.contact.name:
                    print(f"✅ Endereço e nome já salvos. Pulando para pagamento.")
                    cart.state = "AWAITING_PAYMENT_METHOD"
                    response_to_user = (
                        f"Notei que já temos seu endereço salvo:\n\n"
                        f"🏠 *{cart.customer_address}*\n\n"
                        f"E o pedido está no nome de *{cart.contact.name}*.\n\n"
                        f"Qual será a forma de pagamento? (PIX ou Cartão)"
                    )
                elif cart.customer_address:
                    print(f"✅ Endereço salvo, mas nome não encontrado. Solicitando nome.")
                    cart.state = "AWAITING_CUSTOMER_NAME"
                    response_to_user = (
                        "Ok, vamos continuar. Já tenho seu endereço. "
                        "Agora, por favor, me informe o nome completo para a entrega."
                    )
                elif cart.pending_address:
                    print(f"▶️ Retomando fluxo: Aguardando confirmação do endereço.")
                    cart.state = "AWAITING_ADDRESS_CONFIRMATION"
                    response_to_user = (
                        f"Por favor, confirme se o endereço final está correto:\n\n"
                        f"🏠 *{cart.pending_address}*\n\n"
                        f"Posso confirmar? (Sim / Não)"
                    )
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
                else:
                    # Este 'else' agora só é atingido se o método de entrega for DELIVERY, mas nenhum endereço foi coletado
                    cart.state = "AWAITING_CEP"
                    response_to_user = "Ótimo, faremos a entrega! Para começar, por favor, me informe o seu CEP."
                    
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
                                    
                                    # ▼▼▼ MUDANÇA AQUI ▼▼▼
                                    response_to_user = (
                                        _build_cart_summary_message(cart, bot, "✅") + 
                                        "\n\nAdicionado! Se quiser incluir alguma observação (ex: 'sem salada'), é só falar agora. Ou deseja algo mais?"
                                    )
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
                                    response_to_user = _build_cart_summary_message(cart, bot, "❌") + "\n\nAlgo mais?"

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
                                    response_to_user = _build_cart_summary_message(cart, bot, "✏️") + "\n\nAlgo mais?"
                            
                            elif tool_name == "update_item_observation":
                                pid = tool_args.get("product_id")
                                notes = tool_args.get("notes")
                            
                                if pid and notes:
                                    # Chama a nova função do CRUD
                                    await crud.update_item_notes(session, cart.id, int(pid), str(notes))
                                    await session.refresh(cart, attribute_names=['items'])
                                    response_to_user = _build_cart_summary_message(cart, bot, "✏️") + "\n\nObservação anotada! Mais alguma coisa?"
                                else:
                                    response_to_user = "Não entendi qual item você quer alterar. Pode repetir?"

                            else:  
                                    # bulk_modify_quantities
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
                                        response_to_user = _build_cart_summary_message(cart, bot, "✏️") + "\n\nAlgo mais?"    

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
                            
                            
                            await send_whatsapp_message(contact_number, response_to_user, token=bot.whatsapp_token, phone_id=bot.phone_number_id)
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
            
            
            await send_whatsapp_message(contact_number, response_to_user, token=bot.whatsapp_token, phone_id=bot.phone_number_id)
            await crud.add_interaction_to_history(session, bot.id, contact_number, text_body, response_to_user)
            await session.commit()
            print("✅ Mensagem processada e resposta enviada.")

        except Exception as e:
            print(f"❌ Erro crítico: {e}")
            
            # Tenta reverter o banco para não deixar travado
            if 'session' in locals() and session.is_active:
                try:
                    await session.rollback()
                except: pass
            
            # ▼▼▼ LÓGICA SEGURA DE ENVIO DE ERRO ▼▼▼
            # Verifica se:
            # 1. Temos o número do cliente ('contact_number' existe)
            # 2. Temos o token salvo na memória ('current_token' existe)
            if 'contact_number' in locals() and 'current_token' in locals() and current_token:
                 try:
                     await send_whatsapp_message(
                         to=contact_number, 
                         message="Desculpe, tive um erro técnico momentâneo. Tente novamente em instantes. 🔧", 
                         token=current_token,      # Usa a variável local (segura)
                         phone_id=current_phone_id # Usa a variável local (segura)
                     )
                 except Exception as send_err:
                     # Se falhar aqui (ex: erro de rede), apenas loga e não quebra o app
                     print(f"Não foi possível enviar mensagem de erro ao usuário: {send_err}")


def _build_cart_summary_message(cart: ShoppingCart, bot: Bot, emoji: str = "🛒") -> str:
    if not cart.items:
        return "🗑️ *Seu carrinho agora está vazio.*"
        
    cart_summary_lines = []
    subtotal = 0.0
    for item in cart.items:
        line_total = item.product.price * item.quantity
        subtotal += line_total

        # Formata a linha do item
        item_line = f"- {item.quantity}x {item.product.name} (R$ {line_total:.2f})"
        
        # Se tiver observação, adiciona na linha de baixo
        if item.notes:
            item_line += f"\n  _Obs: {item.notes}_"
            
        # Adiciona à lista APENAS UMA VEZ
        cart_summary_lines.append(item_line)

    summary_text = f"{emoji} *Seu Pedido Atual:*\n" + "\n".join(cart_summary_lines)
    
    total_amount = subtotal
    
    # Verifica se o método é entrega E se a taxa é maior que zero
    if cart.delivery_method == DeliveryMethod.DELIVERY and bot.delivery_fee > 0:
        total_amount += bot.delivery_fee
        summary_text += f"\n\nTaxa de Entrega: R$ {bot.delivery_fee:.2f}"

    summary_text += f"\n\nTotal: *R$ {total_amount:.2f}*"
    return summary_text

@router.get("/webhook")
async def verify_webhook(request: Request):
    params = dict(request.query_params)
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == os.getenv("META_VERIFY_TOKEN"):
        return PlainTextResponse(content=params.get("hub.challenge"))
    return PlainTextResponse(content="Invalid verification", status_code=403)

async def send_whatsapp_message(
    to: str, 
    message: str, 
    token: str, 
    phone_id: str, 
    media_url: str = None, 
    media_type: str = "image"  # Pode ser "image" ou "document" (para PDF)
):
    url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}", 
        "Content-Type": "application/json"
    }
    
    # Lógica para decidir se manda Texto Puro ou Mídia
    if media_url:
        data = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": media_type,
            media_type: {
                "link": media_url,
                "caption": message  # O texto vai junto com a imagem
            }
        }
        # Se for documento, podemos adicionar um nome de arquivo bonito
        if media_type == "document":
            data["document"]["filename"] = "Cardapio_Restaurante.pdf"
    else:
        data = {
            "messaging_product": "whatsapp",
            "to": to,
            "text": {"body": message}
        }
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, headers=headers, json=data)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            print(f"❌ Erro ao enviar mensagem: {e.response.text}")

async def mark_message_as_read(message_id: str, token: str, phone_id: str):
    url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, headers=headers, json=data)
            response.raise_for_status()
            print(f"Mensagem {message_id} marcada como lida.")
        except httpx.HTTPStatusError as e:
            print(f"Erro ao marcar mensagem como lida: {e.response.text}")

async def _execute_pending_action(session: AsyncSession, cart: ShoppingCart, bot: Bot) -> str:
    tool = cart.pending_action_tool
    args = cart.pending_action_args or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
            
    print(f"EXECUTANDO AÇÃO PENDENTE -> Ferramenta: {tool}, Argumentos: {args}")

    # Obtém o bot_id a partir do objeto bot para usar na lógica existente
    bot_id = bot.id
    
    if not tool:
        clear_pending(cart)
        return "Não encontrei nenhuma ação para confirmar. Quer continuar seu pedido?"

    if tool == "add_items_to_cart":
        items_to_add = args.get("items", [])
        if not items_to_add:
            rebuilt = await _items_from_confirmation_question(session, bot_id, cart.pending_action_question)
            if rebuilt:
                items_to_add = rebuilt
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
        # --- CORREÇÃO APLICADA ---
        return _build_cart_summary_message(cart, bot, "✅") + "\n\nAlgo mais?"

    if tool == "modify_item_quantity":
        pid, newq = args.get("product_id"), args.get("new_quantity")
        if pid is None or newq is None:
            clear_pending(cart)
            return "A proposta para modificar o item estava incompleta. Pode repetir?"
            
        await crud.modify_item_quantity_in_db_cart(session, cart.id, int(pid), int(newq))
        await session.flush()
        await session.refresh(cart, attribute_names=['items'])
        clear_pending(cart)
        # --- CORREÇÃO APLICADA ---
        return _build_cart_summary_message(cart, bot, "✏️") + "\n\nAlgo mais?"

    if tool == "remove_items_from_cart":
        ids = args.get("product_ids", [])
        for pid in ids:
            await crud.modify_item_quantity_in_db_cart(session, cart.id, int(pid), 0)
        await session.flush()
        await session.refresh(cart, attribute_names=['items'])
        clear_pending(cart)
        # --- CORREÇÃO APLICADA ---
        return _build_cart_summary_message(cart, bot, "❌") + "\n\nAlgo mais?"

    if tool == "answer_with_found_products":
        names = args.get("product_names", [])
        text = "Essas são algumas sugestões para você:\n" + "\n".join(f"{i+1}. {n}" for i, n in enumerate(names)) if names else "Posso sugerir algumas opções, se quiser."
        if names:
            res = await session.execute(select(Product).where(Product.bot_id == bot_id, Product.name.in_(names)))
            prods = res.scalars().all()
            if prods:
                cart.last_suggestions = [p.id for p in prods]
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

@router.post("/webhooks/payment-confirm/{order_id}")
async def handle_payment_notification(order_id: int, request: Request):
    """
    Webhook dinâmico:
    1. Recebe o ID do pedido na URL.
    2. Busca o pedido no banco para descobrir quem é o BOT dono.
    3. Usa o Token desse BOT para consultar o Mercado Pago.
    """
    async with async_session() as session:
        data = await request.json()
        print(f"🔔 Webhook recebido para Pedido #{order_id}: {data}")

        try:
            # 1. Busca o Pedido e o Bot Dono
            # Precisamos carregar o bot e a config de pagamento
            from app.models import Order 
            result = await session.execute(select(Order).where(Order.id == order_id))
            order = result.scalars().first()

            if not order:
                print(f"❌ Pedido {order_id} não encontrado no banco.")
                return JSONResponse(content={"status": "order_not_found"}, status_code=404)

            # Carrega o Bot e a Configuração
            await session.refresh(order, attribute_names=["bot"])
            await session.refresh(order.bot, attribute_names=["payment_config"])

            if not order.bot.payment_config or not order.bot.payment_config.access_token:
                print(f"❌ Bot do pedido {order_id} não tem token configurado.")
                return JSONResponse(content={"status": "no_token"}, status_code=200)

            # 2. Inicializa o SDK com o token DO CLIENTE (Dono do Bot)
            specific_sdk = mercadopago.SDK(order.bot.payment_config.access_token)

            # 3. Processa a notificação (Igual antes, mas usando specific_sdk)
            notification_type = data.get("type") or data.get("topic") # MP as vezes manda 'topic'
            payment_id_str = data.get("data", {}).get("id")

            if notification_type != "payment" or not payment_id_str:
                return JSONResponse(content={"status": "ignored"}, status_code=200)

            # Consulta o MP
            payment_info = specific_sdk.payment().get(payment_id_str)
            
            if payment_info["status"] != 200:
                print(f"❌ Erro ao consultar MP: {payment_info}")
                return JSONResponse(content={"status": "mp_error"}, status_code=200)
            
            payment_data = payment_info["response"]
            payment_status = payment_data.get("status")

            # Mapeia Status
            db_status = None
            if payment_status == "approved":
                db_status = OrderStatus.PAID
            elif payment_status in ("rejected", "cancelled"):
                db_status = OrderStatus.FAILED
            
            if db_status:
                await crud.update_order_status_by_id(session, order_id, db_status, str(payment_id_str))
                
                # Notifica no WhatsApp se aprovado
                if db_status == OrderStatus.PAID:
                    msg = f"Pagamento APROVADO! ✅\n\nSeu pedido #{order_id} foi confirmado e já vai para a cozinha."
                    # Usa as credenciais do bot para enviar a mensagem
                    await send_whatsapp_message(
                        to=payment_data["payer"]["email"].split('@')[0], # Pegamos o fone do email fake que geramos
                        message=msg,
                        token=order.bot.whatsapp_token,
                        phone_id=order.bot.phone_number_id
                    )

            return JSONResponse(content={"status": "ok"}, status_code=200)

        except Exception as e:
            print(f"❌ Erro no Webhook: {e}")
            return JSONResponse(content={"status": "error"}, status_code=500)

def is_store_open(bot: Bot) -> bool:
    """
    Verifica se a loja está aberta baseada na configuração manual E no horário.
    Prioridade:
    1. Se o botão manual (is_open) for False -> FECHADO (Férias/Emergência).
    2. Se manual for True -> Verifica o horário agendado (schedule).
    """
    # 1. Bloqueio Manual (O "Disjuntor")
    if not bot.is_open:
        return False

    # Se não tiver horário configurado, assumimos que segue apenas o botão manual (Aberto)
    if not bot.schedule:
        return True

    try:
        # 2. Obtém a hora atual no fuso do restaurante
        tz = pytz.timezone(bot.timezone)
        now = datetime.now(tz)
        
        # Mapeia dia da semana (0=Segunda, 6=Domingo) para nossas chaves
        weekdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        today_key = weekdays[now.weekday()]
        
        day_config = bot.schedule.get(today_key)

        # Se não tem config para hoje ou o dia está inativo
        if not day_config or not day_config.get("active", False):
            return False # Fechado neste dia

        start_time = day_config.get("start", "00:00")
        end_time = day_config.get("end", "23:59")

        # Converte strings "HH:MM" para objetos comparáveis
        current_time_str = now.strftime("%H:%M")
        
        # Lógica simples de comparação de strings (funciona bem para formato 24h)
        # Se passar da meia-noite (ex: 18:00 as 02:00), a lógica precisaria ser mais complexa.
        # Para o MVP, assumimos que abre e fecha no mesmo dia operacional.
        if start_time <= current_time_str <= end_time:
            return True
        else:
            return False

    except Exception as e:
        print(f"Erro ao calcular horário: {e}. Assumindo aberto.")
        return True

def get_next_opening_text(bot: Bot) -> str:
    """
    Calcula o próximo horário de abertura baseado no schedule do bot.
    Retorna algo como: "Amanhã às 18:00" ou "Segunda às 10:00".
    """
    if not bot.schedule:
        return "em breve"

    try:
        tz = pytz.timezone(bot.timezone)
        now = datetime.now(tz)
        weekdays_map = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        weekdays_pt = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
        
        current_day_idx = now.weekday()
        
        # Procura nos próximos 7 dias
        for i in range(1, 8):
            next_day_idx = (current_day_idx + i) % 7
            day_key = weekdays_map[next_day_idx]
            
            day_config = bot.schedule.get(day_key)
            
            if day_config and day_config.get("active"):
                start_time = day_config.get("start", "00:00")
                
                # Se for amanhã
                if i == 1:
                    return f"Amanhã às {start_time}"
                # Se for hoje (caso raro de janelas multiplas, mas simplificamos aqui)
                elif i == 0: 
                    return f"Hoje às {start_time}"
                else:
                    return f"{weekdays_pt[next_day_idx]} às {start_time}"
                    
        return "em breve"
    except Exception:
        return "em breve"