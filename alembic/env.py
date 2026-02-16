import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from sqlmodel import SQLModel

# ------------------------------------------------------------------
# 1. IMPORTANTE: Importe seus Models aqui para o Alembic "vê-los"
# Certifique-se de importar o módulo onde seus modelos estão definidos
# ------------------------------------------------------------------
import sys
import os

# Adiciona o diretório raiz ao path para conseguir importar 'app'
sys.path.insert(0, os.getcwd()) 

from app.models import * # Importa todos os modelos (Bot, User, etc)

# Configuração do Alembic
config = context.config

# Configuração de Log
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Define os metadados dos seus modelos para geração automática
target_metadata = SQLModel.metadata

def run_migrations_offline() -> None:
    """Roda migrações no modo 'offline' (sem conexão, apenas gera SQL)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()

def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()

async def run_migrations_online() -> None:
    """Roda migrações no modo 'online' (conectado ao banco)."""
    
    # Cria a engine Assíncrona baseada na URL do .ini
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        # O "pulo do gato": Roda a migração síncrona dentro do contexto assíncrono
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()

if context.is_offline_mode():
    run_migrations_offline()
else:
    # Roda o loop assíncrono
    asyncio.run(run_migrations_online())