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

from fastapi import UploadFile, File, Form
from app.openai_client import extract_products_from_image
import fitz  # PyMuPDF (Necessário para ler PDFs)

import os
import httpx
from pydantic import BaseModel
from urllib.parse import urlparse
from arq import ArqRedis
import asyncio

router = APIRouter()
WEBHOOK_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN")

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
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """Lista todos os produtos de um bot específico, verificando a permissão do usuário."""
    db_bot = await crud.get_bot_by_id(session, bot_id=bot_id)
    if not db_bot or db_bot.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    products = await crud.get_products_by_bot_id(session, bot_id=bot_id)
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
    """
    Recebe Imagem ou PDF (multipágina), processa com Visão em PARALELO e cadastra produtos.
    """
    
    # 1. Validação de Segurança
    db_bot = await crud.get_bot_by_id(session, bot_id=bot_id)
    if not db_bot or db_bot.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    valid_types = ["image/jpeg", "image/png", "image/webp", "application/pdf"]
    if file.content_type not in valid_types:
        raise HTTPException(status_code=400, detail="Apenas Imagens ou PDF são suportados.")

    try:
        print(f"📸 Lendo arquivo: {file.filename} ({file.content_type})")
        contents = await file.read()
        
        # Lista final que vai acumular produtos de todas as páginas/imagens
        all_extracted_products = []

        # ▼▼▼ LÓGICA DE MULTIPÁGINAS PARALELIZADA (TURBO) ▼▼▼
        if file.content_type == "application/pdf":
            print("📄 PDF detectado. Iniciando processamento PARALELO...")
            try:
                # Abre o PDF
                doc = fitz.open(stream=contents, filetype="pdf")
                
                # Limite de segurança: processar no máximo 5 páginas
                max_pages = 5
                pages_to_process = min(len(doc), max_pages)
                
                tasks = [] # Lista de tarefas para o asyncio

                for i in range(pages_to_process):
                    page = doc.load_page(i)
                    
                    # 150 DPI é um bom equilíbrio
                    pix = page.get_pixmap(dpi=150) 
                    page_bytes = pix.tobytes("png")
                    
                    # ⚠️ O PULO DO GATO: Não usamos 'await' aqui.
                    # Apenas chamamos a função (que retorna uma corrotina) e guardamos na lista.
                    print(f"   -> Agendando página {i+1}...")
                    tasks.append(extract_products_from_image(page_bytes, "image/png"))
                
                print(f"🚀 Disparando {len(tasks)} requisições para a IA simultaneamente...")
                
                # Executa todas as tarefas ao mesmo tempo
                # O tempo total será o da página mais lenta, não a soma de todas!
                results_list = await asyncio.gather(*tasks)
                
                # Consolida os resultados
                for page_products in results_list:
                    if page_products:
                        all_extracted_products.extend(page_products)
                
                doc.close()
                print(f"✅ Processamento paralelo concluído. Total de itens brutos: {len(all_extracted_products)}")

            except Exception as e:
                print(f"Erro ao processar PDF: {e}")
                raise HTTPException(status_code=400, detail="Erro ao ler o arquivo PDF.")
        
        else:
            # Lógica para Imagem Única (JPG/PNG) continua igual
            all_extracted_products = await extract_products_from_image(contents, file.content_type)
        # ▲▲▲ FIM DA LÓGICA ▲▲▲

        
        if not all_extracted_products:
            raise HTTPException(status_code=400, detail="A IA não conseguiu identificar produtos.")

        # 4. Enriquecimento e Salvamento (Batch único no final)
        enriched_products = []
        for product in all_extracted_products:
            name_words = product.get("name", "").lower()
            desc_words = product.get("description", "").lower()
            full_text = name_words + " " + desc_words
            words = set(re.findall(r'\b\w+\b', full_text))
            
            product["keywords"] = list(words)
            product["is_available"] = True 
            enriched_products.append(product)

        # Chama o Bulk Create (versão Blindada que você ajustou no passo anterior)
        count = await crud.bulk_create_products(
            session=session,
            bot_id=bot_id,
            products_data=enriched_products
        )
            
        return {"message": f"Sucesso! {count} produtos processados do arquivo."}

    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"Erro no upload: {e}")
        raise HTTPException(status_code=500, detail="Erro interno ao processar arquivo.")

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
    """Atualiza o status do pedido (ex: de 'paid' para 'completed')."""
    # 1. Segurança
    db_bot = await crud.get_bot_by_id(session, bot_id=bot_id)
    if not db_bot or db_bot.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")
    
    # 2. Atualização
    updated_order = await crud.update_order_status_by_id(
        session, 
        order_id=order_id, 
        new_status=status_data.status
    )
    
    if not updated_order:
        raise HTTPException(status_code=404, detail="Pedido não encontrado.")

    order_dict = updated_order.model_dump()
        
    order_dict["display_items"] = [
        {"quantity": item.quantity, "product_name": item.product.name} 
        for item in updated_order.items
    ]
    
    return order_dict

