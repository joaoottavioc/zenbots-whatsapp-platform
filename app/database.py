from sqlmodel import SQLModel
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

# Criando as tabelas no banco de forma assíncrona
async def create_db_and_tables():
    async with engine.begin() as conn:
        # AVISO: A linha abaixo APAGA TODOS os dados. Use apenas para desenvolvimento.
        #DONT--await conn.run_syncDONT(SQLModel.metadata.drop_all)DONT--

        
        # Esta linha recria tudo a partir dos seus modelos mais recentes.
        await conn.run_sync(SQLModel.metadata.create_all)
