# app/worker.py
import os
import asyncio
from arq.connections import RedisSettings
# Importamos a função pesada que já existe
from app.whatsapp import process_whatsapp_message 

# Configurações do Redis (Pega do env ou usa default)
# Note o database=1 para separar da fila do RateLimit
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DATABASE = 1 

# Configurações do Worker do ARQ
class WorkerSettings:
    # Configuração da conexão
    redis_settings = RedisSettings(
        host=REDIS_HOST,
        port=REDIS_PORT,
        database=REDIS_DATABASE
    )
    
    # Funções que este worker sabe executar
    functions = [process_whatsapp_message]
    
    # Configurações extras
    max_jobs = 10  # Quantas mensagens processar ao mesmo tempo por worker
    poll_delay = 0.5 # Tempo de espera se a fila estiver vazia
    
    # Executado quando o worker inicia
    async def on_startup(self):
        print("🚀 Worker de Mensagens Iniciado! Aguardando jobs...")

    # Executado quando o worker desliga
    async def on_shutdown(self):
        print("🛑 Worker Encerrando...")