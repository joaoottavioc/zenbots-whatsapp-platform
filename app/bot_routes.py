from fastapi import APIRouter, Depends, HTTPException, Header
from sqlmodel.ext.asyncio.session import AsyncSession
from app.database import get_session
from app.crud import create_bot, list_user_bots
from app.auth import SECRET_KEY, ALGORITHM
from jose import jwt, JWTError
from typing import List
from app.schemas import BotCreate, BotResponse  # Schema Pydantic para entrada e saída
from app import crud, schemas
from app.models import User
from app.auth import get_current_user # Sua função de autenticação
from app.database import get_session # Sua dependência de sessão

router = APIRouter()

@router.post("/bots", response_model=BotResponse)
async def create_user_bot(
    bot: BotCreate,  # Recebe JSON com dados do bot
    user_email: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    bot_created = await create_bot(session, user_email, bot.whatsapp_number, bot.system_prompt)
    if not bot_created:
        raise HTTPException(status_code=400, detail="Could not create bot")
    return bot_created

@router.get("/bots", response_model=List[BotResponse])
async def get_user_bots(
    user_email: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    bots = await list_user_bots(session, user_email)
    return bots

@router.put("/bots/{bot_id}", response_model=schemas.BotResponse)
async def update_user_bot(
    bot_id: int,
    bot_update_data: schemas.BotUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """
    Atualiza um bot, garantindo que apenas o seu dono possa modificá-lo.
    """
    # Busca o bot que o usuário quer editar
    db_bot = await crud.get_bot_by_id(session, bot_id=bot_id)

    # Verifica se o bot existe
    if not db_bot:
        raise HTTPException(status_code=404, detail="Bot não encontrado.")

    # A verificação de segurança crucial: o bot pertence ao usuário logado?
    if db_bot.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Acesso negado. Você não é o dono deste bot.")

    # Se a segurança passar, chama a função do CRUD para fazer a atualização
    updated_bot = await crud.update_bot(
        session=session, 
        db_bot=db_bot, 
        update_data=bot_update_data
    )

    return updated_bot
