# app/embedding_service.py
from __future__ import annotations
import logging
from typing import List, Iterable
import unicodedata
import difflib
import math
import asyncio
import regex as re
from sentence_transformers import SentenceTransformer
import threading  # <--- ADICIONADO PARA PROTEÇÃO

logger = logging.getLogger(__name__)

# ========= Config dos modelos (384d em ambos) =========
# Catálogo (mantém o que você já tem)
_PRODUCTS_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"  # 384 dims

# Router de intenções (mais robusto a PT/typos; também 384d)
_ROUTER_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"

# Lazy-load
_model_products: SentenceTransformer | None = None
_model_router: SentenceTransformer | None = None

# ▼▼▼ CADEADOS DE SEGURANÇA (LOCKS) ▼▼▼
# Isso impede que 10 requisições carreguem o modelo ao mesmo tempo
_products_lock = threading.Lock()
_router_lock = threading.Lock()


def _get_products_model() -> SentenceTransformer:
    global _model_products
    # Primeira verificação (rápida, sem bloqueio)
    if _model_products is None:
        # Bloqueia a thread para carregar com segurança
        with _products_lock:
            # Segunda verificação (garante que ninguém carregou enquanto esperávamos)
            if _model_products is None:
                logger.info("Loading products embedding model (thread-safe)")
                _model_products = SentenceTransformer(_PRODUCTS_MODEL_NAME)
    return _model_products


def _get_router_model() -> SentenceTransformer:
    global _model_router
    if _model_router is None:
        with _router_lock:
            if _model_router is None:
                logger.info("Loading router embedding model (thread-safe)")
                _model_router = SentenceTransformer(_ROUTER_MODEL_NAME)
    return _model_router


# ========= Normalização focada em PT (para robustez a typos) =========

# Léxico mínimo de âncoras para fuzzy-correction (ajuste à vontade)
_LEXICON = {
    "carrinho",
    "pedido",
    "qual",
    "ver",
    "mostrar",
    "itens",
    "limpar",
    "esvaziar",
    "adicionar",
    "remover",
    "modificar",
    "finalizar",
    "fechar",
    "sugestoes",
    "sugestao",
}

_REPEAT_RE = re.compile(r"(.)\1{2,}")  # colapsa 3+ repetições


def _strip_accents(s: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch)
    )


def normalize_pt(text: str) -> str:
    """
    Normalização leve para o roteador (não use em catálogo).
    - lowercase, remove acentos, colapsa repetição exagerada
    - fuzzy em léxico pequeno de âncoras (evita 'carinho'→'carrinho', 'qual' etc.)
    """
    t = (text or "").strip().lower()
    t = _strip_accents(t)
    t = _REPEAT_RE.sub(r"\1\1", t)

    tokens = re.findall(r"[a-z0-9]+", t)
    corrected: List[str] = []
    for tok in tokens:
        if len(tok) < 4:
            corrected.append(tok)
            continue
        match = difflib.get_close_matches(tok, _LEXICON, n=1, cutoff=0.86)
        corrected.append(match[0] if match else tok)

    return " ".join(corrected)


# ========= Utilidades de embedding (com L2-normalização) =========


def _l2_norm(v: List[float]) -> List[float]:
    s = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / s for x in v]


def _encode_sync(texts: List[str], space: str, normalize: bool) -> List[List[float]]:
    if space == "router":
        model = _get_router_model()
        if normalize:
            texts = [normalize_pt(t if t is not None else "") for t in texts]
        else:
            texts = [t if t is not None else "" for t in texts]
    elif space == "products":
        model = _get_products_model()
        # ⚠️ Catálogo NÃO deve passar por normalize_pt (para não sujar busca)
        texts = [t if t is not None else "" for t in texts]
    else:
        raise ValueError(f"Unknown embedding space: {space}")

    embs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    try:
        arr = embs.tolist()
    except AttributeError:
        arr = [list(map(float, row)) for row in embs]
    return [_l2_norm(v) for v in arr]


# ---- APIs públicas ----


def embed_sync(
    texts: Iterable[str], space: str = "router", normalize: bool = True
) -> List[List[float]]:
    """Embed síncrono (útil para pré-processamentos/offline)."""
    return _encode_sync(list(texts), space=space, normalize=normalize)


async def embed_async(
    texts: Iterable[str], space: str = "router", normalize: bool = True
) -> List[List[float]]:
    """Embed assíncrono (não bloqueia o event loop)."""
    lst = list(texts)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _encode_sync, lst, space, normalize)


# ---- Compat para código já existente no catálogo ----


def generate_embedding(text: str) -> List[float]:
    """Compat: embedding individual para catálogo (products)."""
    # MANTÉM SÍNCRONO POR COMPATIBILIDADE, MAS EVITE USAR EM ROTAS ASYNC
    return embed_sync([text], space="products", normalize=False)[0]


def generate_embeddings(texts: List[str]) -> List[List[float]]:
    """Compat: batch para catálogo (products)."""
    return embed_sync(texts, space="products", normalize=False)


# ---- Atalhos ergonomicos (router) ----


async def embed_router(texts: Iterable[str]) -> List[List[float]]:
    return await embed_async(texts, space="router", normalize=True)


async def embed_products(texts: Iterable[str]) -> List[List[float]]:
    return await embed_async(texts, space="products", normalize=False)
