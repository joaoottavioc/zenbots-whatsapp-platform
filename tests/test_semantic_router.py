# tests/test_semantic_router.py
"""
Tests for app/semantic_router.py.

Covered:
- cos_sim          (pure sync function)
- semantic_intent  (async, embed_router mocked)
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, patch

import app.semantic_router as sr
from app.semantic_router import cos_sim, semantic_intent, PROTOS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unit_vec(*components):
    """Return a list of floats from the given components (no normalisation needed
    in tests — we control the dot products directly)."""
    return list(components)


# ---------------------------------------------------------------------------
# cos_sim (pure, sync)
# ---------------------------------------------------------------------------


class TestCosSim:
    def test_identical_vectors(self):
        """Dot product of identical unit vectors equals 1.0."""
        assert cos_sim([1.0, 0.0], [1.0, 0.0]) == 1.0

    def test_orthogonal_vectors(self):
        """Dot product of perpendicular vectors equals 0.0."""
        assert cos_sim([1.0, 0.0], [0.0, 1.0]) == 0.0

    def test_opposite_vectors(self):
        """Dot product of anti-parallel unit vectors equals -1.0."""
        assert cos_sim([1.0, 0.0], [-1.0, 0.0]) == -1.0


# ---------------------------------------------------------------------------
# semantic_intent (async)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_emb_cache():
    """Wipe the module-level _EMB_CACHE before every test so there is no
    cross-test state contamination."""
    sr._EMB_CACHE.clear()
    yield
    sr._EMB_CACHE.clear()


class TestSemanticIntent:
    async def test_returns_best_matching_intent(self):
        """
        Set up embed_router so that:
        - The user query embeds to [1.0, 0.0, 0.0]
        - SHOW_CART proto phrases embed to [[1.0, 0.0, 0.0], ...] (high similarity)
        - All other intent proto phrases embed to [[0.0, 1.0, 0.0], ...] (zero similarity)

        Expected: semantic_intent returns "SHOW_CART" with score 1.0.
        """
        len(PROTOS["SHOW_CART"])
        [k for k in PROTOS if k != "SHOW_CART"]

        # The call sequence is:
        #   Phase 1 — _ensure_proto_embeddings calls embed_router once per intent
        #             (in PROTOS dict order).
        #   Phase 2 — the user-query call: embed_router([text]) → [[1.0, 0.0, 0.0]]
        #
        # We build a side_effect list that matches this order exactly.

        side_effects = []
        for intent in PROTOS:
            n = len(PROTOS[intent])
            if intent == "SHOW_CART":
                # High similarity: all phrases aligned with query vector
                side_effects.append([[1.0, 0.0, 0.0]] * n)
            else:
                # Orthogonal to query vector → zero dot product
                side_effects.append([[0.0, 1.0, 0.0]] * n)

        # User query: same direction as SHOW_CART protos
        side_effects.append([[1.0, 0.0, 0.0]])

        mock_embed = AsyncMock(side_effect=side_effects)

        with patch("app.semantic_router.embed_router", mock_embed):
            intent, score, phrase = await semantic_intent("ver meu carrinho")

        assert intent == "SHOW_CART"
        assert score == pytest.approx(1.0)
        assert phrase in PROTOS["SHOW_CART"]

    async def test_default_intent_when_no_match(self):
        """
        When all proto embeddings are orthogonal to the query, every dot product
        is 0.0.  The function still returns the intent with the highest score —
        it just happens to be whichever intent is iterated first (or tied at 0.0).
        The important contract is: the function does not raise and the returned
        score is 0.0.
        """
        # All proto phrases → [1.0, 0.0] (orthogonal to query)
        side_effects = []
        for intent in PROTOS:
            n = len(PROTOS[intent])
            side_effects.append([[1.0, 0.0]] * n)

        # User query → [0.0, 1.0] — orthogonal to every proto
        side_effects.append([[0.0, 1.0]])

        mock_embed = AsyncMock(side_effect=side_effects)

        with patch("app.semantic_router.embed_router", mock_embed):
            intent, score, phrase = await semantic_intent("xyzzy nonsense")

        # Score should be 0.0 (dot product of [0,1] with [1,0])
        assert score == pytest.approx(0.0)
        # Returned intent must be one of the valid intents
        assert intent in PROTOS

    async def test_cache_is_populated_after_first_call(self):
        """
        After the first call to semantic_intent the _EMB_CACHE must be filled
        with one entry per intent so that a second call skips re-embedding the
        prototypes and only calls embed_router once (for the query).
        """
        n_calls_proto = len(PROTOS)

        side_effects_first_run = []
        for intent in PROTOS:
            n = len(PROTOS[intent])
            side_effects_first_run.append([[0.5, 0.5, 0.0]] * n)
        # Query embedding for first call
        side_effects_first_run.append([[0.5, 0.5, 0.0]])
        # Query embedding for second call (cache must be warm — no proto calls)
        side_effects_first_run.append([[0.5, 0.5, 0.0]])

        mock_embed = AsyncMock(side_effect=side_effects_first_run)

        with patch("app.semantic_router.embed_router", mock_embed):
            await semantic_intent("primeira chamada")
            await semantic_intent("segunda chamada")

        # Total calls: n_calls_proto (proto phase) + 2 (one query per call)
        assert mock_embed.call_count == n_calls_proto + 2

    async def test_score_reflects_highest_similarity(self):
        """
        If two intents both have protos, the one with the higher dot product
        wins and its score is returned correctly.
        """
        # Three-dimensional space.
        # FINISH_ORDER protos: [0.9, 0.1, 0.0] — closer to query
        # All other intents:   [0.0, 0.0, 1.0] — orthogonal to query
        # User query:          [1.0, 0.0, 0.0]
        # Expected FINISH_ORDER score: 0.9

        side_effects = []
        for intent in PROTOS:
            n = len(PROTOS[intent])
            if intent == "FINISH_ORDER":
                side_effects.append([[0.9, 0.1, 0.0]] * n)
            else:
                side_effects.append([[0.0, 0.0, 1.0]] * n)

        side_effects.append([[1.0, 0.0, 0.0]])  # user query

        mock_embed = AsyncMock(side_effect=side_effects)

        with patch("app.semantic_router.embed_router", mock_embed):
            intent, score, phrase = await semantic_intent("fechar pedido agora")

        assert intent == "FINISH_ORDER"
        assert score == pytest.approx(0.9)

    async def test_concurrent_cold_start_single_load(self):
        """
        When 5 tasks call semantic_intent concurrently on cold start,
        embed_router should only be called len(PROTOS) times for proto
        embeddings (not 5x), thanks to the asyncio.Lock.
        """
        n_protos = len(PROTOS)

        call_count = 0

        async def mock_embed(phrases):
            nonlocal call_count
            call_count += 1
            # Small delay to simulate real work and increase race window
            await asyncio.sleep(0.01)
            return [[0.5, 0.5, 0.0]] * len(phrases)

        mock_fn = AsyncMock(side_effect=mock_embed)

        with patch("app.semantic_router.embed_router", mock_fn):
            tasks = [semantic_intent(f"query {i}") for i in range(5)]
            await asyncio.gather(*tasks)

        # Proto embeddings: n_protos calls (only once, not 5x)
        # Query embeddings: 5 calls (one per task)
        assert mock_fn.call_count == n_protos + 5
