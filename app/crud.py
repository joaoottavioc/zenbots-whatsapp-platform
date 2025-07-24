from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from sqlalchemy.orm import selectinload
from typing import List, Optional, Dict
from sqlmodel import delete # Importe a função delete
from app.models import Order, OrderItem # 👈 Adicione os novos modelos


# Importações de modelos e schemas
from app.models import Bot, User, ConversationHistory, Product
from app.schemas import BotUpdate, ProductUpdate

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
    restaurant_name: Optional[str],
    pix_key: Optional[str]  # 👈 1. Adiciona o novo argumento aqui
) -> Optional[Bot]:
    """Cria um novo bot 'casca', com as informações essenciais."""
    
    existing_bot = await session.execute(select(Bot).where(Bot.whatsapp_number == whatsapp_number))
    if existing_bot.scalars().first():
        return None

    new_bot = Bot(
        user_id=user_id,
        whatsapp_number=whatsapp_number,
        restaurant_name=restaurant_name,
        pix_key=pix_key  # 👈 2. Usa o novo argumento ao criar o objeto
    )
    session.add(new_bot)
    await session.commit()
    await session.refresh(new_bot)
    
    return await get_bot_by_id(session, bot_id=new_bot.id)


async def get_bot_by_id(session: AsyncSession, bot_id: int) -> Optional[Bot]:
    """Busca um bot pelo ID, pré-carregando TODOS os seus relacionamentos."""
    query = (
        select(Bot)
        .where(Bot.id == bot_id)
        .options(
            selectinload(Bot.history),
            selectinload(Bot.products) # 👈 Garanta que esta linha exista
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
            selectinload(Bot.products) # 👈 Garanta que esta linha exista
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

async def delete_bot(session: AsyncSession, db_bot: Bot):
    """Exclui um bot do banco de dados."""
    await session.delete(db_bot)
    await session.commit()
    return {"ok": True}

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


async def get_product_by_id(session: AsyncSession, product_id: int) -> Optional[Product]:
    """Busca um produto específico pelo seu ID."""
    # Pré-carrega o relacionamento 'bot' para usarmos na verificação de permissão
    query = select(Product).where(Product.id == product_id).options(selectinload(Product.bot))
    result = await session.execute(query)
    return result.scalars().first()


async def update_product(session: AsyncSession, db_product: Product, update_data: ProductUpdate) -> Product:
    """Atualiza os dados de um produto."""
    update_data_dict = update_data.model_dump(exclude_unset=True)
    
    # Atualiza os campos do objeto
    for key, value in update_data_dict.items():
        setattr(db_product, key, value)
        
    # Se o nome ou a descrição foram alterados, gera um novo embedding
    if "name" in update_data_dict or "description" in update_data_dict:
        text_to_embed = f"{db_product.name} - {db_product.description}"
        db_product.embedding = generate_embedding(text_to_embed)
        
    session.add(db_product)
    await session.commit()
    await session.refresh(db_product)
    return db_product

# Em app/crud.py

async def bulk_create_products(session: AsyncSession, bot_id: int, products_data: List[Dict]):
    """
    Cria múltiplos produtos em lote a partir de uma lista de dicionários.
    Gera embeddings para cada um e salva tudo em uma única transação.
    """
    products_to_add = []
    for item in products_data:
        # Validação simples para garantir que os dados mínimos existem
        if item.get("name") and item.get("price") is not None:
            text_to_embed = f"{item.get('name')} - {item.get('description', '')}"
            embedding_vector = generate_embedding(text_to_embed)
            
            new_product = Product(
                bot_id=bot_id,
                name=item.get("name"),
                description=item.get("description"),
                price=float(item.get("price")),
                embedding=embedding_vector
            )
            products_to_add.append(new_product)

    if products_to_add:
        session.add_all(products_to_add)
        await session.commit()
    
    return len(products_to_add)

async def bulk_delete_products(session: AsyncSession, user_id: int, product_ids: List[int]) -> int:
    """
    Exclui múltiplos produtos em lote, garantindo que todos pertençam ao usuário.
    Retorna o número de produtos deletados.
    """
    # Passo de segurança: busca os produtos para garantir que eles pertencem ao usuário logado
    subquery = select(Bot.id).where(Bot.user_id == user_id)
    query = (
        select(Product)
        .where(Product.bot_id.in_(subquery))
        .where(Product.id.in_(product_ids))
    )
    result = await session.execute(query)
    products_to_delete = result.scalars().all()

    if not products_to_delete:
        return 0

    # Extrai os IDs dos produtos que realmente serão deletados para a consulta final
    ids_to_delete = [p.id for p in products_to_delete]

    # Executa a exclusão em lote
    delete_statement = delete(Product).where(Product.id.in_(ids_to_delete))
    await session.execute(delete_statement)
    await session.commit()
    
    return len(ids_to_delete)


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

async def delete_product(session: AsyncSession, db_product: Product):
    """Exclui um produto do banco de dados."""
    await session.delete(db_product)
    await session.commit()
    return {"ok": True}

# --- NOVAS FUNÇÕES DE PEDIDO ---

async def create_order(
    session: AsyncSession,
    bot_id: int,
    items: List[Dict] # Espera uma lista de dicionários, ex: [{"product_id": 1, "quantity": 2}, ...]
) -> Optional[Order]:
    """
    Cria um novo pedido com seus itens no banco de dados.
    """
    try:
        total_amount = 0.0
        order_items_to_create = []
        
        # Itera sobre os itens para calcular o total e buscar os preços atuais
        for item_data in items:
            product = await session.get(Product, item_data["product_id"])
            if not product:
                # Se um produto não for encontrado, falha a criação do pedido
                print(f"Erro: Produto com ID {item_data['product_id']} não encontrado.")
                return None
            
            price = product.price
            quantity = item_data["quantity"]
            total_amount += price * quantity
            
            # Prepara o OrderItem para ser criado
            order_items_to_create.append(
                OrderItem(
                    product_id=product.id,
                    quantity=quantity,
                    price_at_time_of_order=price
                )
            )

        # Cria o objeto do pedido principal
        new_order = Order(
            bot_id=bot_id,
            total_amount=round(total_amount, 2), # Arredonda para 2 casas decimais
            items=order_items_to_create
        )
        
        session.add(new_order)
        await session.commit()
        await session.refresh(new_order)
        
        return new_order
        
    except Exception as e:
        print(f"Erro ao criar pedido no banco de dados: {e}")
        await session.rollback()
        return None
