from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlmodel.ext.asyncio.session import AsyncSession
from typing import List, Dict, Any

# --- Importações Consolidadas ---
from app import crud, schemas, data_extractor
from app.database import get_session
from app.models import User
from app.auth import get_current_user
import re

from fastapi import UploadFile, File, Form
from app.openai_client import extract_products_from_image
import fitz  # PyMuPDF (Necessário para ler PDFs)

router = APIRouter()

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
        pix_key=bot_data.pix_key # 👈 CORREÇÃO: Passa a chave pix para o CRUD
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
        price=product_data.price
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
    Recebe Imagem ou PDF (multipágina), processa com Visão e cadastra produtos.
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

        # ▼▼▼ LÓGICA DE MULTIPÁGINAS ▼▼▼
        if file.content_type == "application/pdf":
            print("📄 PDF detectado. Processando páginas...")
            try:
                # Abre o PDF
                doc = fitz.open(stream=contents, filetype="pdf")
                
                # Limite de segurança: processar no máximo 5 páginas para não estourar tempo/custo
                # (Você pode ajustar isso conforme sua política de negócio)
                max_pages = 5
                pages_to_process = min(len(doc), max_pages)

                for i in range(pages_to_process):
                    print(f"   -> Processando página {i+1}/{len(doc)}...")
                    page = doc.load_page(i)
                    
                    # 150 DPI é um bom equilíbrio entre qualidade para leitura e tamanho de arquivo
                    pix = page.get_pixmap(dpi=150) 
                    page_bytes = pix.tobytes("png")
                    
                    # Chama a IA para ESTA página específica
                    products_on_page = await extract_products_from_image(page_bytes, "image/png")
                    
                    if products_on_page:
                        all_extracted_products.extend(products_on_page)
                        print(f"      Encontrados {len(products_on_page)} itens na página {i+1}.")
                
                doc.close()

            except Exception as e:
                print(f"Erro ao processar PDF: {e}")
                raise HTTPException(status_code=400, detail="Erro ao ler o arquivo PDF.")
        
        else:
            # Lógica para Imagem Única (JPG/PNG)
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