# app/rate_limiter.py
import redis
import os

# Pega a URL do ambiente (definida no docker-compose) ou usa localhost como fallback
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Cria a conexão (pool)
# decode_responses=True faz o Redis devolver strings ao invés de bytes
r = redis.from_url(REDIS_URL, decode_responses=True)

def is_spamming(user_phone: str, limit: int = 15, window_seconds: int = 60) -> bool:
    """
    Verifica se o usuário excedeu o limite de mensagens na janela de tempo.
    Retorna True se for SPAM (deve bloquear).
    """
    if not user_phone:
        return False

    # Cria uma chave única para este usuário
    key = f"spam:{user_phone}"

    try:
        # Pipelining agrupa comandos para ser mais rápido (1 viagem ao Redis)
        pipe = r.pipeline()
        pipe.incr(key) # Incrementa contador
        pipe.ttl(key)  # Pega quanto tempo falta para expirar
        result = pipe.execute()

        current_count = result[0]
        ttl = result[1]

        # Se é a primeira mensagem (ou expirou), define o tempo de vida (TTL)
        if current_count == 1 or ttl == -1:
            r.expire(key, window_seconds)

        # Se passou do limite, é spam
        if current_count > limit:
            return True
            
        return False

    except redis.RedisError as e:
        # Se o Redis cair, não queremos parar o bot completamente.
        # Logamos o erro e deixamos passar (Fail Open).
        print(f"⚠️ Erro no Redis Rate Limiter: {e}")
        return False