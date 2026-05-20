from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator, validator
from datetime import datetime
from typing import List, Optional, Any, Dict
import pytz

# --- Schemas de Produto ---
# Usados para criar e retornar itens do cardápio


class ProductBase(BaseModel):
    name: str = Field(max_length=150)
    description: Optional[str] = Field(default=None, max_length=500)
    price: float = Field(gt=0)
    category: str = Field(max_length=100)


class ProductCreate(ProductBase):
    category: str
    pass


class ProductResponse(ProductBase):
    id: int
    bot_id: int
    is_available: bool
    model_config = ConfigDict(from_attributes=True)
    category: str


class ProductUpdate(BaseModel):
    """
    Schema para atualização. Todos os campos devem ser opcionais
    para permitir atualizações parciais (ex: mudar só o preço ou só a disponibilidade).
    """

    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = Field(default=None, gt=0)
    category: Optional[str] = (
        None  # <--- MUDANÇA CRÍTICA: De 'str' para 'Optional[str] = None'
    )
    is_available: Optional[bool] = None


class ProductBulkDeleteRequest(BaseModel):
    product_ids: List[int]


# --- Schemas de Histórico ---
# Usado para exibir o histórico de conversas


class ConversationHistoryResponse(BaseModel):
    role: str
    content: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


# --- Schemas de Bot ---
# O coração da nossa aplicação


class BotCreate(BaseModel):
    """Schema para criar um bot 'casca', apenas com os dados essenciais.
    WhatsApp credentials are optional — filled later via Embedded Signup."""

    restaurant_name: str = Field(max_length=150)
    whatsapp_number: Optional[str] = Field(default=None, max_length=20)
    pix_key: Optional[str] = Field(default=None, max_length=100)
    delivery_fee: Optional[float] = Field(default=0.0, ge=0)
    min_order_value: Optional[float] = Field(default=0.0, ge=0)
    whatsapp_token: Optional[str] = None
    phone_number_id: Optional[str] = Field(default=None, max_length=30)
    timezone: str = Field(default="America/Sao_Paulo")

    # Store status & schedule (accepted at creation so the form can set them)
    is_open: Optional[bool] = True
    closing_message: Optional[str] = Field(default=None, max_length=500)
    schedule: Optional[Dict[str, Any]] = None

    # NOVOS CAMPOS (Opcionais na criação, o usuário configura depois)
    max_delivery_radius: Optional[float] = 10.0
    cep: Optional[str] = Field(default=None, max_length=10)
    address: Optional[str] = Field(default=None, max_length=300)
    latitude: Optional[float] = None  # Essencial para o cálculo
    longitude: Optional[float] = None  # Essencial para o cálculo

    # F-07: ETA
    default_delivery_time_minutes: Optional[int] = Field(default=None, ge=1, le=180)
    default_pickup_time_minutes: Optional[int] = Field(default=None, ge=1, le=180)

    # F-09: Owner notifications
    owner_notification_phone: Optional[str] = Field(default=None, max_length=20)

    # F-17: Cancellation window
    cancellation_window_minutes: Optional[int] = Field(default=5, ge=0, le=30)

    @validator("timezone")
    def validate_timezone(cls, v):
        if v not in pytz.all_timezones:
            raise ValueError(f"Timezone inválido: {v}")
        return v


class BotUpdate(BaseModel):
    """Schema para atualizar os dados de um bot."""

    restaurant_name: Optional[str] = Field(default=None, max_length=150)
    whatsapp_number: Optional[str] = Field(default=None, max_length=20)
    pix_key: Optional[str] = Field(default=None, max_length=100)
    delivery_fee: Optional[float] = Field(default=None, ge=0)
    min_order_value: Optional[float] = Field(default=None, ge=0)
    is_open: Optional[bool] = None
    closing_message: Optional[str] = Field(default=None, max_length=500)
    timezone: Optional[str] = None
    schedule: Optional[Dict[str, Any]] = None
    whatsapp_token: Optional[str] = None
    phone_number_id: Optional[str] = Field(default=None, max_length=30)

    # NOVOS CAMPOS PARA ATUALIZAÇÃO VIA CONFIGURAÇÕES
    max_delivery_radius: Optional[float] = None
    cep: Optional[str] = Field(default=None, max_length=10)
    address: Optional[str] = Field(default=None, max_length=300)
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    # F-07: ETA
    default_delivery_time_minutes: Optional[int] = Field(default=None, ge=1, le=180)
    default_pickup_time_minutes: Optional[int] = Field(default=None, ge=1, le=180)

    # F-09: Owner notifications
    owner_notification_phone: Optional[str] = Field(default=None, max_length=20)

    # F-17: Cancellation window
    cancellation_window_minutes: Optional[int] = Field(default=None, ge=0, le=30)

    # Customer-facing widget slug (plan/in_browser_bots.md). Owner can
    # override the auto-generated value. Validator enforces kebab-case
    # so the URL stays clean and predictable.
    slug: Optional[str] = Field(default=None, min_length=3, max_length=80)

    # Channel toggles + widget config — editable from the bot card UI
    # (plan/in_browser_bots.md Phase 4). web_widget_enabled flips the
    # ingress gate; allowed_origins is the CORS allowlist.
    whatsapp_enabled: Optional[bool] = None
    web_widget_enabled: Optional[bool] = None
    web_widget_allowed_origins: Optional[List[str]] = None
    web_widget_theme: Optional[Dict[str, Any]] = None
    web_widget_offline_message: Optional[str] = Field(default=None, max_length=500)

    @validator("timezone")
    def validate_timezone(cls, v):
        if v is not None and v not in pytz.all_timezones:
            raise ValueError(f"Timezone inválido: {v}")
        return v

    @validator("slug")
    def validate_slug(cls, v):
        if v is None:
            return v
        import re

        # Lowercase letters, digits, hyphens. No leading/trailing hyphen.
        # No consecutive hyphens (keeps URLs readable).
        if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", v):
            raise ValueError(
                "slug deve conter apenas letras minúsculas, dígitos e hífens "
                "(sem hífens no início, fim, ou em sequência)"
            )
        return v


