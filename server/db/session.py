"""
SQLAlchemy async session and database init.
"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from config import settings
import structlog

logger = structlog.get_logger()

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def init_db():
    from models import miner_model, site_model  # noqa — registers tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database initialised")


async def close_db():
    await engine.dispose()


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
