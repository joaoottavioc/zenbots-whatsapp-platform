"""Cadastro Mágico (menu extraction) per-tier limits.

Single gatekeeper for the three upload endpoints that trigger extraction:

  - POST /bots/{id}/catalog/upload               (text catalog)
  - POST /bots/{id}/catalog/upload-from-file     (images + PDF)
  - POST /bots/{id}/catalog/upload-from-url      (URL scrape)

All three must call `consume_extraction_quota` before doing any work
(S3 upload, ARQ enqueue, LLM call). The function performs, in order:

  1. Shape validation — PDF allowed? image count within cap? — raised
     BEFORE incrementing the counter, so bad requests don't burn a slot.
  2. Atomic counter bump on `bot_monthly_usage.menu_extractions` via
     UPSERT-with-RETURNING. Two concurrent uploads cannot both pass the
     `used < limit` check: one observes `new_count <= limit`, the other
     observes `new_count > limit` and rolls back.
  3. On over-limit, the caller's transaction is rolled back and a 403
     raised so the counter is not persisted.

Failed extractions (worker crash, OpenAI 500, etc.) still count against
quota. The alternative (two-phase reserve/commit across Redis + PG +
ARQ) is an abuse vector and significant complexity. Admin refund is an
out-of-scope escape hatch.

Limits are data-driven via Plan columns (`max_menu_extractions_per_month`,
`allows_pdf_extraction`, `max_images_per_extraction`). No caching — this
is a low-volume endpoint (~3–5 calls/bot/month), not the WhatsApp hot
path. One SQL query per upload is fine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app import crud
from app.models import Plan, Subscription

logger = logging.getLogger(__name__)


# Fallback policy used only if no `free` Plan row is seeded (shouldn't
# happen in practice — migration r3s4t5u6v7w8 seeds it — but the
# extraction path must never 500 on a missing seed row). Matches the
# Free-tier contract from backlog_pricing.md and migration x9y0z1a2b3c4.
_FREE_FALLBACK_MAX_EXTRACTIONS = 3
_FREE_FALLBACK_MAX_IMAGES = 3


@dataclass(frozen=True)
class MenuExtractionPolicy:
    """Resolved per-plan limits for one bot.

    `None` on any limit field means "unlimited". Plan keys and tier
    strings are kept separate because billing.is_founder and
    is_founder-like flags can piggyback on the same policy later
    (Founder is tier='founder', not tier='pro').
    """

    plan_key: str
    tier: str
    max_per_month: Optional[int]
    allows_pdf: bool
    max_images: Optional[int]


def policy_from_plan(plan: Plan) -> MenuExtractionPolicy:
    return MenuExtractionPolicy(
        plan_key=plan.key,
        tier=plan.tier,
        max_per_month=plan.max_menu_extractions_per_month,
        allows_pdf=plan.allows_pdf_extraction,
        max_images=plan.max_images_per_extraction,
    )


def _free_fallback_policy() -> MenuExtractionPolicy:
    return MenuExtractionPolicy(
        plan_key="free",
        tier="free",
        max_per_month=_FREE_FALLBACK_MAX_EXTRACTIONS,
        allows_pdf=False,
        max_images=_FREE_FALLBACK_MAX_IMAGES,
    )


async def get_bot_policy(session: AsyncSession, bot_id: int) -> MenuExtractionPolicy:
    """Resolve the bot's current extraction policy.

    Mirrors the resolution used by billing_cache._resolve_tier_from_db:
    an authorized Subscription selects its Plan, otherwise the bot
    falls through to the Free plan. A missing `free` Plan row falls
    through to `_free_fallback_policy()` so uploads never 500 on a
    misconfigured DB.
    """
    # 1. Active subscription → its plan.
    sub_q = await session.execute(
        select(Subscription.plan_type).where(
            Subscription.bot_id == bot_id,
            Subscription.status == "authorized",
        )
    )
    plan_key = sub_q.scalar_one_or_none() or "free"

    plan = await crud.get_plan_by_key(session, plan_key)
    if plan is not None:
        return policy_from_plan(plan)

    # 2. Plan key resolved to nothing — try the Free row explicitly.
    if plan_key != "free":
        free_plan = await crud.get_plan_by_key(session, "free")
        if free_plan is not None:
            return policy_from_plan(free_plan)

    # 3. Neither the subscribed plan nor `free` exists — fall back to
    #    hardcoded Free defaults. Log loudly: this is a seed-data bug.
    logger.error(
        "menu_extraction_policy: no Plan row found (plan_key=%s, bot_id=%s) — "
        "using hardcoded Free fallback. Check alembic seed migrations.",
        plan_key,
        bot_id,
    )
    return _free_fallback_policy()


def _raise_pdf_not_allowed(policy: MenuExtractionPolicy) -> None:
    raise HTTPException(
        status_code=400,
        detail={
            "error": "pdf_not_allowed",
            "message": (
                "O plano Grátis aceita apenas imagens e texto. "
                "Atualize para Pro para extrair PDFs."
            ),
            "tier": policy.tier,
            "upgrade_url": "/portal/billing",
        },
    )


def _raise_too_many_images(policy: MenuExtractionPolicy, submitted: int) -> None:
    assert policy.max_images is not None
    raise HTTPException(
        status_code=400,
        detail={
            "error": "too_many_images",
            "message": (
                f"O plano {policy.tier} permite até {policy.max_images} "
                f"imagens por extração. Você enviou {submitted}."
            ),
            "tier": policy.tier,
            "max_images": policy.max_images,
            "submitted": submitted,
            "upgrade_url": "/portal/billing",
        },
    )


def _raise_quota_exceeded(policy: MenuExtractionPolicy) -> None:
    assert policy.max_per_month is not None
    raise HTTPException(
        status_code=403,
        detail={
            "error": "quota_exceeded",
            "message": (
                f"Você atingiu o limite de {policy.max_per_month} extrações "
                f"neste mês do plano {policy.tier}. "
                + (
                    "Atualize para Pro para 5 extrações/mês + PDFs."
                    if policy.tier == "free"
                    else "Entre em contato para um plano Enterprise."
                )
            ),
            "tier": policy.tier,
            "limit": policy.max_per_month,
            "upgrade_url": "/portal/billing",
        },
    )


async def consume_extraction_quota(
    session: AsyncSession,
    bot_id: int,
    *,
    has_pdf: bool,
    image_count: int,
) -> MenuExtractionPolicy:
    """Validate extraction request shape + atomically consume one slot.

    Order of checks matters:
      1. Shape validation raises first, so an invalid request (PDF on
         Free, too many images) does NOT consume a quota slot.
      2. The atomic INCREMENT happens only after shape is valid. If the
         post-increment count exceeds the plan cap, rollback the
         session's pending changes and raise 403.

    Returns the resolved policy on success so callers can log / surface
    the current tier to the response.
    """
    policy = await get_bot_policy(session, bot_id)

    # ── 1. Shape checks (do not consume quota) ────────────────────────
    if has_pdf and not policy.allows_pdf:
        _raise_pdf_not_allowed(policy)

    if policy.max_images is not None and image_count > policy.max_images:
        _raise_too_many_images(policy, image_count)

    # ── 2. Atomic counter bump + quota check ──────────────────────────
    if policy.max_per_month is None:
        # Unlimited tier (Enterprise) — still increment for admin dashboards.
        await crud.increment_and_return_menu_extractions(session, bot_id)
        return policy

    new_count = await crud.increment_and_return_menu_extractions(session, bot_id)
    if new_count > policy.max_per_month:
        # Rollback pending changes (the increment is the only write so
        # far; any caller-side writes before this point will also be
        # reverted, which is the desired all-or-nothing semantics).
        await session.rollback()
        _raise_quota_exceeded(policy)

    return policy