class BotResponse(BaseModel):
    """
    Schema completo para retornar os dados de um bot, incluindo
    seu catálogo de produtos e seu histórico de conversas.
    """

    id: int
    user_id: int
    restaurant_name: Optional[str] = None
    whatsapp_number: Optional[str] = None
    created_at: datetime
    menu_url: Optional[str] = None
    pix_key: Optional[str] = None

    delivery_fee: Optional[float] = 0.0
    min_order_value: Optional[float] = 0.0

    # NOVOS CAMPOS NO RETORNO
    max_delivery_radius: float = 10.0
    cep: Optional[str] = None
    address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    # F-07: ETA
    default_delivery_time_minutes: Optional[int] = None
    default_pickup_time_minutes: Optional[int] = None

    # F-09: Owner notifications
    owner_notification_phone: Optional[str] = None

    # F-17: Cancellation window
    cancellation_window_minutes: int = 5

    # Restaurant cover image
    restaurant_image_url: Optional[str] = None

    # Customer-facing widget URL slug + channel toggles + widget config.
    # The dashboard renders these on the bot card to manage Web Atendimento.
    slug: Optional[str] = None
    whatsapp_enabled: bool = True
    web_widget_enabled: bool = False
    web_widget_allowed_origins: List[str] = []
    web_widget_theme: Dict[str, Any] = {}
    web_widget_offline_message: Optional[str] = None

    # O bot agora retorna a lista de produtos e de histórico associados a ele
    products: List[ProductResponse] = []
    history: List[ConversationHistoryResponse] = []

    is_open: bool
    closing_message: str
    schedule: Dict[str, Any]
    whatsapp_token: Optional[str] = None
    phone_number_id: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def decrypt_whatsapp_token(self) -> "BotResponse":
        from app.encryption import decrypt_value

        if self.whatsapp_token:
            self.whatsapp_token = decrypt_value(self.whatsapp_token)
        return self

    @model_validator(mode="after")
    def presign_menu_url(self) -> "BotResponse":
        if self.menu_url:
            from app.menu_storage import generate_presigned_url

            self.menu_url = generate_presigned_url(self.menu_url)
        return self

    @model_validator(mode="after")
    def presign_restaurant_image_url(self) -> "BotResponse":
        if self.restaurant_image_url:
            from app.menu_storage import generate_presigned_url

            self.restaurant_image_url = generate_presigned_url(
                self.restaurant_image_url
            )
        return self


# --- Schemas de Autenticação e Usuário ---


class UserCreate(BaseModel):
    email: str
    password: str


class UserResponse(BaseModel):
    id: int
    email: str
    is_email_verified: bool = False
    is_admin: bool = False
    model_config = ConfigDict(from_attributes=True)


class Token(BaseModel):
    access_token: str
    token_type: str
    csrf_token: Optional[str] = None


# --- Schema para Upload de Catálogo ---


class CatalogUploadRequest(BaseModel):
    """Schema para o endpoint que recebe o texto bruto do cardápio."""

    catalog_text: str


class CatalogUrlUploadRequest(BaseModel):
    """Schema para o endpoint que recebe um link do iFood."""

    url: str = Field(max_length=500)


class OrderItemResponse(BaseModel):
    quantity: int
    notes: Optional[str] = None
    product_name: str
    model_config = ConfigDict(from_attributes=True)
    price_at_time_of_order: Optional[float] = None


