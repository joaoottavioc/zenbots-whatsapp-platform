# app/embedding_service.py
from __future__ import annotations
import logging
from typing import List, Iterable
import unicodedata
import difflib
import math
import asyncio
import regex as re
import threading  # <--- ADICIONADO PARA PROTEÇÃO

logger = logging.getLogger(__name__)


class EmbeddingsUnavailable(Exception):
    """Raised when the embedding model cannot be loaded.

    Callers should catch this and degrade gracefully — for product search,
    that means skipping the semantic similarity layer and returning whatever
    the keyword/ILIKE layers found. For the semantic router, it means
    falling back to a low-confidence default intent so the LLM tool-calling
    path can still handle the message.

    Added 2026-04-09 after Hugging Face Hub rate-limited the model_info()
    call during SentenceTransformer init, which used to cascade into a
    process-wide outage where every shopping intent crashed with "algo
    deu errado".
    """


# ========= Config do modelo (384d, compartilhado entre router e catálogo) =========
_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"  # 384 dims

# Lazy-load: single shared instance (same model for router + products)
_model = None
_model_lock = threading.Lock()
_model_load_failed = False  # sticky flag — once set, stop retrying


def _get_model():
    """Return the loaded SentenceTransformer model, or raise EmbeddingsUnavailable.

    Sticky failure: once a load attempt fails, subsequent calls raise
    immediately rather than retrying every time. This avoids hammering
    HF Hub when it's rate-limiting. The flag clears only on container
    restart (which is the right escalation — if HF is down for more
    than a minute, embeddings genuinely aren't available).
    """
    global _model, _model_load_failed
    if _model is not None:
        return _model
    if _model_load_failed:
        raise EmbeddingsUnavailable(
            "Embedding model previously failed to load; not retrying. "
            "Restart the container to retry."
        )
    with _model_lock:
        if _model is not None:
            return _model
        if _model_load_failed:
            raise EmbeddingsUnavailable(
                "Embedding model previously failed to load; not retrying."
            )
        logger.info("Loading embedding model (thread-safe)")
        try:
            from sentence_transformers import SentenceTransformer

            _model = SentenceTransformer(_MODEL_NAME)
            logger.info("Embedding model loaded successfully")
            return _model
        except Exception as e:
            _model_load_failed = True
            logger.error(
                "FAILED to load embedding model: %s. "
                "Embedding-dependent features will degrade gracefully — "
                "semantic router falls back to default intent, product "
                "search uses keyword/ILIKE layers only.",
                e,
            )
            raise EmbeddingsUnavailable(str(e)) from e


# Public aliases for backward compat (both return the same instance)
_get_products_model = _get_model
_get_router_model = _get_model


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
    model = _get_model()
    if space == "router":
        if normalize:
            texts = [normalize_pt(t if t is not None else "") for t in texts]
        else:
            texts = [t if t is not None else "" for t in texts]
    elif space == "products":
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


# ---- Atalhos ergonomicos (router) ----


async def embed_router(texts: Iterable[str]) -> List[List[float]]:
    return await embed_async(texts, space="router", normalize=True)
