# app/crud.py - VERSÃO CORRIGIDA
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select, delete
from sqlalchemy.orm import selectinload
from typing import List, Optional, Dict, Any
from datetime import datetime
from app.utils import normalize_phone
from sqlalchemy import text, func, desc
from app.models import Contact, ShoppingCart, Order, OrderStatus

# 1. Imports unificados e limpos
from app.models import (
    Bot, User, ConversationHistory, Product, Order, OrderItem, ProcessedMessage,
    Contact, ShoppingCart, CartItem
)
from app.schemas import BotUpdate, ProductUpdate
from app.embedding_service import generate_embedding, embed_async

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

async def add_items_to_db_cart_old(session: AsyncSession, cart_id: int, items_to_add: List[Dict]) -> Optional[ShoppingCart]:
    """Adiciona ou atualiza itens no carrinho. O commit é feito no final do fluxo."""
    # CORREÇÃO: Busca o carrinho pelo ID dentro da sessão atual
    cart = await session.get(ShoppingCart, cart_id, options=[selectinload(ShoppingCart.items)])
    if not cart:
        return None

    for item_data in items_to_add:
        product_id = item_data.get("product_id")
        quantity_to_add = item_data.get("quantity", 1)

        if not isinstance(quantity_to_add, int) or quantity_to_add <= 0:
            continue

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

