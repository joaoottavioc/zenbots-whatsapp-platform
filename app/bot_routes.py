from fastapi import APIRouter, Depends, HTTPException, Response, status, Request, Query
from fastapi.responses import RedirectResponse
from sqlmodel.ext.asyncio.session import AsyncSession
from typing import List, Dict, Any

# --- Importações Consolidadas ---
from app import crud, schemas, data_extractor
from app.database import get_session
from app.models import User
from app.auth import get_current_user
from app.schemas import WhatsAppAuthRequest, EmbeddedSignupPayload, ForgotPasswordRequest
from app.auth import get_current_user
import re
import logging

from fastapi import UploadFile, File, Form
from app.openai_client import extract_products_from_image
import fitz  # PyMuPDF (Necessário para ler PDFs)

import os
import httpx
from pydantic import BaseModel
from urllib.parse import urlparse
from arq import ArqRedis
import asyncio
from app.menu_storage import upload_bytes_to_s3
from app.whatsapp import send_whatsapp_message

logger = logging.getLogger(__name__)

router = APIRouter()
WEBHOOK_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN")

MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB

# --- Rotas para Gerenciamento de Bots ---

@router.post("/bots", response_model=schemas.BotResponse, status_code=201)
async def create_new_bot(
    bot_data: schemas.BotCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Cria um novo bot 'casca' para o usuário logado."""

    bot_created = await crud.create_bot(
        session=session,
        user_id=current_user.id,
        whatsapp_number=bot_data.whatsapp_number,
        restaurant_name=bot_data.restaurant_name,
        pix_key=bot_data.pix_key,
        delivery_fee=bot_data.delivery_fee,
        min_order_value=bot_data.min_order_value,
        whatsapp_token=bot_data.whatsapp_token,
        phone_number_id=bot_data.phone_number_id
    )

    if not bot_created:
        raise HTTPException(status_code=400, detail="Um bot com este número de WhatsApp já existe.")

    return bot_created

@router.get("/bots", response_model=List[schemas.BotResponse])
async def get_user_bots(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Lista todos os bots pertencentes ao usuário logado."""
    bots = await crud.list_user_bots(session, user_id=current_user.id)
    return bots


@router.put("/bots/{bot_id}", response_model=schemas.BotResponse)
async def update_user_bot(
    bot_id: int,
    bot_update_data: schemas.BotUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """
    Atualiza os dados de configuração de um bot (nome, número, chave pix).
    """
    # 1. Busca o bot no banco
    db_bot = await crud.get_bot_by_id(session, bot_id=bot_id)

    # 2. Verifica se o bot existe e se pertence ao usuário logado
    if not db_bot:
        raise HTTPException(status_code=404, detail="Bot não encontrado.")
    if db_bot.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    # ▼▼▼ CORREÇÃO APLICADA AQUI ▼▼▼
    # 3. Chama a função do CRUD passando 'bot_id' em vez de 'db_bot'
    updated_bot = await crud.update_bot(
        session=session,
        bot_id=bot_id,  # Passa o ID que a função espera
        update_data=bot_update_data
    )
    # ▲▲▲ FIM DA CORREÇÃO ▲▲▲

    return updated_bot

# --- Rotas para Gerenciamento de Catálogo/Produtos ---

@router.post("/bots/{bot_id}/products", response_model=schemas.ProductResponse, status_code=201)
async def create_product_for_bot(
    bot_id: int,
    product_data: schemas.ProductCreate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """Cria um novo produto (item de cardápio) para um bot específico."""
    db_bot = await crud.get_bot_by_id(session, bot_id=bot_id)
    if not db_bot or db_bot.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    new_product = await crud.create_product(
        session=session,
        bot_id=bot_id,
        name=product_data.name,
        description=product_data.description,
        price=product_data.price,
        # ▼▼▼ CORREÇÃO: ADICIONE ESTA LINHA ▼▼▼
        category=product_data.category # Agora passamos a categoria recebida!
    )

    return new_product


# No seu arquivo de rotas (ex: app/bot_routes.py)

# Em app/bot_routes.py

@router.post("/bots/{bot_id}/catalog/upload", status_code=201)
async def upload_catalog_from_text(
    bot_id: int,
    request_data: schemas.CatalogUploadRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
) -> Dict[str, Any]:
    """Recebe um texto de cardápio, extrai os produtos e salva em lote."""
    db_bot = await crud.get_bot_by_id(session, bot_id=bot_id)
    if not db_bot or db_bot.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    # 1. Extrai os produtos do texto usando o LLM (como antes)
    extracted_products = await data_extractor.extract_products_from_text(request_data.catalog_text)

    if not extracted_products:
        raise HTTPException(status_code=400, detail="Não foi possível extrair itens do cardápio.")

    # ▼▼▼ PASSO NOVO E CRUCIAL: ENRIQUECER OS DADOS EXTRAÍDOS ▼▼▼

    enriched_products = []
    for product in extracted_products:
        # Pega o nome e a descrição para criar keywords automáticas
        name_words = product.get("name", "").lower()
        desc_words = product.get("description", "").lower()

        # Combina, remove caracteres especiais e cria uma lista de palavras únicas
        full_text = name_words + " " + desc_words
        words = set(re.findall(r'\b\w+\b', full_text)) # Extrai palavras

        # Adiciona a nova chave "keywords" ao dicionário do produto
        product["keywords"] = list(words)
        enriched_products.append(product)

    # ▲▲▲ FIM DO PASSO DE ENRIQUECIMENTO ▲▲▲

    # 2. Chama a função de CRUD com os dados agora enriquecidos
    products_added_count = await crud.bulk_create_products(
        session=session,
        bot_id=bot_id,
        products_data=enriched_products  # 👈 Usa a lista enriquecida
    )

    return {"message": f"{products_added_count} produtos adicionados com sucesso ao bot {bot_id}."}

@router.delete("/bots/{bot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user_bot(
    bot_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """Exclui um bot, garantindo que apenas o seu dono possa fazê-lo."""
    db_bot = await crud.get_bot_by_id(session, bot_id=bot_id)

    if not db_bot:
        # Retorna 204 mesmo se não encontrar, pois o resultado final (o bot
        # não existir) é o mesmo. É uma prática comum em DELETEs.
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # Verificação de segurança crucial
    if db_bot.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    await crud.delete_bot(session, db_bot=db_bot)

    # Respostas 204 (No Content) não devem ter corpo
    return Response(status_code=status.HTTP_204_NO_CONTENT)

@router.get("/bots/{bot_id}/products", response_model=List[schemas.ProductResponse])
async def list_products_for_bot(
    bot_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """Lista todos os produtos de um bot específico, verificando a permissão do usuário."""
    db_bot = await crud.get_bot_by_id(session, bot_id=bot_id)
    if not db_bot or db_bot.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    products = await crud.get_products_by_bot_id(session, bot_id=bot_id, limit=limit, offset=offset)
    return products

@router.put("/bots/{bot_id}/products/{product_id}", response_model=schemas.ProductResponse)
async def update_product_endpoint(
    bot_id: int,
    product_id: int,
    product_data: schemas.ProductUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """Atualiza um produto, verificando a propriedade do bot e do produto."""

    # 1. Verifica se o bot pertence ao usuário
    db_bot = await crud.get_bot_by_id(session, bot_id=bot_id)
    if not db_bot or db_bot.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Acesso negado ao bot.")

    # 2. Busca o produto
    db_product = await crud.get_product_by_id(session, product_id=product_id)

    # 3. Verifica se o produto existe e se pertence ao bot da URL
    if not db_product:
        raise HTTPException(status_code=404, detail="Produto não encontrado.")
    if db_product.bot_id != bot_id:
        raise HTTPException(status_code=403, detail="Produto não pertence a este bot.")

    # 4. Chama a função CRUD (agora corrigida)
    return await crud.update_product(session, db_product=db_product, update_data=product_data)

# No seu arquivo de rotas (ex: app/bot_routes.py)

@router.delete("/bots/{bot_id}/products/{product_id}", status_code=204)
async def delete_product_endpoint(
    bot_id: int,
    product_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """Exclui um produto, verificando a propriedade do bot e do produto."""

    # 1. Verifica se o bot pertence ao usuário
    db_bot = await crud.get_bot_by_id(session, bot_id=bot_id)
    if not db_bot or db_bot.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Acesso negado ao bot.")

    # 2. Busca o produto
    db_product = await crud.get_product_by_id(session, product_id=product_id)

    # 3. Verifica se o produto existe e se pertence ao bot da URL
    if not db_product:
        raise HTTPException(status_code=404, detail="Produto não encontrado.")
    if db_product.bot_id != bot_id:
        raise HTTPException(status_code=403, detail="Produto não pertence a este bot.")

    await crud.delete_product(session, db_product=db_product)
    return

# No seu arquivo de rotas (ex: app/bot_routes.py)

@router.post("/bots/{bot_id}/products/bulk-delete")
async def bulk_delete_products_endpoint(
    bot_id: int, # <-- 1. Adiciona o bot_id
    delete_data: schemas.ProductBulkDeleteRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """Exclui uma lista de produtos de um bot específico em uma única operação."""

    # 2. Adiciona a verificação de posse do bot ( crucial para segurança)
    db_bot = await crud.get_bot_by_id(session, bot_id=bot_id)
    if not db_bot or db_bot.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Acesso negado ao bot.")

    # 3. Chama a nova função CRUD simplificada
    deleted_count = await crud.bulk_delete_products(
        session=session,
        bot_id=bot_id, # <-- Passa o bot_id validado
        product_ids=delete_data.product_ids
    )

    if deleted_count == 0 and len(delete_data.product_ids) > 0:
        # A mensagem de erro agora é mais específica
        raise HTTPException(status_code=403, detail="Nenhum produto foi deletado. Verifique se os IDs pertencem a este bot.")

    return {"message": f"{deleted_count} produtos foram excluídos com sucesso."}

@router.post("/bots/{bot_id}/catalog/upload-from-file", status_code=201)
async def upload_catalog_from_file_endpoint(
    bot_id: int,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
) -> Dict[str, Any]:

    # 1. Validação de Segurança
    db_bot = await crud.get_bot_by_id(session, bot_id=bot_id)
    if not db_bot or db_bot.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    # 2. Leitura Segura do Arquivo com limite de tamanho
    try:
        logger.info("File received: %s | Type: %s", file.filename, file.content_type)
        chunks = []
        total_size = 0
        while True:
            chunk = await file.read(8192)  # 8 KB chunks
            if not chunk:
                break
            total_size += len(chunk)
            if total_size > MAX_UPLOAD_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail=f"Arquivo excede o limite de {MAX_UPLOAD_SIZE // (1024 * 1024)} MB.",
                )
            chunks.append(chunk)
        contents = b"".join(chunks)

        # 3. Upload para S3 (Assíncrono via Thread)
        try:
            s3_url = await asyncio.to_thread(
                upload_bytes_to_s3,
                contents,
                file.filename,
                file.content_type,
                f"menus/bot_{bot_id}"
            )
            logger.info("S3 upload successful: %s", s3_url)

            # Salva URL no banco
            db_bot.menu_url = s3_url
            session.add(db_bot)
            await session.commit()

        except Exception as e:
            logger.warning("S3 upload failed, proceeding with extraction: %s", e)

        # 4. Processamento IA (Lógica Blindada)
        all_extracted_products = []

        # DETECÇÃO HÍBRIDA: Confia no Content-Type E na extensão do arquivo
        is_pdf = False
        if file.content_type and "pdf" in file.content_type.lower():
            is_pdf = True
        elif file.filename and file.filename.lower().endswith(".pdf"):
            is_pdf = True

        if is_pdf:
            logger.info("PDF mode activated, starting page conversion")
            try:
                # fitz abre direto dos bytes da memória
                doc = fitz.open(stream=contents, filetype="pdf")

                # Processa até 5 páginas para não estourar tempo/custo
                max_pages = 5
                pages_to_process = min(len(doc), max_pages)
                tasks = []

                for i in range(pages_to_process):
                    page = doc.load_page(i)
                    # Aumentei DPI para 200 para melhorar leitura de letras pequenas
                    pix = page.get_pixmap(dpi=200)
                    page_bytes = pix.tobytes("png")

                    logger.info("Sending page %d to AI", i + 1)
                    tasks.append(extract_products_from_image(page_bytes, "image/png"))

                # Executa tudo em paralelo
                results_list = await asyncio.gather(*tasks)

                for page_products in results_list:
                    if page_products:
                        all_extracted_products.extend(page_products)

                doc.close()
                logger.info("PDF processed, items found: %d", len(all_extracted_products))

            except Exception as e:
                logger.error("Critical error reading PDF: %s", e)
                # Não damos raise aqui para tentar ver se achou algo antes de falhar

        else:
            logger.info("Single image mode activated")
            all_extracted_products = await extract_products_from_image(contents, file.content_type)

        # 5. Validação Final
        if not all_extracted_products:
            msg = "O arquivo foi salvo, mas a IA não identificou nenhum produto. Verifique se a imagem está legível."
            logger.error("Extraction failed: %s", msg)
            raise HTTPException(status_code=400, detail=msg)

        # 6. Salvar no Banco (Batch)
        enriched_products = []
        for product in all_extracted_products:
            name_words = product.get("name", "").lower()
            desc_words = product.get("description", "").lower()
            full_text = name_words + " " + desc_words
            words = set(re.findall(r'\b\w+\b', full_text))

            product["keywords"] = list(words)
            product["is_available"] = True
            enriched_products.append(product)

        count = await crud.bulk_create_products(
            session=session,
            bot_id=bot_id,
            products_data=enriched_products
        )

        return {"message": f"Sucesso! {count} produtos cadastrados a partir do cardápio."}

    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error("Unhandled error in upload route: %s", e)
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")

# --- Rotas de Pedidos (KDS) ---

@router.get("/bots/{bot_id}/orders", response_model=List[schemas.OrderResponse])
async def list_bot_orders(
    bot_id: int,
    status: str | None = None, # Ex: ?status=paid
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """Lista os pedidos do bot. Útil para o painel da cozinha."""
    # 1. Validação de segurança (Obrigatória em SaaS multi-tenant)
    db_bot = await crud.get_bot_by_id(session, bot_id=bot_id)
    if not db_bot or db_bot.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    # 2. Busca os pedidos usando a função otimizada do CRUD
    orders = await crud.list_orders_by_bot(session, bot_id=bot_id, status_filter=status)
    return orders

@router.patch("/bots/{bot_id}/orders/{order_id}", response_model=schemas.OrderResponse)
async def update_order_status(
    bot_id: int,
    order_id: int,
    status_data: schemas.OrderStatusUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """Atualiza o status do pedido e desativa automaticamente o atendimento humano se finalizado."""
    # 1. Segurança: Verifica se o bot pertence ao usuário
    db_bot = await crud.get_bot_by_id(session, bot_id=bot_id)
    if not db_bot or db_bot.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    # 2. Atualização do Status do Pedido (Ex: de PAID para COMPLETED)
    updated_order = await crud.update_order_status_by_id(
        session,
        order_id=order_id,
        new_status=status_data.status
    )

    if not updated_order:
        raise HTTPException(status_code=404, detail="Pedido não encontrado.")

    # ==============================================================================
    # 🤖 AUTO-SWITCH: Desativa o "Human Takeover" se o pedido for finalizado
    # ==============================================================================

    # 1. Normaliza para MAIÚSCULO para evitar erro de 'completed' vs 'COMPLETED'
    incoming_status = str(status_data.status).lower()

    # Debug para você ver no log o que está chegando
    logger.info("Auto-switch attempt, status received: %s", incoming_status)

    if incoming_status in ["completed", "canceled"]:
        try:
            # 2. Carrega o contato dono do pedido
            await session.refresh(updated_order, attribute_names=["contact"])

            if updated_order.contact:
                # 3. Busca o carrinho ATIVO desse contato
                cart = await crud.get_or_create_cart(session, updated_order.contact.id)

                # 4. Se estiver em modo manual, desliga
                if cart and cart.human_takeover_active:
                    logger.info("Auto-switch: order %s, reactivating bot for %s", incoming_status, updated_order.contact.name)
                    cart.human_takeover_active = False
                    session.add(cart)
                    await session.commit()
                else:
                    logger.info("Auto-switch: bot already active for %s", updated_order.contact.name)
            else:
                logger.warning("Auto-switch: order has no associated contact")

        except Exception as e:
            logger.warning("Auto-switch takeover error: %s", e)
    # ==============================================================================

    # 3. Notificação WhatsApp automática ao cliente
    from app.models import DeliveryMethod

    is_delivery = updated_order.delivery_method == DeliveryMethod.DELIVERY

    STATUS_MESSAGES = {
        "preparing": "👨‍🍳 Seu pedido está sendo preparado! Em breve ficará pronto.",
        "ready": (
            "🛵 Seu pedido está pronto e em breve sairá para entrega!"
            if is_delivery else
            "✅ Seu pedido está pronto e disponível para coleta no restaurante, obrigado pela preferência!"
        ),
        "completed": (
            "🎉 Seu pedido saiu para entrega e logo chegará até você! Obrigado pela preferência!"
            if is_delivery else
            None
        ),
        "canceled":  "❌ Infelizmente seu pedido foi cancelado. Entre em contato para mais informações.",
    }

    notify_msg = STATUS_MESSAGES.get(incoming_status)
    if notify_msg:
        try:
            await session.refresh(updated_order, attribute_names=["contact", "bot"])
            contact = updated_order.contact
            bot = updated_order.bot
            if contact and bot and bot.whatsapp_token and bot.phone_number_id:
                await send_whatsapp_message(
                    to=contact.phone_number,
                    message=notify_msg,
                    token=bot.whatsapp_token,
                    phone_id=bot.phone_number_id,
                )
                logger.info("WhatsApp notification sent to %s: %s", contact.phone_number, incoming_status)
        except Exception as e:
            logger.warning("Failed to send WhatsApp notification: %s", e)

    # 4. Preparação da Resposta
    order_dict = updated_order.model_dump()

    order_dict["display_items"] = [
        {"quantity": item.quantity, "product_name": item.product.name}
        for item in updated_order.items
    ]

    return order_dict

@router.get("/bots/{bot_id}/analytics/best-sellers")
async def get_best_sellers(
    bot_id: int,
    session: AsyncSession = Depends(get_session),
    # Adicione current_user para garantir segurança
    current_user: User = Depends(get_current_user)
):
    # 1. Validação de Propriedade (Segurança Básica)
    db_bot = await crud.get_bot_by_id(session, bot_id=bot_id)
    if not db_bot or db_bot.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    # 2. Validação de Plano (Feature Gating)
    # Buscamos a assinatura vinculada ao bot
    sub = await crud.get_subscription_by_bot(session, bot_id)

    # Regra: Se não tiver assinatura, ou se o status não for ativo/authorized, bloqueia.
    # Você pode refinar isso para verificar "plan_type" também (ex: if sub.plan_type == 'basic')
    is_pro = sub and sub.status == "authorized" and sub.plan_type in ["pro", "enterprise"]

    # Se você quiser retornar um erro 403 para o front tratar:
    if not is_pro:
        raise HTTPException(status_code=403, detail="SUBSCRIPTION_REQUIRED")

    # 3. Busca os dados (Se passou no gate)
    results = await crud.get_top_selling_products(session, bot_id)

    return [
        {
            "name": row[0],
            "quantity": row[1],
            "revenue": row[2]
        }
        for row in results
    ]

@router.post("/bots/whatsapp/auth", status_code=201)
async def authenticate_whatsapp_bot(
    auth_data: WhatsAppAuthRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """
    Fluxo Sandbox/Live: Code -> Token -> WABA Direta -> Phone -> Save
    """
    app_id = os.getenv("FB_APP_ID")
    app_secret = os.getenv("FB_APP_SECRET")

    logger.info("[Auth] Starting token exchange for user: %s", current_user.email)

    async with httpx.AsyncClient(timeout=30.0) as client:
        # ⚠️ Passo 1: Trocar Code por Access Token
        token_url = "https://graph.facebook.com/v19.0/oauth/access_token"
        params = {
            "client_id": app_id,
            "client_secret": app_secret,
            "code": auth_data.code,
            "redirect_uri": auth_data.redirect_uri
        }

        resp = await client.get(token_url, params=params)
        data = resp.json()

        if "error" in data:
            logger.error("OAuth error: %s", data.get("error", {}).get("message", "unknown"))
            raise HTTPException(status_code=400, detail=data['error']['message'])

        access_token = data["access_token"]
        logger.info("Access token obtained successfully")

        # ⚠️ Passo 2: Descobrir WABA diretamente (CORRIGIDO PARA EMBEDDED SIGNUP)
        # Em vez de buscar /me/businesses, buscamos direto as contas de WhatsApp vinculadas ao token

        logger.info("Fetching WABA via /me/whatsapp_business_accounts")
        me_waba_url = "https://graph.facebook.com/v19.0/me/whatsapp_business_accounts"
        waba_resp = await client.get(me_waba_url, params={"access_token": access_token})
        waba_data = waba_resp.json()

        if "error" in waba_data:
            logger.error("Graph API error: %s", waba_data.get("error", {}).get("message", "unknown"))
            raise HTTPException(status_code=400, detail=waba_data['error']['message'])

        if not waba_data.get("data"):
            logger.warning("No WABA found in /me/whatsapp_business_accounts")
            raise HTTPException(
                status_code=400,
                detail="Nenhuma conta de WhatsApp Business encontrada. Verifique se o cadastro no Facebook foi concluído."
            )

        # No Embedded Signup, o token geralmente dá acesso a apenas uma WABA recém-criada/selecionada
        target_waba = waba_data["data"][0]
        waba_id = target_waba["id"]
        waba_name = target_waba.get("name", "WhatsApp Bot")
        logger.info("WABA found: %s (ID: %s)", waba_name, waba_id)

        # ⚠️ Passo 3: Pegar Números da WABA
        phone_url = f"https://graph.facebook.com/v19.0/{waba_id}/phone_numbers"
        phone_resp = await client.get(phone_url, params={"access_token": access_token})
        phone_data = phone_resp.json()

        phone_number_id = None
        display_number = None

        if phone_data.get("data"):
            # Pega o primeiro número disponível
            num_obj = phone_data["data"][0]
            phone_number_id = num_obj["id"]
            display_number = num_obj["display_phone_number"]
            logger.info("Phone number found: %s (ID: %s)", display_number, phone_number_id)
        else:
            raise HTTPException(status_code=400, detail="WABA encontrada, mas sem número de telefone associado.")

        # ⚠️ Passo 4: Inscrever Webhook Automaticamente
        try:
            sub_url = f"https://graph.facebook.com/v19.0/{waba_id}/subscribed_apps"
            sub_resp = await client.post(sub_url, params={"access_token": access_token})
            if sub_resp.status_code == 200:
                logger.info("Webhook subscribed to WABA successfully")
            else:
                logger.warning("Webhook subscription response: %s", sub_resp.json())
        except Exception as e:
            logger.warning("Error subscribing webhook: %s", e)

        # ⚠️ Passo 5: Limpeza e Salvamento no Banco
        clean_number = re.sub(r'\D', '', display_number)

        # Tenta achar um bot existente por número (Lógica original mantida)
        existing_bot = await crud.get_bot_by_number(session, clean_number)

        if existing_bot:
             if existing_bot.user_id == current_user.id:
                 existing_bot.whatsapp_token = access_token
                 existing_bot.phone_number_id = phone_number_id
                 session.add(existing_bot)
                 await session.commit()
                 return {"status": "success", "number": clean_number, "message": "Bot reconectado e token atualizado"}
             else:
                 # Se o número existe mas é de outro usuário
                 raise HTTPException(status_code=400, detail="Este número já está em uso por outra conta.")

        # Se não existe, cria novo bot
        await crud.create_bot(
            session=session,
            user_id=current_user.id,
            whatsapp_number=clean_number,
            restaurant_name=waba_name,
            whatsapp_token=access_token,
            phone_number_id=phone_number_id,
            pix_key=None,
            delivery_fee=0,
            min_order_value=0
        )

        return {"status": "success", "number": clean_number, "message": "Novo bot criado e conectado"}

# ==========================================
# 🚀 ROTA DE ONBOARDING ENTERPRISE-GRADE
# ==========================================

@router.post("/bots/whatsapp/complete-onboarding", status_code=201)
async def complete_onboarding(
    data: schemas.EmbeddedSignupPayload,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """
    Onboarding Definitivo (Enterprise-Grade):
    - Aceita dados do evento (Happy Path)
    - Aceita apenas Token (Fallback) e faz Discovery completo
    """
    logger.info("[Onboarding] Processing bot #%s", data.bot_id)

    app_id = os.getenv("FB_APP_ID")
    app_secret = os.getenv("FB_APP_SECRET")

    final_token = None

    # --- FASE 1: OBTENÇÃO DE TOKEN CONFIÁVEL ---
    async with httpx.AsyncClient(timeout=30.0) as client:

        # CASO A: Veio Code (Cadastro novo via SDK)
        if data.code:
            token_url = "https://graph.facebook.com/v19.0/oauth/access_token"
            params = {
                "client_id": app_id,
                "client_secret": app_secret,
                "code": data.code,
                "redirect_uri": data.redirect_uri
            }
            try:
                resp = await client.get(token_url, params=params)
                if "access_token" in resp.json():
                    final_token = resp.json()["access_token"]
                else:
                    logger.error("Code exchange failed")
                    raise HTTPException(status_code=400, detail="Falha na validação do login.")
            except Exception as e:
                raise HTTPException(status_code=500, detail="Erro de comunicação com a Meta.")

        # CASO B: Veio Token Curto (Fallback/Reconnect)
        elif data.access_token:
            logger.info("Exchanging short-lived token for long-lived token")
            exchange_url = "https://graph.facebook.com/v19.0/oauth/access_token"
            params = {
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": data.access_token
            }
            try:
                resp = await client.get(exchange_url, params=params)
                if "access_token" in resp.json():
                    final_token = resp.json()["access_token"]
                    logger.info("Long-lived token obtained")
                else:
                    logger.error("Token exchange failed")
                    raise HTTPException(status_code=400, detail="Sessão expirada. Tente novamente.")
            except Exception as e:
                 raise HTTPException(status_code=500, detail="Erro ao renovar sessão.")

        else:
            raise HTTPException(status_code=400, detail="Nenhuma credencial recebida.")

    # --- FASE 2: DESCOBERTA DE IDS (Se necessário) ---
    phone_number_id = data.phone_number_id
    display_phone_number = data.display_phone_number

    # Se faltar dados (Fallback), o Backend assume a responsabilidade de descobrir
    if not phone_number_id or not display_phone_number:
        logger.info("Missing IDs, executing discovery")

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                # 1. Buscar Empresas (Businesses)
                # O endpoint /me/whatsapp_business_accounts é instável. Usamos /me/businesses primeiro.
                biz_resp = await client.get(
                    "https://graph.facebook.com/v19.0/me/businesses",
                    params={"access_token": final_token}
                )
                biz_data = biz_resp.json()

                if not biz_data.get("data"):
                     # Edge case: Usuário pode ter acesso direto sem business (raro, mas possível via tasks)
                     # Nesse caso tentamos o endpoint direto como último recurso
                     fallback_waba = await client.get(
                        "https://graph.facebook.com/v19.0/me/whatsapp_business_accounts",
                        params={"access_token": final_token}
                     )
                     if fallback_waba.json().get("data"):
                         waba_data = fallback_waba.json()
                         logger.warning("WABA found outside of Business")
                     else:
                         raise HTTPException(status_code=400, detail="Nenhuma empresa ou conta WhatsApp encontrada.")
                else:
                    # Pega o primeiro Business (Assumimos fluxo simplificado)
                    target_biz_id = biz_data["data"][0]["id"]

                    # 2. Buscar WABAs desse Business
                    waba_resp = await client.get(
                        f"https://graph.facebook.com/v19.0/{target_biz_id}/owned_whatsapp_business_accounts",
                        params={"access_token": final_token}
                    )
                    waba_data = waba_resp.json()

                if not waba_data.get("data"):
                     raise HTTPException(status_code=400, detail="Empresa encontrada, mas sem conta WhatsApp.")

                target_waba_id = waba_data["data"][0]["id"]

                # 3. Buscar Números da WABA
                phones_resp = await client.get(
                    f"https://graph.facebook.com/v19.0/{target_waba_id}/phone_numbers",
                    params={"access_token": final_token}
                )
                phones_data = phones_resp.json()

                if not phones_data.get("data"):
                     raise HTTPException(status_code=400, detail="Conta WhatsApp sem número cadastrado.")

                # Pega o primeiro número disponível
                target_phone = phones_data["data"][0]
                phone_number_id = target_phone["id"]
                display_phone_number = target_phone["display_phone_number"]

                logger.info("Discovery success: %s (ID: %s)", display_phone_number, phone_number_id)

            except HTTPException as he:
                raise he
            except Exception as e:
                logger.error("Discovery error: %s", e)
                raise HTTPException(status_code=500, detail="Erro ao identificar sua conta WhatsApp.")

    # --- FASE 3: PERSISTÊNCIA E INSCRIÇÃO ---

    # Inscrever Webhook (Importante para garantir recebimento de mensagens)
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
             # Precisamos do WABA ID. Se veio do frontend, ok. Se não, pegamos do Discovery ou deduzimos.
             # Para simplificar a inscrição, usamos o phone_number_id para achar a WABA se necessário,
             # mas o ideal é ter o waba_id. Se veio do discovery, já temos target_waba_id.
             pass
             # Nota: A inscrição de webhook geralmente é automática no Embedded Signup,
             # mas em reconexões manuais pode ser bom reforçar. Fica como melhoria futura.
        except Exception:
            pass

    clean_number = re.sub(r'\D', '', display_phone_number)

    # Verifica duplicidade
    existing_bot = await crud.get_bot_by_number(session, clean_number)

    if existing_bot:
        if existing_bot.user_id != current_user.id:
             raise HTTPException(status_code=400, detail="Número já pertence a outro usuário.")

        # Atualiza bot existente
        existing_bot.whatsapp_token = final_token
        existing_bot.phone_number_id = phone_number_id
        session.add(existing_bot)
        await session.commit()
    else:
        # Vincula ao bot atual (Rascunho)
        target_bot = await crud.get_bot_by_id(session, data.bot_id)

        if not target_bot or target_bot.user_id != current_user.id:
             raise HTTPException(status_code=403, detail="Acesso negado ao bot.")

        target_bot.whatsapp_number = clean_number
        target_bot.phone_number_id = phone_number_id
        target_bot.whatsapp_token = final_token

        if "Loja" in target_bot.restaurant_name or not target_bot.restaurant_name:
             target_bot.restaurant_name = f"Loja {clean_number}"

        session.add(target_bot)
        await session.commit()

    return {"status": "connected", "number": clean_number}

# 1. ROTA DE VERIFICAÇÃO (GET)
@router.get("/bots/whatsapp/webhook")
async def verify_webhook(
    mode: str = Query(alias="hub.mode"),
    token: str = Query(alias="hub.verify_token"),
    challenge: str = Query(alias="hub.challenge")
):
    if mode == "subscribe" and token == WEBHOOK_VERIFY_TOKEN:
        logger.info("Webhook verified by Meta")
        return int(challenge)
    raise HTTPException(status_code=403, detail="Token inválido.")

# 2. ROTA DE RECEBIMENTO (POST)
@router.post("/bots/whatsapp/webhook")
async def receive_whatsapp_message(request: Request):
    """
    Recebe o evento da Meta e joga para o Worker processar em background.
    """
    try:
        payload = await request.json()

        # Validação básica: é um evento de mensagem de WhatsApp?
        if payload.get("object") == "whatsapp_business_account" and payload.get("entry"):
            entry = payload["entry"][0]
            changes = entry.get("changes", [])

            if changes and changes[0].get("value"):
                value = changes[0].get("value")

                # Se tiver mensagens ou status, enviamos para a fila
                if "messages" in value or "statuses" in value:

                    # 1. Recupera o pool do Redis que foi criado no main.py
                    redis_queue: ArqRedis = request.app.state.arq_redis

                    # 2. Enfileira o job 'process_whatsapp_message'
                    # O Worker vai pegar isso aqui e rodar a IA
                    await redis_queue.enqueue_job('process_whatsapp_message', payload)

                    # Debug leve (opcional)
                    waba_id = value.get("metadata", {}).get("phone_number_id", "Unknown")
                    logger.info("WABA event %s enqueued to Redis", waba_id)

    except Exception as e:
        logger.warning("Error processing webhook: %s", e)
        # Retornamos 200 OK mesmo com erro para a Meta não ficar reenviando infinitamente
        return {"status": "error_handled"}

    return {"status": "received"}
