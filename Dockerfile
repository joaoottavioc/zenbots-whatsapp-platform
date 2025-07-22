# Use official Python image
FROM python:3.10

# Set working directory
WORKDIR /code

# Copy and install dependencies first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- MUDANÇA ADICIONADA AQUI ---
# Pré-aquece o cache, baixando o modelo de embedding durante o build.
# Isso garante que a aplicação inicie instantaneamente, sem travar no download.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# Copy the rest of the application code
COPY . .

# Expose port
EXPOSE 8000

# Default command to run the app (sem --reload, ideal para produção)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]