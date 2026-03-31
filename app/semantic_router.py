# app/semantic_router.py
from __future__ import annotations
import asyncio
import logging
from typing import List, Tuple, Dict

from app.embedding_service import embed_router  # ✅ centralizado

logger = logging.getLogger(__name__)


def cos_sim(u: List[float], v: List[float]) -> float:
    return sum(a * b for a, b in zip(u, v))


# Protótipos SÓ em PT (como você preferiu)
# T2-2: Expanded prototypes to push semantic router coverage from ~60-70% to ~85-90%
PROTOS: Dict[str, List[str]] = {
    "SHOW_CART": [
        "qual meu pedido",
        "ver carrinho",
        "mostrar carrinho",
        "o que eu pedi",
        "itens do meu carrinho",
        "meu carrinho",
        "ver meu pedido",
    ],
    "ADD": [
        "quero adicionar itens",
        "adiciona no carrinho",
        "coloca esses pratos",
        "mandar pratos com quantidade",
        "quero pedir",
        "me manda",
        "bota aí",
        "coloca",
        "manda ver",
        "vou querer",
        "pode mandar",
        "me vê",
        "eu quero",
        # Longer prototypes matching real orders with product names + quantities
        "quero um hamburguer e uma coca",
        "me manda uma pizza e um suco",
        "quero dois lanches e uma batata frita",
        "quero uma coca-cola e um lanche",
    ],
    "CLEAR_CART": [
        "esvaziar carrinho",
        "limpar carrinho",
        "cancelar tudo",
        "pode apagar td",
        "cancela",
        "apaga tudo",
        "zera o carrinho",
    ],
    "FINISH_ORDER": [
        "fechar pedido",
        "finalizar compra",
        "pode fechar",
        "só isso mesmo",
        "por hoje é só",
        "somente isso",
        "é isso",
        "tá bom assim",
        "pronto",
        "quero fechar",
        "finalizar",
        "fecha aí",
    ],
    "REMOVE": [
        "remover item",
        "tirar do carrinho",
        "tira isso",
        "remove esse",
        "não quero mais",
        "pode tirar",
        "tira dois do meu pedido",
        "pode tirar 5 prensadão",
        "tire 3 coca do carrinho",
        "retira 9 bagunça",
    ],
    "MODIFY": [
        "alterar quantidade",
        "trocar quantidade do item",
        "na verdade quero",
        "pode mudar para",
        "muda pra",
        "mudar a quantidade",
    ],
    "REQUEST_SUGGESTION": [
        "ver sugestões",
        "me indique algo",
        "o que você recomenda",
        "sim, sugestões",
        "sugestão",
        "tem sobremesa",
        "algo com peixe",
        "queria ver os vinhos",
        "o que tem de bom",
        "me sugere algo",
        "tem dicas de pedidos",
        "dicas de pedidos",
        "sugestões de pedidos",
        "me manda sugestões",
        "o que sugere",
        "o que tem pra pedir",
    ],
    "GREETING_OR_QUESTION": [
        "oi",
        "olá",
        "bom dia",
        "boa tarde",
        "boa noite",
        "tudo bem?",
        "como vai?",
        "e aí",
        "fala",
        "salve",
        "como funciona",
        "qual o horário",
        "oi, tudo bem",
    ],
    "CONFIRM": [
        "sim",
        "claro",
        "pode ser",
        "confirmo",
        "confirmar",
        "ok",
        "isso mesmo",
        "com certeza",
        "pode sim",
        "aham",
        "uhum",
        "positivo",
        "fechou",
        "combinado",
        "perfeito",
    ],
    "NEGATE": [
        "não",
        "nope",
        "nah",
        "não quero",
        "deixa pra lá",
        "cancela isso",
        "melhor não",
        "não precisa",
        "nao",
    ],
    "ORDER_CANCEL": [
        "cancelar meu pedido",
        "quero cancelar o pedido",
        "cancela o pedido",
        "não quero mais o pedido",
        "desistir do pedido",
        "cancelar pedido",
        "quero cancelar minha encomenda",
    ],
    "ORDER_REPEAT": [
        "repetir pedido",
        "mesmo pedido",
        "quero o mesmo",
        "repete o ultimo",
        "mesmo de sempre",
        "repetir ultimo pedido",
        "quero o mesmo pedido",
    ],
}

THRESHOLDS = {
    "SHOW_CART": 0.76,
    "ADD": 0.78,
    "CLEAR_CART": 0.83,
    "FINISH_ORDER": 0.78,
    "REMOVE": 0.78,
    "MODIFY": 0.78,
    "REQUEST_SUGGESTION": 0.75,
    "GREETING_OR_QUESTION": 0.72,
    "CONFIRM": 0.82,
    "NEGATE": 0.82,
    "ORDER_CANCEL": 0.82,
    "ORDER_REPEAT": 0.80,
}

_EMB_CACHE: Dict[str, List[List[float]]] = {}
_EMB_LOCK = asyncio.Lock()


async def _ensure_proto_embeddings():
    if _EMB_CACHE:
        return
    async with _EMB_LOCK:
        if _EMB_CACHE:  # double-check after acquiring lock
            return
        logger.debug(
            "Populating semantic router embedding cache (%d intents)", len(PROTOS)
        )
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