@router.get("/bots/{bot_id}/analytics/best-sellers")
async def get_best_sellers(bot_id: int, session: AsyncSession = Depends(get_session)):
    results = await crud.get_top_selling_products(session, bot_id)
    
    # Formata para JSON amigável
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
    
    print(f"\n🚀 [Auth] Iniciando troca de token para: {current_user.email}")

    async with httpx.AsyncClient() as client:
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
            print(f"❌ Erro OAuth: {data}")
            raise HTTPException(status_code=400, detail=data['error']['message'])
            
        access_token = data["access_token"]
        print("✅ Token obtido.")

        # ⚠️ Passo 2: Descobrir WABA diretamente (CORRIGIDO PARA EMBEDDED SIGNUP)
        # Em vez de buscar /me/businesses, buscamos direto as contas de WhatsApp vinculadas ao token
        
        print("🔍 Buscando WABA via /me/whatsapp_business_accounts...")
        me_waba_url = "https://graph.facebook.com/v19.0/me/whatsapp_business_accounts"
        waba_resp = await client.get(me_waba_url, params={"access_token": access_token})
        waba_data = waba_resp.json()

        if "error" in waba_data:
            print(f"❌ Erro Graph API: {waba_data}")
            raise HTTPException(status_code=400, detail=waba_data['error']['message'])

        if not waba_data.get("data"):
            print("⚠️ Nenhuma WABA encontrada em /me/whatsapp_business_accounts")
            raise HTTPException(
                status_code=400, 
                detail="Nenhuma conta de WhatsApp Business encontrada. Verifique se o cadastro no Facebook foi concluído."
            )

        # No Embedded Signup, o token geralmente dá acesso a apenas uma WABA recém-criada/selecionada
        target_waba = waba_data["data"][0]
        waba_id = target_waba["id"]
        waba_name = target_waba.get("name", "WhatsApp Bot")
        print(f"🔹 WABA encontrada: {waba_name} ({waba_id})")

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
            print(f"📞 Número Encontrado: {display_number} (ID: {phone_number_id})")
        else:
            raise HTTPException(status_code=400, detail="WABA encontrada, mas sem número de telefone associado.")

        # ⚠️ Passo 4: Inscrever Webhook Automaticamente
        try:
            sub_url = f"https://graph.facebook.com/v19.0/{waba_id}/subscribed_apps"
            sub_resp = await client.post(sub_url, params={"access_token": access_token})
            if sub_resp.status_code == 200:
                print("✅ Webhook inscrito na WABA com sucesso.")
            else:
                print(f"⚠️ Resposta Webhook: {sub_resp.json()}")
        except Exception as e:
            print(f"⚠️ Erro ao inscrever webhook: {e}")

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
    print(f"🚀 [Onboarding] Processando Bot #{data.bot_id}")
    
    app_id = os.getenv("FB_APP_ID")
    app_secret = os.getenv("FB_APP_SECRET")
    
    final_token = None
    
    # --- FASE 1: OBTENÇÃO DE TOKEN CONFIÁVEL ---
    async with httpx.AsyncClient() as client:
        
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
                    print(f"❌ Erro Code Exchange: {resp.json()}")
                    raise HTTPException(status_code=400, detail="Falha na validação do login.")
            except Exception as e:
                raise HTTPException(status_code=500, detail="Erro de comunicação com a Meta.")

        # CASO B: Veio Token Curto (Fallback/Reconnect)
        elif data.access_token:
            print("🔄 Trocando Token Curto por Longo...")
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
                    print("✅ Token Long-Lived obtido.")
                else:
                    print(f"❌ Erro Token Exchange: {resp.json()}")
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
        print("🔍 IDs ausentes. Executando Discovery Robusto...")
        
        async with httpx.AsyncClient() as client:
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
                         print("⚠️ WABA encontrada fora de Business.")
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
                
                print(f"📞 Discovery Sucesso: {display_phone_number} (ID: {phone_number_id})")

            except HTTPException as he:
                raise he
            except Exception as e:
                print(f"❌ Erro Discovery: {e}")
                raise HTTPException(status_code=500, detail="Erro ao identificar sua conta WhatsApp.")

    # --- FASE 3: PERSISTÊNCIA E INSCRIÇÃO ---
    
    # Inscrever Webhook (Importante para garantir recebimento de mensagens)
    async with httpx.AsyncClient() as client:
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

    # ------------------------------------------------------------------
    # FASE 3: PERSISTÊNCIA (Salvar no Banco)
    # ------------------------------------------------------------------
    
    if not phone_number_id or not display_phone_number:
         raise HTTPException(status_code=400, detail="Falha crítica: IDs não identificados.")

    clean_number = re.sub(r'\D', '', display_phone_number)
    
    # Validação de Duplicidade
    existing_bot = await crud.get_bot_by_number(session, clean_number)
    
    if existing_bot:
        if existing_bot.user_id != current_user.id:
             raise HTTPException(status_code=400, detail="Este número já está em uso por outra conta.")
        
        # Atualiza
        existing_bot.whatsapp_token = final_token
        existing_bot.phone_number_id = phone_number_id
        session.add(existing_bot)
        await session.commit()
    else:
        # Cria/Vincula no bot rascunho
        target_bot = await crud.get_bot_by_id(session, data.bot_id)
        if not target_bot or target_bot.user_id != current_user.id:
             raise HTTPException(status_code=403, detail="Bot alvo não encontrado ou acesso negado.")
        
        target_bot.whatsapp_number = clean_number
        target_bot.phone_number_id = phone_number_id
        target_bot.whatsapp_token = final_token
        
        # Atualiza nome se for padrão
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
        print("✅ Webhook verificado pela Meta!")
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
                    print(f"📨 Evento WABA {waba_id} enviado para fila Redis.")

    except Exception as e:
        print(f"⚠️ Erro ao processar webhook: {e}")
        # Retornamos 200 OK mesmo com erro para a Meta não ficar reenviando infinitamente
        return {"status": "error_handled"}

    return {"status": "received"}

