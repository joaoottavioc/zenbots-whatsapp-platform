from fastapi import FastAPI
from app import whatsapp, auth
from app.database import create_db_and_tables
from app import bot_routes
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

@app.on_event("startup")
async def on_startup():
    await create_db_and_tables()

app.include_router(whatsapp.router)
app.include_router(auth.router)
app.include_router(bot_routes.router)
