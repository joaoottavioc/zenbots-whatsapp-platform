from sqlmodel import SQLModel, Field, Relationship, Column
from pgvector.sqlalchemy import Vector
from typing import Optional, List, Dict, Any
from datetime import datetime
import enum
from sqlmodel import JSON as SA_JSON

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
    pix_key: Optional[str] = Field(default=None, index=True)
    
    user_id: int = Field(foreign_key="user.id")
    user: "User" = Relationship(back_populates="bots")

    # Relationships with cascade deletion
    products: List["Product"] = Relationship(back_populates="bot", sa_relationship_kwargs={"cascade": "all, delete-orphan"})
    history: List["ConversationHistory"] = Relationship(back_populates="bot", sa_relationship_kwargs={"cascade": "all, delete-orphan"})
    orders: List["Order"] = Relationship(back_populates="bot", sa_relationship_kwargs={"cascade": "all, delete-orphan"})
    contacts: List["Contact"] = Relationship(back_populates="bot", sa_relationship_kwargs={"cascade": "all, delete-orphan"})

class Contact(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    phone_number: str = Field(index=True)

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
    
    bot_id: int = Field(foreign_key="bot.id")
    bot: "Bot" = Relationship(back_populates="products")

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

class ShoppingCart(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    state: str = Field(default="GREETING")
    proposed_action: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(SA_JSON))
    last_activity_at: datetime = Field(default_factory=datetime.utcnow)


    contact_id: int = Field(foreign_key="contact.id", unique=True)
    contact: "Contact" = Relationship(back_populates="cart")

    items: List["CartItem"] = Relationship(back_populates="cart", sa_relationship_kwargs={"cascade": "all, delete-orphan"})

class CartItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    quantity: int

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

    bot_id: int = Field(foreign_key="bot.id")
    bot: "Bot" = Relationship(back_populates="orders")

    items: List["OrderItem"] = Relationship(back_populates="order", sa_relationship_kwargs={"cascade": "all, delete-orphan"})

class OrderItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    quantity: int
    price_at_time_of_order: float

    order_id: int = Field(foreign_key="order.id")
    order: "Order" = Relationship(back_populates="items")

    product_id: int = Field(foreign_key="product.id")
    product: "Product" = Relationship()