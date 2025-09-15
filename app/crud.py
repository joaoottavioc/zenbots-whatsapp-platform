# app/crud.py - VERSÃO CORRIGIDA
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select, delete
from sqlalchemy.orm import selectinload
from typing import List, Optional, Dict
from datetime import datetime
from app.utils import normalize_phone
from sqlalchemy import text

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
    #contact_number = normalize_phone(contact_number)
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
    cart = await session.get(ShoppingCart, cart_id, options=[selectinload(ShoppingCart.items)])
    if not cart:
        return None

    for item in cart.items:
        await session.delete(item)
    
    cart.items = [] # Limpa a lista na memória também
    cart.state = "GREETING"
    
    # A linha que definia 'last_activity_at' foi REMOVIDA.
    # A responsabilidade de atualizar o timestamp é da função principal que orquestra a conversa.
    
     # ▼▼▼ higiene extra (importante!)
    cart.pending_action_tool = None
    cart.pending_action_args = None
    cart.pending_action_question = None
    cart.pending_action_expires_at = None

    # se você usa last_suggestions, pode limpar também:
    cart.last_suggestions = None
    
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

async def find_relevant_products(session: AsyncSession, bot_id: int, extracted_items: List[str], limit_per_item: int = 3) -> List[Product]:
    """
    Busca em camadas otimizada: 1. Nome > 2. Keywords > 3. Descrição > 4. Embedding.
    """
    if not extracted_items:
        return []

    # Usamos um dicionário para evitar produtos duplicados nos resultados
    all_results_map: Dict[int, Product] = {}

    for item_name in extracted_items:
        # Pula para o próximo item se já atingimos o limite de resultados
        if len(all_results_map) >= limit_per_item * len(extracted_items):
            break

        # 🔹 1. Busca por nome (correspondência exata/parcial forte)
        name_query = select(Product).where(
            Product.bot_id == bot_id,
            Product.name.ilike(f"%{item_name}%")
        ).limit(limit_per_item)
        for p in (await session.execute(name_query)).scalars().all():
            if p.id not in all_results_map: all_results_map[p.id] = p

        # 🔹 2. Busca por keywords (nova camada super importante!)
        keywords_query = select(Product).where(
            Product.bot_id == bot_id,
            Product.keywords.ilike(f"%{item_name}%")
        ).limit(limit_per_item)
        for p in (await session.execute(keywords_query)).scalars().all():
            if p.id not in all_results_map: all_results_map[p.id] = p

        # 🔹 3. Busca por descrição
        desc_query = select(Product).where(
            Product.bot_id == bot_id,
            Product.description.is_not(None),
            Product.description.ilike(f"%{item_name}%")
        ).limit(limit_per_item)
        for p in (await session.execute(desc_query)).scalars().all():
            if p.id not in all_results_map: all_results_map[p.id] = p
            
        # 🔹 4. Fallback: busca semântica (RAG)
        query_embedding = generate_embedding(f"PRODUTO PRINCIPAL: {item_name}") #
        embedding_query = select(Product).where(Product.bot_id == bot_id).order_by(
            Product.embedding.cosine_distance(query_embedding) #
        ).limit(limit_per_item) #
        for p in (await session.execute(embedding_query)).scalars().all():
             if p.id not in all_results_map: all_results_map[p.id] = p

    # Retorna apenas os valores do dicionário, garantindo produtos únicos
    return list(all_results_map.values())

async def find_relevant_products_old(
    session: AsyncSession, 
    bot_id: int, 
    extracted_items: List[str], 
    limit_per_item: int = 3,
    min_similarity: float = 0.55  # <-- NOSSO NOVO CONTROLE DE QUALIDADE!
) -> List[Product]:
    """
    Busca em camadas otimizada com threshold de similaridade semântica.
    1. Nome > 2. Keywords > 3. Descrição (matches de alta confiança)
    4. Embedding (somente se a similaridade for > min_similarity)
    """
    if not extracted_items:
        return []

    all_results_map: Dict[int, Product] = {}

    for item_name in extracted_items:
        # Camadas 1, 2 e 3 (buscas por texto) continuam iguais, pois são de alta confiança.
        # 🔹 1. Busca por nome
        name_query = select(Product).where(
            Product.bot_id == bot_id,
            Product.name.ilike(f"%{item_name}%")
        ).limit(limit_per_item)
        for p in (await session.execute(name_query)).scalars().all():
            if p.id not in all_results_map: all_results_map[p.id] = p

        # 🔹 2. Busca por keywords
        keywords_query = select(Product).where(
            Product.bot_id == bot_id,
            Product.keywords.ilike(f"%{item_name}%")
        ).limit(limit_per_item)
        for p in (await session.execute(keywords_query)).scalars().all():
            if p.id not in all_results_map: all_results_map[p.id] = p

        # 🔹 3. Busca por descrição
        desc_query = select(Product).where(
            Product.bot_id == bot_id,
            Product.description.is_not(None),
            Product.description.ilike(f"%{item_name}%")
        ).limit(limit_per_item)
        for p in (await session.execute(desc_query)).scalars().all():
            if p.id not in all_results_map: all_results_map[p.id] = p
            
        # ▼▼▼ A MÁGICA ACONTECE AQUI ▼▼▼
        # 🔹 4. Fallback: busca semântica (RAG) com filtro de qualidade
        query_embedding = generate_embedding(item_name)
        
        # Criamos uma "coluna" virtual com o score de similaridade
        # Lembre-se: Similaridade = 1 - Distância
        similarity_score = (1 - Product.embedding.cosine_distance(query_embedding)).label("similarity")

        embedding_query = (
            select(Product, similarity_score)
            .where(Product.bot_id == bot_id)
            .filter(similarity_score > min_similarity) # <-- FILTRA PELA QUALIDADE MÍNIMA
            .order_by(text("similarity DESC")) # <-- Ordena pela maior similaridade
            .limit(limit_per_item)
        )
        
        # O resultado agora vem como uma tupla (Produto, similaridade)
        embedding_results = await session.execute(embedding_query)
        for product, similarity in embedding_results.all():
            print(f"[DEBUG RAG] Item encontrado: '{product.name}' com similaridade: {similarity:.2f}")
            if product.id not in all_results_map:
                all_results_map[product.id] = product
    
    return list(all_results_map.values())

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
    # A linha abaixo é a chave da correção.
    # Em vez de apenas dar refresh, nós re-buscamos o bot usando a função
    # que já faz o 'selectinload' das relações 'products' e 'history'.
    return await get_bot_by_id(session, bot_id=new_bot.id)

async def get_bot_by_id(session: AsyncSession, bot_id: int) -> Optional[Bot]:
    query = select(Bot).where(Bot.id == bot_id).options(selectinload(Bot.history), selectinload(Bot.products))
    result = await session.execute(query)
    return result.scalars().first()

async def list_user_bots(session: AsyncSession, user_id: int) -> List[Bot]:
    query = select(Bot).where(Bot.user_id == user_id).options(selectinload(Bot.history), selectinload(Bot.products))
    result = await session.execute(query)
    return result.scalars().all()

async def update_bot(session: AsyncSession, bot_id: int, update_data: BotUpdate) -> Optional[Bot]:
    """CORREÇÃO: A assinatura já estava recebendo bot_id, o que é ótimo."""
    db_bot = await session.get(Bot, bot_id)
    if not db_bot:
        return None

    update_data_dict = update_data.model_dump(exclude_unset=True)
    for key, value in update_data_dict.items():
        setattr(db_bot, key, value)
        
    session.add(db_bot)
    await session.commit()
    
    # Chave da correção: Retorna o bot com as relações carregadas para o FastAPI.
    return await get_bot_by_id(session, bot_id=db_bot.id)

async def delete_bot(session: AsyncSession, bot_id: int) -> bool:
    """CORREÇÃO: Recebe bot_id e busca o bot para deletar."""
    db_bot = await session.get(Bot, bot_id)
    if not db_bot:
        return False
        
    await session.delete(db_bot)
    await session.commit()
    return True

