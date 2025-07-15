from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from sqlalchemy.orm import selectinload
from typing import List, Optional

# Importações de modelos e schemas
from app.models import Bot, User, ConversationHistory, Product
from app.schemas import BotUpdate

# Importação dos nossos serviços de IA
from app.embedding_service import generate_embedding

# --- FUNÇÕES DE USUÁRIO ---

async def get_user_by_email(session: AsyncSession, email: str) -> Optional[User]:
    """Busca um usuário pelo e-mail."""
    result = await session.execute(select(User).where(User.email == email))
    return result.scalars().first()

# --- FUNÇÕES DE BOT ---

async def create_bot(
    session: AsyncSession,
    user_id: int,
    whatsapp_number: str,
    restaurant_name: Optional[str]
) -> Optional[Bot]:
    """Cria um novo bot 'casca', apenas com as informações essenciais."""
    
    existing_bot = await session.execute(select(Bot).where(Bot.whatsapp_number == whatsapp_number))
    if existing_bot.scalars().first():
        return None  # Bot com este número já existe

    new_bot = Bot(
        user_id=user_id,
        whatsapp_number=whatsapp_number,
        restaurant_name=restaurant_name
    )
    session.add(new_bot)
    await session.commit()
    await session.refresh(new_bot)
    
    # Retorna o objeto completo com relacionamentos pré-carregados
    return await get_bot_by_id(session, bot_id=new_bot.id)


async def get_bot_by_id(session: AsyncSession, bot_id: int) -> Optional[Bot]:
    """Busca um bot pelo ID, pré-carregando seus relacionamentos para a API."""
    query = (
        select(Bot)
        .where(Bot.id == bot_id)
        .options(
            selectinload(Bot.history),
            selectinload(Bot.products) # Também pré-carrega os produtos
        )
    )
    result = await session.execute(query)
    return result.scalars().first()


async def get_bot_by_number(session: AsyncSession, whatsapp_number: str) -> Optional[Bot]:
    """Busca um bot pelo número. Usado internamente pelo webhook."""
    result = await session.execute(select(Bot).where(Bot.whatsapp_number == whatsapp_number))
    return result.scalars().first()


async def list_user_bots(session: AsyncSession, user_id: int) -> List[Bot]:
    """Lista os bots de um usuário, pré-carregando os relacionamentos."""
    query = (
        select(Bot)
        .where(Bot.user_id == user_id)
        .options(
            selectinload(Bot.history),
            selectinload(Bot.products)
        )
    )
    result = await session.execute(query)
    return result.scalars().all()


async def update_bot(session: AsyncSession, db_bot: Bot, update_data: BotUpdate) -> Bot:
    """Atualiza um bot e retorna a versão completa."""
    update_data_dict = update_data.model_dump(exclude_unset=True)
    for key, value in update_data_dict.items():
        setattr(db_bot, key, value)
    
    session.add(db_bot)
    await session.commit()
    return await get_bot_by_id(session, bot_id=db_bot.id)

# --- FUNÇÕES DE PRODUTO (CATÁLOGO) ---

async def create_product(
    session: AsyncSession,
    bot_id: int,
    name: str,
    description: Optional[str],
    price: float
) -> Product:
    """Cria um novo produto, gera seu embedding e o salva no banco."""
    text_to_embed = f"{name} - {description}" if description else name
    embedding_vector = generate_embedding(text_to_embed)
    
    new_product = Product(
        bot_id=bot_id,
        name=name,
        description=description,
        price=price,
        embedding=embedding_vector
    )
    
    session.add(new_product)
    await session.commit()
    await session.refresh(new_product)
    return new_product


async def get_products_by_bot_id(session: AsyncSession, bot_id: int) -> List[Product]:
    """Lista todos os produtos de um bot específico."""
    result = await session.execute(select(Product).where(Product.bot_id == bot_id))
    return result.scalars().all()


async def search_products_by_similarity(
    session: AsyncSession,
    bot_id: int,
    query_text: str,
    limit: int = 3
) -> List[Product]:
    """Busca produtos por similaridade semântica usando embeddings."""
    query_embedding = generate_embedding(query_text)
    
    query = (
        select(Product)
        .where(Product.bot_id == bot_id)
        .order_by(Product.embedding.cosine_distance(query_embedding))
        .limit(limit)
    )
    
    result = await session.execute(query)
    return result.scalars().all()

# --- FUNÇÕES DE HISTÓRICO DE CONVERSA ---

async def get_history_for_contact(session: AsyncSession, bot_id: int, contact_number: str, limit: int = 20) -> List[ConversationHistory]:
    """Recupera as últimas 'limit' mensagens de uma conversa."""
    query = (
        select(ConversationHistory)
        .where(ConversationHistory.bot_id == bot_id, ConversationHistory.contact_number == contact_number)
        .order_by(ConversationHistory.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(query)
    return result.scalars().all()[::-1]


async def add_interaction_to_history(session: AsyncSession, bot_id: int, contact_number: str, user_content: str, assistant_content: str):
    """Salva a interação completa (usuário e assistente) em uma única transação."""
    user_entry = ConversationHistory(bot_id=bot_id, contact_number=contact_number, role="user", content=user_content)
    assistant_entry = ConversationHistory(bot_id=bot_id, contact_number=contact_number, role="assistant", content=assistant_content)

    session.add_all([user_entry, assistant_entry])
    await session.commit()