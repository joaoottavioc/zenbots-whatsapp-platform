from pydantic import BaseSettings, Field

class Settings(BaseSettings):
    PENDING_ACTION_TTL_MINUTES: int = Field(default=10)

settings = Settings()   # lê do .env se existir
