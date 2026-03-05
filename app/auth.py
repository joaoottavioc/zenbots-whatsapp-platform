import logging
import os
import re
import secrets
import uuid
from datetime import timedelta
from typing import Optional

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, validator
from sqlmodel.ext.asyncio.session import AsyncSession

from app import crud, models, schemas
from app.database import get_session
from app.schemas import ForgotPasswordRequest
from app.email_service import send_password_reset_email, send_verification_email
from sqlmodel import select
from app.time import utcnow
from app.rate_limiter import (
    is_rate_limited,
    create_sse_ticket,
    mark_reset_token_used,
    is_reset_token_used,
    store_email_verification_token,
    consume_email_verification_token,
)


logger = logging.getLogger(__name__)

# Carrega as variáveis de ambiente do arquivo .env
load_dotenv()

# --- Configuração de Segurança ---
# ✅ CORREÇÃO: A chave agora é lida de forma segura do seu arquivo .env
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY env var is not set.")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 horas

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)

# --- Cookie configuration ---
COOKIE_NAME = "access_token"
CSRF_COOKIE_NAME = "csrf_token"
COOKIE_MAX_AGE = 60 * 60 * 24  # 24h, matches JWT expiry
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "lax").lower()


def _is_secure_cookie() -> bool:
    # SameSite=None requires Secure=True (browser requirement)
    if COOKIE_SAMESITE == "none":
        return True
    return os.getenv("ENVIRONMENT", "development") != "development"


router = APIRouter()

# --- Funções Utilitárias de Autenticação ---


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = utcnow() + expires_delta
    else:
        expire = utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


# --- Password validation ---

COMMON_PASSWORDS = frozenset(
    [
        "password",
        "12345678",
        "123456789",
        "1234567890",
        "qwerty123",
        "abc12345",
        "password1",
        "iloveyou",
        "sunshine1",
        "princess1",
        "football1",
        "charlie1",
        "shadow12",
        "master12",
        "dragon12",
        "qwerty12",
        "michael1",
        "letmein1",
        "monkey123",
        "trustno1",
        "whatever1",
        "welcome1",
        "jordan23",
        "harley12",
        "robert12",
        "thomas12",
        "hockey12",
        "ranger12",
        "daniel12",
        "starwars1",
        "klaster1",
        "george12",
        "computer1",
        "michelle1",
        "jessica1",
        "pepper12",
        "1111111a",
        "zaq12wsx",
        "samsung1",
        "freedom1",
    ]
)


def validate_password_strength(v: str) -> str:
    """Shared password validation — used by all password schemas."""
    if len(v) < 8:
        raise ValueError("A senha deve ter pelo menos 8 caracteres.")
    if not re.search(r"[a-z]", v):
        raise ValueError("A senha deve conter pelo menos uma letra minúscula.")
    if not re.search(r"[A-Z]", v):
        raise ValueError("A senha deve conter pelo menos uma letra maiúscula.")
    if not re.search(r"\d", v):
        raise ValueError("A senha deve conter pelo menos um número.")
    if not re.search(r"[^a-zA-Z0-9]", v):
        raise ValueError("A senha deve conter pelo menos um caractere especial.")
    # Check both the full password and the alphanumeric-only version
    v_lower = v.lower()
    v_alpha = re.sub(r"[^a-z0-9]", "", v_lower)
    if v_lower in COMMON_PASSWORDS or v_alpha in COMMON_PASSWORDS:
        raise ValueError("Esta senha é muito comum. Escolha uma senha mais segura.")
    return v


# --- Schemas (Modelos de Dados da API) ---


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str

    @validator("password")
    def password_complexity(cls, v):
        return validate_password_strength(v)


# --- Rate Limit Dependencies ---


async def _check_auth_rate_limit(request: Request):
    """10 requests per 5 minutes per IP for /register."""
    client_ip = request.client.host if request.client else "unknown"
    key = f"rl:auth:{client_ip}"
    if await is_rate_limited(key, limit=10, window_seconds=300):
        logger.warning("RATE_LIMIT auth ip=%s", client_ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please try again later.",
        )


async def _check_login_rate_limit(request: Request):
    """5 requests per 5 minutes per IP for /token, /forgot-password, /reset-password."""
    client_ip = request.client.host if request.client else "unknown"
    key = f"rl:login:{client_ip}"
    if await is_rate_limited(key, limit=5, window_seconds=300):
        logger.warning("RATE_LIMIT login ip=%s", client_ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please try again later.",
        )


