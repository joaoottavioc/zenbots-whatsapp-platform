from sqlmodel import SQLModel, Field, Relationship
from typing import Optional, List
from datetime import datetime

# Tabela para armazenar as mensagens individuais de cada conversa
class ConversationHistory(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    
    bot_id: int = Field(foreign_key="bot.id")
    contact_number: str = Field(index=True)
    
    # Define o autor da mensagem: 'user' ou 'assistant' (o bot)
    role: str 
    # O conteúdo real da mensagem
    content: str 
    
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    
    # Cria o link de volta para o objeto Bot
    bot: "Bot" = Relationship(back_populates="history")

# Tabela de Bots, agora com o relacionamento para o histórico
class Bot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    whatsapp_number: str
    system_prompt: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    user_id: int = Field(foreign_key="user.id")
    
    # Cria os links para User e ConversationHistory
    user: "User" = Relationship(back_populates="bots")
    history: List["ConversationHistory"] = Relationship(back_populates="bot")

# Tabela de Usuários, agora com o relacionamento para os bots
class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str
    hashed_password: str
    
    # Cria o link para a lista de Bots que este usuário possui
    bots: List["Bot"] = Relationship(back_populates="user")
    
# Tabela para evitar processamento duplicado de webhooks (está ótima, sem alterações)
class ProcessedMessage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    message_id: str = Field(index=True, unique=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)