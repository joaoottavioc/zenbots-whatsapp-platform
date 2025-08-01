# app/crud.py - VERSÃO CORRIGIDA
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select, delete
from sqlalchemy.orm import selectinload
from typing import List, Optional, Dict
from datetime import datetime

# 1. Imports unificados e limpos
from app.models import (
    Bot, User, ConversationHistory, Product, Order, OrderItem, ProcessedMessage,
    Contact, ShoppingCart, CartItem
)
from app.schemas import BotUpdate, ProductUpdate
from app.embedding_service import generate_embedding

# --- Funções do Webhook (NÃO DEVEM FAZER COMMIT) ---

async def get_bot_by_number(session: AsyncSession, whatsapp_number: str) -> Optional[Bot]:
    """Busca um bot pelo número. Usado internamente pelo webhook."""
    result = await session.execute(select(Bot).where(Bot.whatsapp_number == whatsapp_number))
    return result.scalars().first()

async def is_message_processed(session: AsyncSession, message_id: str) -> bool:
    result = await session.execute(
        select(ProcessedMessage).where(ProcessedMessage.message_id == message_id)
    )
    return result.scalar_one_or_none() is not None

async def add_processed_message(session: AsyncSession, message_id: str):
    processed_message = ProcessedMessage(message_id=message_id)
    session.add(processed_message)

async def get_or_create_contact(session: AsyncSession, bot_id: int, contact_number: str) -> Contact:
    """Busca um contato pelo número ou o cria, sem commitar a sessão."""
    result = await session.execute(
        select(Contact).where(Contact.phone_number == contact_number, Contact.bot_id == bot_id)
    )
    contact = result.scalar_one_or_none()
    
    if not contact:
        contact = Contact(phone_number=contact_number, bot_id=bot_id)
        session.add(contact)
        await session.flush()
        await session.refresh(contact)
        
    return contact

async def get_or_create_cart(session: AsyncSession, contact_id: int) -> ShoppingCart:
    """Busca o carrinho de um contato ou cria um novo, sem commitar a sessão."""
    query = (
        select(ShoppingCart)
        .where(ShoppingCart.contact_id == contact_id)
        .options(selectinload(ShoppingCart.items).selectinload(CartItem.product))
    )
    result = await session.execute(query)
    cart = result.scalars().first()

    if not cart:
        cart = ShoppingCart(contact_id=contact_id, state="GREETING")
        session.add(cart)
        await session.flush()
        await session.refresh(cart)
    return cart

async def add_items_to_db_cart(session: AsyncSession, cart_id: int, items_to_add: List[Dict]) -> Optional[ShoppingCart]:
    """Adiciona ou atualiza itens no carrinho. O commit é feito no final do fluxo."""
    # CORREÇÃO: Busca o carrinho pelo ID dentro da sessão atual
    cart = await session.get(ShoppingCart, cart_id, options=[selectinload(ShoppingCart.items)])
    if not cart:
        return None

    for item_data in items_to_add:
        product_id = item_data.get("product_id")
        quantity_to_add = item_data.get("quantity", 1)
        existing_item = next((item for item in cart.items if item.product_id == product_id), None)
        
        if existing_item:
            existing_item.quantity += quantity_to_add
        else:
            product = await session.get(Product, product_id)
            if product:
                new_item = CartItem(product_id=product_id, quantity=quantity_to_add, cart_id=cart.id)
                session.add(new_item)
    await session.flush()
    return cart

async def modify_item_quantity_in_db_cart(session: AsyncSession, cart_id: int, product_id: int, new_quantity: int) -> Optional[ShoppingCart]:
    """Modifica a quantidade de um item ou o remove. O commit é feito no final do fluxo."""
    # CORREÇÃO: Busca o carrinho pelo ID dentro da sessão atual
    cart = await session.get(ShoppingCart, cart_id, options=[selectinload(ShoppingCart.items)])
    if not cart:
        return None
        
    item_to_modify = next((item for item in cart.items if item.product_id == product_id), None)
    if item_to_modify:
        if new_quantity > 0:
            item_to_modify.quantity = new_quantity
        else:
            await session.delete(item_to_modify)
    await session.flush()
    return cart

async def clear_db_cart(session: AsyncSession, cart_id: int) -> Optional[ShoppingCart]:
    """Limpa todos os itens de um carrinho e reseta o seu estado."""
    # CORREÇÃO: Busca o carrinho pelo ID dentro da sessão atual
    cart = await session.get(ShoppingCart, cart_id, options=[selectinload(ShoppingCart.items)])
    if not cart:
        return None

    for item in cart.items:
        await session.delete(item)
    
    cart.items = [] # Limpa a lista na memória também
    cart.state = "GREETING"
    cart.last_activity_at = datetime.utcnow()
    await session.flush()
    return cart

