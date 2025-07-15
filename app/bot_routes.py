from fastapi import APIRouter, Depends, HTTPException
from sqlmodel.ext.asyncio.session import AsyncSession
from typing import List, Dict, Any

# --- Importações Consolidadas ---
from app import crud, schemas, data_extractor
from app.database import get_session
from app.models import User
from app.auth import get_current_user

router = APIRouter()

# --- Rotas para Gerenciamento de Bots ---

@router.post("/bots", response_model=schemas.BotResponse, status_code=201)
async def create_new_bot(
    bot_data: schemas.BotCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """
    Cria um novo bot 'casca' para o usuário logado, apenas com as informações essenciais.
    O catálogo de produtos deve ser adicionado depois através dos endpoints de produtos.
    """
    # 👇 CORREÇÃO: Chamada simplificada para a função do CRUD
    bot_created = await crud.create_bot(
        session=session, 
        user_id=current_user.id, 
        whatsapp_number=bot_data.whatsapp_number, 
        restaurant_name=bot_data.restaurant_name
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
    """Atualiza os dados de um bot, como o nome do restaurante."""
    db_bot = await crud.get_bot_by_id(session, bot_id=bot_id)

    if not db_bot:
        raise HTTPException(status_code=404, detail="Bot não encontrado.")

    if db_bot.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    updated_bot = await crud.update_bot(
        session=session, 
        db_bot=db_bot, 
        update_data=bot_update_data
    )

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

@router.post("/bots/{bot_id}/catalog/upload", status_code=201)
async def upload_catalog_from_text(
    bot_id: int,
    request_data: schemas.CatalogUploadRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
) -> Dict[str, Any]:
    """Recebe um texto de cardápio, extrai os produtos usando IA e os salva no banco."""
    db_bot = await crud.get_bot_by_id(session, bot_id=bot_id)
    if not db_bot or db_bot.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    extracted_products = await data_extractor.extract_products_from_text(request_data.catalog_text)

    if not extracted_products:
        raise HTTPException(status_code=400, detail="Não foi possível extrair itens do cardápio. Verifique o texto.")

    # Salva cada produto extraído no banco de dados
    for product in extracted_products:
        # Validação simples para garantir que os dados mínimos existem
        if product.get("name") and product.get("price") is not None:
            await crud.create_product(
                session=session,
                bot_id=bot_id,
                name=product.get("name"),
                description=product.get("description"),
                price=float(product.get("price"))
            )
        
    return {"message": f"{len(extracted_products)} produtos adicionados com sucesso ao bot {bot_id}."}