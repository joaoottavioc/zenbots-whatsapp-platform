# app/semantic_router.py
from __future__ import annotations
import logging
from typing import List, Tuple, Dict

from app.embedding_service import embed_router  # ✅ centralizado

logger = logging.getLogger(__name__)


def cos_sim(u: List[float], v: List[float]) -> float:
    return sum(a * b for a, b in zip(u, v))


# Protótipos SÓ em PT (como você preferiu)
PROTOS: Dict[str, List[str]] = {
    "SHOW_CART": [
        "qual meu pedido",
        "ver carrinho",
        "mostrar carrinho",
        "o que eu pedi",
        "itens do meu carrinho",
    ],
    "ADD": [
        "quero adicionar itens",
        "adiciona no carrinho",
        "coloca esses pratos",
        "mandar pratos com quantidade",
    ],
    "CLEAR_CART": [
        "esvaziar carrinho",
        "limpar carrinho",
        "cancelar tudo",
        "pode apagar td",
        "cancela",
    ],
    "FINISH_ORDER": [
        "fechar pedido",
        "finalizar compra",
        "pode fechar",
        "só isso mesmo",
        "por hoje é só",
        "somente isso",
    ],
    "REMOVE": ["remover item", "tirar do carrinho"],
    "MODIFY": ["alterar quantidade", "trocar quantidade do item"],
    "REQUEST_SUGGESTION": [
        "ver sugestões",
        "me indique algo",
        "o que você recomenda",
        "sim, sugestões",
        "sugestão",
    ],
    "GREETING_OR_QUESTION": [
        "oi",
        "olá",
        "bom dia",
        "boa tarde",
        "boa noite",
        "tudo bem?",
        "como vai?",
    ],
    "CONFIRM": ["sim", "claro", "pode ser", "confirmo", "confirmar", "ok"],
}

THRESHOLDS = {
    "SHOW_CART": 0.76,
    "ADD": 0.80,
    "CLEAR_CART": 0.83,
    "FINISH_ORDER": 0.80,
    "REMOVE": 0.80,
    "MODIFY": 0.80,
    "REQUEST_SUGGESTION": 0.77,
    "GREETING_OR_QUESTION": 0.75,
    "CONFIRM": 0.85,
}

_EMB_CACHE: Dict[str, List[List[float]]] = {}


async def _ensure_proto_embeddings():
    if _EMB_CACHE:
        return
    logger.debug("Populating semantic router embedding cache (%d intents)", len(PROTOS))
    for intent, phrases in PROTOS.items():
        _EMB_CACHE[intent] = await embed_router(phrases)
    logger.debug("Semantic router cache populated")


def _argmax(xs: List[float]) -> Tuple[int, float]:
    i = max(range(len(xs)), key=lambda k: xs[k])
    return i, xs[i]


async def semantic_intent(text: str) -> Tuple[str, float, str]:
    await _ensure_proto_embeddings()
    q = (await embed_router([text]))[0]
    best_intent, best_score, best_phrase = "GREETING_OR_QUESTION", -1.0, ""
    for intent, embs in _EMB_CACHE.items():
        sims = [cos_sim(q, e) for e in embs]
        i, s = _argmax(sims)
        if s > best_score:
            best_intent, best_score, best_phrase = intent, s, PROTOS[intent][i]
    logger.debug(
        "Semantic intent: %s (score=%.3f, phrase='%s')",
        best_intent,
        best_score,
        best_phrase,
    )
    return best_intent, best_score, best_phrase


# (se você usa o detector de "SHOW_CART", reimporte embed_router ali também)
