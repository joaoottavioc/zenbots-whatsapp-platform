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
from app.schemas import ForgotPasswordRequest
from app.email_service import send_password_reset_email
from sqlmodel import select
from app.email_service import send_password_reset_email


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

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
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

@router.post("/forgot-password")
async def forgot_password(
    payload: ForgotPasswordRequest,
    session: AsyncSession = Depends(get_session)
):
    # 1. Busca o usuário
    # CORREÇÃO: Usando 'select' importado e 'models.User'
    statement = select(models.User).where(models.User.email == payload.email)
    result = await session.execute(statement)
    user = result.scalars().first()

    # 2. Segurança: Se não achar, não dizemos "não existe"
    if not user:
        print(f"⚠️ Tentativa de reset para e-mail inexistente: {payload.email}")
        return {"message": "Se o e-mail existir, um link foi enviado."}

    # 3. Gera Token de Recuperação (15 min)
    reset_token = create_access_token(
        data={"sub": user.email, "type": "reset"}, 
        expires_delta=timedelta(minutes=15)
    )

    # 4. Envia o E-mail
    try:
        await send_password_reset_email(user.email, reset_token)
    except Exception as e:
        print(f"❌ Erro ao enviar e-mail: {e}")
        raise HTTPException(status_code=500, detail="Falha ao enviar e-mail de recuperação.")

    return {"message": "Se o e-mail existir, um link foi enviado."}


# Schema para o pedido de reset
class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

@router.post("/reset-password")
async def reset_password(
    payload: ResetPasswordRequest,
    session: AsyncSession = Depends(get_session)
):
    # 1. Decodificar e Validar o Token
    credentials_exception = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Token inválido ou expirado.",
    )
    
    try:
        # Decodifica o token usando a SECRET_KEY
        data = jwt.decode(payload.token, SECRET_KEY, algorithms=[ALGORITHM])
        email: Optional[str] = data.get("sub")
        token_type: Optional[str] = data.get("type")
        
        if email is None or token_type != "reset":
            raise credentials_exception
            
    except JWTError:
        raise credentials_exception

    # 2. Buscar o Usuário
    statement = select(models.User).where(models.User.email == email)
    result = await session.execute(statement)
    user = result.scalars().first()

    if not user:
        raise credentials_exception

    # 3. Atualizar a Senha
    user.hashed_password = get_password_hash(payload.new_password)
    session.add(user)
    await session.commit()

    return {"message": "Senha atualizada com sucesso!"}

# 1. Modelo de dados para receber as senhas
class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

# 2. Rota Protegida (Exige token via get_current_user)
@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    current_user: models.User = Depends(get_current_user), # Garante que está logado
    session: AsyncSession = Depends(get_session)
):
    # A. Verifica se a senha ATUAL está correta
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A senha atual está incorreta."
        )

    # B. Verifica se a NOVA senha é igual à antiga (opcional, mas boa prática)
    if verify_password(payload.new_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A nova senha não pode ser igual à atual."
        )

    # C. Criptografa e salva a nova senha
    current_user.hashed_password = get_password_hash(payload.new_password)
    session.add(current_user)
    await session.commit()

    return {"message": "Senha alterada com sucesso!"}

# Rota para o Frontend pegar os dados do usuário logado
@router.get("/me", response_model=schemas.UserResponse)
async def read_users_me(current_user: models.User = Depends(get_current_user)):
    """
    Retorna os dados do usuário atual baseado no Token JWT enviado no Header.
    """
    return current_user