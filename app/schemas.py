from pydantic import BaseModel, ConfigDict, EmailStr, Field, validator
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
    category: Optional[str] = None  # <--- MUDANÇA CRÍTICA: De 'str' para 'Optional[str] = None'
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
    """Schema para criar um bot 'casca', apenas com os dados essenciais."""
    restaurant_name: str = Field(max_length=150)
    whatsapp_number: str = Field(max_length=20)
    pix_key: Optional[str] = Field(default=None, max_length=100)
    delivery_fee: Optional[float] = Field(default=0.0, ge=0)
    min_order_value: Optional[float] = Field(default=0.0, ge=0)
    whatsapp_token: str
    phone_number_id: str = Field(max_length=30)
    timezone: str = Field(default="America/Sao_Paulo")

    # NOVOS CAMPOS (Opcionais na criação, o usuário configura depois)
    max_delivery_radius: Optional[float] = 10.0
    cep: Optional[str] = Field(default=None, max_length=10)
    address: Optional[str] = Field(default=None, max_length=300)
    latitude: Optional[float] = None  # Essencial para o cálculo
    longitude: Optional[float] = None # Essencial para o cálculo

    @validator('timezone')
    def validate_timezone(cls, v):
        if v not in pytz.all_timezones:
            raise ValueError(f'Timezone inválido: {v}')
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

    @validator('timezone')
    def validate_timezone(cls, v):
        if v is not None and v not in pytz.all_timezones:
            raise ValueError(f'Timezone inválido: {v}')
        return v

class BotResponse(BaseModel):
    """
    Schema completo para retornar os dados de um bot, incluindo
    seu catálogo de produtos e seu histórico de conversas.
    """
    id: int
    user_id: int
    restaurant_name: Optional[str] = None
    whatsapp_number: str
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
    
    # O bot agora retorna a lista de produtos e de histórico associados a ele
    products: List[ProductResponse] = []
    history: List[ConversationHistoryResponse] = []

    is_open: bool
    closing_message: str
    schedule: Dict[str, Any]
    whatsapp_token: str
    phone_number_id: str

    model_config = ConfigDict(from_attributes=True)

# --- Schemas de Autenticação e Usuário ---

class UserCreate(BaseModel):
    email: str
    password: str

class UserResponse(BaseModel):
    id: int
    email: str
    model_config = ConfigDict(from_attributes=True)

class Token(BaseModel):
    access_token: str
    token_type: str

# --- Schema para Upload de Catálogo ---

class CatalogUploadRequest(BaseModel):
    """Schema para o endpoint que recebe o texto bruto do cardápio."""
    catalog_text: str

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
    access_token: str = None # Novo campo

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

class CheckoutRequest(BaseModel):
    plan_key: str = "pro"
    bot_id: int

class CepResponse(BaseModel):
    address: str
    city: str
    state: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None