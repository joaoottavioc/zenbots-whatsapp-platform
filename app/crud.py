from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from sqlalchemy.orm import selectinload  # 👈 Importação Essencial
from app.models import Bot, User, ConversationHistory
from app.schemas import BotUpdate
from datetime import datetime
from typing import List, Optional

# --- FUNÇÕES DE USUÁRIO ---

async def get_user_by_email(session: AsyncSession, email: str) -> Optional[User]:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalars().first()

# --- FUNÇÕES DE BOT ---

async def create_bot(session: AsyncSession, user_id: int, whatsapp_number: str, system_prompt: str) -> Optional[Bot]:
    existing_bot_result = await session.execute(select(Bot).where(Bot.whatsapp_number == whatsapp_number))
    if existing_bot_result.scalars().first():
        return None  # Bot já existe

    new_bot = Bot(
        user_id=user_id,
        whatsapp_number=whatsapp_number,
        system_prompt=system_prompt
    )
    session.add(new_bot)
    await session.commit()
    await session.refresh(new_bot)
    return new_bot

async def get_bot_by_id(session: AsyncSession, bot_id: int) -> Optional[Bot]:
    """
    Busca um bot pelo ID, pré-carregando o relacionamento do histórico para
    evitar erros de lazy loading na serialização da resposta.
    """
    query = (
        select(Bot)
        .where(Bot.id == bot_id)
        .options(selectinload(Bot.history)) # ✅ Carregamento antecipado
    )
    result = await session.execute(query)
    return result.scalars().first()

async def get_bot_by_number(session: AsyncSession, whatsapp_number: str) -> Optional[Bot]:
    """
    Busca um bot pelo número, usado principalmente pelo webhook.
    Não precisa carregar o histórico aqui, pois não será retornado numa API.
    """
    result = await session.execute(select(Bot).where(Bot.whatsapp_number == whatsapp_number))
    return result.scalars().first()

async def list_user_bots(session: AsyncSession, user_id: int) -> List[Bot]:
    """
    Lista os bots de um usuário, pré-carregando o histórico de cada um.
    """
    query = (
        select(Bot)
        .where(Bot.user_id == user_id)
        .options(selectinload(Bot.history)) # ✅ Carregamento antecipado
    )
    result = await session.execute(query)
    return result.scalars().all()

async def update_bot(session: AsyncSession, db_bot: Bot, update_data: BotUpdate) -> Bot:
    """
    Atualiza os dados de um objeto Bot e retorna a versão completa com o histórico.
    """
    update_data_dict = update_data.model_dump(exclude_unset=True)
    for key, value in update_data_dict.items():
        setattr(db_bot, key, value)
        
    session.add(db_bot)
    await session.commit()
    await session.refresh(db_bot)
    
    # A forma mais garantida de retornar o objeto completo para a API
    # é buscá-lo novamente com a função que já faz o eager loading.
    return await get_bot_by_id(session, bot_id=db_bot.id)

# --- FUNÇÕES DE HISTÓRICO ---

async def get_history_for_contact(session: AsyncSession, bot_id: int, contact_number: str, limit: int = 20) -> List[ConversationHistory]:
    """
    Recupera as últimas 'limit' mensagens de uma conversa para dar contexto à IA.
    """
    query = (
        select(ConversationHistory)
        .where(ConversationHistory.bot_id == bot_id, ConversationHistory.contact_number == contact_number)
        .order_by(ConversationHistory.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(query)
    return result.scalars().all()[::-1] # Inverte para ordem cronológica

async def add_interaction_to_history(session: AsyncSession, bot_id: int, contact_number: str, user_content: str, assistant_content: str):
    """
    Salva a interação completa em uma única transação.
    """
    user_entry = ConversationHistory(bot_id=bot_id, contact_number=contact_number, role="user", content=user_content)
    assistant_entry = ConversationHistory(bot_id=bot_id, contact_number=contact_number, role="assistant", content=assistant_content)

    session.add_all([user_entry, assistant_entry]) # Mais eficiente para adicionar múltiplos objetos
    await session.commit()