"""Restore a dispatched operation into a disposable database; never restore over application data."""

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_e2b import cloud as cloud
from test_e2b import finish, setup
from test_general_semantic import http_client

from agent_runtime.db import Database
from agent_runtime.general_db import ExtensionSlotRow, GeneralOperationRow
from agent_runtime.general_runtime import general_action
from agent_runtime.runtime import configure_store
from agent_runtime.store import Store
from examples.e2b_invoice import EXPECTED_SUMMARY, verify_output

pytestmark = pytest.mark.integration


async def test_upgrade_and_backup_restore_preserve_dispatched_e2b_identity(pg_store, cloud, tmp_path):
    async with http_client(pg_store) as client:
        run, payload = await setup(pg_store, client)
        for _ in range(3):
            assert (await general_action(payload))["external_pending"]
        assert cloud.count("create") == cloud.count("run") == 1
    identity = run.id + ":action:0"
    async with pg_store.database.sessions.begin() as db:
        before = (await db.get(GeneralOperationRow, identity)).data.copy()
        assert before["handler_state"]["phase"] == "executing"
        # Reconstruct 0004 before applying the additive capacity/blob migrations.
        await db.execute(text("DROP TABLE computer_sessions"))
        await db.execute(text("ALTER TABLE artifacts DROP CONSTRAINT ck_artifacts_content_location"))
        await db.execute(text("ALTER TABLE artifacts DROP COLUMN blob_key"))
        await db.execute(text("ALTER TABLE artifacts ALTER COLUMN content SET NOT NULL"))
        await db.execute(text("DROP TABLE extension_slots"))
        await db.execute(text("DROP INDEX ix_general_runs_pending_cleanup"))
        await db.execute(text("DROP INDEX ix_toolkit_runs_pending_cleanup"))
        await db.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
        await db.execute(text("INSERT INTO alembic_version VALUES ('0004')"))
    import sys

    env = {**os.environ, "DATABASE_URL": pg_store.test_url, "DATABASE_SCHEMA": pg_store.test_schema}
    for command in (("upgrade", "head"), ("check",)):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "alembic",
            *command,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert await process.wait() == 0
    async with pg_store.database.sessions() as db:
        assert (await db.get(GeneralOperationRow, identity)).data == before
        assert (await db.get(ExtensionSlotRow, identity)).active
    restored_name = "e2b_restore_" + uuid4().hex
    from sqlalchemy.engine import make_url

    original_url = make_url(pg_store.test_url)
    admin = create_async_engine(pg_store.test_url, isolation_level="AUTOCOMMIT")
    restored = Database(
        original_url.set(database=restored_name).render_as_string(hide_password=False), pg_store.test_schema
    )
    container = os.environ.get("TEST_POSTGRES_CONTAINER", "agent_runtime-postgres-1")
    import subprocess

    docker = ["docker"]
    if subprocess.run(docker + ["info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        docker = ["sudo", "-n", "docker"]
    backup = tmp_path / "recovery.dump"
    created = False
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f"CREATE DATABASE {restored_name}"))
        created = True
        with backup.open("wb") as output:
            process = await asyncio.create_subprocess_exec(
                *docker,
                "exec",
                container,
                "pg_dump",
                "-U",
                original_url.username,
                "-d",
                original_url.database,
                "-n",
                pg_store.test_schema,
                "-Fc",
                "--no-owner",
                stdout=output,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert await process.wait() == 0
        with backup.open("rb") as source:
            process = await asyncio.create_subprocess_exec(
                *docker,
                "exec",
                "-i",
                container,
                "pg_restore",
                "-U",
                original_url.username,
                "-d",
                restored_name,
                "--no-owner",
                "--exit-on-error",
                stdin=source,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert await process.wait() == 0
        restored_store = Store(restored)
        restored_store.extensions = pg_store.extensions
        configure_store(restored_store)
        assert verify_output((await finish(payload))["output"]) == EXPECTED_SUMMARY
        assert cloud.count("create") == cloud.count("run") == cloud.count("kill") == 1
        async with restored.sessions() as db:
            assert not (await db.get(ExtensionSlotRow, identity)).active
    finally:
        await restored.close()
        if created:
            async with admin.connect() as connection:
                await connection.execute(text(f"DROP DATABASE {restored_name}"))
        await admin.dispose()
        backup.unlink(missing_ok=True)
        configure_store(pg_store)
