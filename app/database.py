from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

# Criando o engine assíncrono
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=20,
    max_overflow=10,
    pool_timeout=30,
    pool_pre_ping=True,
    pool_recycle=3600,
)

# Criando a factory de sessão assíncrona
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# Gerando sessões assíncronas
async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
