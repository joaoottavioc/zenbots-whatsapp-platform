"""Seed the rows the corpus QA runner needs on a fresh (CI) database.

A DB bootstrapped with run_migrations.py (create_all + alembic stamp head)
has no data-migration rows, so this adds:
  - the `free` and `pro_monthly` plans (without them the plan gate answers
    every message with the maintenance notice)
  - the verified login run_corpus_qa.py uses (test1@exemplo.com)

Idempotent. Refuses to run when ENVIRONMENT is production.

    python -m scripts.ci_seed_qa_user
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import select  # noqa: E402

from app.auth import get_password_hash  # noqa: E402
from app.database import async_session  # noqa: E402
from app.models import Plan, User  # noqa: E402

# Same credentials as tests/simulation/corpus/run_corpus_qa.py (test-only).
QA_EMAIL = "test1@exemplo.com"
QA_PASSWORD = "Test123$"


async def seed() -> None:
    async with async_session() as session:
        have = set(
            (
                await session.execute(
                    select(Plan.key).where(Plan.key.in_(["free", "pro_monthly"]))
                )
            )
            .scalars()
            .all()
        )
        if "free" not in have:
            session.add(
                Plan(key="free", title="Free", description="ci", price=0.0, tier="free")
            )
        if "pro_monthly" not in have:
            session.add(
                Plan(
                    key="pro_monthly",
                    title="Pro",
                    description="ci",
                    price=0.0,
                    max_bots=50,
                )
            )

        user = (
            await session.execute(select(User).where(User.email == QA_EMAIL))
        ).scalar_one_or_none()
        if user is None:
            session.add(
                User(
                    email=QA_EMAIL,
                    hashed_password=get_password_hash(QA_PASSWORD),
                    is_email_verified=True,
                )
            )
        await session.commit()
    print(f"Seeded plans (free, pro_monthly) and QA user {QA_EMAIL}")


if __name__ == "__main__":
    if os.getenv("ENVIRONMENT", "").lower() in ("production", "prod"):
        sys.exit("Refusing to seed test credentials into a production database.")
    asyncio.run(seed())
