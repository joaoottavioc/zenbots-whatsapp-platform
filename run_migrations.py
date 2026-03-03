"""Migration runner for AWS ECS.

1. Creates all tables via SQLModel metadata (idempotent - skips existing tables)
2. Stamps alembic to head if no version table exists (fresh DB)
3. Runs alembic upgrade head for any pending migrations
"""

import asyncio
import subprocess
import sys

from sqlalchemy import text


async def main():
    # Import models to register them with SQLModel metadata
    import app.models  # noqa: F401
    from app.database import engine

    async with engine.begin() as conn:
        # Check if alembic_version table exists (= DB has been migrated before)
        result = await conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'alembic_version')"
            )
        )
        has_alembic = result.scalar()

        if not has_alembic:
            # Fresh DB: create all tables from SQLModel metadata
            from sqlmodel import SQLModel

            print("Fresh database detected. Creating tables from SQLModel metadata...")
            await conn.run_sync(SQLModel.metadata.create_all)
            print("Tables created.")

    await engine.dispose()

    if not has_alembic:
        # Stamp current state so Alembic knows all migrations are "applied"
        print("Stamping alembic version to head...")
        subprocess.run(["alembic", "stamp", "head"], check=True)
        print("Alembic stamped to head. Migration complete.")
    else:
        # Existing DB: run pending migrations
        print("Running alembic upgrade head...")
        result = subprocess.run(["alembic", "upgrade", "head"])
        sys.exit(result.returncode)


if __name__ == "__main__":
    asyncio.run(main())
