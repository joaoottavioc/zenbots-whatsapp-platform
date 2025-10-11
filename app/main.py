from fastapi import FastAPI
from app import whatsapp, auth
from app.database import create_db_and_tables
from app import bot_routes
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
from app import bot_routes, auth, takeover_routes

load_dotenv()

app = FastAPI()

# ▼▼▼ ADICIONE ESTE BLOCO INTEIRO ▼▼▼
# Configuração do CORS
origins = [
    # Permite todas as origens. Para desenvolvimento, é o mais simples.
    # Em produção, você pode restringir para domínios específicos.
    "*"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"], # Permite todos os métodos (GET, POST, etc.)
    allow_headers=["*"], # Permite todos os cabeçalhos
)
# ▲▲▲ FIM DO BLOCO ▲▲▲

@app.on_event("startup")
async def on_startup():
    await create_db_and_tables()

app.include_router(whatsapp.router)
app.include_router(auth.router)
app.include_router(bot_routes.router)
app.include_router(takeover_routes.router)
