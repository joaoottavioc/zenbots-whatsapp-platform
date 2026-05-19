import secrets
from sqlmodel import SQLModel, Field, Relationship, Column
from pgvector.sqlalchemy import Vector
from typing import Optional, List, Dict, Any
from datetime import date as date_type, datetime, timezone
import enum
from sqlmodel import JSON as SA_JSON
from sqlalchemy import JSON, DateTime, ForeignKey, Integer, UniqueConstraint, Index
from app.time import utcnow


# ▼▼▼ 1. ADICIONE ESTE ENUM NO TOPO DO ARQUIVO ▼▼▼
class DeliveryMethod(str, enum.Enum):
    DELIVERY = "delivery"
    PICKUP = "pickup"


class CartState(str, enum.Enum):
    GREETING = "GREETING"
    SHOPPING = "SHOPPING"
    AWAITING_DELIVERY_METHOD = "AWAITING_DELIVERY_METHOD"
    AWAITING_CEP = "AWAITING_CEP"
    AWAITING_NUMBER_COMPLEMENT = "AWAITING_NUMBER_COMPLEMENT"
    AWAITING_ADDRESS_CONFIRMATION = "AWAITING_ADDRESS_CONFIRMATION"
    AWAITING_CUSTOMER_NAME = "AWAITING_CUSTOMER_NAME"
    AWAITING_PAYMENT_METHOD = "AWAITING_PAYMENT_METHOD"


class Channel(str, enum.Enum):
    """Bot deploy channels. WhatsApp is the historical default; web is the
    embeddable widget added per plan/in_browser_bots.md."""

    WHATSAPP = "whatsapp"
    WEB = "web"


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    hashed_password: str
    is_admin: bool = Field(default=False)
    is_email_verified: bool = Field(default=False)

    bots: List["Bot"] = Relationship(back_populates="user")

    subscriptions: List["Subscription"] = Relationship(back_populates="user")


