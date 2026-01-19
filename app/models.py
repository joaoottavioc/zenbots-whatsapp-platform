from sqlmodel import SQLModel, Field, Relationship, Column
from pgvector.sqlalchemy import Vector
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, timezone
import enum
from sqlmodel import JSON as SA_JSON
from sqlalchemy import Column, TIMESTAMP, text, JSON, DateTime
import enum

# ▼▼▼ 1. ADICIONE ESTE ENUM NO TOPO DO ARQUIVO ▼▼▼
class DeliveryMethod(str, enum.Enum):
    DELIVERY = "delivery"
    PICKUP = "pickup"

# Define forward references for type hinting
class Bot(SQLModel): pass
class Product(SQLModel): pass
class ConversationHistory(SQLModel): pass
class Contact(SQLModel): pass
class ShoppingCart(SQLModel): pass
class Order(SQLModel): pass
class OrderItem(SQLModel): pass

class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    hashed_password: str
    
    bots: List["Bot"] = Relationship(back_populates="user")

class Bot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    restaurant_name: Optional[str] = Field(default=None)
    whatsapp_number: str = Field(unique=True, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # ▼▼▼ CREDENCIAIS DA META (NOVOS CAMPOS) ▼▼▼
    whatsapp_token: str = Field(default="") # O Token de acesso (EAA...)
    phone_number_id: str = Field(default="", index=True) # O ID numérico (8812...)
    
    pix_key: Optional[str] = Field(default=None, index=True)

    # Permite que cada restaurante defina sua taxa de entrega
    delivery_fee: float = Field(default=0.0)

    # Permite que cada restaurante defina um valor mínimo para pedidos
    min_order_value: float = Field(default=0.0)
    
    user_id: int = Field(foreign_key="user.id")
    user: "User" = Relationship(back_populates="bots")

    # Relationships with cascade deletion
    products: List["Product"] = Relationship(back_populates="bot", sa_relationship_kwargs={"cascade": "all, delete-orphan"})
    history: List["ConversationHistory"] = Relationship(back_populates="bot", sa_relationship_kwargs={"cascade": "all, delete-orphan"})
    orders: List["Order"] = Relationship(back_populates="bot", sa_relationship_kwargs={"cascade": "all, delete-orphan"})
    contacts: List["Contact"] = Relationship(back_populates="bot", sa_relationship_kwargs={"cascade": "all, delete-orphan"})

    is_open: bool = Field(default=True) # Por padrão, a loja nasce aberta
    closing_message: str = Field(default="Olá! No momento estamos fechados. Nosso horário é das 18h às 23h. 🕒")

    timezone: str = Field(default="America/Sao_Paulo")
    schedule: Dict[str, Any] = Field(default={}, sa_column=Column(SA_JSON))

class Contact(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    phone_number: str = Field(index=True)

    name: Optional[str] = Field(default=None)

    bot_id: int = Field(foreign_key="bot.id")
    bot: "Bot" = Relationship(back_populates="contacts")

    # Relationships from Contact
    history: List["ConversationHistory"] = Relationship(back_populates="contact", sa_relationship_kwargs={"cascade": "all, delete-orphan"})
    cart: Optional["ShoppingCart"] = Relationship(back_populates="contact", sa_relationship_kwargs={"cascade": "all, delete-orphan"})

class Product(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    description: Optional[str] = Field(default=None)
    price: float
    embedding: List[float] = Field(sa_column=Column(Vector(384)))
    keywords: Optional[str] = Field(default=None, description="Palavras-chave separadas por vírgula para melhorar a busca.")

    category: str = Field(default="Geral", index=True)
    is_available: bool = Field(default=True)
    
    bot_id: int = Field(foreign_key="bot.id")
    bot: "Bot" = Relationship(back_populates="products")
    is_deleted: bool = Field(default=False)

class ConversationHistory(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    role: str 
    content: str
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    
    bot_id: int = Field(foreign_key="bot.id")
    bot: "Bot" = Relationship(back_populates="history")

    # 👇 THIS IS THE CORRECTED RELATIONSHIP 👇
    contact_id: int = Field(foreign_key="contact.id")
    contact: "Contact" = Relationship(back_populates="history")
    
class ProcessedMessage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    message_id: str = Field(index=True, unique=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)

class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    EXPIRED = "expired"
    PREPARING = "preparing"
    READY = "ready"
    COMPLETED = "completed"

class ShoppingCart(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    state: str = Field(default="GREETING")

    # Usaremos este campo para guardar o endereço encontrado pelo CEP enquanto esperamos o número.
    partial_address: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(SA_JSON))

    # Usaremos para guardar o endereço completo montado, aguardando o "sim" do cliente.
    pending_address: Optional[str] = Field(default=None)

    customer_address: Optional[str] = Field(default=None)

    proposed_action: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(SA_JSON))

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

    last_suggestions: Optional[List[int]] = Field(default=None, sa_column=Column(SA_JSON))

    contact_id: int = Field(foreign_key="contact.id", unique=True)
    contact: "Contact" = Relationship(back_populates="cart")

    items: List["CartItem"] = Relationship(back_populates="cart", sa_relationship_kwargs={"cascade": "all, delete-orphan"})

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

    notes: Optional[str] = Field(default=None, description="Observações do item (ex: Sem cebola)")

    product_id: int = Field(foreign_key="product.id")
    product: "Product" = Relationship()

    cart_id: int = Field(foreign_key="shoppingcart.id")
    cart: "ShoppingCart" = Relationship(back_populates="items")

class Order(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    total_amount: float
    status: OrderStatus = Field(default=OrderStatus.PENDING)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    psp_charge_id: Optional[str] = Field(default=None, index=True)
    customer_address: Optional[str] = Field(default=None)

    bot_id: int = Field(foreign_key="bot.id")
    bot: "Bot" = Relationship(back_populates="orders")

    # ▼▼▼ NOVOS CAMPOS ▼▼▼
    contact_id: Optional[int] = Field(default=None, foreign_key="contact.id")
    contact: Optional["Contact"] = Relationship()

    items: List["OrderItem"] = Relationship(back_populates="order", sa_relationship_kwargs={"cascade": "all, delete-orphan"})

class OrderItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    quantity: int
    price_at_time_of_order: float

    notes: Optional[str] = Field(default=None)

    order_id: int = Field(foreign_key="order.id")
    order: "Order" = Relationship(back_populates="items")

    product_id: int = Field(foreign_key="product.id")
    product: "Product" = Relationship()