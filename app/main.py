import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from arq import create_pool
from arq.connections import RedisSettings
from app.bot_routes import router as bot_router
from app.payment_routes import router as payment_router

# Importações dos seus módulos locais
from app.database import create_db_and_tables
from app import whatsapp, auth, bot_routes, takeover_routes
from fastapi.responses import StreamingResponse
import redis.asyncio as redis
import asyncio
import json
import ast
from app import billing_routes

load_dotenv()

# Configurações do Redis para a Fila
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DATABASE = 1  # Mesmo banco definido no worker.py

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- INICIALIZAÇÃO (STARTUP) ---
    print("🚀 Inicializando aplicação...")
    
    # 1. Cria as tabelas do Banco de Dados
    await create_db_and_tables()
    print("✅ Banco de dados verificado.")

    # 2. Cria o pool de conexão com o Redis da Fila (ARQ)
    print("🔌 Conectando ao Redis Queue...")
    try:
        app.state.arq_redis = await create_pool(
            RedisSettings(host=REDIS_HOST, port=REDIS_PORT, database=REDIS_DATABASE)
        )
        print("✅ Conexão com Redis Queue estabelecida!")
    except Exception as e:
        print(f"❌ Falha ao conectar no Redis: {e}")
    
    yield  # O servidor roda aqui e atende as requisições
    
    # --- ENCERRAMENTO (SHUTDOWN) ---
    print("🔌 Fechando conexão com Redis Queue...")
    if hasattr(app.state, 'arq_redis'):
        await app.state.arq_redis.close()
    print("🛑 Aplicação encerrada.")

# Criação única da aplicação com o ciclo de vida configurado
app = FastAPI(lifespan=lifespan)

# --- Configuração do CORS ---
app.add_middleware(
    CORSMiddleware,
    # Em vez de listar origens fixas, usamos um Regex que aceita tudo que começa com http/https
    # Isso resolve o problema de URLs do Ngrok mudando toda hora e permite credenciais.
    allow_origin_regex="https?://.*", 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Inclusão das Rotas ---
app.include_router(whatsapp.router)
app.include_router(auth.router, prefix="/auth", tags=["Auth"])
app.include_router(bot_routes.router)
app.include_router(takeover_routes.router)
app.include_router(bot_router, prefix="/api/v1")
app.include_router(payment_router)
app.include_router(billing_routes.router)

# Rota de verificação de saúde (Health Check)
@app.get("/")
async def root():
    return {"status": "ZenBots API Online 🚀", "queue": "Active"}

@app.get("/stream")
async def stream_events(request: Request):
    """
    Rota SSE Robusta com Heartbeat e Sanitização de JSON.
    """
    async def event_generator():
        # 1. Configuração da Conexão Redis
        local_redis_url = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DATABASE}"
        
        r = redis.from_url(local_redis_url, encoding="utf-8", decode_responses=True)
        pubsub = r.pubsub()
        await pubsub.subscribe("dashboard_events")
        
        try:
            print("📡 Cliente conectado ao stream SSE")
            # Envia o ping inicial já em formato JSON correto
            yield f"data: {json.dumps({'type': 'ping', 'message': 'connected'})}\n\n"
            
            while True:
                # 2. Verifica desconexão do cliente
                if await request.is_disconnected():
                    print("📴 Cliente desconectou do stream")
                    break
                
                # 3. Aguarda mensagem do Redis (Timeout curto p/ manter o loop rodando)
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                
                if message:
                    raw_data = message['data']
                    final_payload = raw_data

                    # --- BLOCO DE CORREÇÃO (Sanitização de JSON) ---
                    try:
                        # Tenta ler como JSON padrão. Se falhar, é string Python.
                        json.loads(raw_data)
                    except json.JSONDecodeError:
                        try:
                            # Converte string Python "{'a': 1}" para Dict, depois para JSON "{\"a\": 1}"
                            dict_data = ast.literal_eval(raw_data)
                            final_payload = json.dumps(dict_data)
                        except Exception as e:
                            print(f"⚠️ Erro ao converter dados do Redis: {e}")
                            # Payload de emergência para não quebrar o front
                            final_payload = json.dumps({"type": "error", "message": "Dados inválidos do Redis"})
                    
                    print(f"📤 Enviando evento SSE: {final_payload}")
                    yield f"data: {final_payload}\n\n"
                else:
                    # 4. Heartbeat (Mantém a conexão viva em Load Balancers/Nginx)
                    yield ": keep-alive\n\n"
                
        except asyncio.CancelledError:
            print("Client disconnected (CancelledError)")
        except Exception as e:
            print(f"❌ Erro crítico no Stream: {e}")
            # Tenta avisar o front se a conexão ainda existir
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            # 5. Limpeza de recursos
            try:
                await pubsub.unsubscribe("dashboard_events")
                await r.aclose()
            except:
                pass

    return StreamingResponse(
        event_generator(), 
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no", # Essencial para Nginx
        }
    )