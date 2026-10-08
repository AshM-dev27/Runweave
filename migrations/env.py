import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from agent_runtime import computer_db, general_db  # noqa: F401
from agent_runtime.config import settings
from agent_runtime.db import Base


def migrate(connection):
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def main():
    engine = create_async_engine(
        settings().database_url,
        connect_args={"server_settings": {"search_path": settings().database_schema}}
        if settings().database_url.startswith("postgresql")
        else {},
    )
    async with engine.connect() as connection:
        await connection.run_sync(migrate)
    await engine.dispose()


asyncio.run(main())
