import secrets
from sqlmodel import SQLModel, Field, Relationship, Column
from pgvector.sqlalchemy import Vector
from typing import Optional, List, Dict, Any
from datetime import date as date_type, datetime, timezone
import enum
from sqlmodel import JSON as SA_JSON
from sqlalchemy import JSON, DateTime, UniqueConstraint, Index
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


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    hashed_password: str

    bots: List["Bot"] = Relationship(back_populates="user")

    subscriptions: List["Subscription"] = Relationship(back_populates="user")


class Bot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    restaurant_name: Optional[str] = Field(default=None)
    whatsapp_number: str = Field(unique=True, index=True)

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

    # MUDANÇA 2: O Bot ganha a Assinatura (1-pra-1 com o Bot)
    subscription: Optional["Subscription"] = Relationship(
        back_populates="bot", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )

    payment_config: Optional["PaymentConfig"] = Relationship(
        back_populates="bot",
        sa_relationship_kwargs={"uselist": False, "cascade": "all, delete-orphan"},
    )


class Contact(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    phone_number: str = Field(index=True)

    name: Optional[str] = Field(default=None)

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
    status: OrderStatus = Field(default=OrderStatus.PENDING)
    created_at: datetime = Field(default_factory=utcnow)
    psp_charge_id: Optional[str] = Field(default=None, index=True)
    customer_address: Optional[str] = Field(default=None)
    webhook_token: str = Field(
        default_factory=lambda: secrets.token_urlsafe(32), index=True
    )

    bot_id: int = Field(foreign_key="bot.id")
    bot: "Bot" = Relationship(back_populates="orders")

    # ▼▼▼ NOVOS CAMPOS ▼▼▼
    contact_id: Optional[int] = Field(default=None, foreign_key="contact.id")
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
    user_id: int = Field(foreign_key="user.id", unique=True)
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
    plan_type: str = Field(default="pro")


class Plan(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    # A "chave" para o frontend achar (ex: 'basic', 'pro', 'enterprise')
    key: str = Field(unique=True, index=True)

    title: str  # Ex: "ZenBotZ Pro"
    description: str
    price: float  # Ex: 199.00

    # Configurações opcionais
    currency: str = Field(default="BRL")
    frequency: int = Field(default=1)  # 1 mês

    created_at: datetime = Field(default_factory=utcnow)


# ────────────────────────────────────────────────────────────────
# Monitoring & Observability Models
# ────────────────────────────────────────────────────────────────


class UsageEvent(SQLModel, table=True):
    """Append-only log of every external API call with cost attribution."""

    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_events_bot_created", "bot_id", "created_at"),
        Index("ix_usage_events_service_created", "service", "created_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    bot_id: int = Field(foreign_key="bot.id", index=True)
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
    """Materialized daily cost aggregates per bot per service."""

    __tablename__ = "daily_cost_summary"
    __table_args__ = (
        UniqueConstraint(
            "bot_id", "date", "service", name="uq_daily_cost_bot_date_service"
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    bot_id: int = Field(foreign_key="bot.id", index=True)
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