# --- Endpoints de Autenticação ---


@router.post(
    "/register", status_code=status.HTTP_201_CREATED, summary="Registra um novo usuário"
)
async def register(
    register_data: RegisterRequest,
    session: AsyncSession = Depends(get_session),
    _rate_limit: None = Depends(_check_auth_rate_limit),
):
    user = await crud.get_user_by_email(session, email=register_data.email)
    if user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered"
        )
    hashed_password = get_password_hash(register_data.password)
    new_user = models.User(email=register_data.email, hashed_password=hashed_password)

    session.add(new_user)
    await session.commit()
    await session.refresh(new_user)

    # Send verification email (non-blocking — registration succeeds even if email fails)
    try:
        token = await store_email_verification_token(new_user.id, new_user.email)
        await send_verification_email(new_user.email, token)
    except Exception as e:
        logger.error("Failed to send verification email on register: %s", e)

    return {
        "message": "User created successfully. Please check your email to verify your account."
    }


class VerifyEmailRequest(BaseModel):
    token: str


@router.post("/verify-email", summary="Verifica o e-mail do usuário via token")
async def verify_email(
    payload: VerifyEmailRequest,
    session: AsyncSession = Depends(get_session),
):
    result = await consume_email_verification_token(payload.token)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token inválido ou expirado.",
        )

    user_id, email = result
    user = await crud.get_user_by_id(session, user_id)
    if not user or user.email != email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token inválido ou expirado.",
        )

    if user.is_email_verified:
        return {"message": "E-mail já verificado."}

    user.is_email_verified = True
    session.add(user)
    await session.commit()

    return {"message": "E-mail verificado com sucesso!"}


class ResendVerificationRequest(BaseModel):
    email: EmailStr


@router.post("/resend-verification", summary="Reenvia o e-mail de verificação")
async def resend_verification(
    payload: ResendVerificationRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    _rate_limit: None = Depends(_check_auth_rate_limit),
):
    # Rate limit per email: 3 per hour
    email_key = f"rl:resend_verification:{payload.email}"
    if await is_rate_limited(email_key, limit=3, window_seconds=3600):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Muitas tentativas. Tente novamente mais tarde.",
        )

    user = await crud.get_user_by_email(session, email=payload.email)

    # Don't reveal whether the email exists
    if not user or user.is_email_verified:
        return {
            "message": "Se o e-mail existir e não estiver verificado, um link foi enviado."
        }

    try:
        token = await store_email_verification_token(user.id, user.email)
        await send_verification_email(user.email, token)
    except Exception as e:
        logger.error("Failed to send verification email on resend: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Falha ao enviar e-mail de verificação.",
        )

    return {
        "message": "Se o e-mail existir e não estiver verificado, um link foi enviado."
    }


@router.post(
    "/token",
    response_model=schemas.Token,
    summary="Realiza o login e retorna um token de acesso",
)
async def login_for_access_token(
    request: Request,
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
    session: AsyncSession = Depends(get_session),
    _rate_limit: None = Depends(_check_login_rate_limit),
):
    user = await crud.get_user_by_email(session, email=form_data.username)
    if not user or not verify_password(form_data.password, user.hashed_password):
        client_ip = request.client.host if request.client else "unknown"
        logger.warning("AUTH_FAIL login email=%s ip=%s", form_data.username, client_ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email não verificado. Verifique sua caixa de entrada ou solicite um novo link.",
        )

    access_token = create_access_token(data={"sub": user.email})
    csrf_token = secrets.token_urlsafe(32)
    secure = _is_secure_cookie()

    response.set_cookie(
        key=COOKIE_NAME,
        value=access_token,
        httponly=True,
        secure=secure,
        samesite=COOKIE_SAMESITE,
        path="/",
        max_age=COOKIE_MAX_AGE,
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=secure,
        samesite=COOKIE_SAMESITE,
        path="/",
        max_age=COOKIE_MAX_AGE,
    )

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "csrf_token": csrf_token,
    }


# --- Logout ---


