from sqlmodel import SQLModel, Field, Relationship, Column, TEXT
from pgvector.sqlalchemy import Vector
from typing import Optional, List
from datetime import datetime

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
    
    user_id: int = Field(foreign_key="user.id")
    user: "User" = Relationship(back_populates="bots")
    
    # ✅ CORREÇÃO: Relacionamentos com Product e ConversationHistory
    products: List["Product"] = Relationship(back_populates="bot")
    history: List["ConversationHistory"] = Relationship(back_populates="bot")


class Product(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    description: Optional[str] = Field(default=None)
    price: float
    embedding: List[float] = Field(sa_column=Column(Vector(384)))
    
    bot_id: int = Field(foreign_key="bot.id")
    bot: "Bot" = Relationship(back_populates="products")


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