async def get_history_for_contact(session: AsyncSession, bot_id: int, contact_number: str, limit: int = 20) -> List[ConversationHistory]:
    """Recupera o histórico de uma conversa usando o objeto Contact."""
    contact = await get_or_create_contact(session, bot_id, contact_number)
    query = (
        select(ConversationHistory)
        .where(ConversationHistory.contact_id == contact.id)
        .order_by(ConversationHistory.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(query)
    # Retorna as mensagens em ordem cronológica (a mais antiga primeiro)
    return result.scalars().all()[::-1]

async def add_interaction_to_history(session: AsyncSession, bot_id: int, contact_number: str, user_content: str, assistant_content: str):
    """Salva a interação completa, ligando-a ao objeto Contact correto."""
    contact = await get_or_create_contact(session, bot_id, contact_number)
    user_entry = ConversationHistory(bot_id=bot_id, contact_id=contact.id, role="user", content=user_content)
    assistant_entry = ConversationHistory(bot_id=bot_id, contact_id=contact.id, role="assistant", content=assistant_content)
    session.add_all([user_entry, assistant_entry])
    await session.flush()

async def find_relevant_products(session: AsyncSession, bot_id: int, extracted_items: List[str], limit_per_item: int = 2) -> List[Product]:
    """Executa uma busca em funil com múltiplas camadas, agora segura para descrições nulas."""
    candidate_products = {}
    for item_name in extracted_items:
        # Nível 1: Busca no NOME
        name_query = select(Product).where(Product.bot_id == bot_id, Product.name.ilike(f"%{item_name}%")).limit(limit_per_item)
        name_results = await session.execute(name_query)
        for p in name_results.scalars().all():
            candidate_products[p.id] = p

        # Nível 2: Busca na DESCRIÇÃO (segura para nulos)
        desc_query = select(Product).where(Product.bot_id == bot_id, Product.description != None, Product.description.ilike(f"%{item_name}%")).limit(limit_per_item)
        desc_results = await session.execute(desc_query)
        for p in desc_results.scalars().all():
            candidate_products[p.id] = p
            
        # Nível 3: Busca por Significado (fallback)
        text_to_embed = f"PRODUTO PRINCIPAL: {item_name}."
        query_embedding = generate_embedding(text_to_embed)
        embedding_query = select(Product).where(Product.bot_id == bot_id).order_by(Product.embedding.cosine_distance(query_embedding)).limit(limit_per_item)
        embedding_results = await session.execute(embedding_query)
        for p in embedding_results.scalars().all():
            candidate_products[p.id] = p
    return list(candidate_products.values())


# --- Funções de API (Podem e devem fazer commit) ---

async def get_user_by_email(session: AsyncSession, email: str) -> Optional[User]:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalars().first()

async def create_bot(session: AsyncSession, user_id: int, whatsapp_number: str, restaurant_name: Optional[str], pix_key: Optional[str]) -> Optional[Bot]:
    existing_bot_result = await session.execute(select(Bot).where(Bot.whatsapp_number == whatsapp_number))
    if existing_bot_result.scalars().first(): 
        return None
    
    new_bot = Bot(user_id=user_id, whatsapp_number=whatsapp_number, restaurant_name=restaurant_name, pix_key=pix_key)
    session.add(new_bot)
    await session.commit()
    await session.refresh(new_bot)
    return new_bot

async def get_bot_by_id(session: AsyncSession, bot_id: int) -> Optional[Bot]:
    query = select(Bot).where(Bot.id == bot_id).options(selectinload(Bot.history), selectinload(Bot.products))
    result = await session.execute(query)
    return result.scalars().first()

async def list_user_bots(session: AsyncSession, user_id: int) -> List[Bot]:
    query = select(Bot).where(Bot.user_id == user_id).options(selectinload(Bot.history), selectinload(Bot.products))
    result = await session.execute(query)
    return result.scalars().all()

async def update_bot(session: AsyncSession, bot_id: int, update_data: BotUpdate) -> Optional[Bot]:
    """CORREÇÃO: Recebe bot_id e busca o bot dentro da sessão."""
    db_bot = await session.get(Bot, bot_id)
    if not db_bot:
        return None

    update_data_dict = update_data.model_dump(exclude_unset=True)
    for key, value in update_data_dict.items():
        setattr(db_bot, key, value)
        
    session.add(db_bot)
    await session.commit()
    await session.refresh(db_bot)
    return db_bot

async def delete_bot(session: AsyncSession, bot_id: int) -> bool:
    """CORREÇÃO: Recebe bot_id e busca o bot para deletar."""
    db_bot = await session.get(Bot, bot_id)
    if not db_bot:
        return False
        
    await session.delete(db_bot)
    await session.commit()
    return True

async def create_product(session: AsyncSession, bot_id: int, name: str, description: Optional[str], price: float) -> Product:
    text_to_embed = f"PRODUTO PRINCIPAL: {name}. DESCRIÇÃO E INGREDIENTES: {description or 'N/A'}."
    embedding_vector = generate_embedding(text_to_embed)
    new_product = Product(bot_id=bot_id, name=name, description=description, price=price, embedding=embedding_vector)
    session.add(new_product)
    await session.commit()
    await session.refresh(new_product)
    return new_product

async def update_product(session: AsyncSession, product_id: int, update_data: ProductUpdate) -> Optional[Product]:
    """CORREÇÃO: Recebe product_id e busca o produto dentro da sessão."""
    db_product = await session.get(Product, product_id)
    if not db_product:
        return None

    update_data_dict = update_data.model_dump(exclude_unset=True)
    for key, value in update_data_dict.items():
        setattr(db_product, key, value)
        
    if "name" in update_data_dict or "description" in update_data_dict:
        text_to_embed = f"PRODUTO PRINCIPAL: {db_product.name}. DESCRIÇÃO E INGREDIENTES: {db_product.description or 'N/A'}."
        db_product.embedding = generate_embedding(text_to_embed)
        
    session.add(db_product)
    await session.commit()
    await session.refresh(db_product)
    return db_product

async def bulk_create_products(session: AsyncSession, bot_id: int, products_data: List[Dict]) -> int:
    products_to_add = []
    for item in products_data:
        if item.get("name") and item.get("price") is not None:
            keywords_list = item.get("keywords", [])
            keywords_str = ", ".join(keywords_list) if keywords_list else None
            text_to_embed = f"PRODUTO PRINCIPAL: {item.get('name')}. DESCRIÇÃO E INGREDIENTES: {item.get('description', 'N/A')}. PALAVRAS-CHAVE: {keywords_str or 'N/A'}."
            embedding_vector = generate_embedding(text_to_embed)
            new_product = Product(
                bot_id=bot_id, name=item.get("name"), description=item.get("description"),
                price=float(item.get("price")), embedding=embedding_vector, keywords=keywords_str
            )
            products_to_add.append(new_product)
            
    if products_to_add:
        session.add_all(products_to_add)
        await session.commit()
        
    return len(products_to_add)

async def get_product_by_id(session: AsyncSession, product_id: int) -> Optional[Product]:
    query = select(Product).where(Product.id == product_id).options(selectinload(Product.bot))
    result = await session.execute(query)
    return result.scalars().first()

async def bulk_delete_products(session: AsyncSession, user_id: int, product_ids: List[int]) -> int:
    # Esta função já estava correta, pois opera com IDs.
    subquery = select(Bot.id).where(Bot.user_id == user_id)
    query = select(Product.id).where(Product.bot_id.in_(subquery)).where(Product.id.in_(product_ids))
    result = await session.execute(query)
    ids_to_delete = result.scalars().all()
    
    if not ids_to_delete: 
        return 0
        
    delete_statement = delete(Product).where(Product.id.in_(ids_to_delete))
    await session.execute(delete_statement)
    await session.commit()
    return len(ids_to_delete)

async def delete_product(session: AsyncSession, product_id: int) -> bool:
    """CORREÇÃO: Recebe product_id e busca o produto para deletar."""
    db_product = await session.get(Product, product_id)
    if not db_product:
        return False
        
    await session.delete(db_product)
    await session.commit()
    return True

async def create_order(session: AsyncSession, bot_id: int, items: List[Dict]) -> Optional[Order]:
    try:
        total_amount = 0.0
        order_items_to_create = []
        for item_data in items:
            product = await session.get(Product, item_data["product_id"])
            if not product:
                raise ValueError(f"Product with id {item_data['product_id']} not found.")
            
            price = product.price
            quantity = item_data["quantity"]
            total_amount += price * quantity
            order_items_to_create.append(
                OrderItem(product_id=product.id, quantity=quantity, price_at_time_of_order=price)
            )
            
        new_order = Order(bot_id=bot_id, total_amount=round(total_amount, 2), items=order_items_to_create)
        session.add(new_order)
        await session.commit()
        await session.refresh(new_order, attribute_names=["items"]) # Garante que os itens sejam carregados
        return new_order
    except Exception:
        await session.rollback()
        return None