class OrderResponse(BaseModel):
    id: int
    total_amount: float
    delivery_fee: float = 0.0
    status: str
    customer_address: Optional[str] = None
    created_at: datetime
    display_items: List[OrderItemResponse] = []
    payment_method: Optional[str] = None

    customer_name: Optional[str] = None

    customer_phone: Optional[str] = None
    human_takeover_active: bool = False

    model_config = ConfigDict(from_attributes=True)


class OrderStatusUpdate(BaseModel):
    status: str


class WhatsAppAuthRequest(BaseModel):
    code: str = None  # Agora é opcional
    redirect_uri: str = None
    access_token: str = None  # Novo campo


class EmbeddedSignupPayload(BaseModel):
    bot_id: int
    redirect_uri: str

    # IDs são opcionais pois no fluxo de Fallback (Reconexão) o frontend não os tem.
    # O Backend descobrirá esses valores se eles vierem nulos.
    business_id: Optional[str] = None
    waba_id: Optional[str] = None
    phone_number_id: Optional[str] = None
    display_phone_number: Optional[str] = None

    # Auth: Aceitamos Code (Fluxo Ideal) ou Token (Fluxo Fallback)
    code: Optional[str] = None
    access_token: Optional[str] = None


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class CheckoutResponse(BaseModel):
    checkout_url: str


class SubscriptionStatusResponse(BaseModel):
    status: str
    is_active: bool
    days_remaining: int
    next_payment: datetime
    plan_type: str


# --- Usage summary (Free-tier grace + overage widget) ---


class UsagePlan(BaseModel):
    key: str
    tier: str
    title: str
    monthly_order_cap: Optional[int]
    overage_per_order_brl: float
    price: float


class UsageCurrentPeriod(BaseModel):
    year_month: str
    completed_orders: int
    cap: Optional[int]
    overage_starts_at: Optional[int]
    orders_remaining: Optional[int]
    pct_used: float
    overage_orders: int
    overage_amount_brl: float
    projected_month_end_orders: Optional[int]
    projected_overage_brl: Optional[float]


class UsageUpgradePlanOption(BaseModel):
    key: str
    price: float
    price_per_month: Optional[float] = None
    label: str
    slots_remaining: Optional[int] = None


class UsageUpgradeOffer(BaseModel):
    available_plans: List[UsageUpgradePlanOption]


class UsageSummary(BaseModel):
    bot_id: int
    plan: UsagePlan
    current_period: UsageCurrentPeriod
    upgrade_offer: UsageUpgradeOffer


class CheckoutRequest(BaseModel):
    plan_key: str = "pro"
    bot_id: int


class CancelSubscriptionRequest(BaseModel):
    bot_id: int
    reason: Optional[str] = Field(default=None, max_length=500)


class CancelSubscriptionResponse(BaseModel):
    status: str
    active_until: datetime
    plan_type: str
    cancel_at_period_end: bool


class CurrentPlanResponse(BaseModel):
    plan_key: str
    plan_tier: str
    plan_title: str
    price: float
    monthly_order_cap: Optional[int]
    fair_use_orders_cap: Optional[int]
    overage_per_order_brl: Optional[float]
    billing_cycle_months: int
    current_period_end: Optional[datetime]
    cancel_at_period_end: bool
    is_founder: bool


class CepResponse(BaseModel):
    address: str
    city: str
    state: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None


# --- Schemas de Plano (Admin + Público) ---


class PlanResponse(BaseModel):
    id: int
    key: str
    title: str
    description: str
    price: float
    currency: str
    frequency: int
    allows_bot_usage: bool
    model_config = ConfigDict(from_attributes=True)


class PlanCreate(BaseModel):
    key: str = Field(max_length=50)
    title: str = Field(max_length=150)
    description: str = Field(max_length=500)
    price: float = Field(gt=0)
    currency: str = Field(default="BRL", max_length=10)
    frequency: int = Field(default=1, ge=1)
    allows_bot_usage: bool = Field(default=True)


class PlanUpdate(BaseModel):
    key: Optional[str] = Field(default=None, max_length=50)
    title: Optional[str] = Field(default=None, max_length=150)
    description: Optional[str] = Field(default=None, max_length=500)
    price: Optional[float] = Field(default=None, gt=0)
    currency: Optional[str] = Field(default=None, max_length=10)
    frequency: Optional[int] = Field(default=None, ge=1)
    allows_bot_usage: Optional[bool] = None


class AdminUpsertSubscription(BaseModel):
    status: str = Field(default="authorized", max_length=30)
    plan_type: str = Field(default="pro_monthly", max_length=30)


