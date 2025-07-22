import os
import re
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, validator
from sqlmodel.ext.asyncio.session import AsyncSession

from app import crud, models, schemas
from app.database import get_session

# Carrega as variáveis de ambiente do arquivo .env
load_dotenv()

# --- Configuração de Segurança ---
# ✅ CORREÇÃO: A chave agora é lida de forma segura do seu arquivo .env
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 horas

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

router = APIRouter()

# --- Funções Utilitárias de Autenticação ---

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# --- Schemas (Modelos de Dados da API) ---

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str

    @validator('password')
    def password_complexity(cls, v):
        if len(v) < 8:
            raise ValueError('A senha deve ter pelo menos 8 caracteres.')
        if not re.search(r'[a-z]', v):
            raise ValueError('A senha deve conter pelo menos uma letra minúscula.')
        if not re.search(r'[A-Z]', v):
            raise ValueError('A senha deve conter pelo menos uma letra maiúscula.')
        if not re.search(r'\d', v):
            raise ValueError('A senha deve conter pelo menos um número.')
        return v

# --- Endpoints de Autenticação ---

@router.post("/register", status_code=status.HTTP_201_CREATED, summary="Registra um novo usuário")
async def register(
    register_data: RegisterRequest, 
    session: AsyncSession = Depends(get_session)
):
    user = await crud.get_user_by_email(session, email=register_data.email)
    if user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Email already registered"
        )
    hashed_password = get_password_hash(register_data.password)
    new_user = models.User(email=register_data.email, hashed_password=hashed_password)
    
    session.add(new_user)
    await session.commit()
    
    return {"message": "User created successfully"}

@router.post("/token", response_model=schemas.Token, summary="Realiza o login e retorna um token de acesso")
async def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    session: AsyncSession = Depends(get_session)
):
    user = await crud.get_user_by_email(session, email=form_data.username)
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    access_token = create_access_token(data={"sub": user.email})
    return {"access_token": access_token, "token_type": "bearer"}

# --- Dependência de Autenticação ---

async def get_current_user(
    token: str = Depends(oauth2_scheme), 
    session: AsyncSession = Depends(get_session)
) -> models.User:
    """
    Dependência para ser usada em endpoints protegidos.
    Valida o token JWT e retorna o objeto User completo do banco de dados.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: Optional[str] = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    
    user = await crud.get_user_by_email(session, email=email)
    if user is None:
        raise credentials_exception
        
    return user