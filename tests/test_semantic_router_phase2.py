# tests/test_semantic_router_phase2.py
"""
Tests for semantic router Phase 2 expansion (T2-2).

Validates:
- NEGATE intent exists with prototype phrases
- All original intents still exist
- Expanded prototype counts (more phrases per intent)
- Thresholds adjusted correctly (lowered for high-confidence intents)
- Every intent has a corresponding threshold
- No duplicate phrases across intents
"""

from app.semantic_router import PROTOS, THRESHOLDS


class TestProtosExpanded:
    def test_all_original_intents_preserved(self):
        """All 9 original intents must still exist."""
        original_intents = {
            "SHOW_CART",
            "ADD",
            "CLEAR_CART",
            "FINISH_ORDER",
            "REMOVE",
            "MODIFY",
            "REQUEST_SUGGESTION",
            "GREETING_OR_QUESTION",
            "CONFIRM",
        }
        assert original_intents.issubset(set(PROTOS.keys()))

    def test_negate_intent_added(self):
        """NEGATE must be a new intent with prototype phrases."""
        assert "NEGATE" in PROTOS
        assert len(PROTOS["NEGATE"]) >= 5

    def test_negate_contains_key_phrases(self):
        phrases = PROTOS["NEGATE"]
        assert "não" in phrases
        assert "não quero" in phrases

    def test_add_expanded(self):
        """ADD should have significantly more prototypes than original 4."""
        assert len(PROTOS["ADD"]) >= 10

    def test_add_contains_new_phrases(self):
        phrases = PROTOS["ADD"]
        new_expected = {"quero pedir", "me manda", "me vê", "eu quero"}
        assert new_expected.issubset(set(phrases))

    def test_finish_order_expanded(self):
        """FINISH_ORDER should have more prototypes."""
        assert len(PROTOS["FINISH_ORDER"]) >= 10
        assert "é isso" in PROTOS["FINISH_ORDER"]
        assert "pronto" in PROTOS["FINISH_ORDER"]

    def test_confirm_expanded(self):
        """CONFIRM should include common affirmative expressions."""
        phrases = PROTOS["CONFIRM"]
        assert len(phrases) >= 10
        new_expected = {"aham", "uhum", "fechou", "perfeito"}
        assert new_expected.issubset(set(phrases))

    def test_remove_expanded(self):
        """REMOVE should have more than the original 2 phrases."""
        assert len(PROTOS["REMOVE"]) >= 5

    def test_greeting_expanded(self):
        """GREETING_OR_QUESTION should include informal greetings."""
        phrases = PROTOS["GREETING_OR_QUESTION"]
        assert len(phrases) >= 10
        assert "e aí" in phrases
        assert "salve" in phrases

    def test_modify_expanded(self):
        assert len(PROTOS["MODIFY"]) >= 4

    def test_request_suggestion_expanded(self):
        assert len(PROTOS["REQUEST_SUGGESTION"]) >= 8


class TestThresholds:
    def test_every_intent_has_threshold(self):
        """Every intent in PROTOS must have a corresponding threshold."""
        for intent in PROTOS:
            assert intent in THRESHOLDS, f"Missing threshold for {intent}"

    def test_negate_has_threshold(self):
        assert "NEGATE" in THRESHOLDS
        assert 0.70 <= THRESHOLDS["NEGATE"] <= 0.90

    def test_greeting_threshold_lowered(self):
        """GREETING_OR_QUESTION threshold should be <= 0.73 (from 0.75)."""
        assert THRESHOLDS["GREETING_OR_QUESTION"] <= 0.73

    def test_confirm_threshold_lowered(self):
        """CONFIRM threshold should be <= 0.83 (from 0.85)."""
        assert THRESHOLDS["CONFIRM"] <= 0.83

    def test_add_threshold_lowered(self):
        """ADD threshold should be <= 0.79 (from 0.80)."""
        assert THRESHOLDS["ADD"] <= 0.79

    def test_finish_order_threshold_lowered(self):
        """FINISH_ORDER threshold should be <= 0.79 (from 0.80)."""
        assert THRESHOLDS["FINISH_ORDER"] <= 0.79

    def test_all_thresholds_in_valid_range(self):
        """All thresholds should be between 0.60 and 0.95."""
        for intent, thresh in THRESHOLDS.items():
            assert 0.60 <= thresh <= 0.95, f"{intent} threshold {thresh} out of range"


class TestNoDuplicates:
    def test_no_duplicate_phrases_within_intent(self):
        """No intent should have duplicate prototype phrases."""
        for intent, phrases in PROTOS.items():
            assert len(phrases) == len(set(phrases)), f"Duplicate phrases in {intent}"

    def test_no_orphan_thresholds(self):
        """No threshold entry should exist without a matching PROTOS intent."""
        for intent in THRESHOLDS:
            assert intent in PROTOS, f"Orphan threshold for {intent}"