async def add_items_to_db_cart(session: AsyncSession, cart_id: int, items_to_add: List[Dict]) -> Optional[ShoppingCart]:
    """Adiciona itens ao carrinho, ignorando produtos indisponíveis."""
    # Carrega o carrinho e seus itens
    cart = await session.get(ShoppingCart, cart_id, options=[selectinload(ShoppingCart.items)])
    if not cart:
        return None

    for item_data in items_to_add:
        product_id = item_data.get("product_id")
        quantity_to_add = item_data.get("quantity", 1)

        if not isinstance(quantity_to_add, int) or quantity_to_add <= 0:
            continue

        # 1. Carrega o produto para verificar disponibilidade
        product = await session.get(Product, product_id)
        
        # 2. SE O PRODUTO NÃO EXISTIR OU ESTIVER INDISPONÍVEL, PULA
        if not product or not product.is_available:
            print(f"🚫 Tentativa de adicionar produto indisponível: {product_id}")
            continue

        existing_item = next((item for item in cart.items if item.product_id == product_id), None)
        
        if existing_item:
            existing_item.quantity += quantity_to_add
        else:
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
    
    cart.customer_address = None
    # A linha que definia 'last_activity_at' foi REMOVIDA.
    # A responsabilidade de atualizar o timestamp é da função principal que orquestra a conversa.
    
     # ▼▼▼ higiene extra (importante!)
    cart.pending_action_tool = None
    cart.pending_action_args = None
    cart.pending_action_question = None
    cart.pending_action_expires_at = None
    cart.delivery_method = None

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
        text_to_embed = f"PRODUTO PRINCIPAL: {item_name}"
        embeddings = await embed_async([text_to_embed], space="products", normalize=False)
        query_embedding = embeddings[0]
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
            Product.is_available == True,
            Product.name.ilike(f"%{item_name}%")
        ).limit(limit_per_item)
        for p in (await session.execute(name_query)).scalars().all():
            if p.id not in all_results_map: all_results_map[p.id] = p

        # 🔹 2. Busca por keywords
        keywords_query = select(Product).where(
            Product.bot_id == bot_id,
            Product.is_available == True,
            Product.keywords.ilike(f"%{item_name}%")
        ).limit(limit_per_item)
        for p in (await session.execute(keywords_query)).scalars().all():
            if p.id not in all_results_map: all_results_map[p.id] = p

        # 🔹 3. Busca por descrição
        desc_query = select(Product).where(
            Product.bot_id == bot_id,
            Product.is_available == True,
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
            .where(Product.bot_id == bot_id,
                   Product.is_available == True)
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

async def create_bot(
    session: AsyncSession, 
    user_id: int, 
    whatsapp_number: str, 
    restaurant_name: Optional[str], 
    pix_key: Optional[str],
    # ▼▼▼ NEW ARGUMENTS ▼▼▼
    whatsapp_token: str = "", 
    phone_number_id: str = "",
    delivery_fee: float = 0.0,
    min_order_value: float = 0.0,
    is_open: bool = True,
    closing_message: Optional[str] = None,
    schedule: Optional[Dict[str, Any]] = None
    # ▲▲▲ END NEW ARGUMENTS ▲▲▲
) -> Optional[Bot]:
    
    # Check if a bot with this number already exists
    existing_bot_result = await session.execute(select(Bot).where(Bot.whatsapp_number == whatsapp_number))
    if existing_bot_result.scalars().first(): 
        return None
    
    # Create the new Bot object with all fields
    new_bot = Bot(
        user_id=user_id, 
        whatsapp_number=whatsapp_number, 
        restaurant_name=restaurant_name, 
        pix_key=pix_key,
        # ▼▼▼ ASSIGN NEW FIELDS ▼▼▼
        whatsapp_token=whatsapp_token,
        phone_number_id=phone_number_id,
        delivery_fee=delivery_fee,
        min_order_value=min_order_value,
        is_open=is_open,
        closing_message=closing_message or "Olá! No momento estamos fechados.",
        schedule=schedule or {}
    )
    
    session.add(new_bot)
    await session.commit()
    
    # Re-fetch to ensure relationships are loaded (consistent with previous logic)
    return await get_bot_by_id(session, bot_id=new_bot.id)

async def get_bot_by_id(session: AsyncSession, bot_id: int) -> Optional[Bot]:
    query = select(Bot).where(Bot.id == bot_id).options(selectinload(Bot.history), selectinload(Bot.products))
    result = await session.execute(query)
    return result.scalars().first()

async def list_user_bots(session: AsyncSession, user_id: int) -> List[Bot]:
    """
    Lista os bots do usuário ordenados por ÚLTIMA ATIVIDADE.
    O bot que teve o pedido mais recente (created_at) aparece no topo.
    Em caso de empate (sem pedidos), mostra os bots criados recentemente primeiro.
    """
    query = (
        select(Bot)
        .outerjoin(Order, Bot.id == Order.bot_id) # Junta com pedidos para poder checar datas
        .where(Bot.user_id == user_id)
        .options(
            selectinload(Bot.history), 
            selectinload(Bot.products)
        )
        .group_by(Bot.id) # Agrupa para calcular o MAX(date)
        .order_by(
            # 1º Critério: Data do pedido mais recente (os NULLs ficam por último automaticamente)
            desc(func.max(Order.created_at)), 
            # 2º Critério: Desempate pelo ID do bot (bots mais novos primeiro)
            desc(Bot.id)
        )
    )
    
    result = await session.execute(query)
    # .unique() é boa prática quando se usa joins que poderiam duplicar linhas, 
    # embora o group_by já trate isso na maioria dos casos.
    return result.unique().scalars().all()

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

async def delete_bot(session: AsyncSession, db_bot: Bot) -> bool:
    """
    Deleta um objeto Bot que já foi buscado e verificado pela rota.
    (Recebe o objeto Bot, não o bot_id)
    """
    if not db_bot:
        return False
        
    await session.delete(db_bot)
    await session.commit()
    return True

async def create_product(
    session: AsyncSession, 
    bot_id: int, 
    name: str, 
    description: Optional[str], 
    price: float, 
    keywords: Optional[str] = None,
    category: str = "Geral" # <--- Novo parâmetro com valor padrão
) -> Product:
    # Incluímos a CATEGORIA no texto do embedding para ajudar na busca semântica
    text_to_embed = (
        f"CATEGORIA: {category}. "
        f"PRODUTO PRINCIPAL: {name}. "
        f"DESCRIÇÃO E INGREDIENTES: {description or 'N/A'}. "
        f"CATEGORIAS E TAGS: {keywords or 'N/A'}."
    )
    embeddings = await embed_async([text_to_embed], space="products", normalize=False)
    embedding_vector = embeddings[0]
    
    new_product = Product(
        bot_id=bot_id, 
        name=name, 
        description=description, 
        price=price, 
        embedding=embedding_vector, 
        keywords=keywords,
        category=category,      # <--- Salva no banco
        is_available=True
    )
    session.add(new_product)
    await session.commit()
    await session.refresh(new_product)
    return new_product

async def update_product(session: AsyncSession, db_product: Product, update_data: ProductUpdate) -> Optional[Product]:
    """
    Atualiza um objeto Product. Se nome, descrição, keywords OU CATEGORIA mudarem,
    o embedding é recalculado.
    """
    if not db_product:
        return None

    update_data_dict = update_data.model_dump(exclude_unset=True)
    needs_re_embedding = False
    
    for key, value in update_data_dict.items():
        setattr(db_product, key, value)
        # Verifica se um campo relevante para o significado do produto mudou
        # Adicionamos "category" a esta lista
        if key in ["name", "description", "keywords", "category"]:
            needs_re_embedding = True
    
    if needs_re_embedding:
        # Recalcula o embedding com os dados novos (incluindo a categoria atualizada)
        text_to_embed = (
            f"CATEGORIA: {db_product.category or 'Geral'}. "
            f"PRODUTO PRINCIPAL: {db_product.name}. "
            f"DESCRIÇÃO E INGREDIENTES: {db_product.description or 'N/A'}. "
            f"CATEGORIAS E TAGS: {db_product.keywords or 'N/A'}."
        )
        embeddings = await embed_async([text_to_embed], space="products", normalize=False)
        db_product.embedding = embeddings[0]
        
    session.add(db_product)
    await session.commit()
    await session.refresh(db_product)
    return db_product

async def bulk_create_products(session: AsyncSession, bot_id: int, products_data: List[Dict]) -> int:
    """
    Substitui o catálogo antigo pelo novo (Delete All -> Create New).
    Limpa os itens dos carrinhos ativos para evitar erro de chave estrangeira.
    """
    
    # --- 1. LIMPEZA PRÉVIA (DELETE ALL) ---
    
    # Busca todos os IDs de produtos que já existem para este bot
    existing_result = await session.execute(select(Product.id).where(Product.bot_id == bot_id))
    existing_ids = existing_result.scalars().all()

    if existing_ids:
        # A. Remove esses produtos de quaisquer carrinhos ativos (CartItem)
        # Isso é CRUCIAL para não dar erro de integridade (Foreign Key)
        await session.execute(
            delete(CartItem).where(CartItem.product_id.in_(existing_ids))
        )
        
        # B. Deleta os produtos em si
        # Nota: Produtos em PEDIDOS fechados (Order) não serão afetados se o delete estiver
        # configurado corretamente, ou podem impedir a deleção. 
        # Se o banco bloquear por causa de pedidos passados, o ideal seria apenas marcar como 
        # is_available=False, mas para um "reset" de cardápio, o delete físico é o esperado.
        # Se der erro aqui, é porque o produto está num Pedido.
        try:
            await session.execute(
                delete(Product).where(Product.id.in_(existing_ids))
            )
        except Exception as e:
            print(f"⚠️ Aviso: Não foi possível deletar alguns produtos antigos (provavelmente vendidos): {e}")
            # Se não der para deletar (ex: histórico de vendas), podemos optar por 
            # apenas arquivá-los ou ignorar o erro e criar os novos assim mesmo.
            # Por segurança neste MVP, vamos seguir criando os novos.

    # --- 2. CRIAÇÃO DOS NOVOS PRODUTOS (Lógica Original) ---
    
    products_to_add = []
    for item in products_data:
        if item.get("name") and item.get("price") is not None:
            keywords_list = item.get("keywords", [])
            keywords_str = ", ".join(keywords_list) if keywords_list else None
            
            category = item.get("category", "Geral")

            text_to_embed = (
                f"CATEGORIA: {category}. "
                f"PRODUTO PRINCIPAL: {item.get('name')}. "
                f"DESCRIÇÃO E INGREDIENTES: {item.get('description', 'N/A')}. "
                f"PALAVRAS-CHAVE: {keywords_str or 'N/A'}."
            )
            embeddings = await embed_async([text_to_embed], space="products", normalize=False)
            embedding_vector = embeddings[0]
            
            new_product = Product(
                bot_id=bot_id, 
                name=item.get("name"), 
                description=item.get("description"),
                price=float(item.get("price")), 
                embedding=embedding_vector, 
                keywords=keywords_str,
                category=category,
                is_available=True
            )
            products_to_add.append(new_product)
            
    if products_to_add:
        session.add_all(products_to_add)
        await session.commit()
        
    return len(products_to_add)

async def get_products_by_bot_id(session: AsyncSession, bot_id: int) -> List[Product]:
    """Busca todos os produtos associados a um bot_id específico."""
    # ANTES: query = select(Product).where(Product.bot_id == bot_id)
    
    # DEPOIS (Correção): Adicionamos order_by(Product.name.asc())
    query = (
        select(Product)
        .where(Product.bot_id == bot_id)
        .order_by(Product.name.asc())  # <--- AQUI ESTÁ A MÁGICA
    )
    result = await session.execute(query)
    return result.scalars().all()

async def get_product_by_id(session: AsyncSession, product_id: int) -> Optional[Product]:
    query = select(Product).where(Product.id == product_id).options(selectinload(Product.bot))
    result = await session.execute(query)
    return result.scalars().first()

async def bulk_delete_products(session: AsyncSession, bot_id: int, product_ids: List[int]) -> int:
    """Deleta produtos em lote, garantindo que eles pertençam ao bot_id especificado."""
    
    # A consulta fica muito mais simples, pois já validamos o dono do bot na rota
    query = select(Product.id).where(
        Product.bot_id == bot_id, 
        Product.id.in_(product_ids)
    )
    result = await session.execute(query)
    ids_to_delete = result.scalars().all()
    
    if not ids_to_delete: 
        return 0
        
    delete_statement = delete(Product).where(Product.id.in_(ids_to_delete))
    await session.execute(delete_statement)
    await session.commit()
    return len(ids_to_delete)

async def delete_product(session: AsyncSession, db_product: Product) -> bool:
    """
    Deleta um objeto Product que já foi buscado e verificado pela rota.
    (Recebe o objeto Product, não o product_id)
    """
    if not db_product:
        return False
        
    await session.delete(db_product)
    await session.commit()
    return True

async def create_order(
    session: AsyncSession,
    bot_id: int,
    items: List[Dict],
    total_amount: float,
    customer_address: str | None = None,
    contact_id: int | None = None # <-- 1. Novo parâmetro
) -> Order | None:
    try:
        order_items_to_create: list[OrderItem] = []
        for item_data in items:
            product = await session.get(Product, item_data["product_id"])
            if not product:
                raise ValueError(f"Produto {item_data['product_id']} não encontrado.")
            order_items_to_create.append(OrderItem(product_id=product.id, quantity=item_data["quantity"], price_at_time_of_order=product.price))

        new_order = Order(
            bot_id=bot_id,
            total_amount=round(total_amount, 2),
            customer_address=customer_address,
            contact_id=contact_id, # <-- 2. Salva o contato
            items=order_items_to_create,
        )

        session.add(new_order)
        await session.commit()
        await session.refresh(new_order, attribute_names=["items"])
        return new_order

    except ValueError as exc:
        await session.rollback()
        print(f"Erro ao criar pedido: {exc}")
        return None

# app/crud.py

async def save_address_to_cart(session: AsyncSession, cart_id: int, address: str) -> Optional[ShoppingCart]:
    """
    Apenas salva o endereço confirmado no objeto do carrinho para uso posterior.
    """
    cart = await session.get(ShoppingCart, cart_id)
    if not cart:
        return None
    
    cart.customer_address = address
    session.add(cart)
    await session.flush()
    return cart

async def save_customer_name_to_contact(session: AsyncSession, contact_id: int, name: str) -> Optional[Contact]:
    """
    Salva ou atualiza o nome de um contato no banco de dados.
    """
    contact = await session.get(Contact, contact_id)
    if not contact:
        return None
    
    contact.name = name
    session.add(contact)
    await session.flush()
    await session.refresh(contact)
    return contact

async def set_human_takeover_by_phone(session: AsyncSession, bot_id: int, phone_number: str, active: bool) -> bool:
    """
    Encontra o carrinho de um cliente pelo número de telefone e ativa/desativa o modo de atendimento humano.
    """
    # Encontra o contato pelo número de telefone para o bot específico
    contact_result = await session.execute(
        select(Contact).where(Contact.phone_number == phone_number, Contact.bot_id == bot_id)
    )
    contact = contact_result.scalar_one_or_none()

    if not contact:
        print(f"Contato com o número {phone_number} não encontrado para o bot {bot_id}.")
        return False

    # Encontra o carrinho associado a esse contato
    cart_result = await session.execute(
        select(ShoppingCart).where(ShoppingCart.contact_id == contact.id)
    )
    cart = cart_result.scalar_one_or_none()

    if not cart:
        print(f"Carrinho para o contato {contact.id} não encontrado.")
        return False
    
    # Ativa ou desativa o interruptor
    cart.human_takeover_active = active
    session.add(cart)
    await session.commit()
    
    status = "ATIVADO" if active else "DESATIVADO"
    print(f"✅ Atendimento humano {status} para o número {phone_number}.")
    return True

async def delete_order(session: AsyncSession, order_id: int) -> bool:
    """
    Encontra e deleta um pedido pelo seu ID.
    Útil para reverter a criação de um pedido se o pagamento falhar.
    """
    order_to_delete = await session.get(Order, order_id)
    
    if not order_to_delete:
        print(f"Pedido com ID {order_id} não encontrado para deleção.")
        return False
        
    await session.delete(order_to_delete)
    await session.commit()
    print(f"Pedido {order_id} deletado com sucesso.")
    return True

async def update_order_status_by_id(session: AsyncSession, order_id: int, new_status: OrderStatus, psp_charge_id: Optional[str] = None) -> Optional[Order]:
    """
    Encontra um pedido pelo ID, atualiza seu status e o psp_charge_id.
    Retorna o objeto Order com o 'contact' e 'bot' carregados se for bem-sucedido
    e o status tiver sido realmente alterado.
    """
    # Usamos selectinload para já carregar os dados do contato e do bot
    query = (
        select(Order)
        .where(Order.id == order_id)
        .options(selectinload(Order.bot), selectinload(Order.items).selectinload(OrderItem.product))
    )
    result = await session.execute(query)
    order = result.scalars().first()
    
    if not order:
        print(f"Pedido {order_id} não encontrado para atualização de status.")
        return None
    
    # IMPORTANTE: Proteção de idempotência
    # Se o status já for o final, não fazemos nada e não retornamos o pedido.
    # Isso evita enviar 5 confirmações para o cliente se o MP enviar 5 webhooks.
    if order.status == new_status or order.status in [OrderStatus.FAILED, OrderStatus.EXPIRED]:
        print(f"Pedido {order_id} já está em estado final ({order.status}). Ignorando atualização.")
        return None
    
    order.status = new_status
    if psp_charge_id:
        order.psp_charge_id = psp_charge_id
    
    session.add(order)
    await session.commit()
    await session.refresh(order, attribute_names=["bot", "items"]) # Garante que os dados carregados estão frescos
    
    print(f"✅ Pedido {order_id} atualizado para {new_status}.")
    return order

async def list_orders_by_bot_old(session: AsyncSession, bot_id: int, status_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Busca pedidos de um bot.
    Retorna uma lista de DICIONÁRIOS para incluir o campo calculado 'display_items'.
    """
    query = (
        select(Order)
        .where(Order.bot_id == bot_id)
        .options(selectinload(Order.items).selectinload(OrderItem.product))
        .order_by(Order.created_at.desc())
    )
    
    if status_filter:
        query = query.where(Order.status == status_filter)
        
    result = await session.execute(query)
    orders = result.scalars().all()
    
    # ▼▼▼ A CORREÇÃO ESTÁ AQUI ▼▼▼
    # Em vez de modificar o objeto 'order', criamos dicionários
    formatted_orders = []
    for order in orders:
        # 1. Converte o objeto do banco para um dicionário Python simples
        order_dict = order.model_dump()
        
        # 2. Agora podemos adicionar campos extras sem erro
        order_dict["display_items"] = [
            {"quantity": item.quantity, "product_name": item.product.name} 
            for item in order.items
        ]
        formatted_orders.append(order_dict)
        
    return formatted_orders

async def list_orders_by_bot(session: AsyncSession, bot_id: int, status_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    query = (
        select(Order)
        .where(Order.bot_id == bot_id)
        # Agora podemos carregar o contato, pois o relacionamento existe!
        .options(
            selectinload(Order.items).selectinload(OrderItem.product),
            selectinload(Order.contact).selectinload(Contact.cart) 
        )
        .order_by(Order.created_at.desc())
    )
    
    if status_filter:
        query = query.where(Order.status == status_filter)
        
    result = await session.execute(query)
    orders = result.scalars().all()
    
    formatted_orders = []
    for order in orders:
        order_dict = order.model_dump()
        
        order_dict["display_items"] = [
            {"quantity": item.quantity, "product_name": item.product.name} 
            for item in order.items
        ]
        
        # ▼▼▼ CORREÇÃO AQUI ▼▼▼
        if order.contact:
            order_dict["customer_phone"] = order.contact.phone_number
            # Adicionamos explicitamente o nome aqui:
            order_dict["customer_name"] = order.contact.name 
            
            if order.contact.cart:
                order_dict["human_takeover_active"] = order.contact.cart.human_takeover_active
            else:
                order_dict["human_takeover_active"] = False
        else:
             order_dict["customer_phone"] = "Desconhecido"
             order_dict["customer_name"] = None # Garante que a chave exista
             order_dict["human_takeover_active"] = False
        # ▲▲▲ FIM DA CORREÇÃO ▲▲▲
        
        formatted_orders.append(order_dict)
        
    return formatted_orders

async def get_top_selling_products(session: AsyncSession, bot_id: int, limit: int = 5):
    # --- DEBUG: Adicione isto ---
    print(f"🔍 DEBUG ANALYTICS: Buscando dados para o Bot ID: {bot_id}")
    
    # Verifica se existem pedidos para este bot, independente dos produtos
    check_query = select(func.count(Order.id)).where(Order.bot_id == bot_id)
    total_orders = (await session.execute(check_query)).scalar()
    print(f"📊 DEBUG ANALYTICS: O Bot {bot_id} tem um total de {total_orders} pedidos no banco.")
    # -----------------------------

    query = (
        select(
            Product.name, 
            func.sum(OrderItem.quantity).label("total_sold"),
            func.sum(OrderItem.quantity * OrderItem.price_at_time_of_order).label("total_revenue")
        )
        .join(OrderItem, Product.id == OrderItem.product_id)
        .join(Order, OrderItem.order_id == Order.id)
        .where(Order.bot_id == bot_id) 
        # .where(Order.status == 'COMPLETED') 
        .group_by(Product.id, Product.name)
        .order_by(desc("total_sold"))
        .limit(limit)
    )
    
    result = await session.execute(query)
    final_list = result.all()
    print(f"📈 DEBUG ANALYTICS: Resultado final da query: {len(final_list)} produtos encontrados.")
    
    return final_list