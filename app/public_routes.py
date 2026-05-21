"""Public portfolio endpoints (no auth, no rate-limit by user).

P5 of plan/portfolio_pivot.md. These are read-only artifacts surfaced
for recruiters / curious visitors — separate from `chat_routes` (web
widget customer-facing) and `monitoring_routes` (owner-authenticated
cost data).

Endpoints:

  - GET /public/eval/latest  → docs/eval/latest.json

The snapshot is committed to the repo (see scripts/snapshot_corpus_qa.py
+ docs/eval/latest.json) so every deploy serves a deterministic value
without needing to run the corpus suite at request time. To refresh
the numbers, re-run the snapshot script, commit, redeploy.

CORS is wide-open for this router because the response is intentionally
public; the frontend `/eval` page may be fetched from anywhere
(including from a third-party blog post embedding it). The data itself
is aggregate-only — no per-bot, per-customer, or per-conversation
content.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Path as PathParam
from fastapi.responses import JSONResponse
from sqlalchemy import select, func

from app import crud
from app.database import async_session
from app.models import Bot, Contact, ConversationHistory, Product, UsageEvent, User

logger = logging.getLogger(__name__)

# The demo bot is the bot owned by the user the seed script creates
# (demo@zenbotz.com.br). Identifying it by *owner email* instead of
# slug means a dashboard rename ("Pizzaria do Zé" → "Johns Dog",
# slug "pizzaria-do-ze" → "johns-hot-dog") keeps the /eval page
# pointing at the right bot without code changes.
#
# The seed slug from scripts/seed_demo_restaurant.py is kept around as
# a defensive fallback for environments where the seed user got
# removed but a bot with the canonical slug still exists. Never
# hardcoded as the primary identifier.
from scripts.seed_demo_restaurant import DEMO_USER_EMAIL, DEMO_BOT_SLUG

# Cap on how many recent conversations the index shows. Tighter than
# the dashboard's owner-only limit because this is unauthenticated
# traffic — recruiters skim 5-10 conversations, not 100.
_DEMO_INDEX_LIMIT = 10
_DEMO_TRACE_MESSAGE_LIMIT = 60

# Short-but-non-trivial cache: data changes when new conversations
# happen on the demo, which is sporadic. 60s keeps the page fresh
# without hammering the DB on every render.
_DEMO_TRACE_CACHE_CONTROL = "public, max-age=60, s-maxage=60"

router = APIRouter(prefix="/public", tags=["Public"])

# Snapshot lives at repo-root/docs/eval/latest.json. main.py runs from
# the repo root in the container (WORKDIR /code), so the relative path
# resolves correctly. Compute it once at import.
_SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "eval" / "latest.json"

# Short browser cache so a hammered page doesn't re-fetch on every nav,
# but short enough that a deploy with refreshed numbers shows up
# promptly. The data only changes when scripts/snapshot_corpus_qa.py
# is re-run + committed, which is at most weekly in practice.
_CACHE_CONTROL = "public, max-age=300, s-maxage=300"


@router.get(
    "/eval/latest",
    summary="Latest corpus QA snapshot",
    response_class=JSONResponse,
)
async def get_eval_latest() -> JSONResponse:
    """Return the most recent corpus QA snapshot.

    Shape mirrors `scripts/snapshot_corpus_qa.py:build_snapshot` — keys:
    `schema_version`, `generated_at`, `headline`, `comprehension`,
    `consistency`, `scenarios`, `methodology`.

    503 if the snapshot file is missing (e.g., a fresh dev environment
    that hasn't been seeded yet). 500 if it's present but malformed
    (means someone committed a broken JSON, which is a real bug).
    """
    if not _SNAPSHOT_PATH.exists():
        logger.warning("Eval snapshot missing at %s", _SNAPSHOT_PATH)
        raise HTTPException(
            status_code=503,
            detail={
                "error": "eval_snapshot_unavailable",
                "message": (
                    "Eval snapshot has not been generated yet. Run "
                    "scripts/snapshot_corpus_qa.py to seed it."
                ),
            },
        )

    try:
        data = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        logger.error("Eval snapshot malformed at %s: %s", _SNAPSHOT_PATH, e)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "eval_snapshot_invalid",
                "message": "Snapshot file is not valid JSON.",
            },
        )

    return JSONResponse(
        content=data,
        headers={"Cache-Control": _CACHE_CONTROL},
    )


# ─────────────────────────────────────────────────────────────────────
# Public conversation-trace surface — DEMO BOT ONLY
#
# P4-public of plan/portfolio_pivot.md. Mirrors the owner-only
# /bots/{bot_id}/conversations/{contact_id}/trace endpoint, but scoped
# rigidly to the seeded demo bot so a recruiter doesn't need to sign
# up to see the per-message AI telemetry. The endpoints below verify
# the contact_id belongs to the demo bot before returning anything
# — pasting a different bot's contact_id returns 404, not data.
#
# Privacy posture: conversations on the demo bot are public-by-design
# (the widget at /widget?slug=pizzaria-do-ze accepts anyone), so
# rendering them publicly does not leak anything that wasn't already
# observable to whoever sent the messages.
# ─────────────────────────────────────────────────────────────────────


def _short_session(phone: str) -> str:
    """Render a web identity for the UI without showing the full
    session_id. Web contacts have phone_number = 'web:{uuid}'; we keep
    the first 6 chars of the uuid as a stable but anonymized handle."""
    if phone.startswith("web:"):
        sid = phone[4:]
        return f"web:{sid[:6]}" if len(sid) > 6 else phone
    return phone


async def _resolve_demo_bot(session) -> Bot | None:
    """Resolve the demo bot regardless of its current slug.

    Lookup strategy:
      1. The bot owned by the seeded demo user (demo@zenbotz.com.br).
         This is the source of truth — even after the owner renames
         the bot or changes its slug, the user_id linkage stays.
      2. Fallback: bot with the canonical seed slug, for environments
         where the demo user was deleted but the slug-named bot
         survives.
      3. Returns None if neither resolves.
    """
    user = (
        await session.execute(select(User).where(User.email == DEMO_USER_EMAIL))
    ).scalar_one_or_none()
    if user:
        bot = (
            await session.execute(
                select(Bot)
                .where(Bot.user_id == user.id)
                .order_by(Bot.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if bot:
            return bot
    # Fallback path — preserve old behavior when the demo user is missing.
    return await crud.get_bot_by_slug(session, DEMO_BOT_SLUG)


@router.get(
    "/trace/demo/conversations",
    summary="Recent demo conversations (index)",
)
async def list_demo_conversations() -> JSONResponse:
    """List the most recent conversations on the demo bot.

    Each item carries enough to render a clickable card on /trace
    (preview of first message, message count, total cost, total
    duration). The detail endpoint below renders the full trace for
    one conversation.

    Returns 404 if the demo bot hasn't been seeded (fresh dev env).
    """
    async with async_session() as session:
        bot = await _resolve_demo_bot(session)
        if not bot:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "demo_bot_unavailable",
                    "message": (
                        "O bot demo ainda não foi semeado neste ambiente. "
                        "Execute scripts/seed_demo_restaurant.py."
                    ),
                },
            )

        # Pull recent contacts that actually have history (otherwise
        # empty contacts created by a chat that never sent a message
        # clutter the list).
        contacts_q = (
            select(
                Contact.id,
                Contact.phone_number,
                Contact.channel,
                func.count(ConversationHistory.id).label("msg_count"),
                func.min(ConversationHistory.created_at).label("started_at"),
                func.max(ConversationHistory.created_at).label("last_at"),
            )
            .join(ConversationHistory, ConversationHistory.contact_id == Contact.id)
            .where(Contact.bot_id == bot.id)
            .group_by(Contact.id, Contact.phone_number, Contact.channel)
            .order_by(func.max(ConversationHistory.created_at).desc())
            .limit(_DEMO_INDEX_LIMIT)
        )
        contact_rows = (await session.execute(contacts_q)).all()

        if not contact_rows:
            return JSONResponse(
                content={"bot_id": bot.id, "conversations": []},
                headers={"Cache-Control": _DEMO_TRACE_CACHE_CONTROL},
            )

        contact_ids = [r.id for r in contact_rows]

        # Sum cost + duration per contact via the events' trace_ids.
        # Two-step lookup: history rows → distinct trace_ids → events.
        history_q = select(
            ConversationHistory.contact_id, ConversationHistory.trace_id
        ).where(
            ConversationHistory.contact_id.in_(contact_ids),
            ConversationHistory.trace_id.isnot(None),
        )
        traces_by_contact: dict[int, set[str]] = {}
        for row in (await session.execute(history_q)).all():
            traces_by_contact.setdefault(row.contact_id, set()).add(row.trace_id)

        # First user message preview per contact — gives a clickable
        # "what was this conversation about" label.
        preview_q = (
            select(ConversationHistory.contact_id, ConversationHistory.content)
            .where(
                ConversationHistory.contact_id.in_(contact_ids),
                ConversationHistory.role == "user",
            )
            .order_by(
                ConversationHistory.contact_id,
                ConversationHistory.created_at.asc(),
            )
        )
        preview_by_contact: dict[int, str] = {}
        for row in (await session.execute(preview_q)).all():
            if row.contact_id not in preview_by_contact:
                preview_by_contact[row.contact_id] = row.content

        # Aggregate cost + duration per contact.
        all_trace_ids = {
            tid for trace_set in traces_by_contact.values() for tid in trace_set
        }
        stats_by_contact: dict[int, dict] = {
            cid: {"cost": 0.0, "ms": 0, "tokens": 0} for cid in contact_ids
        }
        if all_trace_ids:
            events_q = select(
                UsageEvent.trace_id,
                UsageEvent.cost_usd,
                UsageEvent.duration_ms,
                UsageEvent.input_tokens,
                UsageEvent.output_tokens,
            ).where(UsageEvent.bot_id == bot.id, UsageEvent.trace_id.in_(all_trace_ids))
            trace_to_contact: dict[str, int] = {}
            for cid, trace_set in traces_by_contact.items():
                for tid in trace_set:
                    trace_to_contact[tid] = cid
            for ev in (await session.execute(events_q)).all():
                cid = trace_to_contact.get(ev.trace_id)
                if cid is None:
                    continue
                stats_by_contact[cid]["cost"] += ev.cost_usd
                stats_by_contact[cid]["ms"] += ev.duration_ms
                stats_by_contact[cid]["tokens"] += ev.input_tokens + ev.output_tokens

        conversations = []
        for row in contact_rows:
            stats = stats_by_contact[row.id]
            conversations.append(
                {
                    "contact_id": row.id,
                    "identity": _short_session(row.phone_number),
                    "channel": row.channel,
                    "message_count": row.msg_count,
                    "preview": preview_by_contact.get(row.id, "")[:120],
                    "started_at": row.started_at.isoformat()
                    if row.started_at
                    else None,
                    "last_at": row.last_at.isoformat() if row.last_at else None,
                    "totals": {
                        "cost_usd": round(stats["cost"], 6),
                        "duration_ms": stats["ms"],
                        "tokens": stats["tokens"],
                    },
                }
            )

    return JSONResponse(
        content={
            "bot_id": bot.id,
            "bot_slug": bot.slug,
            "conversations": conversations,
        },
        headers={"Cache-Control": _DEMO_TRACE_CACHE_CONTROL},
    )


@router.get(
    "/trace/demo/conversations/{contact_id}",
    summary="Full trace for one demo conversation",
)
async def get_demo_conversation_trace(
    contact_id: int = PathParam(..., ge=1),
) -> JSONResponse:
    """Per-message AI telemetry for a single demo conversation.

    Re-uses crud.get_conversation_trace under the hood; the only
    difference from the owner-only endpoint is the security model.
    Here we verify the contact belongs to the demo bot before doing
    anything else — pasting another bot's contact_id returns 404.

    Web identities are anonymized in the response (phone_number is
    shortened to 'web:{first 6 chars}' so a session_id can't be
    harvested via this endpoint).
    """
    async with async_session() as session:
        bot = await _resolve_demo_bot(session)
        if not bot:
            raise HTTPException(
                status_code=404,
                detail={"error": "demo_bot_unavailable"},
            )

        contact = (
            await session.execute(
                select(Contact).where(
                    Contact.id == contact_id, Contact.bot_id == bot.id
                )
            )
        ).scalar_one_or_none()
        if not contact:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "conversation_not_found",
                    "message": (
                        "Esta conversa não existe ou não pertence ao bot demo."
                    ),
                },
            )

        trace = await crud.get_conversation_trace(
            session,
            bot_id=bot.id,
            contact_id=contact_id,
            limit_messages=_DEMO_TRACE_MESSAGE_LIMIT,
        )

    # Tag the public view with a non-PII identity label so the frontend
    # doesn't need to format it.
    trace["identity"] = _short_session(contact.phone_number)
    trace["channel"] = contact.channel
    trace["bot_slug"] = bot.slug

    return JSONResponse(
        content=trace,
        headers={"Cache-Control": _DEMO_TRACE_CACHE_CONTROL},
    )


# ─────────────────────────────────────────────────────────────────────
# Demo restaurant menu — public read of seeded products
#
# Surfaced on /eval so recruiters can see what's available to order
# before they open the widget. Closes the "what should I type?"
# friction loop without forcing them to wander the widget UI.
# ─────────────────────────────────────────────────────────────────────

# Canonical category ordering — matches scripts/seed_demo_restaurant.py
# so the rendered menu reads like a real restaurant card (mains first,
# then sides, drinks, desserts). New categories not in this list fall
# to the end in alphabetical order.
_DEMO_CATEGORY_ORDER = [
    "Pizzas",
    "Esfihas",
    "Acompanhamentos",
    "Bebidas",
    "Sobremesas",
]


@router.get(
    "/demo/menu",
    summary="Demo restaurant menu — products grouped by category",
)
async def get_demo_menu() -> JSONResponse:
    """Public, read-only view of the demo restaurant's menu.

    Returns categories in the canonical order defined above, with
    products sorted by price desc within each (cheap-first reads
    weirdly for a menu). Only available products are returned.
    """
    async with async_session() as session:
        bot = await _resolve_demo_bot(session)
        if not bot:
            raise HTTPException(
                status_code=404,
                detail={"error": "demo_bot_unavailable"},
            )

        products_q = (
            select(Product)
            .where(
                Product.bot_id == bot.id,
                Product.is_available.is_(True),
            )
            .order_by(Product.category.asc(), Product.price.desc())
        )
        rows = (await session.execute(products_q)).scalars().all()

    # Group preserving insertion order so the response shape is
    # deterministic for the frontend.
    by_category: dict[str, list[dict]] = {}
    for p in rows:
        # Skip soft-deleted products defensively (the model has
        # is_deleted; the query above doesn't exclude it because some
        # rows predate that column).
        if getattr(p, "is_deleted", False):
            continue
        by_category.setdefault(p.category, []).append(
            {
                "name": p.name,
                "price": round(p.price, 2),
                "description": p.description or "",
            }
        )

    # Order categories: canonical first, then anything else alphabetically.
    def _cat_sort_key(name: str) -> tuple[int, str]:
        try:
            return (_DEMO_CATEGORY_ORDER.index(name), "")
        except ValueError:
            return (len(_DEMO_CATEGORY_ORDER), name.lower())

    categories = [
        {"name": name, "products": by_category[name]}
        for name in sorted(by_category.keys(), key=_cat_sort_key)
    ]

    return JSONResponse(
        content={
            "bot_slug": bot.slug,
            "restaurant_name": bot.restaurant_name,
            "categories": categories,
            "total_products": len(rows),
        },
        headers={"Cache-Control": _DEMO_TRACE_CACHE_CONTROL},
    )


# ─────────────────────────────────────────────────────────────────────
# Router-vs-LLM split — the cost-engineering punchline
#
# Recruiters get "the bot uses an LLM" but rarely encounter the deeper
# story: most production messages don't need one. This endpoint surfaces
# the actual ratio so the /eval page can render a 4th headline metric
# like "Router sem LLM: 68%".
# ─────────────────────────────────────────────────────────────────────

# Window for the ratio. Long enough to smooth out single conversations,
# short enough that recent corpus-tuning improvements show up.
_ROUTER_WINDOW_DAYS = 30


@router.get(
    "/eval/router-savings",
    summary="Router-handled vs LLM-handled message ratio (demo bot, 30d)",
)
async def get_router_savings() -> JSONResponse:
    """Compute the share of messages handled by the semantic router
    *without* invoking an LLM, over the last N days on the demo bot.

    Definitions:
      - "Router decisions" = UsageEvent rows with service="semantic_router"
      - "LLM decisions"    = UsageEvent rows with service="openai" and
                              operation in (get_ai_decision, get_chat_response_gpt,
                              extract_potential_items) — i.e., real per-message
                              decision calls, not menu extraction.
      - "Router-only ratio" = traces with router events but NO LLM events.

    Returns the absolute counts plus the percentage. Cached 60s.
    """
    from datetime import datetime, timedelta, timezone

    async with async_session() as session:
        bot = await _resolve_demo_bot(session)
        if not bot:
            raise HTTPException(
                status_code=404,
                detail={"error": "demo_bot_unavailable"},
            )

        since = datetime.now(timezone.utc) - timedelta(days=_ROUTER_WINDOW_DAYS)
        # naive datetime to match the table's column type (no tz on
        # usage_events.created_at)
        since_naive = since.replace(tzinfo=None)

        # Per-trace operation flags: did the router fire, did an LLM fire?
        # Aggregating in SQL would be neater; with the current row volume
        # the Python loop is fine and easier to read.
        events_q = select(
            UsageEvent.trace_id, UsageEvent.service, UsageEvent.operation
        ).where(
            UsageEvent.bot_id == bot.id,
            UsageEvent.trace_id.isnot(None),
            UsageEvent.created_at >= since_naive,
        )
        per_trace: dict[str, dict[str, bool]] = {}
        per_message_llm_ops = {
            "get_ai_decision",
            "get_chat_response_gpt",
            "extract_potential_items",
        }
        for row in (await session.execute(events_q)).all():
            tid = row.trace_id
            entry = per_trace.setdefault(tid, {"router": False, "llm": False})
            if row.service == "semantic_router":
                entry["router"] = True
            elif row.service == "openai" and row.operation in per_message_llm_ops:
                entry["llm"] = True

        total_traces = len(per_trace)
        router_only = sum(1 for v in per_trace.values() if v["router"] and not v["llm"])
        with_llm = sum(1 for v in per_trace.values() if v["llm"])

        router_only_pct = (
            round((router_only / total_traces) * 100, 1) if total_traces else 0.0
        )

    return JSONResponse(
        content={
            "window_days": _ROUTER_WINDOW_DAYS,
            "total_messages": total_traces,
            "router_only_messages": router_only,
            "with_llm_messages": with_llm,
            "router_only_pct": router_only_pct,
        },
        headers={"Cache-Control": _DEMO_TRACE_CACHE_CONTROL},
    )
