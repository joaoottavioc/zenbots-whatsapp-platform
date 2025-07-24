from sqlmodel import SQLModel, Field, Relationship, Column, TEXT
from pgvector.sqlalchemy import Vector
from typing import Optional, List
from datetime import datetime
import enum

# Forward declarations para resolver dependências circulares de tipo
class Bot(SQLModel):
    pass

class Product(SQLModel):
    pass

class ConversationHistory(SQLModel):
    pass

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
    pix_key: Optional[str] = Field(default=None, index=True)
    
    user_id: int = Field(foreign_key="user.id")
    user: "User" = Relationship(back_populates="bots")

    orders: List["Order"] = Relationship(
        back_populates="bot",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    
    # ✅ CORREÇÃO: Relacionamentos com Product e ConversationHistory
    products: List["Product"] = Relationship(
        back_populates="bot",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    history: List["ConversationHistory"] = Relationship(
        back_populates="bot",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )


class Product(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    description: Optional[str] = Field(default=None)
    price: float
    embedding: List[float] = Field(sa_column=Column(Vector(384)))
    
    bot_id: int = Field(foreign_key="bot.id")
    bot: "Bot" = Relationship(back_populates="products")
    order_items: List["OrderItem"] = Relationship()


class ConversationHistory(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    contact_number: str = Field(index=True)
    role: str 
    content: str = Field(sa_column=Column(TEXT))
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    
    bot_id: int = Field(foreign_key="bot.id")
    bot: "Bot" = Relationship(back_populates="history")
    

class ProcessedMessage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    message_id: str = Field(index=True, unique=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)

class OrderStatus(str, enum.Enum):
    """Define os status possíveis para um pedido."""
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    EXPIRED = "expired"

class OrderItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    quantity: int
    price_at_time_of_order: float # Guarda o preço do produto no momento da venda

    order_id: int = Field(foreign_key="order.id")
    order: "Order" = Relationship(back_populates="items")

    product_id: int = Field(foreign_key="product.id")
    product: "Product" = Relationship(back_populates="order_items")


class Order(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    total_amount: float
    status: OrderStatus = Field(default=OrderStatus.PENDING)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    # ID da cobrança no nosso provedor de pagamento (ex: Stripe, Mercado Pago)
    psp_charge_id: Optional[str] = Field(default=None, index=True)

    bot_id: int = Field(foreign_key="bot.id")
    bot: "Bot" = Relationship(back_populates="orders")

    # 👇 ADICIONE A REGRA DE CASCADE AQUI 👇
    items: List["OrderItem"] = Relationship(
        back_populates="order",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )