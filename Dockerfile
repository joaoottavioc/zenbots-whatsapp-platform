# ============================================
# Base stage — shared by all targets
# ============================================
FROM python:3.10-slim AS base
WORKDIR /code

# Prevent stale .pyc bytecode cache issues across deploys
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Non-root user for security
RUN useradd -m -u 1000 appuser

# Install dependencies (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-warm embedding model (cached layer, ~400MB)
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')"

# Copy application code
COPY . .
RUN chown -R appuser:appuser /code

USER appuser

# ============================================
# Backend target — serves HTTP via Uvicorn
# ============================================
FROM base AS backend
EXPOSE 8000
# SIGTERM triggers graceful shutdown via uvicorn
ENV WEB_WORKERS=2
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_WORKERS}"]

# ============================================
# Worker target — ARQ async job consumer
# ============================================
FROM base AS worker
# stopTimeout=120s in ECS task def gives running jobs time to finish
CMD ["arq", "app.worker.WorkerSettings"]

# ============================================
# Migrations target — one-off alembic task
# ============================================
FROM base AS migrations
CMD ["python", "run_migrations.py"]
