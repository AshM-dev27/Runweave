import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from agent_runtime.db import Database
from agent_runtime.runtime import configure_store
from agent_runtime.store import Store


def pytest_addoption(parser):
    parser.addoption(
        "--integration", action="store_true", help="Run real PostgreSQL/Temporal/MCP integration tests"
    )
    parser.addoption(
        "--live", action="store_true", help="Allow bounded provider calls using environment credentials"
    )


def pytest_collection_modifyitems(config, items):
    for item in items:
        for flag in ["integration", "live"]:
            if flag in item.keywords and not config.getoption(f"--{flag}"):
                item.add_marker(pytest.mark.skip(reason=f"Explicit --{flag} required"))


@pytest.fixture(autouse=True)
def no_paid_calls(request, monkeypatch):
    import pydantic_ai.models

    monkeypatch.setattr(pydantic_ai.models, "ALLOW_MODEL_REQUESTS", "live" in request.keywords)
    if "live" not in request.keywords:
        monkeypatch.delenv("BROWSER_USE_API_KEY", raising=False)


@pytest_asyncio.fixture
async def store(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/state.db")
    await database.create_test_schema()
    value = Store(database)
    configure_store(value)
    yield value
    await database.close()


@pytest_asyncio.fixture
async def pg_store():
    url = os.environ.get(
        "TEST_DATABASE_URL", "postgresql+asyncpg://agents:local-development-only@localhost:5432/agents"
    )
    schema = f"test_{uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA {schema}"))
    database = Database(url, schema)
    await database.create_test_schema()
    store = Store(database)
    configure_store(store)
    store.test_url, store.test_schema = url, schema
    yield store
    await database.close()
    async with admin.begin() as conn:
        await conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
    await admin.dispose()


@pytest_asyncio.fixture
async def time_environment():
    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
    from temporalio.testing import WorkflowEnvironment

    async with await WorkflowEnvironment.start_time_skipping(plugins=[PydanticAIPlugin()]) as environment:
        yield environment