async def create_product(session: AsyncSession, bot_id: int, name: str, description: Optional[str], price: float, keywords: Optional[str] = None) -> Product:
    # ▼▼▼ LÓGICA DE EMBEDDING CORRIGIDA ▼▼▼
    text_to_embed = (
        f"PRODUTO PRINCIPAL: {name}. "
        f"DESCRIÇÃO E INGREDIENTES: {description or 'N/A'}. "
        f"CATEGORIAS E TAGS: {keywords or 'N/A'}."
    )
    embedding_vector = generate_embedding(text_to_embed)
    
    # ▼▼▼ Adiciona keywords ao criar o produto ▼▼▼
    new_product = Product(
        bot_id=bot_id, name=name, description=description, 
        price=price, embedding=embedding_vector, keywords=keywords
    )
    session.add(new_product)
    await session.commit()
    await session.refresh(new_product)
    return new_product

async def update_product(session: AsyncSession, product_id: int, update_data: ProductUpdate) -> Optional[Product]:
    db_product = await session.get(Product, product_id)
    if not db_product:
        return None

    update_data_dict = update_data.model_dump(exclude_unset=True)
    needs_re_embedding = False
    for key, value in update_data_dict.items():
        setattr(db_product, key, value)
        # ▼▼▼ Verifica se um campo relevante para o embedding mudou ▼▼▼
        if key in ["name", "description", "keywords"]:
            needs_re_embedding = True
    
    # ▼▼▼ LÓGICA DE RE-EMBEDDING CORRIGIDA ▼▼▼
    if needs_re_embedding:
        text_to_embed = (
            f"PRODUTO PRINCIPAL: {db_product.name}. "
            f"DESCRIÇÃO E INGREDIENTES: {db_product.description or 'N/A'}. "
            f"CATEGORIAS E TAGS: {db_product.keywords or 'N/A'}."
        )
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

async def create_order(
    session: AsyncSession,
    bot_id: int,
    items: List[Dict],
    customer_address: str | None = None,
) -> Optional[Order]:
    """
    Registra um novo pedido e seus itens.

    Args:
        session: sessão assíncrona do SQLAlchemy.
        bot_id: identificador do bot/restaurante.
        items: lista de dicts no formato {"product_id": int, "quantity": int}.
        customer_address: endereço do cliente, se disponível.

    Returns:
        Instância de Order recém-criada ou None em caso de erro.
    """
    try:
        total_amount = 0.0
        order_items_to_create: list[OrderItem] = []

        for item_data in items:
            product = await session.get(Product, item_data["product_id"])
            if product is None:
                raise ValueError(f"Product with id {item_data['product_id']} not found.")

            quantity = item_data["quantity"]
            price = product.price
            total_amount += price * quantity

            order_items_to_create.append(
                OrderItem(
                    product_id=product.id,
                    quantity=quantity,
                    price_at_time_of_order=price,
                )
            )

        new_order = Order(
            bot_id=bot_id,
            total_amount=round(total_amount, 2),
            customer_address=customer_address,
            items=order_items_to_create,
        )

        session.add(new_order)
        await session.commit()
        await session.refresh(new_order, attribute_names=["items"])  # garante lazy-load resolvido
        return new_order

    except (SQLAlchemyError, ValueError) as exc:
        await session.rollback()
        # opcional: faça um logger.error("Erro ao criar pedido", exc_info=True)
        return None

async def save_customer_address(session: AsyncSession, cart_id: int, address: str) -> Optional[ShoppingCart]:
    """
    Atualiza o endereço do cliente no carrinho e cria uma nova ordem no banco.
    """
    cart = await session.get(ShoppingCart, cart_id)
    if not cart:
        return None

    # Cria o pedido vinculado ao bot e ao carrinho
    await session.refresh(cart, attribute_names=["items"])
    bot_id = cart.contact.bot_id  # Assumindo que você tem o relacionamento reverso de cart -> contact -> bot

    # Validação mínima
    if not cart.items or not bot_id:
        return None

    items = [{"product_id": item.product_id, "quantity": item.quantity} for item in cart.items]
    from app.crud import create_order
    await create_order(session, bot_id=bot_id, items=items, customer_address=address)

    return cart
