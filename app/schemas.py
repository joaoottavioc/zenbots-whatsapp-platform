from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import List, Optional

# --- Schemas de Bot (Sem alterações na lógica) ---

class BotCreate(BaseModel):
    whatsapp_number: str
    system_prompt: str

# --- Schemas de Histórico (Novos) ---

class ConversationHistoryResponse(BaseModel):
    # Usado para representar uma única mensagem na resposta da API
    role: str
    content: str
    created_at: datetime
    
    # Configuração para converter o modelo do banco de dados para este schema
    model_config = ConfigDict(from_attributes=True)

# --- Schema de Resposta Completo ---

class BotResponse(BaseModel):
    # Dados básicos do Bot
    id: int
    user_id: int
    whatsapp_number: str
    system_prompt: str
    created_at: datetime
    
    # Adicionamos uma lista para carregar o histórico junto com os dados do bot
    history: List[ConversationHistoryResponse] = []
    
    # Configuração para converter o modelo do banco de dados para este schema
    model_config = ConfigDict(from_attributes=True)

# --- Schemas de Usuário (Exemplo para completar) ---

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
    
    
class BotUpdate(BaseModel):
    # O Optional[...] significa que o campo não é obrigatório.
    # O cliente pode enviar apenas o campo que deseja atualizar.
    system_prompt: Optional[str] = None
    # whatsapp_number: Optional[str] = None # Poderia adicionar outros campos aqui no futuro