class AdminBotSummary(BaseModel):
    id: int
    restaurant_name: Optional[str] = None
    whatsapp_number: Optional[str] = None
    created_at: datetime
    is_open: bool
    model_config = ConfigDict(from_attributes=True)


# --- Web widget channel (plan/in_browser_bots.md Phase 2.1) ──────────
# Contract for POST /chat/{bot_id}/message and POST /chat/{bot_id}/session.
# Defined before the endpoint bodies land so the frontend widget can
# build against the schemas in parallel with Phase 2.2-2.4 backend work.

# Hard upper bound on inbound web message text. Keeps a misbehaving
# widget (or a curl loop) from flooding the worker with megabyte payloads.
# WhatsApp's own limit is 4096 chars; matching it here.
_WEB_MESSAGE_MAX_LENGTH = 4096


class ChatMessageRequest(BaseModel):
    """Inbound message from the widget.

    The widget generates `session_id` once per browser localStorage entry
    and `message_id` per send (both UUIDs). The server uses message_id
    for deduplication via the existing ProcessedMessage table — same row
    shape as WhatsApp's wamid.
    """

    session_id: str = Field(min_length=1, max_length=64)
    message_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=_WEB_MESSAGE_MAX_LENGTH)


class ChatMessageAccepted(BaseModel):
    """Returned on 202 from POST /chat/{bot_id}/message.

    The reply itself arrives on the SSE stream — the POST just enqueues
    the ARQ job and returns. The widget should already have an open SSE
    connection when it sends a message; the message_id echo lets the
    widget correlate (so the bot's typing/message events can be matched
    to which user message they're answering)."""

    accepted: bool = True
    message_id: str


class ChatAudioAccepted(BaseModel):
    """Returned on 202 from POST /chat/{bot_id}/audio.

    Symmetric with ChatMessageAccepted but also echoes the transcript so
    the widget can replace its optimistic "🎤 transcribing…" bubble with
    the actual text — same text the worker will then process through the
    LLM pipeline. The audio itself is not stored in v1; only the
    transcript is persisted in ConversationHistory by the downstream
    worker, exactly like a typed message.
    """

    accepted: bool = True
    message_id: str
    transcript: str


class ChatSessionResponse(BaseModel):
    """Optional handshake. The widget can call POST /chat/{bot_id}/session
    on first load to fetch the bot's display name, welcome message, theme,
    and a fresh server-issued session_id (if the widget doesn't already
    have one in localStorage).
    """

    session_id: str
    bot_display_name: str
    welcome_message: str
    theme: Dict[str, Any] = Field(default_factory=dict)
    # Mirrors the Plan tier (free/pro/founder) so the widget can decide
    # whether to render the "Powered by ZenBotZ®" footer.
    plan_tier: str


class ChatSlugResolution(BaseModel):
    """Returned by GET /chat/by-slug/{slug}. The frontend dynamic route
    `/<slug>` calls this to resolve a customer-facing slug into the
    bot_id it should pass to ZenBotsWidget. Tiny response shape — just
    what the widget bootstrap needs."""

    bot_id: int
    slug: str
    restaurant_name: Optional[str] = None
    web_widget_enabled: bool


# --- SSE event envelopes (server → widget) ───────────────────────────
# Events published by app/web_channel.py on `chat:{bot_id}:{session_id}`
# and forwarded by the GET /chat/{bot_id}/stream endpoint as
# `data: <json>\n\n`. Each event is wrapped in
# `{"type": "...", "payload": {...}}` per broadcast.py's convention.


class ChatSSETyping(BaseModel):
    """Typing indicator. Emit `on=True` before the LLM call kicks off,
    `on=False` when the reply is in flight (the `message` event implicitly
    ends typing, but explicit off covers error paths with no follow-up)."""

    on: bool


class ChatSSEMessage(BaseModel):
    """Text reply from the bot. Attachments carry inline media (PIX QR,
    menu image) the widget renders alongside the text bubble."""

    text: str
    attachments: List[Dict[str, Any]] = Field(default_factory=list)


class ChatSSEPaymentQR(BaseModel):
    """PIX QR delivered as a data: URL so the widget can render
    <img src="...">. payment_url is the Mercado Pago hosted checkout
    page — used as the "Abrir Mercado Pago" fallback button."""

    qr_data_url: str
    payment_url: str
    expires_at: datetime


class ChatSSEOrderStatus(BaseModel):
    """Order state transition (pending → paid, paid → preparing, etc.).
    The widget renders a success/in-progress chip when these arrive."""

    order_id: int
    status: str  # OrderStatus enum value


class ChatSSEPing(BaseModel):
    """Heartbeat. Server emits one every 10s so proxies (Caddy, ALB)
    keep the SSE connection open. Payload is empty — the widget ignores."""

    pass
