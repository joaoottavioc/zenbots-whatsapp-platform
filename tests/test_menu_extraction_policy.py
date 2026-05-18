"""Per-tier limits for the Cadastro Mágico (menu extraction) feature.

The policy module enforces three rules at upload time:
  1. File-type gating (PDFs blocked on Free).
  2. Image-count cap (3 max on Free, unlimited on Pro/Founder).
  3. Monthly quota (3 on Free, 5 on Pro/Founder, unlimited on Enterprise).

Shape checks (#1, #2) must fire BEFORE the atomic counter increment
(#3) so a bad request doesn't burn a quota slot. On a quota violation
the caller's transaction must be rolled back so the increment is not
persisted.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.menu_extraction_policy import (
    MenuExtractionPolicy,
    consume_extraction_quota,
    get_bot_policy,
    policy_from_plan,
)


# ── Fixtures ─────────────────────────────────────────────────────────


def _plan(
    key: str,
    tier: str,
    max_per_month,
    allows_pdf: bool,
    max_images,
):
    plan = MagicMock()
    plan.key = key
    plan.tier = tier
    plan.max_menu_extractions_per_month = max_per_month
    plan.allows_pdf_extraction = allows_pdf
    plan.max_images_per_extraction = max_images
    return plan


FREE_PLAN = _plan("free", "free", 3, False, 3)
PRO_PLAN = _plan("pro_monthly", "pro", 5, True, None)
FOUNDER_PLAN = _plan("founder", "founder", 5, True, None)
ENTERPRISE_PLAN = _plan("enterprise", "enterprise", None, True, None)


def _session_returning_sub(plan_type):
    """Mock AsyncSession.execute that returns `plan_type` for Subscription lookup."""
    session = AsyncMock()
    sub_result = MagicMock()
    sub_result.scalar_one_or_none.return_value = plan_type
    session.execute.return_value = sub_result
    session.rollback = AsyncMock()
    return session


# ── policy_from_plan mapping ─────────────────────────────────────────


class TestPolicyFromPlan:
    def test_free_plan_maps_to_limits(self):
        p = policy_from_plan(FREE_PLAN)
        assert p == MenuExtractionPolicy(
            plan_key="free",
            tier="free",
            max_per_month=3,
            allows_pdf=False,
            max_images=3,
        )

    def test_pro_plan_maps_to_unlimited_images(self):
        p = policy_from_plan(PRO_PLAN)
        assert p.max_per_month == 5
        assert p.allows_pdf is True
        assert p.max_images is None

    def test_enterprise_is_fully_unlimited(self):
        p = policy_from_plan(ENTERPRISE_PLAN)
        assert p.max_per_month is None
        assert p.max_images is None
        assert p.allows_pdf is True


# ── get_bot_policy resolution ────────────────────────────────────────


class TestGetBotPolicy:
    @pytest.mark.asyncio
    async def test_bot_with_pro_subscription_returns_pro_policy(self):
        session = _session_returning_sub("pro_monthly")
        with patch(
            "app.menu_extraction_policy.crud.get_plan_by_key",
            new_callable=AsyncMock,
            return_value=PRO_PLAN,
        ):
            policy = await get_bot_policy(session, bot_id=1)
        assert policy.tier == "pro"
        assert policy.max_per_month == 5

    @pytest.mark.asyncio
    async def test_bot_with_no_subscription_falls_through_to_free(self):
        session = _session_returning_sub(None)  # no authorized sub
        with patch(
            "app.menu_extraction_policy.crud.get_plan_by_key",
            new_callable=AsyncMock,
            return_value=FREE_PLAN,
        ):
            policy = await get_bot_policy(session, bot_id=1)
        assert policy.tier == "free"
        assert policy.allows_pdf is False

    @pytest.mark.asyncio
    async def test_missing_plan_row_uses_hardcoded_free_fallback(self):
        """Neither the subscribed plan nor `free` exists — safety net kicks in."""
        session = _session_returning_sub("pro_monthly")
        with patch(
            "app.menu_extraction_policy.crud.get_plan_by_key",
            new_callable=AsyncMock,
            return_value=None,
        ):
            policy = await get_bot_policy(session, bot_id=1)
        # Fallback matches Free-tier contract from the migration.
        assert policy.tier == "free"
        assert policy.max_per_month == 3
        assert policy.allows_pdf is False
        assert policy.max_images == 3


# ── Shape validation (does NOT consume quota) ────────────────────────


class TestShapeValidation:
    @pytest.mark.asyncio
    async def test_pdf_on_free_rejected_before_increment(self):
        session = _session_returning_sub("free")
        with patch(
            "app.menu_extraction_policy.crud.get_plan_by_key",
            new_callable=AsyncMock,
            return_value=FREE_PLAN,
        ), patch(
            "app.menu_extraction_policy.crud.increment_and_return_menu_extractions",
            new_callable=AsyncMock,
        ) as mock_inc:
            with pytest.raises(HTTPException) as exc:
                await consume_extraction_quota(
                    session, bot_id=1, has_pdf=True, image_count=0
                )
            assert exc.value.status_code == 400
            assert exc.value.detail["error"] == "pdf_not_allowed"
            # Counter must NOT be touched on shape rejection.
            mock_inc.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_too_many_images_on_free_rejected_before_increment(self):
        session = _session_returning_sub("free")
        with patch(
            "app.menu_extraction_policy.crud.get_plan_by_key",
            new_callable=AsyncMock,
            return_value=FREE_PLAN,
        ), patch(
            "app.menu_extraction_policy.crud.increment_and_return_menu_extractions",
            new_callable=AsyncMock,
        ) as mock_inc:
            with pytest.raises(HTTPException) as exc:
                await consume_extraction_quota(
                    session, bot_id=1, has_pdf=False, image_count=4
                )
            assert exc.value.status_code == 400
            assert exc.value.detail["error"] == "too_many_images"
            assert exc.value.detail["submitted"] == 4
            assert exc.value.detail["max_images"] == 3
            mock_inc.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pdf_allowed_on_pro(self):
        session = _session_returning_sub("pro_monthly")
        with patch(
            "app.menu_extraction_policy.crud.get_plan_by_key",
            new_callable=AsyncMock,
            return_value=PRO_PLAN,
        ), patch(
            "app.menu_extraction_policy.crud.increment_and_return_menu_extractions",
            new_callable=AsyncMock,
            return_value=1,
        ):
            policy = await consume_extraction_quota(
                session, bot_id=1, has_pdf=True, image_count=0
            )
            assert policy.tier == "pro"

    @pytest.mark.asyncio
    async def test_many_images_allowed_on_pro(self):
        """Pro has max_images=None (unlimited) — 50 images must pass."""
        session = _session_returning_sub("pro_monthly")
        with patch(
            "app.menu_extraction_policy.crud.get_plan_by_key",
            new_callable=AsyncMock,
            return_value=PRO_PLAN,
        ), patch(
            "app.menu_extraction_policy.crud.increment_and_return_menu_extractions",
            new_callable=AsyncMock,
            return_value=1,
        ):
            await consume_extraction_quota(
                session, bot_id=1, has_pdf=False, image_count=50
            )


# ── Monthly quota enforcement ────────────────────────────────────────


class TestQuotaEnforcement:
    @pytest.mark.asyncio
    async def test_free_third_extraction_passes(self):
        session = _session_returning_sub("free")
        with patch(
            "app.menu_extraction_policy.crud.get_plan_by_key",
            new_callable=AsyncMock,
            return_value=FREE_PLAN,
        ), patch(
            "app.menu_extraction_policy.crud.increment_and_return_menu_extractions",
            new_callable=AsyncMock,
            return_value=3,  # post-increment = 3, equal to limit
        ):
            policy = await consume_extraction_quota(
                session, bot_id=1, has_pdf=False, image_count=1
            )
            assert policy.tier == "free"
            session.rollback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_free_fourth_extraction_rolls_back_and_rejects(self):
        session = _session_returning_sub("free")
        with patch(
            "app.menu_extraction_policy.crud.get_plan_by_key",
            new_callable=AsyncMock,
            return_value=FREE_PLAN,
        ), patch(
            "app.menu_extraction_policy.crud.increment_and_return_menu_extractions",
            new_callable=AsyncMock,
            return_value=4,  # post-increment = 4, over limit
        ):
            with pytest.raises(HTTPException) as exc:
                await consume_extraction_quota(
                    session, bot_id=1, has_pdf=False, image_count=1
                )
            assert exc.value.status_code == 403
            assert exc.value.detail["error"] == "quota_exceeded"
            assert exc.value.detail["limit"] == 3
            # The increment must be rolled back so it doesn't persist.
            session.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pro_sixth_extraction_rolls_back_and_rejects(self):
        session = _session_returning_sub("pro_monthly")
        with patch(
            "app.menu_extraction_policy.crud.get_plan_by_key",
            new_callable=AsyncMock,
            return_value=PRO_PLAN,
        ), patch(
            "app.menu_extraction_policy.crud.increment_and_return_menu_extractions",
            new_callable=AsyncMock,
            return_value=6,
        ):
            with pytest.raises(HTTPException) as exc:
                await consume_extraction_quota(
                    session, bot_id=1, has_pdf=True, image_count=0
                )
            assert exc.value.status_code == 403
            assert exc.value.detail["limit"] == 5
            session.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_enterprise_unlimited_never_raises_quota(self):
        session = _session_returning_sub("enterprise")
        with patch(
            "app.menu_extraction_policy.crud.get_plan_by_key",
            new_callable=AsyncMock,
            return_value=ENTERPRISE_PLAN,
        ), patch(
            "app.menu_extraction_policy.crud.increment_and_return_menu_extractions",
            new_callable=AsyncMock,
            return_value=9999,
        ):
            policy = await consume_extraction_quota(
                session, bot_id=1, has_pdf=True, image_count=100
            )
            assert policy.max_per_month is None
            session.rollback.assert_not_awaited()