class Bot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    restaurant_name: Optional[str] = Field(default=None)
    whatsapp_number: Optional[str] = Field(default=None, unique=True, index=True)

    menu_url: Optional[str] = Field(
        default=None, description="URL pública do cardápio (PDF/Imagem) no S3"
    )
    created_at: datetime = Field(default_factory=utcnow)

    # ▼▼▼ CREDENCIAIS DA META (NOVOS CAMPOS) ▼▼▼
    whatsapp_token: str = Field(default="")  # O Token de acesso (EAA...)
    phone_number_id: str = Field(default="", index=True)  # O ID numérico (8812...)

    pix_key: Optional[str] = Field(default=None, index=True)

    # Permite que cada restaurante defina sua taxa de entrega
    delivery_fee: float = Field(default=0.0)

    # Permite que cada restaurante defina um valor mínimo para pedidos
    min_order_value: float = Field(default=0.0)

    user_id: int = Field(foreign_key="user.id")
    user: "User" = Relationship(back_populates="bots")

    # Relationships with cascade deletion
    products: List["Product"] = Relationship(
        back_populates="bot", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    history: List["ConversationHistory"] = Relationship(
        back_populates="bot", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    orders: List["Order"] = Relationship(
        back_populates="bot", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    contacts: List["Contact"] = Relationship(
        back_populates="bot", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )

    is_open: bool = Field(default=True)  # Por padrão, a loja nasce aberta
    closing_message: str = Field(
        default="Olá! No momento estamos fechados. Nosso horário é das 18h às 23h. 🕒"
    )

    timezone: str = Field(default="America/Sao_Paulo")
    schedule: Dict[str, Any] = Field(default={}, sa_column=Column(SA_JSON))

    max_delivery_radius: float = Field(default=10.0)
    cep: Optional[str] = None
    address: Optional[str] = None  # Ex: Av Paulista, 1000 - Bela Vista
    latitude: Optional[float] = None  # Ex: -23.555
    longitude: Optional[float] = None  # Ex: -46.666

    # F-07: ETA
    default_delivery_time_minutes: Optional[int] = Field(default=None)
    default_pickup_time_minutes: Optional[int] = Field(default=None)

    # F-09: Owner notifications
    owner_notification_phone: Optional[str] = Field(default=None, max_length=20)

    # F-17: Cancellation window (minutes after order creation)
    cancellation_window_minutes: int = Field(default=5)

    # Restaurant cover image
    restaurant_image_url: Optional[str] = Field(default=None)

    # ── Channel matrix (plan/in_browser_bots.md Phase 2.5) ──────────────
    # Every existing bot was WhatsApp-only, so whatsapp_enabled defaults
    # true to preserve current behavior. web_widget_enabled defaults false
    # — bots stay WhatsApp-only until the owner opts in via dashboard.
    # Both true = customer can order via either channel, separate Contact
    # rows per channel (A1b synthesis keeps the carts isolated).
    whatsapp_enabled: bool = Field(default=True)
    web_widget_enabled: bool = Field(default=False)

    # Per-bot CORS allowlist for the widget. Empty list = block all
    # browser embeds. Phase 2 ingress checks Origin against this list.
    web_widget_allowed_origins: List[str] = Field(
        default_factory=list, sa_column=Column(SA_JSON)
    )

    # Widget UI customization (primary_color, position, welcome_message,
    # etc.). Free-form dict to avoid migration churn on theme additions.
    web_widget_theme: Dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(SA_JSON)
    )

    # Replaces the "store closed" text on the web channel. WhatsApp keeps
    # its richer multi-line closing_message with menu image — that's the
    # established UX customers expect on WA.
    web_widget_offline_message: Optional[str] = Field(default=None, max_length=500)

    # MUDANÇA 2: O Bot ganha a Assinatura (1-pra-1 com o Bot)
    subscription: Optional["Subscription"] = Relationship(
        back_populates="bot", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )

    payment_config: Optional["PaymentConfig"] = Relationship(
        back_populates="bot",
        sa_relationship_kwargs={"uselist": False, "cascade": "all, delete-orphan"},
    )

    usage_events: List["UsageEvent"] = Relationship(
        back_populates="bot", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    daily_cost_summaries: List["DailyCostSummary"] = Relationship(
        back_populates="bot", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )


class Contact(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("bot_id", "phone_number", name="uq_contact_bot_phone"),
        Index("ix_contact_bot_id_channel", "bot_id", "channel"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    # Identity key. For WhatsApp: real E.164. For web: synthesized
    # "web:{session_id}" (A1b in plan/in_browser_bots.md). Stays NOT NULL
    # and unique per bot — no code path treats this as a real phone.
    phone_number: str = Field(index=True)

    # Deploy channel this contact entered through. Drives KDS display,
    # cost attribution, and the channel-specific egress path.
    channel: str = Field(default=Channel.WHATSAPP.value, max_length=16)

    # The human phone the customer actually uses. WhatsApp contacts have
    # this populated from phone_number on creation; web contacts are NULL
    # until the customer provides it at checkout.
    contact_phone: Optional[str] = Field(default=None, max_length=32)

    name: Optional[str] = Field(default=None)

    # F-01: Customer memory
    default_address_json: Optional[Dict[str, Any]] = Field(
        default=None, sa_column=Column(SA_JSON)
    )
    last_order_date: Optional[datetime] = Field(default=None)

    bot_id: int = Field(foreign_key="bot.id")
    bot: "Bot" = Relationship(back_populates="contacts")

    # Relationships from Contact
    history: List["ConversationHistory"] = Relationship(
        back_populates="contact",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    cart: Optional["ShoppingCart"] = Relationship(
        back_populates="contact",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class Product(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    description: Optional[str] = Field(default=None)
    price: float
    embedding: List[float] = Field(sa_column=Column(Vector(384)))
    keywords: Optional[str] = Field(
        default=None,
        description="Palavras-chave separadas por vírgula para melhorar a busca.",
    )

    category: str = Field(default="Geral", index=True)
    is_available: bool = Field(default=True)

    bot_id: int = Field(foreign_key="bot.id")
    bot: "Bot" = Relationship(back_populates="products")
    is_deleted: bool = Field(default=False)


class ConversationHistory(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    role: str
    content: str
    created_at: datetime = Field(default_factory=utcnow, index=True)

    bot_id: int = Field(foreign_key="bot.id")
    bot: "Bot" = Relationship(back_populates="history")

    # 👇 THIS IS THE CORRECTED RELATIONSHIP 👇
    contact_id: int = Field(foreign_key="contact.id")
    contact: "Contact" = Relationship(back_populates="history")


class ProcessedMessage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    message_id: str = Field(index=True, unique=True)
    created_at: datetime = Field(default_factory=utcnow)


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    EXPIRED = "expired"
    PREPARING = "preparing"
    READY = "ready"
    COMPLETED = "completed"
    CANCELED = "canceled"
    REFUNDED = "refunded"


class ShoppingCart(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    state: CartState = Field(default=CartState.GREETING)

    # Usaremos este campo para guardar o endereço encontrado pelo CEP enquanto esperamos o número.
    partial_address: Optional[Dict[str, Any]] = Field(
        default=None, sa_column=Column(SA_JSON)
    )

    # Usaremos para guardar o endereço completo montado, aguardando o "sim" do cliente.
    pending_address: Optional[str] = Field(default=None)

    customer_address: Optional[str] = Field(default=None)

    proposed_action: Optional[Dict[str, Any]] = Field(
        default=None, sa_column=Column(SA_JSON)
    )

    # Armazena a escolha do cliente para a sessão atual
    delivery_method: Optional[DeliveryMethod] = Field(default=None)

    # ▼▼▼ NOVO CAMPO ▼▼▼
    # Este campo controlará se o bot está ativo ou não para este carrinho/cliente.
    # Por padrão, ele é falso, significando que o bot está no controle.
    human_takeover_active: bool = Field(default=False, index=True)

    last_activity_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True)),
    )

    last_suggestions: Optional[List[int]] = Field(
        default=None, sa_column=Column(SA_JSON)
    )

    contact_id: int = Field(foreign_key="contact.id", unique=True)
    contact: "Contact" = Relationship(back_populates="cart")

    items: List["CartItem"] = Relationship(
        back_populates="cart", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )

    pix_only: bool = Field(default=False)

    # ▼▼▼ novos campos para pending_action
    pending_action_tool: Optional[str] = None
    pending_action_args: Optional[Dict] = Field(default=None, sa_column=Column(JSON))
    pending_action_question: Optional[str] = None
    pending_action_expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )


class CartItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    quantity: int

    notes: Optional[str] = Field(
        default=None, description="Observações do item (ex: Sem cebola)"
    )

    product_id: int = Field(foreign_key="product.id")
    product: "Product" = Relationship()

    cart_id: int = Field(foreign_key="shoppingcart.id")
    cart: "ShoppingCart" = Relationship(back_populates="items")


class Order(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    total_amount: float
    status: OrderStatus = Field(default=OrderStatus.PENDING, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    psp_charge_id: Optional[str] = Field(default=None, index=True)
    customer_address: Optional[str] = Field(default=None)
    webhook_token: str = Field(
        default_factory=lambda: secrets.token_urlsafe(32), index=True
    )

    bot_id: int = Field(foreign_key="bot.id", index=True)
    bot: "Bot" = Relationship(back_populates="orders")

    # ▼▼▼ NOVOS CAMPOS ▼▼▼
    contact_id: Optional[int] = Field(
        default=None, foreign_key="contact.id", index=True
    )
    payment_method: Optional[str] = Field(default=None)
    contact: Optional["Contact"] = Relationship()

    items: List["OrderItem"] = Relationship(
        back_populates="order", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    delivery_method: DeliveryMethod = Field(default=DeliveryMethod.PICKUP)


class OrderItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    quantity: int
    price_at_time_of_order: float

    notes: Optional[str] = Field(default=None)

    order_id: int = Field(foreign_key="order.id")
    order: "Order" = Relationship(back_populates="items")

    product_id: int = Field(foreign_key="product.id")
    product: "Product" = Relationship()


class PaymentConfig(SQLModel, table=True):
    __tablename__ = "payment_configs"

    id: Optional[int] = Field(default=None, primary_key=True)

    provider: str = Field(default="mercadopago")
    access_token: Optional[str] = Field(default=None)
    public_key: Optional[str] = Field(default=None)
    refresh_token: Optional[str] = Field(default=None)
    token_expires_at: Optional[datetime] = Field(default=None)

    is_active: bool = Field(default=False)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    # ▼▼▼ MUDANÇA AQUI: VINCULA AO BOT, NÃO AO USUÁRIO ▼▼▼
    bot_id: int = Field(foreign_key="bot.id", unique=True)
    bot: "Bot" = Relationship(back_populates="payment_config")


class Subscription(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    # MUDANÇA 3: Vínculo principal agora é com o BOT
    bot_id: int = Field(
        foreign_key="bot.id", unique=True
    )  # Cada bot só tem uma assinatura ativa
    bot: "Bot" = Relationship(back_populates="subscription")

    # Vínculo com o dono da conta (Um usuário tem uma assinatura)
    user_id: int = Field(foreign_key="user.id")
    user: "User" = Relationship(
        back_populates="subscriptions"
    )  # Precisamos adicionar isso no User

    # Dados do Mercado Pago
    mp_subscription_id: str = Field(
        index=True, unique=True
    )  # ID da assinatura no MP (ex: 2c9380...)
    payer_email: Optional[str] = Field(default=None)

    # Status e Validade
    status: str = Field(default="pending")  # authorized, pending, cancelled, paused

    # Data vital: Até quando o sistema libera o acesso
    current_period_end: datetime = Field(sa_column=Column(DateTime(timezone=True)))

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    plan_type: str = Field(default="pro_monthly")

    plan_id: Optional[int] = Field(default=None, foreign_key="plan.id", index=True)
    is_founder: bool = Field(default=False)

    # Self-serve cancellation (1.6): user can cancel mid-period without losing
    # what they paid for. When true, the bot stays on its paid plan until
    # current_period_end, then falls through to Free (per _check_subscription).
    cancel_at_period_end: bool = Field(default=False)
    cancelled_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    cancelled_reason: Optional[str] = Field(default=None)

    # Founder lifetime price lock (2.2): snapshotted on first founder checkout,
    # survives Plan.price edits and plan deactivation.
    snapshotted_price_brl: Optional[float] = Field(default=None)


class Plan(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    # A "chave" para o frontend achar (ex: 'free', 'pro_monthly', 'pro_annual', 'founder')
    key: str = Field(unique=True, index=True)

    title: str  # Ex: "ZenBotZ Pro"
    description: str
    price: float  # Ex: 129.90

    # Configurações opcionais
    currency: str = Field(default="BRL")
    frequency: int = Field(default=1)  # 1 mês
    allows_bot_usage: bool = Field(default=True)

    # Pricing tier fields (2026-04 Free/Pro rollout)
    tier: str = Field(
        default="pro", index=True
    )  # free | pro | plus | founder | enterprise
    # None on Pro/Founder = unlimited (subject to fair_use_orders_cap)
    # Set on Free = hard soft-cap that triggers overage billing
    monthly_order_cap: Optional[int] = Field(default=None)
    # Advisory soft cap for Pro/Founder — triggers Enterprise outreach, never blocks bot
    fair_use_orders_cap: Optional[int] = Field(default=None)
    # R$ per order above monthly_order_cap (Free tier only)
    overage_per_order_brl: Optional[float] = Field(default=None)
    # Grace threshold: orders counted but NOT charged between monthly_order_cap
    # and overage_starts_at. Above this, each billable order costs
    # overage_per_order_brl. NULL = no grace window (cap itself triggers overage).
    overage_starts_at: Optional[int] = Field(default=None)
    billing_cycle_months: int = Field(default=1)  # 1 = monthly, 12 = annual
    is_active: bool = Field(default=True)
    max_bots: int = Field(default=1)
    allows_template_messages: bool = Field(default=False)

    # Cadastro Mágico (menu extraction) limits — enforced per bot per BRT month
    # via bot_monthly_usage.menu_extractions. See app/menu_extraction_policy.py.
    # None on any field = unlimited. Free: 3/mês, images+text only, 3 images max.
    # Pro/Founder: 5/mês, any file type, unlimited images.
    max_menu_extractions_per_month: Optional[int] = Field(default=None)
    allows_pdf_extraction: bool = Field(default=False)
    max_images_per_extraction: Optional[int] = Field(default=None)

    created_at: datetime = Field(default_factory=utcnow)


# ────────────────────────────────────────────────────────────────
# Monitoring & Observability Models
# ────────────────────────────────────────────────────────────────


class UsageEvent(SQLModel, table=True):
    """Append-only log of every external API call with cost attribution.

    bot_id is nullable: events recorded outside a WhatsApp request context
    (Cadastro Mágico image extraction, alias regeneration, ad-hoc CLI tools)
    are tracked as "system / untracked" instead of being silently dropped.
    The FK uses ON DELETE SET NULL so deleting a bot preserves its cost
    history (the events become orphaned but readable).
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_events_bot_created", "bot_id", "created_at"),
        Index("ix_usage_events_service_created", "service", "created_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    bot_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("bot.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    bot: Optional["Bot"] = Relationship(back_populates="usage_events")
    service: str  # "openai" | "google_maps" | "aws_s3" | "whatsapp" | "mercado_pago" | "facebook"
    operation: str  # "get_ai_decision" | "geocode" | "s3_put" | "send_message" | ...
    model: Optional[str] = Field(default=None)  # "gpt-4o-mini" | "gpt-4o" | None

    # Token metrics (OpenAI only)
    input_tokens: int = Field(default=0)
    output_tokens: int = Field(default=0)
    cached_tokens: int = Field(default=0)

    # Cost (pre-calculated at write time)
    cost_usd: float = Field(default=0.0)

    # Generic metrics
    quantity: int = Field(default=1)

    # Latency
    duration_ms: int = Field(default=0)

    # Status
    success: bool = Field(default=True)

    # Context
    contact_id: Optional[int] = Field(default=None)
    trace_id: Optional[str] = Field(default=None)
    created_at: datetime = Field(
        default_factory=utcnow, sa_column=Column(DateTime, index=True, default=utcnow)
    )


class DailyCostSummary(SQLModel, table=True):
    """Materialized daily cost aggregates per bot per service.

    bot_id is nullable to mirror UsageEvent: events without a bot context
    are aggregated under bot_id=NULL ("system / untracked"). FK uses
    ON DELETE SET NULL so deleting a bot preserves the cost history.
    """

    __tablename__ = "daily_cost_summary"
    __table_args__ = (
        UniqueConstraint(
            "bot_id", "date", "service", name="uq_daily_cost_bot_date_service"
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    bot_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("bot.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    bot: Optional["Bot"] = Relationship(back_populates="daily_cost_summaries")
    date: date_type = Field(index=True)
    service: str

    # Aggregated metrics
    total_cost_usd: float = Field(default=0.0)
    total_input_tokens: int = Field(default=0)
    total_output_tokens: int = Field(default=0)
    total_api_calls: int = Field(default=0)
    total_failed_calls: int = Field(default=0)
    total_duration_ms: int = Field(default=0)

    # Anomaly detection helpers
    avg_cost_per_call: float = Field(default=0.0)
    max_cost_single_call: float = Field(default=0.0)


class BotMonthlyUsage(SQLModel, table=True):
    """Per-bot, per-calendar-month order counter driving pricing enforcement.

    `year_month` is "YYYY-MM" in BRT (UTC-3) — Brazilian customers expect
    month rollover at midnight BRT, not UTC. Rows are created lazily via
    UPSERT on the first counted order of each month.

    bot_id uses ON DELETE SET NULL so billing history survives bot deletion
    (mirrors usage_events and daily_cost_summary).

    The hot-path increment only bumps `completed_orders` + `updated_at`.
    Overage amounts (`overage_*`) and fair-use flags are populated lazily
    by the billing cron / outreach job — reading the plan cap at hot-path
    time would add a join we don't need.

    See tech_debt/backlog_pricing.md §1.2.
    """

    __tablename__ = "bot_monthly_usage"
    __table_args__ = (
        UniqueConstraint("bot_id", "year_month", name="uq_bot_monthly_usage_bot_month"),
        Index("ix_bot_monthly_usage_year_month", "year_month"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    bot_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("bot.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    year_month: str  # "YYYY-MM" in BRT calendar

    completed_orders: int = Field(default=0)

    # Cadastro Mágico (menu extraction) counter. Incremented at upload
    # reception by consume_extraction_quota; failed extractions still
    # count against quota. See app/menu_extraction_policy.py.
    menu_extractions: int = Field(default=0)

    # Free-tier overage (populated by the monthly billing cron)
    overage_orders: int = Field(default=0)
    overage_amount_brl: float = Field(default=0.0)
    overage_billed_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    overage_billed_amount: Optional[float] = Field(default=None)
    overage_charge_id: Optional[str] = Field(default=None)

    # Pro/Founder fair-use (populated by the outreach job)
    fair_use_warning_sent_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    fair_use_exceeded_sent_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    # null | "notified" | "in_conversation" | "upgraded" | "declined"
    enterprise_outreach_status: Optional[str] = Field(default=None)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
