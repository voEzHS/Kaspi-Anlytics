from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/kaspi_analytics")

# Render gives postgresql:// but asyncpg needs postgresql+asyncpg://
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    """Create all tables on startup."""
    async with engine.begin() as conn:
        from app.models.models import Base as ModelBase  # noqa: F401 – triggers model registration
        await conn.run_sync(ModelBase.metadata.create_all)

    await _migrate_dept_enum()


async def _migrate_dept_enum():
    """
    Idempotent migration: add any new DeptEnum members to the existing
    Postgres enum type. create_all() only creates types/tables that don't
    exist yet — it never alters an enum type that's already there, so new
    department values (e.g. adding 'ovens') need this explicit step.

    Finds the real enum type name via pg_catalog instead of assuming a name,
    and never lets a failure here crash app startup.
    """
    from sqlalchemy import text
    from app.models.models import DeptEnum

    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(
                "SELECT t.typname FROM pg_type t "
                "JOIN pg_enum e ON t.oid = e.enumtypid "
                "WHERE e.enumlabel = 'freezers' LIMIT 1"
            ))
            row = result.first()
            if not row:
                return
            type_name = row[0]

            existing = await conn.execute(text(
                "SELECT e.enumlabel FROM pg_type t "
                "JOIN pg_enum e ON t.oid = e.enumtypid "
                "WHERE t.typname = :tn"
            ), {"tn": type_name})
            existing_values = {r[0] for r in existing.all()}

            for member in DeptEnum:
                if member.value not in existing_values:
                    await conn.execute(text(
                        f'ALTER TYPE "{type_name}" ADD VALUE IF NOT EXISTS \'{member.value}\''
                    ))
    except Exception as e:
        print(f"[init_db] dept enum migration skipped: {e}")