@router.post("/logout", summary="Logout and clear auth cookies")
async def logout(response: Response):
    response.delete_cookie(key=COOKIE_NAME, path="/")
    response.delete_cookie(key=CSRF_COOKIE_NAME, path="/")
    return {"message": "Logged out successfully"}


# --- Dependência de Autenticação ---


def _extract_token(request: Request) -> Optional[str]:
    """Extract JWT from cookie first, then Authorization header."""
    token = request.cookies.get(COOKIE_NAME)
    if token:
        return token
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None


async def get_current_user(
    request: Request, session: AsyncSession = Depends(get_session)
) -> models.User:
    """
    Dependência para ser usada em endpoints protegidos.
    Valida o token JWT (cookie ou Bearer header) e retorna o objeto User.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    token = _extract_token(request)
    if token is None:
        raise credentials_exception

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


async def require_admin(
    current_user: models.User = Depends(get_current_user),
) -> models.User:
    """Dependency that ensures the current user is an admin."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return current_user


async def get_user_from_token(
    token: str, session: AsyncSession
) -> Optional[models.User]:
    """
    Decode a JWT and return the User, or None if invalid.
    Usable outside Depends() context (e.g. SSE endpoints).
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: Optional[str] = payload.get("sub")
        if email is None:
            return None
    except JWTError as e:
        logger.debug("JWT decode failed in get_user_from_token: %s", type(e).__name__)
        return None

    return await crud.get_user_by_email(session, email=email)


@router.post("/forgot-password")
async def forgot_password(
    payload: ForgotPasswordRequest,
    session: AsyncSession = Depends(get_session),
    _rate_limit: None = Depends(_check_login_rate_limit),
):
    # 1. Busca o usuário
    # CORREÇÃO: Usando 'select' importado e 'models.User'
    statement = select(models.User).where(models.User.email == payload.email)
    result = await session.execute(statement)
    user = result.scalars().first()

    # 2. Segurança: Se não achar, não dizemos "não existe"
    if not user:
        logger.warning("Password reset attempted for non-existent email")
        return {"message": "Se o e-mail existir, um link foi enviado."}

    # 3. Gera Token de Recuperação (15 min) com JTI para revogação
    reset_token = create_access_token(
        data={"sub": user.email, "type": "reset", "jti": str(uuid.uuid4())},
        expires_delta=timedelta(minutes=15),
    )

    # 4. Envia o E-mail
    try:
        await send_password_reset_email(user.email, reset_token)
    except Exception as e:
        logger.error("Failed to send password reset email: %s", e)
        raise HTTPException(
            status_code=500, detail="Falha ao enviar e-mail de recuperação."
        )

    return {"message": "Se o e-mail existir, um link foi enviado."}


# Schema para o pedido de reset
class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

    @validator("new_password")
    def password_complexity(cls, v):
        return validate_password_strength(v)


@router.post("/reset-password")
async def reset_password(
    payload: ResetPasswordRequest,
    session: AsyncSession = Depends(get_session),
    _rate_limit: None = Depends(_check_login_rate_limit),
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

    # 1b. Check if the reset token has already been used (via JTI)
    jti: Optional[str] = data.get("jti")
    if jti and await is_reset_token_used(jti):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Este link de redefinição já foi utilizado.",
        )

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

    # 4. Mark the reset token as used so it cannot be replayed
    if jti:
        await mark_reset_token_used(jti, ttl=900)

    return {"message": "Senha atualizada com sucesso!"}


# 1. Modelo de dados para receber as senhas
class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

    @validator("new_password")
    def password_complexity(cls, v):
        return validate_password_strength(v)


# 2. Rota Protegida (Exige token via get_current_user)
@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    current_user: models.User = Depends(get_current_user),  # Garante que está logado
    session: AsyncSession = Depends(get_session),
):
    # A. Verifica se a senha ATUAL está correta
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A senha atual está incorreta.",
        )

    # B. Verifica se a NOVA senha é igual à antiga (opcional, mas boa prática)
    if verify_password(payload.new_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A nova senha não pode ser igual à atual.",
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


@router.post("/sse-ticket")
async def issue_sse_ticket(current_user: models.User = Depends(get_current_user)):
    """
    Issue a short-lived, one-time-use ticket for SSE /stream authentication.
    The ticket expires in 60 seconds and can only be used once.
    """
    ticket = await create_sse_ticket(current_user.id)
    return {"ticket": ticket}
