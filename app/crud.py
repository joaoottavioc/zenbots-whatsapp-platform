# app/crud.py - VERSÃO CORRIGIDA
import logging
import re
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select, update
from sqlalchemy.orm import selectinload
from typing import List, Optional, Dict, Any
from datetime import timedelta
from app.utils import normalize_phone, mask_phone
from app.time import utcnow, current_brt_year_month
from sqlalchemy import func, desc, text
from sqlalchemy.exc import IntegrityError
from app.models import (
    Bot,
    User,
    ConversationHistory,
    Product,
    Order,
    OrderItem,
    ProcessedMessage,
    Contact,
    ShoppingCart,
    CartItem,
    Subscription,
    Plan,
    DeliveryMethod,
    OrderStatus,
    CartState,
    BotMonthlyUsage,
)
from app.schemas import BotCreate, BotUpdate, ProductUpdate
from app.embedding_service import embed_async
from app.encryption import encrypt_value

logger = logging.getLogger(__name__)


def escape_ilike(value: str) -> str:
    """Escape ILIKE wildcards (\\, %, _) so user input is matched literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --- Funções do Webhook (NÃO DEVEM FAZER COMMIT) ---


async def get_bot_by_number(
    session: AsyncSession, whatsapp_number: str
) -> Optional[Bot]:
    """Busca um bot pelo número. Usado internamente pelo webhook."""
    result = await session.execute(
        select(Bot).where(Bot.whatsapp_number == whatsapp_number)
    )
    return result.scalars().first()


async def is_message_processed(session: AsyncSession, message_id: str) -> bool:
    result = await session.execute(
        select(ProcessedMessage).where(ProcessedMessage.message_id == message_id)
    )
    return result.scalar_one_or_none() is not None


async def add_processed_message(session: AsyncSession, message_id: str):
    processed_message = ProcessedMessage(message_id=message_id)
    session.add(processed_message)


async def get_or_create_contact(
    session: AsyncSession,
    bot_id: int,
    contact_number: str,
    *,
    channel: str = "whatsapp",
) -> Contact:
    """Busca um contato pelo número ou o cria, sem commitar a sessão.

    For channel='whatsapp' (default), `contact_number` is normalized to
    a digits-only string (existing behavior preserved).

    For channel='web' (plan/in_browser_bots.md A1b), `contact_number` is
    the synthesized identity `web:{session_id}` produced by
    `_parse_web_payload`. Normalization is SKIPPED because the identity
    is not a phone number — the colon and "web" prefix are load-bearing.
    """
    if channel == "whatsapp":
        contact_number = normalize_phone(contact_number) or re.sub(
            r"[^\d]", "", contact_number
        )
    # else: channel='web' — use the synthesized identity verbatim.

    result = await session.execute(
        select(Contact).where(
            Contact.phone_number == contact_number, Contact.bot_id == bot_id
        )
    )
    contact = result.scalar_one_or_none()

    if not contact:
        contact = Contact(
            phone_number=contact_number,
            bot_id=bot_id,
            channel=channel,
        )
        session.add(contact)
        try:
            await session.flush()
        except IntegrityError:
            # Race condition: another request created the same contact concurrently.
            # Roll back the failed INSERT and fetch the existing record.
            await session.rollback()
            result = await session.execute(
                select(Contact).where(
                    Contact.phone_number == contact_number, Contact.bot_id == bot_id
                )
            )
            contact = result.scalar_one_or_none()
            if not contact:
                raise
        else:
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
        cart = ShoppingCart(contact_id=contact_id, state=CartState.GREETING)
        session.add(cart)
        await session.flush()
        await session.refresh(cart)
    return cart


async def add_items_to_db_cart(
    session: AsyncSession,
    cart_id: int,
    items_to_add: List[Dict],
    bot_id: int | None = None,
    skipped_items: List[str] | None = None,
) -> Optional[ShoppingCart]:
    """Adiciona itens ao carrinho, reportando produtos indisponíveis via skipped_items."""
    # Carrega o carrinho e seus itens
    cart = await session.get(
        ShoppingCart, cart_id, options=[selectinload(ShoppingCart.items)]
    )
    if not cart:
        return None

    for item_data in items_to_add:
        product_id = item_data.get("product_id")
        quantity_to_add = item_data.get("quantity", 1)
        notes_to_add = item_data.get("notes")

        if not isinstance(quantity_to_add, int) or quantity_to_add <= 0:
            continue

        # 1. Carrega o produto para verificar disponibilidade
        product = await session.get(Product, product_id)

        # 2. SE O PRODUTO NÃO EXISTIR, ESTIVER DELETADO OU INDISPONÍVEL, PULA
        if not product or product.is_deleted:
            # Deleted or non-existent products are silently skipped — no user-facing message.
            # These are LLM hallucinations, not genuine unavailability.
            logger.warning(
                "Attempt to add non-existent/deleted product: %s", product_id
            )
            continue
        if not product.is_available:
            logger.warning("Attempt to add unavailable product: %s", product_id)
            if skipped_items is not None:
                skipped_items.append(product.name)
            continue

        # 3. SE bot_id FORNECIDO, VERIFICA SE O PRODUTO PERTENCE AO BOT CORRETO
        if bot_id is not None and product.bot_id != bot_id:
            logger.warning(
                "Product %s belongs to bot %s, expected bot %s",
                product_id,
                product.bot_id,
                bot_id,
            )
            if skipped_items is not None:
                skipped_items.append(product.name)
            continue

        existing_item = next(
            (item for item in cart.items if item.product_id == product_id), None
        )

        if existing_item:
            existing_item.quantity += quantity_to_add
            if notes_to_add:
                existing_item.notes = notes_to_add
        else:
            new_item = CartItem(
                product_id=product_id,
                quantity=quantity_to_add,
                cart_id=cart.id,
                notes=notes_to_add,
            )
            session.add(new_item)

    await session.flush()
    return cart


async def modify_item_quantity_in_db_cart(
    session: AsyncSession, cart_id: int, product_id: int, new_quantity: int
) -> Optional[ShoppingCart]:
    """Modifica a quantidade de um item ou o remove. O commit é feito no final do fluxo."""
    # CORREÇÃO: Busca o carrinho pelo ID dentro da sessão atual
    cart = await session.get(
        ShoppingCart, cart_id, options=[selectinload(ShoppingCart.items)]
    )
    if not cart:
        return None

    item_to_modify = next(
        (item for item in cart.items if item.product_id == product_id), None
    )
    if item_to_modify:
        if new_quantity > 0:
            item_to_modify.quantity = new_quantity
        else:
            await session.delete(item_to_modify)
    await session.flush()
    return cart


async def clear_db_cart(session: AsyncSession, cart_id: int) -> Optional[ShoppingCart]:
    """Limpa todos os itens de um carrinho e reseta o seu estado."""
    cart = await session.get(
        ShoppingCart, cart_id, options=[selectinload(ShoppingCart.items)]
    )
    if not cart:
        return None

    for item in cart.items:
        await session.delete(item)

    cart.items = []  # Limpa a lista na memória também
    cart.state = CartState.GREETING

    cart.customer_address = None
    # A linha que definia 'last_activity_at' foi REMOVIDA.
    # A responsabilidade de atualizar o timestamp é da função principal que orquestra a conversa.

    # ▼▼▼ higiene extra (importante!)
    cart.pending_action_tool = None
    cart.pending_action_args = None
    cart.pending_action_question = None
    cart.pending_action_expires_at = None
    cart.delivery_method = None
    cart.pix_only = False

    # se você usa last_suggestions, pode limpar também:
    cart.last_suggestions = None

    await session.flush()
    return cart


async def get_history_for_contact(
    session: AsyncSession, bot_id: int, contact_number: str, limit: int = 20
) -> List[ConversationHistory]:
    """Recupera o histórico de uma conversa. Retorna [] se o contato não existir."""
    contact_number = normalize_phone(contact_number) or re.sub(
        r"[^\d]", "", contact_number
    )
    result = await session.execute(
        select(Contact).where(
            Contact.phone_number == contact_number, Contact.bot_id == bot_id
        )
    )
    contact = result.scalar_one_or_none()
    if not contact:
        return []
    # Session TTL: only include messages from the last 30 minutes
    session_cutoff = utcnow() - timedelta(minutes=30)
    query = (
        select(ConversationHistory)
        .where(
            ConversationHistory.contact_id == contact.id,
            ConversationHistory.created_at >= session_cutoff,
        )
        .order_by(ConversationHistory.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(query)
    # Retorna as mensagens em ordem cronológica (a mais antiga primeiro)
    return result.scalars().all()[::-1]


async def add_interaction_to_history(
    session: AsyncSession,
    bot_id: int,
    contact_number: str,
    user_content: str,
    assistant_content: str,
):
    """Salva a interação completa, ligando-a ao objeto Contact correto."""
    contact = await get_or_create_contact(session, bot_id, contact_number)
    user_entry = ConversationHistory(
        bot_id=bot_id, contact_id=contact.id, role="user", content=user_content
    )
    assistant_entry = ConversationHistory(
        bot_id=bot_id,
        contact_id=contact.id,
        role="assistant",
        content=assistant_content,
    )
    session.add_all([user_entry, assistant_entry])
    await session.flush()


async def find_relevant_products(
    session: AsyncSession,
    bot_id: int,
    extracted_items: List[str],
    limit_per_item: int = 3,
) -> List[Product]:
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

        # Track match quality per layer to gate weaker layers properly.
        # Strong layers: name, split-word, keywords (high precision).
        # Weak layers: pg_trgm, description, embedding (broader, more false positives).
        _found_for_item = False

        # 🔹 Layer 1. Name ILIKE (strongest — exact/partial substring match)
        name_query = (
            select(Product)
            .where(
                Product.bot_id == bot_id,
                Product.is_available == True,
                Product.is_deleted == False,
                Product.name.ilike(f"%{escape_ilike(item_name)}%", escape="\\"),
            )
            .limit(limit_per_item)
        )
        for p in (await session.execute(name_query)).scalars().all():
            if p.id not in all_results_map:
                all_results_map[p.id] = p
                _found_for_item = True

        # 🔹 Layer 2. Split-word name ILIKE (only if full-phrase missed)
        # "johs calabresa" fails full ILIKE but "calabresa" alone matches
        if not _found_for_item:
            words = [w for w in item_name.split() if len(w) > 3]
            for word in words:
                word_query = (
                    select(Product)
                    .where(
                        Product.bot_id == bot_id,
                        Product.is_available == True,
                        Product.is_deleted == False,
                        Product.name.ilike(f"%{escape_ilike(word)}%", escape="\\"),
                    )
                    .limit(limit_per_item)
                )
                for p in (await session.execute(word_query)).scalars().all():
                    if p.id not in all_results_map:
                        all_results_map[p.id] = p
                        _found_for_item = True

        # 🔹 Layer 3. Keywords ILIKE (strong — curated search terms)
        keywords_query = (
            select(Product)
            .where(
                Product.bot_id == bot_id,
                Product.is_available == True,
                Product.is_deleted == False,
                Product.keywords.ilike(f"%{escape_ilike(item_name)}%", escape="\\"),
            )
            .limit(limit_per_item)
        )
        for p in (await session.execute(keywords_query)).scalars().all():
            if p.id not in all_results_map:
                all_results_map[p.id] = p
                _found_for_item = True

        # 🔹 Layer 4. pg_trgm fuzzy name match (typos: "burguer"→"burger")
        # Gated on strong layers only (name + split-word + keywords).
        # Runs BEFORE description because a fuzzy name match is more relevant
        # than a substring match buried in a description.
        if not _found_for_item:
            _TRGM_THRESHOLD = 0.3
            trgm_query = (
                select(Product)
                .where(
                    Product.bot_id == bot_id,
                    Product.is_available == True,
                    Product.is_deleted == False,
                    func.similarity(Product.name, item_name) > _TRGM_THRESHOLD,
                )
                .order_by(func.similarity(Product.name, item_name).desc())
                .limit(limit_per_item)
            )
            try:
                # Use SAVEPOINT so a failure only rolls back this query,
                # not the outer transaction (which would expire all ORM objects).
                async with session.begin_nested():
                    for p in (await session.execute(trgm_query)).scalars().all():
                        if p.id not in all_results_map:
                            all_results_map[p.id] = p
                            _found_for_item = True
            except Exception:
                # pg_trgm extension not available — SAVEPOINT was rolled back
                # automatically, outer transaction is still healthy.
                logger.debug("pg_trgm similarity search unavailable, skipping")

        # 🔹 Layer 5. Description ILIKE (weak — substring in description text)
        # Only runs if stronger layers found nothing.
        if not _found_for_item:
            desc_query = (
                select(Product)
                .where(
                    Product.bot_id == bot_id,
                    Product.is_available == True,
                    Product.is_deleted == False,
                    Product.description.is_not(None),
                    Product.description.ilike(
                        f"%{escape_ilike(item_name)}%", escape="\\"
                    ),
                )
                .limit(limit_per_item)
            )
            for p in (await session.execute(desc_query)).scalars().all():
                if p.id not in all_results_map:
                    all_results_map[p.id] = p
                    _found_for_item = True

        # 🔹 Layer 6. Embedding (weakest — semantic similarity fallback)
        # Only runs if all other layers found nothing.
        if not _found_for_item:
            _EMBEDDING_MAX_DISTANCE = 0.3
            text_to_embed = f"PRODUTO PRINCIPAL: {item_name}"
            try:
                from app.embedding_service import EmbeddingsUnavailable

                embeddings = await embed_async(
                    [text_to_embed], space="products", normalize=False
                )
                query_embedding = embeddings[0]
                embedding_query = (
                    select(Product)
                    .where(
                        Product.bot_id == bot_id,
                        Product.is_available == True,
                        Product.is_deleted == False,
                        Product.embedding.cosine_distance(query_embedding)
                        < _EMBEDDING_MAX_DISTANCE,
                    )
                    .order_by(Product.embedding.cosine_distance(query_embedding))
                    .limit(limit_per_item)
                )
                for p in (await session.execute(embedding_query)).scalars().all():
                    if p.id not in all_results_map:
                        all_results_map[p.id] = p
            except EmbeddingsUnavailable:
                # Embedding model unavailable (HF rate limit, network outage, etc.)
                # Skip the semantic layer — keyword/ILIKE results from layers 1-5
                # are already collected and that's what we'll return.
                pass

    # Retorna apenas os valores do dicionário, garantindo produtos únicos
    return list(all_results_map.values())


async def find_unavailable_products(
    session: AsyncSession,
    bot_id: int,
    extracted_items: List[str],
    limit: int = 3,
) -> List[Product]:
    """
    Check if any of the searched items match products that exist but are unavailable.
    Same 6-layer architecture as find_relevant_products, but filters is_available=False.
    Apostrophe-normalized on name/keywords so "johns" matches "John's".
    """
    if not extracted_items:
        return []

    results_map: Dict[int, Product] = {}

    for item_name in extracted_items:
        _found_for_item = False
        # Normalize: strip apostrophes so "johns bacon" matches "John's Bacon"
        clean_name = item_name.replace("'", "").replace("\u2019", "")
        escaped = escape_ilike(clean_name)

        # 🔹 Layer 1. Name ILIKE (apostrophe-normalized)
        name_query = (
            select(Product)
            .where(
                Product.bot_id == bot_id,
                Product.is_available == False,
                Product.is_deleted == False,
                func.replace(Product.name, "'", "").ilike(f"%{escaped}%", escape="\\"),
            )
            .limit(limit)
        )
        for p in (await session.execute(name_query)).scalars().all():
            if p.id not in results_map:
                results_map[p.id] = p
                _found_for_item = True

        # 🔹 Layer 2. Split-word name ILIKE (only if full-phrase missed)
        if not _found_for_item:
            words = [w for w in clean_name.split() if len(w) > 3]
            for word in words:
                word_escaped = escape_ilike(word)
                word_query = (
                    select(Product)
                    .where(
                        Product.bot_id == bot_id,
                        Product.is_available == False,
                        Product.is_deleted == False,
                        func.replace(Product.name, "'", "").ilike(
                            f"%{word_escaped}%", escape="\\"
                        ),
                    )
                    .limit(limit)
                )
                for p in (await session.execute(word_query)).scalars().all():
                    if p.id not in results_map:
                        results_map[p.id] = p
                        _found_for_item = True

        # 🔹 Layer 3. Keywords ILIKE (apostrophe-normalized)
        kw_query = (
            select(Product)
            .where(
                Product.bot_id == bot_id,
                Product.is_available == False,
                Product.is_deleted == False,
                func.replace(Product.keywords, "'", "").ilike(
                    f"%{escaped}%", escape="\\"
                ),
            )
            .limit(limit)
        )
        for p in (await session.execute(kw_query)).scalars().all():
            if p.id not in results_map:
                results_map[p.id] = p
                _found_for_item = True

        # 🔹 Layer 4. pg_trgm fuzzy name match
        if not _found_for_item:
            _TRGM_THRESHOLD = 0.3
            trgm_query = (
                select(Product)
                .where(
                    Product.bot_id == bot_id,
                    Product.is_available == False,
                    Product.is_deleted == False,
                    func.similarity(Product.name, item_name) > _TRGM_THRESHOLD,
                )
                .order_by(func.similarity(Product.name, item_name).desc())
                .limit(limit)
            )
            try:
                async with session.begin_nested():
                    for p in (await session.execute(trgm_query)).scalars().all():
                        if p.id not in results_map:
                            results_map[p.id] = p
                            _found_for_item = True
            except Exception:
                logger.debug("pg_trgm similarity search unavailable, skipping")

        # 🔹 Layer 5. Description ILIKE
        if not _found_for_item:
            desc_query = (
                select(Product)
                .where(
                    Product.bot_id == bot_id,
                    Product.is_available == False,
                    Product.is_deleted == False,
                    Product.description.is_not(None),
                    Product.description.ilike(f"%{escaped}%", escape="\\"),
                )
                .limit(limit)
            )
            for p in (await session.execute(desc_query)).scalars().all():
                if p.id not in results_map:
                    results_map[p.id] = p
                    _found_for_item = True

        # 🔹 Layer 6. Embedding (with threshold to prevent phantom matches)
        if not _found_for_item:
            _UNAVAIL_EMBEDDING_MAX_DISTANCE = 0.35
            text_to_embed = f"PRODUTO PRINCIPAL: {item_name}"
            try:
                from app.embedding_service import EmbeddingsUnavailable

                embeddings = await embed_async(
                    [text_to_embed], space="products", normalize=False
                )
                query_embedding = embeddings[0]
                emb_query = (
                    select(Product)
                    .where(
                        Product.bot_id == bot_id,
                        Product.is_available == False,
                        Product.is_deleted == False,
                        Product.embedding.cosine_distance(query_embedding)
                        < _UNAVAIL_EMBEDDING_MAX_DISTANCE,
                    )
                    .order_by(Product.embedding.cosine_distance(query_embedding))
                    .limit(limit)
                )
                for p in (await session.execute(emb_query)).scalars().all():
                    if p.id not in results_map:
                        results_map[p.id] = p
            except EmbeddingsUnavailable:
                # Same graceful-degrade pattern as find_relevant_products: skip
                # the semantic layer when the model is unavailable.
                pass

    return list(results_map.values())


async def get_user_by_email(session: AsyncSession, email: str) -> Optional[User]:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalars().first()


async def get_user_by_id(session: AsyncSession, user_id: int) -> Optional[User]:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalars().first()


async def create_bot(
    session: AsyncSession,
    user_id: int,
    bot_data: BotCreate,
) -> Optional[Bot]:

    # Check if a bot with this number already exists (skip if no number yet)
    if bot_data.whatsapp_number:
        existing_bot_result = await session.execute(
            select(Bot).where(Bot.whatsapp_number == bot_data.whatsapp_number)
        )
        if existing_bot_result.scalars().first():
            return None

    # Build bot from all schema fields
    data = bot_data.dict(exclude_unset=False)
    # Encrypt token before storing
    token = data.pop("whatsapp_token", None)
    data["whatsapp_token"] = encrypt_value(token) if token else ""
    data["phone_number_id"] = data.get("phone_number_id") or ""
    data["closing_message"] = (
        data.get("closing_message") or "Olá! No momento estamos fechados."
    )
    data["schedule"] = data.get("schedule") or {}

    new_bot = Bot(user_id=user_id, **data)

    session.add(new_bot)
    await session.commit()

    # Re-fetch to ensure relationships are loaded (consistent with previous logic)
    return await get_bot_by_id(session, bot_id=new_bot.id)


async def get_bot_by_id(session: AsyncSession, bot_id: int) -> Optional[Bot]:
    query = (
        select(Bot)
        .where(Bot.id == bot_id)
        .options(selectinload(Bot.history), selectinload(Bot.products))
    )
    result = await session.execute(query)
    return result.scalars().first()


async def list_user_bots(session: AsyncSession, user_id: int) -> List[Bot]:
    """
    Lista os bots do usuário ordenados por ID Crescente (Mais antigo primeiro).
    """
    query = (
        select(Bot)
        .where(Bot.user_id == user_id)
        .options(selectinload(Bot.history), selectinload(Bot.products))
        # REMOVIDO: outerjoin(Order) e group_by (não precisamos mais calcular datas)
        # ADICIONADO: Ordenação simples por ID ascendente
        .order_by(Bot.id.asc())
    )

    result = await session.execute(query)
    return result.unique().scalars().all()


async def update_bot(
    session: AsyncSession, bot_id: int, update_data: BotUpdate
) -> Optional[Bot]:
    """CORREÇÃO: A assinatura já estava recebendo bot_id, o que é ótimo."""
    db_bot = await session.get(Bot, bot_id)
    if not db_bot:
        return None

    update_data_dict = update_data.model_dump(exclude_unset=True)
    for key, value in update_data_dict.items():
        if key == "whatsapp_token":
            value = encrypt_value(value) if value else ""
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
    category: str = "Geral",  # <--- Novo parâmetro com valor padrão
) -> Product:
    # Incluímos a CATEGORIA no texto do embedding para ajudar na busca semântica
    text_to_embed = (
        f"CATEGORIA: {category}. "
        f"PRODUTO PRINCIPAL: {name}. "
        f"DESCRIÇÃO E INGREDIENTES: {description or 'N/A'}. "
        f"CATEGORIAS E TAGS: {keywords or 'N/A'}."
    )
    # Graceful degradation: if the embedding model is unavailable, persist
    # the product with a zero vector. The product remains fully searchable
    # via the keyword/ILIKE layers (1-5) of find_relevant_products. Layer 6
    # (semantic similarity) skips this product because cosine distance to a
    # zero vector is undefined / always at the threshold ceiling. When
    # embeddings come back online, a future re-embedding pass can backfill.
    try:
        from app.embedding_service import EmbeddingsUnavailable

        embeddings = await embed_async(
            [text_to_embed], space="products", normalize=False
        )
        embedding_vector = embeddings[0]
    except EmbeddingsUnavailable:
        import logging as _log

        _log.getLogger(__name__).warning(
            "Embedding model unavailable — persisting product '%s' with zero vector. "
            "Will need re-embedding when the model is back online.",
            name,
        )
        embedding_vector = [0.0] * 384

    new_product = Product(
        bot_id=bot_id,
        name=name,
        description=description,
        price=price,
        embedding=embedding_vector,
        keywords=keywords,
        category=category,  # <--- Salva no banco
        is_available=True,
    )
    session.add(new_product)
    await session.commit()
    await session.refresh(new_product)

    # Regenerate aliases for all products in this bot (new product may
    # invalidate existing aliases or create new unique ones).
    from app.alias_service import regenerate_aliases_for_bot

    try:
        alias_count = await regenerate_aliases_for_bot(session, bot_id)
        if alias_count:
            await session.commit()
    except Exception as e:
        logger.warning("ALIAS_CREATE_FAIL bot=%s err=%s", bot_id, e)

    return new_product


async def update_product(
    session: AsyncSession, db_product: Product, update_data: ProductUpdate
) -> Optional[Product]:
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
        embeddings = await embed_async(
            [text_to_embed], space="products", normalize=False
        )
        db_product.embedding = embeddings[0]

    session.add(db_product)
    await session.commit()
    await session.refresh(db_product)
    return db_product


async def bulk_create_products(
    session: AsyncSession, bot_id: int, products_data: List[Dict]
) -> int:
    """
    Sincroniza o catálogo com PERFORMANCE MÁXIMA:
    1. Gera embeddings de todos os produtos em 1 única requisição (Batch).
    2. Faz Upsert (Atualiza ou Cria) dos dados no banco.
    3. Faz Soft Delete do que saiu do cardápio.
    """

    # 1. Busca produtos existentes (para decidir entre Update ou Create)
    stmt = select(Product).where(Product.bot_id == bot_id)
    result = await session.execute(stmt)
    existing_products = result.scalars().all()

    # Mapa para busca rápida: nome_normalizado -> Produto
    existing_map = {p.name.strip().lower(): p for p in existing_products}

    # Listas preparatórias para o processamento em lote
    items_to_process = []
    texts_to_embed = []

    processed_names_in_file = set()

    # 2. Pré-processamento: Monta os textos mas NÃO chama a IA ainda
    for item in products_data:
        raw_name = item.get("name", "").strip()
        if not raw_name:
            continue
        raw_name = raw_name[:150]  # Enforce max length

        name_key = raw_name.lower()

        # Evita duplicatas no mesmo arquivo
        if name_key in processed_names_in_file:
            continue
        processed_names_in_file.add(name_key)

        # Extração segura dos dados
        category = item.get("category", "Geral")
        category = str(category).strip()[:100] if category else "Geral"
        description = item.get("description", "")
        description = str(description).strip()[:500] if description else ""
        keywords = item.get("keywords", [])
        keywords_str = (
            ", ".join(keywords) if isinstance(keywords, list) else str(keywords)
        )
        try:
            price = float(item.get("price", 0.0))
        except (ValueError, TypeError):
            logger.warning(
                "BULK_PRODUCT_SKIP_INVALID_PRICE name=%s bot_id=%s",
                raw_name[:50],
                bot_id,
            )
            continue
        if price <= 0:
            logger.warning(
                "BULK_PRODUCT_SKIP_NON_POSITIVE_PRICE name=%s price=%s bot_id=%s",
                raw_name[:50],
                price,
                bot_id,
            )
            continue

        # Texto para o Embedding
        text = (
            f"CATEGORIA: {category}. "
            f"PRODUTO PRINCIPAL: {raw_name}. "
            f"DESCRIÇÃO E INGREDIENTES: {description or 'N/A'}. "
            f"PALAVRAS-CHAVE: {keywords_str or 'N/A'}."
        )

        # Guarda os dados limpos e o texto
        items_to_process.append(
            {
                "raw_name": raw_name,
                "name_key": name_key,
                "description": description,
                "price": price,
                "keywords": keywords_str,
                "category": category,
            }
        )
        texts_to_embed.append(text)

    # 🚀 3. A MÁGICA: Gera todos os embeddings de uma vez (1 Request apenas!)
    if texts_to_embed:
        # Isso transforma 30 segundos de espera em ~1 segundo
        embeddings = await embed_async(
            texts_to_embed, space="products", normalize=False
        )
    else:
        embeddings = []

    # 4. Aplica as alterações no banco (Upsert)
    processed_db_ids = set()
    products_to_add = []

    # Itera sobre os dados e os vetores simultaneamente
    for item_data, embedding_vector in zip(items_to_process, embeddings):
        name_key = item_data["name_key"]

        if name_key in existing_map:
            # --- ATUALIZAR (UPDATE) ---
            product = existing_map[name_key]

            product.name = item_data["raw_name"]
            product.description = item_data["description"]
            product.price = item_data["price"]
            product.keywords = item_data["keywords"]
            product.category = item_data["category"]
            product.embedding = embedding_vector  # Vetor novo

            product.is_available = True
            product.is_deleted = False

            session.add(product)
            processed_db_ids.add(product.id)
        else:
            # --- CRIAR (INSERT) ---
            new_product = Product(
                bot_id=bot_id,
                name=item_data["raw_name"],
                description=item_data["description"],
                price=item_data["price"],
                embedding=embedding_vector,  # Vetor novo
                keywords=item_data["keywords"],
                category=item_data["category"],
                is_available=True,
                is_deleted=False,
            )
            products_to_add.append(new_product)

    # 5. Adiciona novos e Arquiva (Soft Delete) os removidos
    if products_to_add:
        session.add_all(products_to_add)

    for product in existing_products:
        if product.id not in processed_db_ids:
            if not product.is_deleted:
                product.is_available = False
                product.is_deleted = True
                session.add(product)

    await session.commit()

    # Generate disambiguated aliases for all products in this bot.
    # Must run after commit so all products are visible.
    from app.alias_service import regenerate_aliases_for_bot

    try:
        alias_count = await regenerate_aliases_for_bot(session, bot_id)
        if alias_count:
            await session.commit()
            logger.info("ALIAS_BULK bot=%s updated=%d", bot_id, alias_count)
    except Exception as e:
        logger.warning("ALIAS_BULK_FAIL bot=%s err=%s", bot_id, e)

    return len(processed_db_ids) + len(products_to_add)


async def get_products_by_bot_id(
    session: AsyncSession, bot_id: int, limit: int = 50, offset: int = 0
) -> List[Product]:
    statement = (
        select(Product)
        .where(
            Product.bot_id == bot_id,
            Product.is_deleted == False,  # <--- FILTRO NOVO
        )
        .order_by(Product.category, Product.name)
        .offset(offset)
        .limit(limit)
    )
    result = await session.execute(statement)
    return result.scalars().all()


async def get_product_by_id(
    session: AsyncSession, product_id: int
) -> Optional[Product]:
    query = (
        select(Product)
        .where(Product.id == product_id)
        .options(selectinload(Product.bot))
    )
    result = await session.execute(query)
    return result.scalars().first()


async def bulk_delete_products(
    session: AsyncSession, bot_id: int, product_ids: List[int]
) -> int:
    # Em vez de delete(), usamos update()
    statement = (
        update(Product)
        .where(Product.bot_id == bot_id)
        .where(Product.id.in_(product_ids))
        .values(is_deleted=True)  # <--- MARCA COMO DELETADO
    )

    result = await session.execute(statement)
    await session.commit()
    return result.rowcount


async def delete_product(session: AsyncSession, db_product: Product):
    db_product.is_deleted = True  # Soft Delete
    session.add(db_product)
    await session.commit()


async def create_order(
    session: AsyncSession,
    bot_id: int,
    items: List[Dict],
    total_amount: float,
    delivery_method: DeliveryMethod,
    customer_address: str | None = None,
    contact_id: int | None = None,
    payment_method: str | None = None,
    auto_commit: bool = True,
) -> Order | None:
    try:
        order_items_to_create: list[OrderItem] = []
        for item_data in items:
            product = await session.get(Product, item_data["product_id"])
            if not product:
                raise ValueError(f"Produto {item_data['product_id']} não encontrado.")
            order_items_to_create.append(
                OrderItem(
                    product_id=product.id,
                    quantity=item_data["quantity"],
                    price_at_time_of_order=product.price,
                    notes=item_data.get("notes"),
                )
            )

        new_order = Order(
            bot_id=bot_id,
            total_amount=round(total_amount, 2),
            delivery_method=delivery_method,
            customer_address=customer_address,
            contact_id=contact_id,
            payment_method=payment_method,
            items=order_items_to_create,
        )

        session.add(new_order)
        if auto_commit:
            await session.commit()
        else:
            await session.flush()
        await session.refresh(new_order, attribute_names=["items"])
        return new_order

    except ValueError as exc:
        await session.rollback()
        logger.error("Failed to create order: %s", exc)
        return None


# app/crud.py


async def save_address_to_cart(
    session: AsyncSession, cart_id: int, address: str
) -> Optional[ShoppingCart]:
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


async def save_customer_name_to_contact(
    session: AsyncSession, contact_id: int, name: str
) -> Optional[Contact]:
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


async def save_contact_phone_to_contact(
    session: AsyncSession, contact_id: int, phone: str
) -> Optional[Contact]:
    """Persist the human phone for a web Contact at checkout.

    Plan/in_browser_bots.md A1: Contact.phone_number stays as the
    synthesized identity key (`web:{session_id}`); the real phone the
    customer types at checkout lands in Contact.contact_phone — separate
    column so the identity key stays stable. For WhatsApp contacts this
    function is a no-op equivalent: their phone_number is already the
    real E.164 and contact_phone got backfilled by migration
    y0z1a2b3c4d5_web_channel_contact.
    """
    contact = await session.get(Contact, contact_id)
    if not contact:
        return None

    contact.contact_phone = phone
    session.add(contact)
    await session.flush()
    await session.refresh(contact)
    return contact


async def set_human_takeover_by_phone(
    session: AsyncSession, bot_id: int, phone_number: str, active: bool
) -> bool:
    """
    Encontra o carrinho de um cliente pelo número de telefone e ativa/desativa o modo de atendimento humano.
    """
    # Encontra o contato pelo número de telefone para o bot específico
    contact_result = await session.execute(
        select(Contact).where(
            Contact.phone_number == phone_number, Contact.bot_id == bot_id
        )
    )
    contact = contact_result.scalar_one_or_none()

    if not contact:
        logger.warning(
            "Contact not found for phone %s, bot %s", mask_phone(phone_number), bot_id
        )
        return False

    # Encontra o carrinho associado a esse contato
    cart_result = await session.execute(
        select(ShoppingCart).where(ShoppingCart.contact_id == contact.id)
    )
    cart = cart_result.scalar_one_or_none()

    if not cart:
        logger.warning("Cart not found for contact %s", contact.id)
        return False

    # Ativa ou desativa o interruptor
    cart.human_takeover_active = active
    session.add(cart)
    await session.commit()

    status = "ATIVADO" if active else "DESATIVADO"
    logger.info("Human takeover %s for phone %s", status, mask_phone(phone_number))
    return True


# Status set that counts as a "completed order" for pricing / overage billing.
# An order is counted once on its first transition into any of these; further
# transitions between them (paid -> preparing -> ready -> completed) are NOT
# double-counted. See tech_debt/backlog_pricing.md §1.2.
_BILLABLE_ORDER_STATUSES: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.PAID,
        OrderStatus.PREPARING,
        OrderStatus.READY,
        OrderStatus.COMPLETED,
    }
)


async def increment_monthly_orders(
    session: AsyncSession,
    bot_id: int,
    year_month: Optional[str] = None,
) -> None:
    """UPSERT the per-bot per-month order counter.

    Participates in the caller's transaction — does not commit. Uses
    PostgreSQL ON CONFLICT so the first call of a month creates the row
    and subsequent calls atomically increment `completed_orders`.

    Only `completed_orders` and `updated_at` are touched; overage amounts
    and fair-use flags are populated later by the billing cron and the
    outreach job (they need the plan cap, which we intentionally don't
    join in the hot path).
    """
    if year_month is None:
        year_month = current_brt_year_month()

    await session.execute(
        text(
            """
            INSERT INTO bot_monthly_usage
                (bot_id, year_month, completed_orders, created_at, updated_at)
            VALUES
                (:bot_id, :year_month, 1,
                 (NOW() AT TIME ZONE 'UTC'),
                 (NOW() AT TIME ZONE 'UTC'))
            ON CONFLICT ON CONSTRAINT uq_bot_monthly_usage_bot_month DO UPDATE
            SET completed_orders = bot_monthly_usage.completed_orders + 1,
                updated_at = (NOW() AT TIME ZONE 'UTC')
            """
        ),
        {"bot_id": bot_id, "year_month": year_month},
    )


async def increment_and_return_menu_extractions(
    session: AsyncSession,
    bot_id: int,
    year_month: Optional[str] = None,
) -> int:
    """Atomically bump `menu_extractions` and return the post-increment count.

    Participates in the caller's transaction — does not commit. Used by
    consume_extraction_quota to reserve a Cadastro Mágico slot race-safely:
    two concurrent uploads can't both observe `used < limit` and slip
    through, because the single UPSERT-with-RETURNING is atomic. The
    caller compares the returned count against the plan cap and rolls
    back if the limit is exceeded.
    """
    if year_month is None:
        year_month = current_brt_year_month()

    result = await session.execute(
        text(
            """
            INSERT INTO bot_monthly_usage
                (bot_id, year_month, menu_extractions, created_at, updated_at)
            VALUES
                (:bot_id, :year_month, 1,
                 (NOW() AT TIME ZONE 'UTC'),
                 (NOW() AT TIME ZONE 'UTC'))
            ON CONFLICT ON CONSTRAINT uq_bot_monthly_usage_bot_month DO UPDATE
            SET menu_extractions = bot_monthly_usage.menu_extractions + 1,
                updated_at = (NOW() AT TIME ZONE 'UTC')
            RETURNING menu_extractions
            """
        ),
        {"bot_id": bot_id, "year_month": year_month},
    )
    return int(result.scalar_one())


async def get_monthly_usage(
    session: AsyncSession,
    bot_id: int,
    year_month: Optional[str] = None,
) -> Optional[BotMonthlyUsage]:
    """Return the BotMonthlyUsage row for (bot_id, year_month), or None.

    Missing row = no billable orders this month yet (row is created lazily
    by the first increment_monthly_orders call).
    """
    if year_month is None:
        year_month = current_brt_year_month()

    stmt = select(BotMonthlyUsage).where(
        BotMonthlyUsage.bot_id == bot_id,
        BotMonthlyUsage.year_month == year_month,
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def update_order_status_by_id(
    session: AsyncSession,
    order_id: int,
    new_status: OrderStatus,
    psp_charge_id: Optional[str] = None,
) -> Optional[Order]:
    """
    Encontra um pedido pelo ID, atualiza seu status e o psp_charge_id.
    Retorna o objeto Order com o 'contact' e 'bot' carregados se for bem-sucedido
    e o status tiver sido realmente alterado.
    """
    # Usamos selectinload para já carregar os dados do contato e do bot
    # with_for_update() acquires a row-level lock to prevent race conditions
    # from concurrent payment webhooks
    query = (
        select(Order)
        .where(Order.id == order_id)
        .with_for_update()
        .options(
            selectinload(Order.bot),
            selectinload(Order.items).selectinload(OrderItem.product),
        )
    )
    result = await session.execute(query)
    order = result.scalars().first()

    if isinstance(new_status, str):
        new_status = OrderStatus(new_status.lower())

    if not order or not order.status:
        logger.warning("Order %s not found for status update", order_id)
        return None

    # IMPORTANTE: Proteção de idempotência
    # Se o status já for o final, não fazemos nada e não retornamos o pedido.
    # Isso evita enviar 5 confirmações para o cliente se o MP enviar 5 webhooks.
    if order.status == new_status or order.status in [
        OrderStatus.FAILED,
        OrderStatus.EXPIRED,
    ]:
        logger.info(
            "Order %s already in terminal state (%s), skipping", order_id, order.status
        )
        return None

    # Detect the first transition into a billable state so we count the
    # order exactly once, regardless of how many downstream status changes
    # it goes through.
    was_billable = order.status in _BILLABLE_ORDER_STATUSES
    will_be_billable = new_status in _BILLABLE_ORDER_STATUSES
    should_count = will_be_billable and not was_billable
    bot_id_for_counter = order.bot_id if should_count else None

    order.status = new_status
    if psp_charge_id:
        order.psp_charge_id = psp_charge_id

    session.add(order)

    if bot_id_for_counter is not None:
        await increment_monthly_orders(session, bot_id_for_counter)

    await session.commit()
    await session.refresh(
        order, attribute_names=["bot", "items"]
    )  # Garante que os dados carregados estão frescos

    logger.info("Order %s updated to %s", order_id, new_status)
    return order


async def list_orders_by_bot(
    session: AsyncSession,
    bot_id: int,
    status_filter: Optional[OrderStatus] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    query = (
        select(Order)
        .where(Order.bot_id == bot_id)
        # Agora podemos carregar o contato, pois o relacionamento existe!
        .options(
            selectinload(Order.items).selectinload(OrderItem.product),
            selectinload(Order.contact).selectinload(Contact.cart),
        )
        .order_by(Order.created_at.desc())
    )

    if status_filter:
        query = query.where(Order.status == status_filter)

    query = query.offset(offset).limit(limit)

    result = await session.execute(query)
    orders = result.scalars().all()

    formatted_orders = []
    for order in orders:
        order_dict = order.model_dump()

        order_dict["payment_method"] = order.payment_method
        order_dict["delivery_fee"] = (
            order.bot.delivery_fee
            if order.delivery_method == DeliveryMethod.DELIVERY
            else 0.0
        )

        order_dict["display_items"] = [
            {
                "quantity": item.quantity,
                "product_name": item.product.name,
                "notes": item.notes,
                "price_at_time_of_order": item.price_at_time_of_order,
            }
            for item in order.items
        ]

        # ▼▼▼ CORREÇÃO AQUI ▼▼▼
        if order.contact:
            order_dict["customer_phone"] = order.contact.phone_number
            # Adicionamos explicitamente o nome aqui:
            order_dict["customer_name"] = order.contact.name

            if order.contact.cart:
                order_dict["human_takeover_active"] = (
                    order.contact.cart.human_takeover_active
                )
            else:
                order_dict["human_takeover_active"] = False
        else:
            order_dict["customer_phone"] = "Desconhecido"
            order_dict["customer_name"] = None  # Garante que a chave exista
            order_dict["human_takeover_active"] = False
        # ▲▲▲ FIM DA CORREÇÃO ▲▲▲

        formatted_orders.append(order_dict)

    return formatted_orders


async def get_top_selling_products(session: AsyncSession, bot_id: int, limit: int = 5):
    query = (
        select(
            Product.name,
            func.sum(OrderItem.quantity).label("total_sold"),
            func.sum(OrderItem.quantity * OrderItem.price_at_time_of_order).label(
                "total_revenue"
            ),
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
    return result.all()


async def update_item_notes(
    session: AsyncSession, cart_id: int, product_id: int, notes: str
) -> Optional[ShoppingCart]:
    """Atualiza apenas a observação de um item no carrinho."""
    cart = await session.get(
        ShoppingCart,
        cart_id,
        options=[selectinload(ShoppingCart.items).selectinload(CartItem.product)],
    )
    if not cart:
        return None

    item_to_modify = next(
        (item for item in cart.items if item.product_id == product_id), None
    )

    if item_to_modify:
        item_to_modify.notes = notes
        session.add(item_to_modify)
        await session.flush()
        return cart

    return None


# ▼▼▼ NOVA FUNÇÃO PARA BUSCAR ASSINATURA POR BOT ▼▼▼
async def get_subscription_by_bot(
    session: AsyncSession, bot_id: int
) -> Optional[Subscription]:
    stmt = select(Subscription).where(Subscription.bot_id == bot_id)
    result = await session.execute(stmt)
    return result.scalars().first()


# ▼▼▼ FUNÇÃO UPSERT ATUALIZADA ▼▼▼
async def upsert_subscription(
    session: AsyncSession,
    user_id: int,
    bot_id: int,
    mp_id: str,
    status: str,
    plan_type: str = "pro_monthly",
    plan_frequency_months: int = 1,
):
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    values = {
        "user_id": user_id,
        "bot_id": bot_id,
        "mp_subscription_id": mp_id,
        "status": status,
        "plan_type": plan_type,
        "current_period_end": utcnow() + timedelta(days=30 * plan_frequency_months),
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }

    stmt = pg_insert(Subscription).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["bot_id"],
        set_={
            "mp_subscription_id": stmt.excluded.mp_subscription_id,
            "status": stmt.excluded.status,
            "plan_type": stmt.excluded.plan_type,
            "current_period_end": stmt.excluded.current_period_end,
            "updated_at": stmt.excluded.updated_at,
        },
    )

    await session.execute(stmt)
    await session.flush()

    # Reload the subscription to return it
    sub = await get_subscription_by_bot(session, bot_id)
    return sub


async def get_latest_active_order(
    session: AsyncSession, contact_id: int, bot_id: int
) -> Optional[Order]:
    """Returns the most recent non-terminal order for a contact."""
    terminal_statuses = [
        OrderStatus.COMPLETED,
        OrderStatus.CANCELED,
        OrderStatus.FAILED,
        OrderStatus.EXPIRED,
    ]
    query = (
        select(Order)
        .where(
            Order.contact_id == contact_id,
            Order.bot_id == bot_id,
            Order.status.not_in(terminal_statuses),
        )
        .order_by(Order.created_at.desc())
        .limit(1)
    )
    result = await session.execute(query)
    return result.scalars().first()


async def get_last_completed_order_items(
    session: AsyncSession, contact_id: int, bot_id: int
) -> Optional[List[Dict[str, Any]]]:
    """Returns items from the most recent completed order for reorder."""
    query = (
        select(Order)
        .where(
            Order.contact_id == contact_id,
            Order.bot_id == bot_id,
            Order.status.in_([OrderStatus.COMPLETED, OrderStatus.PAID]),
        )
        .options(selectinload(Order.items).selectinload(OrderItem.product))
        .order_by(Order.created_at.desc())
        .limit(1)
    )
    result = await session.execute(query)
    order = result.scalars().first()
    if not order or not order.items:
        return None
    return [
        {
            "product_id": item.product_id,
            "quantity": item.quantity,
            "product_name": item.product.name if item.product else "Produto",
            "notes": item.notes,
        }
        for item in order.items
        if item.product and item.product.is_available and not item.product.is_deleted
    ]


async def cancel_expired_pix_orders(session: AsyncSession):
    """Cancela pedidos PIX pendentes há mais de 15 minutos.

    Returns a list of dicts with contact/bot info for each canceled order,
    so the caller can send WhatsApp notifications.
    """
    limit_time = utcnow() - timedelta(minutes=15)

    query = (
        select(Order)
        .where(
            Order.status == OrderStatus.PENDING,
            Order.payment_method == "pix",
            Order.created_at < limit_time,
        )
        .options(
            selectinload(Order.contact).selectinload(Contact.bot),
        )
    )

    result = await session.execute(query)
    expired_orders = result.scalars().all()

    canceled_info: list[dict] = []
    for order in expired_orders:
        order.status = OrderStatus.CANCELED
        session.add(order)
        # Capture info before commit expires objects
        if order.contact and order.contact.bot:
            canceled_info.append(
                {
                    "order_id": order.id,
                    "contact_phone": order.contact.phone_number,
                    "bot_token": order.contact.bot.whatsapp_token,
                    "bot_phone_id": order.contact.bot.phone_number_id,
                }
            )
        logger.info("Order #%s expired (PIX > 15min), auto-canceled", order.id)

    if canceled_info:
        await session.commit()
    return canceled_info


# ────────────────────────────────────────────────────────────────
# Plan CRUD
# ────────────────────────────────────────────────────────────────


async def list_plans(session: AsyncSession) -> List[Plan]:
    result = await session.execute(select(Plan).order_by(Plan.id))
    return result.scalars().all()


async def get_plan_by_key(session: AsyncSession, key: str) -> Optional[Plan]:
    result = await session.execute(select(Plan).where(Plan.key == key))
    return result.scalars().first()


async def is_plan_active(session: AsyncSession, plan_type: str) -> bool:
    """Check if a plan key exists and allows bot usage."""
    plan = await get_plan_by_key(session, plan_type)
    if not plan:
        return False
    return plan.allows_bot_usage


async def create_plan(session: AsyncSession, data: dict) -> Plan:
    plan = Plan(**data)
    session.add(plan)
    await session.commit()
    await session.refresh(plan)
    return plan


async def update_plan(
    session: AsyncSession, plan_id: int, data: dict
) -> Optional[Plan]:
    plan = await session.get(Plan, plan_id)
    if not plan:
        return None
    for key, value in data.items():
        setattr(plan, key, value)
    session.add(plan)
    await session.commit()
    await session.refresh(plan)
    return plan


async def delete_plan(session: AsyncSession, plan_id: int) -> bool:
    plan = await session.get(Plan, plan_id)
    if not plan:
        return False
    await session.delete(plan)
    await session.commit()
    return True


async def clear_contact_history(session: AsyncSession, contact_id: int) -> int:
    """Clear all conversation history for a specific contact. Does NOT commit."""
    from sqlalchemy import delete as sa_delete

    result = await session.execute(
        sa_delete(ConversationHistory).where(
            ConversationHistory.contact_id == contact_id
        )
    )
    count = result.rowcount
    if count:
        logger.info(
            "Cleared %d conversation history records for contact %d",
            count,
            contact_id,
        )
    return count


async def cleanup_old_conversation_history(session: AsyncSession) -> int:
    """Delete ConversationHistory records older than 24 hours."""
    from sqlalchemy import delete as sa_delete

    cutoff = utcnow() - timedelta(hours=24)
    result = await session.execute(
        sa_delete(ConversationHistory).where(ConversationHistory.created_at < cutoff)
    )
    await session.commit()
    count = result.rowcount
    if count:
        logger.info("Cleaned up %d old conversation history records", count)
    return count
