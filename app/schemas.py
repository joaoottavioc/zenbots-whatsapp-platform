from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import List, Optional, Any

# --- Schemas de Produto ---
# Usados para criar e retornar itens do cardápio

class ProductBase(BaseModel):
    name: str
    description: Optional[str] = None
    price: float

class ProductCreate(ProductBase):
    pass

class ProductResponse(ProductBase):
    id: int
    bot_id: int
    model_config = ConfigDict(from_attributes=True)

class ProductUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None

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
    restaurant_name: str
    whatsapp_number: str
    pix_key: Optional[str] = None

class BotUpdate(BaseModel):
    """Schema para atualizar os dados de um bot."""
    restaurant_name: Optional[str] = None
    whatsapp_number: Optional[str] = None
    pix_key: Optional[str] = None

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
    pix_key: Optional[str] = None
    
    # O bot agora retorna a lista de produtos e de histórico associados a ele
    products: List[ProductResponse] = []
    history: List[ConversationHistoryResponse] = []
    
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