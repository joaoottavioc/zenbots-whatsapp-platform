# app/embedding_service.py

from sentence_transformers import SentenceTransformer
from typing import List

# Carrega um modelo leve e multilíngue, otimizado para busca semântica.
# O modelo será baixado na primeira vez que esta linha for executada.
# O objeto 'model' será mantido em memória para reuso, tornando as
# próximas chamadas muito mais rápidas.
model = SentenceTransformer('all-MiniLM-L6-v2')

def generate_embedding(text: str) -> List[float]:
    """
    Gera um vetor de embedding para um dado texto.
    O modelo 'all-MiniLM-L6-v2' gera vetores de 384 dimensões,
    que é o tamanho que definimos na nossa tabela Product.
    """
    # A função .tolist() converte o resultado para uma lista de floats padrão do Python.
    return model.encode(text).tolist()